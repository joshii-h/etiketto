"""The VC-500W on USB: the same XML protocol as on TCP, on the printer's
USB device file (/dev/usb/lpN). What we know of its USB behaviour comes
from barnguard's notes (see docs/vc500w.md); these tests pin down the
results Tavletta must give, against a fake device that answers through the
same simulated printer as the TCP tests."""

from __future__ import annotations

import io
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import XML_HEAD, FakeConnection, FakeVC500W

from tavletta.backends import usb, vc500w
from tavletta.backends.base import FoundPrinter, PrinterTimeout, PrintOptions, UsbPermission
from tavletta.backends.vc500w import VC500W

REAL_CONNECT = vc500w._connect  # before conftest's guard replaces it
USB_HOST = "usb://Brother/VC-500W?serial=E78123"


class FakeUsbDevice:
    """A usb.Connection look-alike: bytes in, answers out in small pieces."""

    def __init__(self, printer: FakeVC500W, stale: bytes = b"", chunk: int = 37):
        self.link = FakeConnection(printer)
        self.unread = bytearray(stale)  # an earlier, unread answer
        self.chunk = chunk
        self.timeouts: list[float] = []
        self.drained = 0
        self.closed = False

    def sendall(self, data: bytes) -> None:
        if data.startswith(XML_HEAD.encode()):
            self.link.send_message(SimpleNamespace(get_data=lambda: data.decode()))
        else:
            self.link.send_file(io.BytesIO(data))
        while self.link.pending:
            self.unread += self.link.pending.pop(0).encode()

    def recv(self, size: int) -> bytes:
        if not self.unread:
            raise PrinterTimeout("USB printer did not answer")
        piece = bytes(self.unread[: min(size, self.chunk)])
        del self.unread[: len(piece)]
        return piece

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def drain(self, quiet: float = usb.DRAIN_QUIET, limit: float = usb.DRAIN_LIMIT) -> None:
        self.drained += 1
        self.unread.clear()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def usb_printer(monkeypatch):
    device = FakeVC500W()
    made = []

    def connect(address, timeout, wait=None):
        """Like usb.connect(): the device, with stale answers dropped."""
        assert address == USB_HOST
        made.append(FakeUsbDevice(device, stale=b"<status><code>0</code></status>\n"))
        made[-1].drain()
        return made[-1]

    monkeypatch.setattr(vc500w, "_connect", REAL_CONNECT)  # not the test guard
    monkeypatch.setattr(usb, "connect", connect)
    monkeypatch.setattr(vc500w, "_sleep", lambda seconds: None)
    return SimpleNamespace(device=device, made=made)


def test_usb_addresses_go_to_the_usb_device(usb_printer):
    link = vc500w._connect(USB_HOST, 9100)
    assert isinstance(link, vc500w._UsbLink)
    assert usb_printer.made[0].drained == 1  # by usb.connect(), not again by the link


def test_query_over_usb_with_answers_in_pieces(usb_printer):
    info = VC500W().query(USB_HOST)
    assert info.model == "Wedge" and info.tape_mm == 25 and info.firmware == "2026031504"
    device = usb_printer.made[0]
    assert device.closed and usb_printer.device.names()[:2] == ["config", "status"]


def test_print_over_usb_sends_the_job_then_the_data(usb_printer):
    backend = VC500W()
    options = PrintOptions(mode="color")
    from PIL import Image

    image = Image.new("RGB", (400, 120), "white")
    backend.print_label(image, USB_HOST, options)
    printer = usb_printer.device
    assert printer.prints() and printer.data()
    xml = printer.prints()[0]
    assert "<datasize>%d</datasize>" % len(printer.data()[0]) in xml  # the job says its size
    # the printer may take long for vivid prints: the answer to the data waits long
    assert max(usb_printer.made[0].timeouts) == vc500w.PRINT_TIMEOUT


def test_a_printer_that_does_not_answer_on_usb(usb_printer, monkeypatch):
    class Silent(FakeUsbDevice):
        def sendall(self, data):
            pass  # takes everything, answers nothing

    monkeypatch.setattr(
        usb, "connect", lambda address, timeout, wait=None: Silent(usb_printer.device)
    )
    with pytest.raises(PrinterTimeout):
        VC500W().query(USB_HOST)


def test_no_permission_for_the_usb_port(monkeypatch):
    def refuse(address, timeout, wait=None):
        raise UsbPermission("/dev/usb/lp0: Permission denied")

    monkeypatch.setattr(vc500w, "_connect", REAL_CONNECT)
    monkeypatch.setattr(usb, "connect", refuse)
    with pytest.raises(UsbPermission):
        VC500W().query(USB_HOST)


def test_vc500w_on_usb_is_found(tmp_path, monkeypatch):
    root = tmp_path / "usbmisc"
    for name, ieee in (
        ("lp0", "MFG:Brother;MDL:VC-500W;CMD:XML;SN:E78123;"),
        ("lp1", "MFG:Brother;MDL:QL-800;"),  # another Brother: not this backend
    ):
        (root / name / "device").mkdir(parents=True)
        (root / name / "device" / "ieee1284_id").write_text(ieee)
    monkeypatch.setattr(usb, "SYSFS", root)
    monkeypatch.setattr(usb, "DEV", Path("/dev/usb"))
    monkeypatch.setattr(vc500w.mdns, "browse", lambda service_type, match, timeout: [])
    assert VC500W().discover(timeout=0.01) == [
        FoundPrinter("Brother VC-500W (USB)", USB_HOST, "/dev/usb/lp0")
    ]


def test_drain_reads_until_the_device_is_quiet():
    connection = usb.Connection.__new__(usb.Connection)
    connection.timeout = 5.0
    pieces = [b"old", b"er"]

    def recv(size):
        if not pieces:
            raise PrinterTimeout("quiet")
        return pieces.pop(0)

    connection.recv = recv  # type: ignore[method-assign]
    connection.drain()
    assert pieces == [] and connection.timeout == 5.0  # read all, timeout back


def test_a_chatty_printer_does_not_hold_the_answer_forever(monkeypatch):
    class Chatty(FakeUsbDevice):
        def recv(self, size):
            time.sleep(0.01)
            return b"<status/>"  # never quiet

    monkeypatch.setattr(vc500w, "ANSWER_LIMIT", 0.2)
    start = time.monotonic()
    answer = vc500w._UsbLink(Chatty(FakeVC500W())).get_message()
    assert time.monotonic() - start < 1 and 1 < answer.count("<status/>") < 40


def test_an_answer_has_a_size_limit(monkeypatch):
    class Endless(FakeUsbDevice):
        def recv(self, size):
            return b"x" * size

    monkeypatch.setattr(vc500w, "ANSWER_MAX", 10_000)
    answer = vc500w._UsbLink(Endless(FakeVC500W())).get_message()
    assert 10_000 <= len(answer) < 10_000 + 4096


def test_an_empty_piece_ends_the_answer():
    class Empty(FakeUsbDevice):
        pieces = [b"<status>", b""]

        def recv(self, size):
            return self.pieces.pop(0)

    assert vc500w._UsbLink(Empty(FakeVC500W())).get_message() == "<status>"
