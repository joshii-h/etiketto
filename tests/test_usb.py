"""USB printers: IEEE 1284 IDs from sysfs, usb:// addresses, the device file
used like a socket (a pseudo terminal stands in for /dev/usb/lp0)."""

from __future__ import annotations

import errno
import os
import select
import threading
import time
import tty
from pathlib import Path
from types import SimpleNamespace

import pytest

from tavletta.backends import cab, usb
from tavletta.backends.base import (
    FoundPrinter,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterStalled,
    PrinterTimeout,
    UsbNotFound,
    UsbPermission,
)

REAL_CAB_CONNECT = cab._connect  # before conftest's guard replaces it


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A fake /sys/class/usbmisc; add(lpN, device id, usb serial)."""
    root = tmp_path / "usbmisc"
    root.mkdir()
    monkeypatch.setattr(usb, "SYSFS", root)
    monkeypatch.setattr(usb, "DEV", Path("/dev/usb"))

    def add(name, ieee1284_id=None, serial=None):
        device = root / name / "device"
        device.mkdir(parents=True)
        if ieee1284_id is not None:
            (device / "ieee1284_id").write_text(ieee1284_id)
        if serial is not None:
            (root / name / "serial").write_text(serial + "\n")  # device/../serial

    return add


def test_device_id_merges_short_and_long_keys():
    fields = usb.device_id("MFG:cab;MDL:EOS2/300;CMD:JSCRIPT,ZPL; SN : A1 ;junk;:x;")
    assert fields == {
        "MANUFACTURER": "cab",
        "MODEL": "EOS2/300",
        "COMMAND SET": "JSCRIPT,ZPL",
        "SERIALNUMBER": "A1",
    }
    assert usb.device_id("MANUFACTURER:Brother;MODEL:VC-500W;") == {
        "MANUFACTURER": "Brother",
        "MODEL": "VC-500W",
    }
    # Brother names the serial number SERN; a colon inside a value stays
    assert usb.device_id("MFG:Brother;MDL:VC-500W;SERN:E78;DES:a:b;")["SERIALNUMBER"] == "E78"
    assert usb.device_id("MFG:Brother;DES:a:b;")["DES"] == "a:b"


def test_printers_from_sysfs(sysfs):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;SN:A1;")
    sysfs("lp1", "MANUFACTURER:Brother;MODEL:VC-500W;", serial="E78123")  # serial of the device
    sysfs("lp2")  # no device id: not a printer we can name
    assert usb.printers() == [
        usb.UsbPrinter(Path("/dev/usb/lp0"), "cab", "EOS2/300", "A1"),
        usb.UsbPrinter(Path("/dev/usb/lp1"), "Brother", "VC-500W", "E78123"),
    ]


def test_no_sysfs_no_printers(monkeypatch):
    monkeypatch.setattr(usb, "SYSFS", Path("/nonexistent/usbmisc"))
    assert usb.printers() == []


def test_address_like_cups_and_found_again(sysfs):
    sysfs("lp3", "MFG:cab;MDL:EOS2/300;SN:A 1/2;")
    printer = usb.printers()[0]
    assert printer.address == "usb://cab/EOS2%2F300?serial=A%201%2F2"
    assert usb.is_usb(printer.address) and not usb.is_usb("cab-eos2.local")
    assert usb.find(printer.address) == printer
    assert usb.find("usb://cab/EOS2%2F300") == printer  # without serial: any of the model
    without = usb.UsbPrinter(Path("/dev/usb/lp0"), "Dymo", "LabelWriter 450")
    assert without.address == "usb://Dymo/LabelWriter%20450"
    with pytest.raises(UsbNotFound, match="no USB printer"):
        usb.find("usb://cab/EOS2%2F300?serial=other")
    # maker and model in another case: the same printer
    assert usb.find("usb://CAB/eos2%2F300") == printer


@pytest.mark.parametrize("address", ["usb://[x", "usb://cab"])
def test_odd_addresses_are_not_found(sysfs, address):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;")
    with pytest.raises(PrinterNotFound) as raised:  # not a ValueError
        usb.find(address)
    assert raised.value.code == "usb_not_found"


# --- the device file ------------------------------------------------------------------


@pytest.fixture
def device():
    """A pseudo terminal: the slave is the 'device', the master the printer."""
    master, slave = os.openpty()
    tty.setraw(slave)  # bytes as they come, like a printer; no echo, no lines
    yield Path(os.ttyname(slave)), master
    os.close(master)
    os.close(slave)


def full(fd: int) -> None:
    """Fill the pseudo terminal until it takes no more: a printer that stops
    taking data (paper out, buffer full)."""
    blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    # the kernel moves written bytes on in the background and makes room
    # again for a while: fill until it stays full
    while select.select([], [fd], [], 0.1)[1]:
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
    os.set_blocking(fd, blocking)


def test_connection_sends_and_receives(device):
    path, printer = device
    connection = usb.Connection(path, timeout=2)
    connection.sendall(b"\x1bs")
    assert os.read(printer, 10) == b"\x1bs"
    os.write(printer, b"Y-000000N")
    assert connection.recv(64) == b"Y-000000N"
    connection.settimeout(0.05)
    with pytest.raises(PrinterTimeout, match="did not answer"):
        connection.recv(1)  # the printer says nothing
    connection.close()
    connection.close()  # twice is fine


def test_connection_errors(monkeypatch, tmp_path):
    (tmp_path / "lp1").touch()  # a device file that is there

    def refuse(path, flags):
        assert flags & os.O_NONBLOCK  # every wait has a timeout
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(usb.os, "open", refuse)
    with pytest.raises(UsbPermission, match="Permission denied"):
        usb.Connection(tmp_path / "lp0", 1)

    def gone(path, flags):
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(usb.os, "open", gone)
    with pytest.raises(PrinterNotFound):
        usb.Connection(tmp_path / "lp0", 1)

    def broken(path, flags):
        raise OSError(errno.EIO, "Input/output error")

    # a failing device: a USB problem, not a printer missing from the network
    monkeypatch.setattr(usb.os, "open", broken)
    with pytest.raises(PrinterError, match="Input/output") as raised:
        usb.Connection(tmp_path / "lp1", 1)
    assert type(raised.value) is PrinterError and raised.value.code == "generic"
    with pytest.raises(UsbNotFound):  # EIO because it went away
        usb.Connection(tmp_path / "lp0", 1)
    assert usb._device_lock(tmp_path / "lp1").acquire(blocking=False)  # lock released
    usb._device_lock(tmp_path / "lp1").release()


def test_cab_maps_a_failing_usb_device_as_a_usb_problem(sysfs, monkeypatch, tmp_path):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;")
    (tmp_path / "lp0").touch()
    monkeypatch.setattr(usb, "DEV", tmp_path)
    monkeypatch.setattr(cab, "_connect", REAL_CAB_CONNECT)
    monkeypatch.setattr(usb.os, "open", broken(errno.EIO))
    with pytest.raises(PrinterError) as raised:
        cab.Cab().query("usb://cab/EOS2%2F300")
    assert raised.value.code == "generic"  # not "not_found" with network advice


class Recorder:
    """A Connection stand-in that notes what connect() does with it."""

    def __init__(self, device, timeout, fail=None):
        self.device, self.timeout, self.fail = device, timeout, fail
        self.calls = []

    def drain(self, quiet=usb.DRAIN_QUIET, limit=usb.DRAIN_LIMIT):
        self.calls.append("drain")
        if self.fail:
            raise self.fail

    def close(self):
        self.calls.append("close")


def test_connect_finds_the_device_and_drains_it(sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;SN:A1;")
    made = []
    monkeypatch.setattr(
        usb,
        "Connection",
        lambda device, timeout, wait=None: made.append(Recorder(device, timeout)) or made[-1],
    )
    connection = usb.connect("usb://cab/EOS2%2F300?serial=A1", 5)
    assert connection is made[0] and connection.device == Path("/dev/usb/lp0")
    assert connection.calls == ["drain"]  # stale answers dropped, for every backend


def test_connect_closes_when_draining_fails(sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;")
    made = []

    def connection(device, timeout, wait=None):
        made.append(Recorder(device, timeout, UsbNotFound("unplugged")))
        return made[-1]

    monkeypatch.setattr(usb, "Connection", connection)
    with pytest.raises(UsbNotFound):
        usb.connect("usb://cab/EOS2%2F300", 5)
    assert made[0].calls == ["drain", "close"]  # no file left open


# --- writing: the printer may stop taking data --------------------------------------------


def test_a_blocked_write_times_out(device):
    path, printer = device
    connection = usb.Connection(path, timeout=0.3)
    start = time.monotonic()
    with pytest.raises(PrinterStalled, match="stopped taking data") as raised:
        connection.sendall(b"x" * 500_000)  # nobody reads: the buffer fills up
    assert time.monotonic() - start < 2
    assert raised.value.code == "stalled" and isinstance(raised.value, PrinterTimeout)
    connection.close()


class Consumer(threading.Thread):
    """A printer reading `total` bytes at about `rate` bytes per second;
    one that stops reading after `stop_after` bytes, if given. stop()
    before the pseudo terminal is closed."""

    def __init__(self, fd: int, total: int, rate: float, stop_after: int | None = None):
        super().__init__(daemon=True)
        self.fd, self.total, self.rate, self.stop_after = fd, total, rate, stop_after
        self.received = 0
        self.stopped = threading.Event()
        self.start()

    def run(self):
        start = time.monotonic()
        limit = self.total if self.stop_after is None else self.stop_after
        while self.received < limit and not self.stopped.is_set():
            if not select.select([self.fd], [], [], 0.05)[0]:
                continue
            self.received += len(os.read(self.fd, 65536))
            ahead = self.received / self.rate - (time.monotonic() - start)
            if ahead > 0:
                self.stopped.wait(ahead)

    def stop(self) -> None:
        self.stopped.set()
        self.join(5)


def test_a_large_job_on_a_slow_port_goes_through(device):
    """6 MB at 4 MB/s take 1.5 s, far longer than the timeout: every write
    that gets data through starts the timeout again."""
    path, printer = device
    connection = usb.Connection(path, timeout=0.5)
    data = b"x" * 6_000_000
    reader = Consumer(printer, len(data), rate=4_000_000)
    start = time.monotonic()
    connection.sendall(data)
    assert time.monotonic() - start > connection.timeout
    reader.join(5)
    assert reader.received == len(data)
    connection.close()


def test_a_printer_that_stops_taking_data_mid_job_times_out(device):
    path, printer = device
    connection = usb.Connection(path, timeout=0.3)
    reader = Consumer(printer, 6_000_000, rate=20_000_000, stop_after=1_000_000)
    start = time.monotonic()
    with pytest.raises(PrinterStalled):
        connection.sendall(b"x" * 6_000_000)
    assert time.monotonic() - start < 3  # the timeout after it stopped, not the job's budget
    reader.stop()
    assert reader.received >= 1_000_000
    connection.close()


def test_a_trickling_printer_still_ends(device, monkeypatch):
    """Data keeps moving, but slower than MIN_RATE: the job's budget
    (timeout + size / MIN_RATE) ends it."""
    path, printer = device
    monkeypatch.setattr(usb, "MIN_RATE", 1_000_000)
    connection = usb.Connection(path, timeout=0.3)
    data = b"x" * 300_000  # budget 0.3 + 0.3 s; at 50 kB/s it would take 6 s
    reader = Consumer(printer, len(data), rate=50_000)
    start = time.monotonic()
    with pytest.raises(PrinterStalled, match="took data too slowly"):
        connection.sendall(data)
    assert 0.5 < time.monotonic() - start < 2
    reader.stop()
    assert 0 < reader.received < len(data)  # data moved, just too slowly
    connection.close()


def test_cab_sends_a_large_job_on_a_slow_usb_port(device):
    """cab's answers wait 10 s; a job takes as long as the data keeps moving."""
    path, printer = device
    connection = usb.Connection(path, timeout=0.5)
    job = b"d PNG;TAVLETTA\r\n" + b"A" * 6_000_000
    reader = Consumer(printer, len(job), rate=4_000_000)
    cab._Session(connection).send(job)
    reader.join(5)
    assert reader.received == len(job)
    connection.close()


def test_partial_writes_all_arrive(device):
    path, printer = device
    connection = usb.Connection(path, timeout=5)
    data = bytes(range(256)) * 2000  # far more than the buffer takes at once
    received = bytearray()

    def read_slowly():
        while len(received) < len(data):
            received.extend(os.read(printer, 1000))
            time.sleep(0.0005)

    reader = threading.Thread(target=read_slowly)
    reader.start()
    connection.sendall(data)
    reader.join(5)
    assert bytes(received) == data
    connection.close()


def test_write_would_block_is_not_an_error(device, monkeypatch):
    path, printer = device
    connection = usb.Connection(path, timeout=0.2)
    full(connection.fd)
    # select() says writable, the write still says "try again"
    real_select = select.select
    monkeypatch.setattr(usb.select, "select", lambda r, w, x, t: (r, w, []))
    with pytest.raises(PrinterTimeout):
        connection.sendall(b"more")
    monkeypatch.setattr(usb.select, "select", real_select)
    connection.close()


def test_close_waits_for_the_last_write(device):
    path, printer = device
    connection = usb.Connection(path, timeout=0.3)
    full(connection.fd)  # a write still on its way
    start = time.monotonic()
    connection.close()
    assert 0.25 < time.monotonic() - start < 2  # waited, but not forever
    assert connection.fd == -1
    usb.Connection(path, timeout=0.3).close()  # free again


def test_close_when_waiting_fails(device, monkeypatch):
    path, printer = device
    connection = usb.Connection(path, timeout=0.3)
    real_select = select.select

    def fail(*_args):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(usb.select, "select", fail)
    connection.close()  # closed all the same
    monkeypatch.setattr(usb.select, "select", real_select)
    assert connection.fd == -1
    usb.Connection(path, timeout=0.3).close()  # and the device is free


# --- reading: empty reads, chatty printers ------------------------------------------------


def test_empty_reads_mean_nothing_yet(device):
    path, printer = device
    connection = usb.Connection(path, timeout=2)
    reads = [b"", BlockingIOError(), b""]  # zero-length packets, "try again"
    real = connection._read

    def read(size):
        if not reads:
            return real(size)
        if isinstance(reads[0], Exception):
            raise reads.pop(0)
        return reads.pop(0)

    connection._read = read  # type: ignore[method-assign]
    os.write(printer, b"Y-000000N")
    assert connection.recv(64) == b"Y-000000N"
    connection.close()


def test_only_empty_reads_time_out_without_spinning(device):
    path, printer = device
    connection = usb.Connection(path, timeout=0.2)
    calls = []
    connection._read = lambda size: calls.append(size) or b""  # type: ignore[method-assign]
    os.write(printer, b"x")  # select() keeps saying readable
    start = time.monotonic()
    with pytest.raises(PrinterTimeout):
        connection.recv(64)
    assert time.monotonic() - start < 1 and len(calls) < 20
    connection._read = lambda size: b""  # type: ignore[method-assign]
    connection.drain()  # ends too, after the quiet time
    connection.close()


def test_drain_stops_for_a_printer_that_keeps_talking(device):
    path, printer = device
    connection = usb.Connection(path, timeout=5)
    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            os.write(printer, b"<status/>")
            time.sleep(0.02)

    talker = threading.Thread(target=chatter)
    talker.start()
    start = time.monotonic()
    try:
        connection.drain(quiet=0.2, limit=0.5)
    finally:
        stop.set()
        talker.join()
    assert time.monotonic() - start < 1.5 and connection.timeout == 5
    connection.close()


# --- errors on the way: unplugged, broken, busy ------------------------------------------


def broken(error):
    def fail(*_args):
        raise OSError(error, os.strerror(error))

    return fail


@pytest.mark.parametrize("error", [errno.ENODEV, errno.ENXIO])
def test_unplugged_during_io(device, error):
    path, printer = device
    connection = usb.Connection(path, timeout=1)
    connection._write = broken(error)  # type: ignore[method-assign]
    with pytest.raises(UsbNotFound):
        connection.sendall(b"x")
    os.write(printer, b"x")
    connection._read = broken(error)  # type: ignore[method-assign]
    with pytest.raises(UsbNotFound):
        connection.recv(1)
    connection.close()


def test_input_output_error(device, tmp_path):
    path, printer = device
    connection = usb.Connection(path, timeout=1)
    connection._write = broken(errno.EIO)  # type: ignore[method-assign]
    with pytest.raises(PrinterError, match="Input/output") as raised:
        connection.sendall(b"x")
    assert raised.value.code == "generic"  # the device is still there
    connection.device = tmp_path / "lp0"  # EIO because it went away
    with pytest.raises(UsbNotFound):
        connection.sendall(b"x")
    connection.close()


def test_unplugging_is_noticed_while_waiting(device, tmp_path, monkeypatch):
    path, printer = device
    monkeypatch.setattr(usb, "SLICE", 0.05)
    connection = usb.Connection(path, timeout=10)
    connection.device = tmp_path / "lp0"  # the device file is gone
    start = time.monotonic()
    with pytest.raises(UsbNotFound):
        connection.recv(1)
    assert time.monotonic() - start < 1  # not after the whole timeout
    connection.close()


def test_busy_device_is_retried(monkeypatch, tmp_path):
    attempts = []
    real_open = os.open

    def busy_twice(path, flags):
        attempts.append(path)
        if len(attempts) < 3:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_open(os.devnull, os.O_RDWR)

    monkeypatch.setattr(usb.os, "open", busy_twice)
    connection = usb.Connection(tmp_path / "lp0", 1)
    assert len(attempts) == 3
    monkeypatch.setattr(usb.os, "open", real_open)
    connection.close()


def test_busy_device_gives_up(monkeypatch, tmp_path):
    monkeypatch.setattr(usb, "BUSY_WAIT", 0.2)
    monkeypatch.setattr(usb.os, "open", broken(errno.EBUSY))
    with pytest.raises(PrinterBusy) as raised:
        usb.Connection(tmp_path / "lp0", 1)
    assert raised.value.code == "busy"
    monkeypatch.setattr(usb.os, "open", os.open)
    assert usb._device_lock(tmp_path / "lp0").acquire(blocking=False)  # lock released
    usb._device_lock(tmp_path / "lp0").release()


def test_sessions_take_turns(device, monkeypatch):
    path, printer = device
    monkeypatch.setattr(usb, "BUSY_WAIT", 0.2)
    first = usb.Connection(path, timeout=1)
    with pytest.raises(PrinterBusy, match="another session"):
        usb.Connection(path, timeout=1)  # the query while printing, for a moment
    monkeypatch.setattr(usb, "BUSY_WAIT", 5)
    threading.Timer(0.1, first.close).start()
    second = usb.Connection(path, timeout=1)  # waits for the first to finish
    assert first.fd == -1
    second.close()


def test_a_print_waits_longer_than_a_status_query(device, monkeypatch):
    """A print behind a hung status query must not fail as busy."""
    path, printer = device
    monkeypatch.setattr(usb, "BUSY_WAIT", 0.1)
    assert usb.PRINT_WAIT > 2 * cab.SOCKET_TIMEOUT + usb.DRAIN_LIMIT
    query = usb.Connection(path, timeout=1)
    with pytest.raises(PrinterBusy):
        usb.Connection(path, timeout=1)  # another query gives up soon
    threading.Timer(0.4, query.close).start()
    printing = usb.Connection(path, timeout=1, wait=usb.PRINT_WAIT)
    assert query.fd == -1
    printing.close()


def test_cab_prints_with_the_longer_wait(device, sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;")
    waits = []
    monkeypatch.setattr(cab, "_connect", REAL_CAB_CONNECT)
    monkeypatch.setattr(
        usb, "connect", lambda host, timeout, wait=None: waits.append(wait) or SimpleNamespace()
    )
    cab._connect("usb://cab/EOS2%2F300", 9100)
    cab._connect("usb://cab/EOS2%2F300", 9100, usb.PRINT_WAIT)
    assert waits == [usb.BUSY_WAIT, usb.PRINT_WAIT]


# --- the cab backend over USB ------------------------------------------------------------


def test_cab_uses_usb_for_usb_addresses(monkeypatch):
    monkeypatch.setattr(cab, "_connect", REAL_CAB_CONNECT)  # not the test guard
    calls = []
    connection = SimpleNamespace()
    monkeypatch.setattr(
        usb, "connect", lambda host, timeout, wait=None: calls.append((host, wait)) or connection
    )
    assert cab._connect("usb://cab/EOS2%2F300", 9100) is connection
    assert calls == [("usb://cab/EOS2%2F300", usb.BUSY_WAIT)]
    assert connection.min_rate == cab.MIN_RATE  # as on TCP


def test_cab_drops_a_stale_answer_on_usb(device, sysfs, monkeypatch, tmp_path):
    path, printer = device
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;")
    (tmp_path / "lp0").symlink_to(path)  # /dev/usb/lp0 is the pseudo terminal
    monkeypatch.setattr(usb, "DEV", tmp_path)
    monkeypatch.setattr(cab, "_connect", REAL_CAB_CONNECT)
    # an earlier session asked "qv" and gave up; the answer came late
    os.write(printer, b"EOS2/300 V5.41\r")

    def answer():
        assert os.read(printer, 10) == b"\x1bs"
        os.write(printer, b"Y-000000N")

    printer_thread = threading.Thread(target=answer)
    printer_thread.start()
    with cab.session("usb://cab/EOS2%2F300") as s:
        status = s.status()
    printer_thread.join(2)
    assert status.raw == "Y-000000N"  # not the stale version answer


def test_cab_reads_past_empty_packets(device):
    path, printer = device
    connection = usb.Connection(path, timeout=2)
    reads = [b""]
    real = connection._read
    connection._read = lambda size: reads.pop(0) if reads else real(size)  # type: ignore[method-assign]
    os.write(printer, b"Y-000000N")
    status = cab._Session(connection)._read(9)  # not "connection closed"
    assert status == "Y-000000N"
    connection.close()


def test_cab_names_on_usb(sysfs, monkeypatch):
    sysfs("lp0", "MFG:CAB;MDL:cab EOS5/300;")  # maker in capitals, model with the maker
    monkeypatch.setattr(cab.mdns, "browse", lambda service_type, match, timeout: [])
    assert [p.name for p in cab.Cab().discover(timeout=0.01)] == ["cab EOS5/300 (USB)"]
    assert (
        cab.Cab().display_name(PrinterInfo("cab EOS5/300", None, None, None, "IDLE"))
        == "cab EOS5/300"
    )
    assert (
        cab.Cab().display_name(PrinterInfo("EOS5/300", None, None, None, "IDLE")) == "cab EOS5/300"
    )


def test_cab_finds_its_printers_on_usb(sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:SQUIX 4/300;SN:9;")
    sysfs("lp1", "MFG:HP;MDL:LaserJet;")  # not a cab
    monkeypatch.setattr(cab.mdns, "browse", lambda service_type, match, timeout: [])
    assert cab.Cab().discover(timeout=0.01) == [
        FoundPrinter("cab SQUIX 4/300 (USB)", "usb://cab/SQUIX%204%2F300?serial=9", "/dev/usb/lp0")
    ]


def test_the_device_wait_is_applied_once(device, monkeypatch):
    """Waiting for another session (the lock) and then for another program
    (EBUSY) share one deadline: not twice the wait."""
    path, _printer = device
    deadlines = []
    real_open = usb._open
    monkeypatch.setattr(
        usb,
        "_open",
        lambda device, deadline: deadlines.append(deadline) or real_open(device, deadline),
    )
    lock = usb._device_lock(path)
    lock.acquire()
    threading.Timer(0.3, lock.release).start()
    start = time.monotonic()
    connection = usb.Connection(path, timeout=0.5, wait=1.0)
    connection.close()
    assert deadlines and deadlines[0] <= start + 1.05  # not 0.3 + 1.0


def test_tests_never_open_a_real_usb_printer():
    """conftest refuses /dev/usb/lp* like the serial ports: a test that
    reached the machine's printer would fail."""
    with pytest.raises(AssertionError, match="/dev/usb/lp0"):
        os.open("/dev/usb/lp0", os.O_RDWR)
