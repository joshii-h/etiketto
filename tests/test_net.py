"""Sending over TCP without losing the end of a job: progress-based
timeouts and a close that waits for the printer. Printers here are threads
of this test on 127.0.0.1 with small receive buffers, so that the kernel
cannot hide a slow printer."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from fake_raw import ZPL_HS
from PIL import Image

from etiketto.backends import net, raw, zpl
from etiketto.backends.base import PrinterStalled, PrintOptions

REAL_CONNECT = raw._connect  # before conftest's guard replaces it
BUFFER = 4096


class SlowPrinter(threading.Thread):
    """Reads `chunk` bytes every `pause` seconds until the connection ends.
    `late`: an answer it sends after `delay`, when the asker gave up."""

    def __init__(self, chunk=2048, pause=0.005, late=b"", delay=0.0, stop_after=None):
        super().__init__(daemon=True)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER)
        self.port = self.listener.getsockname()[1]
        self.chunk, self.pause, self.late, self.delay = chunk, pause, late, delay
        self.stop_after = stop_after  # stop reading after this many bytes
        self.received = b""
        self.error: OSError | None = None
        self.done = threading.Event()

    def run(self):
        connection, _address = self.listener.accept()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER)
        try:
            if self.late:
                self.received += connection.recv(100)
                time.sleep(self.delay)
                connection.sendall(self.late)
            while self.stop_after is None or len(self.received) < self.stop_after:
                time.sleep(self.pause)
                data = connection.recv(self.chunk)
                if not data:
                    break
                self.received += data
            else:
                self.done.wait(10)  # a printer that stopped taking data
        except OSError as e:
            self.error = e
        connection.close()
        self.listener.close()


def _connect(port):
    sock = socket.create_connection(("127.0.0.1", port), 5)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER)
    return sock


def test_a_late_answer_does_not_cut_the_job_off(monkeypatch):
    # the printer answers ~HS after the question timed out, then reads the
    # job slowly: closing with that answer unread used to reset the
    # connection and drop most of the job
    monkeypatch.setattr(raw, "_connect", REAL_CONNECT)
    monkeypatch.setattr(raw, "ANSWER_TIMEOUT", 0.1)
    printer = SlowPrinter(late=ZPL_HS, delay=0.3)
    printer.start()
    image = Image.frombytes("L", (832, 600), bytes(range(256)) * (832 * 600 // 256))
    options = PrintOptions(settings={"dpi": "203"})
    backend = zpl.Zpl(port=printer.port)
    job = backend.build_job(image, raw.DPI["203"], options, "127.0.0.1")
    backend.print_label(image, "127.0.0.1", options)
    printer.join(10)
    assert printer.error is None
    assert printer.received == b"~HS\n" + job


def test_a_slow_printer_takes_a_large_job():
    # the timeout counts from the last progress, not from the start
    printer = SlowPrinter(chunk=1024, pause=0.002)
    printer.start()
    sock = _connect(printer.port)
    data = b"x" * 400_000
    started = time.monotonic()
    net.send_all(sock, data, timeout=0.5, min_rate=1000)
    net.finish(sock, timeout=0.5)
    sock.close()
    printer.join(10)
    assert time.monotonic() - started > 0.5  # longer than the timeout
    assert printer.received == data


def test_a_printer_that_stops_taking_data():
    printer = SlowPrinter(stop_after=8192)
    printer.start()
    sock = _connect(printer.port)
    with pytest.raises(PrinterStalled, match="printer took"):
        net.send_all(sock, b"x" * 2_000_000, timeout=0.3, min_rate=1000)
    printer.done.set()
    sock.close()
    printer.join(5)


def test_a_trickling_printer_ends_at_the_cap():
    printer = SlowPrinter(chunk=256, pause=0.01)
    printer.start()
    sock = _connect(printer.port)
    started = time.monotonic()
    with pytest.raises(PrinterStalled):  # 0.2 s + 300 kB at 1 MB/s
        net.send_all(sock, b"x" * 300_000, timeout=0.2, min_rate=1_000_000)
    assert time.monotonic() - started < 2
    sock.close()
    printer.join(5)


def test_finishing_waits_for_a_printer_that_stalls():
    printer = SlowPrinter(stop_after=4096)
    printer.start()
    sock = _connect(printer.port)
    sock.setblocking(False)
    try:
        while True:  # fill the buffers: the data stays there
            sock.send(b"x" * 4096)
    except BlockingIOError:
        pass
    sock.setblocking(True)
    with pytest.raises(PrinterStalled, match="not taken"):
        net.finish(sock, timeout=0.3)
    printer.done.set()
    sock.close()
    printer.join(5)


def test_finishing_ends_when_all_was_taken_and_the_printer_is_quiet(monkeypatch):
    monkeypatch.setattr(net, "FINISH_QUIET", 0.1)
    left = [b"answer"]

    class Quiet:
        def shutdown(self, how):
            pass

        def settimeout(self, timeout):
            pass

        def fileno(self):
            return -1  # unsent() cannot tell: 0

        def recv(self, size):
            if left:
                return left.pop()
            time.sleep(0.02)
            raise TimeoutError

    net.finish(Quiet(), timeout=1)  # type: ignore[arg-type]
    assert left == []


def test_finishing_a_closed_connection():
    a, b = socket.socketpair()
    b.close()
    a.close()
    net.finish(a, timeout=0.1)  # nothing to protect: no error

    a, b = socket.socketpair()
    b.close()

    a.settimeout(0.1)
    net.finish(a, timeout=0.1)  # the other side is gone: EOF
    a.close()


def test_unsent_bytes():
    a, b = socket.socketpair()
    assert net.unsent(a) == 0
    a.close()
    b.close()
    assert net.unsent(a) == 0  # closed: cannot tell


def test_finishing_after_the_printer_reset_the_connection():
    class Reset:
        def shutdown(self, how):
            pass

        def settimeout(self, timeout):
            pass

        def recv(self, size):
            raise ConnectionResetError("reset by peer")

        def fileno(self):
            return -1

    net.finish(Reset(), timeout=1)  # type: ignore[arg-type]
