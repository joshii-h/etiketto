"""NIIMBOT printers: packets, row encodings, the serial port and the print
flow against a fake printer on a pseudo terminal (never a real /dev/ttyACM*)."""

from __future__ import annotations

import errno
import os
import select
import threading
import tty
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from PIL import Image, ImageDraw

from etiketto.backends import get_backend, niimbot, serialport
from etiketto.backends.base import (
    CoverOpen,
    Density,
    FoundPrinter,
    HeadOpen,
    JobCanceled,
    LabelTooLong,
    LabelTooWide,
    LowBattery,
    OutOfLabels,
    PaperJam,
    PrinterBusy,
    PrinterError,
    PrinterStalled,
    PrinterTimeout,
    PrintOptions,
    RibbonError,
    SerialPermission,
    UsbNotFound,
)
from etiketto.backends.niimbot import (
    MODELS,
    Niimbot,
    Packet,
    Reader,
    checksum,
    pixel_counts,
    row_packet,
    row_packets,
)
from etiketto.model import Label

ROWS = (niimbot.ROW_BITMAP, niimbot.ROW_INDEXED, niimbot.ROW_EMPTY, niimbot.CHECK_LINE)


def b(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# --- packets --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "packet, expected",
    [
        (Packet(0xDC, b"\x02"), "55 55 DC 01 02 DF AA AA"),  # heartbeat, basic
        (Packet(0x21, b"\x03"), "55 55 21 01 03 23 AA AA"),  # density 3
        (Packet(0x40, b"\x08"), "55 55 40 01 08 49 AA AA"),  # the probe: model id
        (Packet(0xC1), "55 55 C1 01 01 C1 AA AA"),  # connect, default data [1]
    ],
)
def test_golden_packets(packet, expected):
    assert packet.to_bytes() == b(expected)


def test_checksum_is_the_xor_of_command_length_and_data():
    assert checksum(b"\xdc\x01\x02") == 0xDF
    assert checksum(b"") == 0
    assert checksum(bytes([0x13, 4, 0, 240, 1, 128])) == 0x13 ^ 4 ^ 240 ^ 1 ^ 128


def test_packet_data_is_limited_to_one_length_byte():
    assert len(Packet(0x85, bytes(255)).to_bytes()) == 255 + 7
    with pytest.raises(ValueError):
        Packet(0x85, bytes(256)).to_bytes()


def test_indexed_row_from_the_sources():
    """niimbluelib's example: row 126, four black pixels at 39 to 42."""
    row = bytearray(12)
    for x in (39, 40, 41, 42):
        row[x // 8] |= 0x80 >> (x % 8)
    packet = row_packet(126, bytes(row), 1, pixel_counts(bytes(row), 96, total=True))
    assert packet.to_bytes() == b("5555 83 0e 007e 000400 01 0027 0028 0029 002a fa aaaa")


def test_reader_splits_a_bundle_of_two_packets():
    packets = Reader().feed(b("55554a01044faaaa5555f60101f6aaaa"))
    assert packets == [Packet(0x4A, b"\x04"), Packet(0xF6, b"\x01")]


def test_reader_joins_pieces_and_skips_garbage():
    reader = Reader()
    wire = Packet(0x48, b"\x10\x00").to_bytes()
    assert reader.feed(b"\x00\xff\x55" + wire[:3]) == []  # junk, a lone 0x55, half a packet
    assert reader.feed(wire[3:]) == [Packet(0x48, b"\x10\x00")]
    assert reader.feed(b"") == [] and not reader.buffer


@pytest.mark.parametrize(
    "garbage",
    [
        "55 55 48 02 10 00 00 AA AA",  # wrong checksum
        "55 55 48 01 10 59 AB AA",  # wrong tail
        "55 55 55 55",  # headers without a packet
        "AA AA 00 55",
    ],
)
def test_reader_survives_broken_packets(garbage):
    reader = Reader()
    good = Packet(0x02, b"\x01").to_bytes()
    assert reader.feed(b(garbage) + good) == [Packet(0x02, b"\x01")]


def test_reader_keeps_a_trailing_header_byte():
    reader = Reader()
    assert reader.feed(b"\x01\x02\x55") == []
    assert reader.feed(b("55 02 01 01 02 AA AA")) == [Packet(0x02, b"\x01")]


# --- answers --------------------------------------------------------------------------


def test_model_id_protocol_and_firmware():
    assert niimbot.model_id(b"\x10\x00") == 4096
    assert niimbot.model_id(b"\x02") == 512  # a single byte is the high one
    assert niimbot.model_id(b"") == 0
    data = bytearray(13)
    assert niimbot.protocol_version(bytes(data)) == 0
    for version, expected in ((204, 3), (299, 3), (300, 4), (301, 4), (302, 5), (100, 0)):
        data[11], data[12] = divmod(version, 100)
        assert niimbot.protocol_version(bytes(data)) == expected, version
    assert niimbot.protocol_version(b"\x01") == 0
    assert niimbot.firmware(b"\x02\x02") == "5.14"
    assert niimbot.firmware(b"\x02") == ""


def test_print_status():
    assert niimbot.parse_print_status(b"\x00\x03\x64\x64") == niimbot.PrintStatus(3, 0)
    ten = b"\x00\x01\x10\x20\x00\x00\x02\x00\x00\x00"
    assert niimbot.parse_print_status(ten) == niimbot.PrintStatus(1, 2)
    with pytest.raises(PrinterError):
        niimbot.parse_print_status(b"\x01")


@pytest.mark.parametrize(
    "code, cls",
    [
        (1, CoverOpen),
        (2, OutOfLabels),
        (3, LowBattery),
        (5, JobCanceled),
        (8, OutOfLabels),
        (9, PrinterBusy),
        (12, HeadOpen),
        (13, RibbonError),
        (27, PaperJam),
        (99, PrinterError),
    ],
)
def test_error_codes(code, cls):
    error = niimbot.error_for(code)
    assert type(error) is cls
    assert error.params == {"reported": code} and str(code) in error.details


# --- rows -----------------------------------------------------------------------------


def test_ink_rows_put_the_first_pixel_in_the_highest_bit():
    image = Image.new("RGB", (10, 2), "white")
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((9, 1), (40, 40, 40))  # dark grey is black
    image.putpixel((5, 1), (200, 200, 200))  # light grey is white
    assert niimbot.ink_rows(image) == [b"\x80\x00", b"\x00\x40"]


def test_pixel_counts_split_in_thirds_or_total():
    head = 384  # 48 bytes: thirds of 16 bytes
    row = bytes([0xFF] * 16 + [0x0F] * 16 + [0x01] * 16)
    assert pixel_counts(row, head) == bytes([128, 64, 16])
    assert pixel_counts(row, head, total=True) == bytes([0, 208, 0])
    wide = bytes([0xFF] * 50)  # longer than three thirds: the total, low byte first
    assert pixel_counts(wide, head) == bytes([0, 400 & 0xFF, 400 >> 8])
    # B4: a third of 34 bytes holds more black pixels than one byte counts
    assert pixel_counts(bytes([0xFF] * 102), 832) == bytes([255, 255, 255])
    # its full width (104 bytes) does not fit three thirds: the total
    assert pixel_counts(bytes([0xFF] * 104), 832) == bytes([0, 832 & 0xFF, 832 >> 8])


def test_row_encodings():
    empty = row_packet(5, bytes(48), 3, b"\x00\x00\x00")
    assert empty == Packet(niimbot.ROW_EMPTY, b"\x00\x05\x03")
    full = bytes([0xFF]) + bytes(47)  # eight black pixels: the bitmap
    bitmap = row_packet(300, full, 1, b"\x08\x00\x00")
    assert bitmap == Packet(niimbot.ROW_BITMAP, b"\x01\x2c\x08\x00\x00\x01" + full)
    six = bytes([0xFC]) + bytes(47)  # six: their positions
    indexed = row_packet(0, six, 2, b"\x06\x00\x00")
    positions = b"".join(niimbot.u16(x) for x in range(6))
    assert indexed == Packet(niimbot.ROW_INDEXED, b"\x00\x00\x06\x00\x00\x02" + positions)


def test_long_runs_are_split_at_255():
    blank = row_packets([bytes(48)] * 600, 384)
    assert blank == [
        Packet(niimbot.ROW_EMPTY, b"\x00\x00\xff"),
        Packet(niimbot.ROW_EMPTY, b"\x00\xff\xff"),
        Packet(niimbot.ROW_EMPTY, b"\x01\xfe\x5a"),  # row 510, 90 rows
    ]
    line = bytes([0xFF] * 48)
    packets = row_packets([line] * 256 + [bytes(48)], 384)
    assert [(p.command, p.data[:2], p.data[5]) for p in packets[:2]] == [
        (niimbot.ROW_BITMAP, b"\x00\x00", 255),
        (niimbot.ROW_BITMAP, b"\x00\xff", 1),
    ]
    assert packets[2] == Packet(niimbot.ROW_EMPTY, b"\x01\x00\x01")


def test_different_rows_are_not_merged():
    rows = [bytes([1]), bytes([2]), bytes([2]), bytes([1])]
    assert [(start, count) for start, _row, count in niimbot.runs(rows)] == [
        (0, 1),
        (1, 2),
        (3, 1),
    ]


def test_check_lines_every_200_rows():
    packets = row_packets([bytes(48)] * 450, 384, total=True, check_lines=True)
    lines = [p.data for p in packets if p.command == niimbot.CHECK_LINE]
    assert lines == [b"\x00\xc8\x01", b"\x01\x90\x01"]  # after rows 200 and 400


def test_fit_head_keeps_the_middle():
    image = Image.new("RGB", (400, 10), "white")
    image.putpixel((8, 0), (0, 0, 0))  # the first pixel the 384 px head prints
    fitted = niimbot.fit_head(image, 384)
    assert fitted.size == (384, 10) and fitted.getpixel((0, 0)) == (0, 0, 0)
    assert niimbot.fit_head(image, 400) is image


# --- models ---------------------------------------------------------------------------


def test_model_table():
    ids = [i for model in MODELS for i in model.ids]
    assert len(ids) == len(set(ids))  # each id names one model
    b1 = niimbot.BY_ID[4096]
    assert (b1.name, b1.head_px, b1.task, b1.widest_mm) == ("B1", 384, "b1", 50)
    assert b1.head_mm == pytest.approx(48)
    assert niimbot.BY_ID[512].density == Density(1, 3, 2)  # D11
    assert niimbot.BY_ID[2560].head_px == 192  # D101
    assert niimbot.BY_ID[6656].widest_mm == 106  # B4: 104 mm head + 2
    for model in MODELS:
        # no NIIMBOT printer has 229 dpi: 8 dots/mm or 300 dpi, never computed
        assert model.dpi in (niimbot.DPI_203, niimbot.DPI_300), model.name
        assert set(model.media) <= set(niimbot.LABEL_TYPES)
        assert model.task in ("b1", "d110", "b21", "v4")
        assert model.density.lowest <= model.density.default <= model.density.highest
        assert model.head_mm <= model.widest_mm + 0.1  # B21 PRO: 591 px are 50.04 mm
    assert pytest.approx(8) == niimbot.DPI_203 / 25.4


def test_task_for_protocol():
    d110m = niimbot.BY_NAME["D110 M"]
    assert niimbot.task_for(d110m, 0) == "b1"
    assert niimbot.task_for(d110m, 4) == "v4"
    assert niimbot.task_for(niimbot.BY_NAME["B1"], 5) == "b1"


def test_density_clamp():
    density = Density(1, 5, 3)
    assert [density.clamp(v) for v in (0, 1, 4, 9, -2, True, "x")] == [3, 1, 4, 5, 1, 3, 3]


# --- the serial port ------------------------------------------------------------------


@pytest.fixture
def tty_sysfs(tmp_path, monkeypatch):
    """A fake /sys/class/tty and /dev/serial/by-id; add(ttyACMn, ...)."""
    root = tmp_path / "sys"
    by_id = tmp_path / "by-id"
    root.mkdir()
    by_id.mkdir()
    monkeypatch.setattr(serialport, "SYSFS", root)
    monkeypatch.setattr(serialport, "BY_ID", by_id)

    def add(name, vendor="3513", product_id="0002", manufacturer="", product="", link=None):
        usb_device = tmp_path / "devices" / name
        interface = usb_device / "1-2:1.0"
        interface.mkdir(parents=True)
        node = root / name
        node.mkdir()
        (node / "device").symlink_to(interface)
        for key, value in (
            ("idVendor", vendor),
            ("idProduct", product_id),
            ("manufacturer", manufacturer),
            ("product", product),
            ("serial", "SN" + name[-1]),
        ):
            if value is not None:
                (usb_device / key).write_text(value + "\n")
        if link:
            (by_id / link).symlink_to("../../" + name)

    return add


def test_serial_devices_from_sysfs(tty_sysfs):
    tty_sysfs("ttyACM0", manufacturer="NIIMBOT", product="B1", link="usb-NIIMBOT_B1-if00")
    tty_sysfs("ttyACM1", vendor="2341", product_id="0043", manufacturer="Arduino", product="Uno")
    tty_sysfs("ttyACM2", vendor=None)  # not on USB
    found = serialport.devices()
    assert [d.device for d in found] == [Path("/dev/ttyACM0"), Path("/dev/ttyACM1")]
    first = found[0]
    assert (first.vendor_id, first.product_id, first.product, first.serial) == (
        0x3513,
        2,
        "B1",
        "SN0",
    )
    assert first.address == str(serialport.BY_ID / "usb-NIIMBOT_B1-if00")
    assert found[1].address == "/dev/ttyACM1"  # no stable name


def test_discover_lists_only_niimbot_printers(tty_sysfs):
    tty_sysfs("ttyACM0", vendor="1a86", manufacturer="NIIMBOT", product="D110")
    tty_sysfs("ttyACM1", vendor="2341", manufacturer="Arduino", product="Uno")
    tty_sysfs("ttyACM2", vendor="3513", manufacturer="", product="")
    assert Niimbot().discover() == [
        FoundPrinter("D110 (USB)", "/dev/ttyACM0", "/dev/ttyACM0"),
        FoundPrinter("NIIMBOT (USB)", "/dev/ttyACM2", "/dev/ttyACM2"),
    ]


def test_no_sysfs_no_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(serialport, "SYSFS", tmp_path / "missing")
    monkeypatch.setattr(serialport, "BY_ID", tmp_path / "missing-too")
    assert serialport.devices() == []
    assert serialport._hex("zz") == 0


@pytest.mark.parametrize(
    "host, path",
    [
        ("/dev/ttyACM0", "/dev/ttyACM0"),
        (" ttyACM1 ", "/dev/ttyACM1"),
        ("/dev/serial/by-id/usb-NIIMBOT_B1-if00", "/dev/serial/by-id/usb-NIIMBOT_B1-if00"),
    ],
)
def test_device_path(host, path):
    assert serialport.device_path(host) == Path(path)


@pytest.mark.parametrize("host", ["printer.local", "192.0.2.1", "tty/../x", ""])
def test_device_path_refuses_other_addresses(host):
    with pytest.raises(UsbNotFound):
        serialport.device_path(host)


def test_the_tests_never_open_a_real_serial_port():
    for path in ("/dev/ttyACM0", "/dev/ttyUSB0", "/dev/serial/by-id/usb-x"):
        with pytest.raises(AssertionError, match="serial port"):
            os.open(path, os.O_RDWR)


def test_port_permission_message(monkeypatch, tmp_path):
    def refuse(path, flags, *args):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(SerialPermission) as caught:
        serialport.Port(tmp_path / "ttyACM9", wait=0.1)
    assert caught.value.code == "serial_permission"


def test_port_gone_and_not_a_terminal(tmp_path):
    with pytest.raises(UsbNotFound):
        serialport.Port(tmp_path / "ttyACM9", wait=0.1)
    plain = tmp_path / "file"
    plain.write_bytes(b"")
    with pytest.raises(PrinterError, match="file"):
        serialport.Port(plain, wait=0.1)
    # the lock was given back: the next attempt fails the same way, not as busy
    with pytest.raises(PrinterError, match="file"):
        serialport.Port(plain, wait=0.1)


def test_port_busy_while_another_program_holds_it(monkeypatch, tmp_path):
    calls = []

    def busy(path, flags, *args):
        calls.append(path)
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "open", busy)
    with pytest.raises(PrinterBusy):
        serialport.Port(tmp_path / "ttyACM9", wait=0.15)
    assert len(calls) >= 2  # tried again


@pytest.fixture
def pty_port():
    master, slave = os.openpty()
    tty.setraw(slave)
    path = Path(os.ttyname(slave))
    yield SimpleNamespace(master=master, path=path)
    os.close(slave)
    os.close(master)


def test_port_on_a_pseudo_terminal(pty_port):
    port = serialport.Port(pty_port.path, wait=0.5)
    try:
        port.write(b"hello", timeout=1)
        assert os.read(pty_port.master, 10) == b"hello"
        os.write(pty_port.master, b"answer")
        assert port.read(deadline=serialport.time.monotonic() + 1) == b"answer"
        assert port.read(deadline=serialport.time.monotonic() + 0.05) == b""
        with pytest.raises(PrinterBusy):  # one session at a time
            serialport.Port(pty_port.path, wait=0.05)
    finally:
        port.close()
    port.close()  # twice is fine and gives no foreign lock back
    serialport.Port(pty_port.path, wait=0.05).close()


def test_port_write_stalls_when_nothing_is_taken(pty_port, monkeypatch):
    port = serialport.Port(pty_port.path, wait=0.5)
    monkeypatch.setattr(serialport, "MIN_RATE", 10**9)
    try:
        with pytest.raises(PrinterStalled):
            port.write(bytes(1 << 20), timeout=0.2)  # nobody reads the other end
    finally:
        port.close()


def test_port_errors_while_talking(pty_port, monkeypatch, tmp_path):
    port = serialport.Port(pty_port.path, wait=0.5)
    real_write, real_read = os.write, os.read
    try:
        calls = []

        def write_once_blocked(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                raise BlockingIOError
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", write_once_blocked)
        port.write(b"abc", timeout=1)
        assert calls == [3, 3] and real_read(pty_port.master, 10) == b"abc"

        def fail(*args):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "write", fail)
        with pytest.raises(PrinterError, match="Input/output") as caught:
            port.write(b"x", timeout=1)
        assert not isinstance(caught.value, UsbNotFound)  # the device is still there
        real_write(pty_port.master, b"!")
        monkeypatch.setattr(os, "read", fail)
        with pytest.raises(PrinterError):
            port.read(deadline=serialport.time.monotonic() + 1)

        def blocked(*args):
            raise BlockingIOError

        monkeypatch.setattr(os, "read", blocked)  # readable, then nothing after all
        assert port.read(deadline=serialport.time.monotonic() + 0.05) == b""
        monkeypatch.setattr(os, "read", lambda fd, size: b"")  # hung up
        assert port.read(deadline=serialport.time.monotonic() + 0.05) == b""
        monkeypatch.setattr(os, "read", real_read)
        real_read(pty_port.master, 0)

        port.device = tmp_path / "ttyACM0"  # unplugged meanwhile
        monkeypatch.setattr(serialport, "SLICE", 0.01)
        monkeypatch.setattr(os, "read", fail)
        with pytest.raises(UsbNotFound):
            port.read(deadline=serialport.time.monotonic() + 1)
        monkeypatch.setattr(os, "read", real_read)
        real_read(port.fd, 10)  # take the "!" so that nothing is readable
        with pytest.raises(UsbNotFound, match="is gone"):
            port.read(deadline=serialport.time.monotonic() + 1)
    finally:
        monkeypatch.setattr(os, "write", real_write)
        monkeypatch.setattr(os, "read", real_read)
        port.close()


def test_stable_names_skip_entries_that_are_no_links(tty_sysfs):
    (serialport.BY_ID / "plain-file").write_text("")
    tty_sysfs("ttyACM0", link="usb-NIIMBOT_B1-if00")
    assert serialport.devices()[0].by_id == serialport.BY_ID / "usb-NIIMBOT_B1-if00"


# --- a fake printer on a pseudo terminal ----------------------------------------------


class FakeNiimbot:
    """Answers like a NIIMBOT printer on the other end of a pseudo terminal;
    records every packet. Labels print one per status poll."""

    def __init__(self, model_id: int = 4096):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.model_id = model_id
        self.connect_result = 1
        self.status_data = b""
        self.firmware = b"\x02\x02"
        self.error = 0  # answered with 0xDB to every request
        self.errors: dict[int, int] = {}  # command -> error code instead of the answer
        self.status_errors: dict[int, int] = {}  # labels printed -> error in the status
        self.silent: set[int] = set()
        self.garbage = b""  # sent before every answer
        self.busy_ends = 0  # PRINT_END answers 0 this many times
        self.log: list[Packet] = []
        self.copies = 1  # of the page being sent
        self.queued = 0  # labels sent
        self.printed = 0
        self._stop = threading.Event()
        self._reader = Reader()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        os.close(self.slave)
        os.close(self.master)

    def wait_for(self, condition, timeout: float = 2.0) -> None:
        """Until the fake has read what was sent without waiting for an answer."""
        deadline = serialport.time.monotonic() + timeout
        while not condition():
            assert serialport.time.monotonic() < deadline, "the fake never got it"
            serialport.time.sleep(0.005)

    def commands(self, rows: bool = False) -> list[int]:
        return [p.command for p in self.log if rows or p.command not in ROWS]

    def sent(self, command: int) -> list[bytes]:
        return [p.data for p in self.log if p.command == command]

    def _run(self) -> None:
        while not self._stop.is_set():
            if not select.select([self.master], [], [], 0.02)[0]:
                continue
            try:
                data = os.read(self.master, 1 << 16)
            except OSError:
                continue
            for packet in self._reader.feed(data):
                self.log.append(packet)
                self._handle(packet)

    def _answer(self, command: int, data: bytes = b"\x01") -> None:
        os.write(self.master, self.garbage + Packet(command, data).to_bytes())

    def _handle(self, packet: Packet) -> None:
        command = packet.command
        if command in ROWS or command in self.silent:
            return
        code = self.errors.get(command, self.error)
        if code:
            self._answer(niimbot.ERROR, bytes([code]))
            return
        answer = getattr(self, "_on_%02x" % command, None)
        if answer:
            answer(packet.data)
        elif command in niimbot.ANSWERS:
            self._answer(niimbot.ANSWERS[command][0])
        else:
            self._answer(niimbot.NOT_SUPPORTED, bytes([command]))

    def _on_c1(self, data):
        self._answer(0xC2, bytes([self.connect_result]))

    def _on_a5(self, data):
        self._answer(0xB5, self.status_data)

    def _on_40(self, data):
        kind = data[0]
        value = {8: niimbot.u16(self.model_id), 9: self.firmware}.get(kind, b"\x00")
        self._answer(0x40 + kind, value)

    def _on_dc(self, data):
        self._answer(niimbot.HEARTBEAT_ANSWERS[data[0]], bytes(10))

    def _on_13(self, data):
        self.copies = data[4] << 8 | data[5] if len(data) >= 6 else 1
        self._answer(0x14)

    def _on_15(self, data):
        self.copies = data[0] << 8 | data[1]
        self._answer(0x16)

    def _on_e3(self, data):
        self.queued += self.copies
        self._answer(0xE4)

    def _on_a3(self, data):
        if self.printed < self.queued:
            self.printed += 1
        error = self.status_errors.get(self.printed, 0)
        self._answer(
            0xB3, niimbot.u16(self.printed) + b"\x64\x64\x00\x00" + bytes([error, 0, 0, 0])
        )

    def _on_f3(self, data):
        if self.busy_ends:
            self.busy_ends -= 1
            self._answer(0xF4, b"\x00")
            return
        self.printed = max(self.printed, self.queued)
        self._answer(0xF4, b"\x01")


@pytest.fixture
def fake(monkeypatch):
    """A fake printer; every address leads to it, polls do not sleep."""
    printer = FakeNiimbot()
    monkeypatch.setattr(
        niimbot, "_connect", lambda host, wait: serialport.Port(Path(printer.path), wait)
    )
    monkeypatch.setattr(niimbot, "_sleep", lambda seconds: None)
    monkeypatch.setattr(niimbot, "REQUEST_TIMEOUT", 0.3)
    yield printer
    printer.close()


def _image(width=320, height=240) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 60, 40), fill="black")
    draw.line((0, height - 1, width - 1, height - 1), fill="black")
    return image


def test_query_a_b1(fake):
    backend = Niimbot()
    info = backend.query("/dev/ttyACM0")
    assert (info.model, info.firmware, info.state, info.error) == ("B1", "5.14", "IDLE", "")
    assert backend.display_name(info) == "NIIMBOT B1"
    assert fake.commands() == [0xC1, 0x40, 0x40, 0xDC]
    assert fake.sent(0x40) == [b"\x08", b"\x09"]
    assert backend.raster(50, PrintOptions()).dpi == niimbot.DPI_203


def test_query_survives_garbage_and_unasked_answers(fake):
    fake.garbage = b"\x00\xff" + Packet(0xB3, bytes(4)).to_bytes() + b"\x55\x55\x13"
    info = Niimbot().query("printer")
    assert info.model == "B1" and info.firmware == "5.14"


def test_query_reports_a_problem_as_state(fake):
    fake.errors[0xDC] = 1
    info = Niimbot().query("printer")
    assert (info.model, info.state, info.error) == ("B1", "ERROR", "cover_open")
    fake.errors = {0xC1: 2}  # already when connecting: the model is the one asked before
    backend = Niimbot()
    info = backend.query("printer")
    assert (info.model, info.error) == ("", "out_of_labels")
    assert backend.display_name(info) == backend.name


def test_query_without_firmware_heartbeat_or_connect(fake):
    fake.silent = {0xC1, 0xDC}
    fake.firmware = b""
    info = Niimbot().query("printer")
    assert info.model == "B1" and info.firmware == ""
    fake.errors = {0x40: 0}
    fake.silent = {0x40}
    with pytest.raises(PrinterTimeout):
        Niimbot().query("printer")


def test_query_an_unknown_model(fake):
    fake.model_id = 4660
    backend = Niimbot()
    info = backend.query("printer")
    assert info.model == "model 4660" and backend.display_name(info) == "NIIMBOT model 4660"
    assert backend.density(info) == backend.capabilities.density  # the documented range
    assert backend.media_types(info) == backend.capabilities.media_types
    with pytest.raises(PrinterError, match="unknown NIIMBOT model id 4660"):
        backend.print_label(_image(), "printer", PrintOptions())
    assert 0x01 not in fake.commands()  # nothing printed


def test_query_a_d110_with_protocol_4(fake):
    fake.model_id = 2320  # D110 M
    fake.connect_result = 3
    fake.status_data = bytes(11) + bytes([3, 0])  # 300: protocol 4
    backend = Niimbot()
    info = backend.query("printer")
    assert info.model == "D110 M"
    assert fake.commands()[:3] == [0xC1, 0xA5, 0x40]
    assert backend.density(info) == Density(1, 5, 3)
    assert backend.media_types(info) == ("gap", "transparent")


def test_print_on_a_b1(fake):
    events = []
    image = _image(320, 240)  # 40 × 30 mm at 8 dots/mm
    image.info["niimbot_dpi"] = niimbot.DPI_203
    Niimbot().print_label(image, "printer", PrintOptions(copies=2, density=4), events.append)
    assert fake.commands() == [
        0xC1,
        0x40,  # model
        0x21,  # density
        0x23,  # label type
        0x01,  # print start
        0x03,  # page start
        0x13,  # page size
        0xE3,  # page end
        0xA3,
        0xA3,  # two polls, one label each
        0xF3,
    ]
    assert fake.sent(0x21) == [b"\x04"] and fake.sent(0x23) == [b"\x01"]
    assert fake.sent(0x01) == [b"\x00\x02\x00\x00\x00\x00\x00"]
    assert fake.sent(0x13) == [niimbot.u16(240) + niimbot.u16(320) + niimbot.u16(2)]
    rows = [p for p in fake.log if p.command in ROWS]
    assert rows[0].command == niimbot.ROW_EMPTY and rows[0].data == b"\x00\x00\x0a"
    assert rows[-1].data[:2] == niimbot.u16(239)  # the last row
    assert fake.printed == 2
    assert [(e.copy, e.copies) for e in events] == [(1, 2), (2, 2), (2, 2)]


def test_print_on_a_d110(fake):
    fake.model_id = 2304
    image = _image(96, 320)  # 12 × 40 mm
    Niimbot().print_label(image, "printer", PrintOptions(copies=3, density=5, media="continuous"))
    assert fake.commands() == [0xC1, 0x40, 0x21, 0x23, 0x01, 0x20, 0x03, 0x13, 0x15, 0xE3] + [
        0xA3
    ] * 3 + [0xF3]
    assert fake.sent(0x21) == [b"\x03"]  # D110: density 1 to 3
    assert fake.sent(0x23) == [b"\x01"]  # no continuous rolls: gaps
    assert fake.sent(0x01) == [b"\x01"]
    assert fake.sent(0x13) == [niimbot.u16(320) + niimbot.u16(96)]
    assert fake.sent(0x15) == [b"\x00\x03"]
    assert fake.printed == 3


def test_print_on_a_b21(fake):
    fake.model_id = 768
    fake.busy_ends = 2
    events = []
    image = _image(384, 450)
    Niimbot().print_label(
        image, "printer", PrintOptions(copies=2, media="black_mark"), events.append
    )
    commands = fake.commands()
    assert commands == [0xC1, 0x40, 0x21, 0x23, 0x01] + [0x03, 0x13, 0xE3] * 2 + [0xF3] * 3
    assert fake.sent(0x23) == [b"\x02"]
    assert fake.sent(0x13) == [niimbot.u16(450) + niimbot.u16(384)] * 2
    lines = [p.data for p in fake.log if p.command == niimbot.CHECK_LINE]
    assert lines == [b"\x00\xc8\x01", b"\x01\x90\x01"] * 2
    bitmap = [p for p in fake.log if p.command == niimbot.ROW_BITMAP][0]
    assert bitmap.data[2] == 0  # counts as a total
    assert fake.printed == 2 and {e.copy for e in events} == {1, 2}


def test_print_on_a_b1_pro_with_protocol_4(fake):
    fake.model_id = 4097
    image = _image(560, 300)
    image.info["niimbot_dpi"] = niimbot.DPI_300
    backend = Niimbot()
    backend.print_label(image, "printer", PrintOptions(density=2, media="transparent"))
    fake.wait_for(lambda: 0xDC in fake.commands())  # sent last, without an answer
    assert fake.commands() == [
        0xC1,
        0x40,
        0x23,
        0x21,
        0x01,
        0xA3,
        0x03,
        0x13,
        0xE3,
        0xA3,
        0xF3,
        0xDC,
    ]
    assert fake.sent(0x23) == [b"\x05"]
    assert fake.sent(0x01) == [b"\x00\x01" + bytes(5) + b"\x01\x00"]
    assert fake.sent(0x13) == [niimbot.u16(300) + niimbot.u16(560) + niimbot.u16(1) + bytes(7)]
    assert fake.sent(0xDC) == [b"\x01"]
    assert backend.raster(50, PrintOptions()).dpi == niimbot.DPI_300


def test_print_rescales_an_image_made_for_another_resolution(fake):
    fake.model_id = 4097  # 300 dpi
    image = _image(400, 240)  # 50 × 30 mm at 8 dots/mm
    Niimbot().print_label(image, "printer", PrintOptions())
    rows, cols = fake.sent(0x13)[0][:2], fake.sent(0x13)[0][2:4]
    assert niimbot.u16(354) == rows  # 30 mm at 300 dpi
    assert niimbot.u16(568) == cols  # 591 px cut to the 567 px head, padded to bytes


def test_print_cuts_a_wide_label_to_the_head(fake):
    Niimbot().print_label(_image(400, 80), "printer", PrintOptions())  # 50 mm on a B1
    assert fake.sent(0x13)[0][2:4] == niimbot.u16(384)


def test_print_refuses_a_label_too_wide_or_too_long(fake):
    with pytest.raises(LabelTooWide) as caught:
        Niimbot().print_label(_image(424, 80), "printer", PrintOptions())  # 53 mm
    assert caught.value.params["max_mm"] == 50
    with pytest.raises(LabelTooLong):
        Niimbot().print_label(Image.new("RGB", (8, 70000), "white"), "printer", PrintOptions())
    assert 0x01 not in fake.commands()


def test_an_error_while_printing_says_how_many_labels_came_out(fake):
    fake.status_errors = {1: 2}  # no paper after the first label
    with pytest.raises(OutOfLabels) as caught:
        Niimbot().print_label(_image(), "printer", PrintOptions(copies=3))
    assert caught.value.params == {"reported": 2, "printed": 1, "copies": 3}
    assert fake.commands()[-1] == niimbot.CANCEL


def test_non_fatal_errors_do_not_stop_a_job(fake):
    fake.status_errors = {1: 0x1D}  # RFID tag not written
    Niimbot().print_label(_image(), "printer", PrintOptions(copies=2))
    assert fake.printed == 2


def test_an_error_answer_to_a_page_cancels_the_job(fake):
    fake.errors[0xE3] = 6
    with pytest.raises(PrinterError) as caught:
        Niimbot().print_label(_image(), "printer", PrintOptions())
    assert "data error" in caught.value.details
    assert fake.commands()[-1] == niimbot.CANCEL


def test_a_printer_that_stops_answering_times_out(fake, monkeypatch):
    fake.silent = {0x03}
    with pytest.raises(PrinterTimeout):
        Niimbot().print_label(_image(), "printer", PrintOptions())
    assert fake.commands()[-1] == niimbot.CANCEL


def test_a_printer_that_never_finishes_times_out(fake, monkeypatch):
    fake.queued = -100  # the page counter never gets there
    monkeypatch.setattr(niimbot, "PRINT_TIMEOUT", 0.2)
    with pytest.raises(PrinterTimeout, match="printed 0 of 1"):
        Niimbot().print_label(_image(), "printer", PrintOptions())


def test_a_b21_that_never_ends_times_out(fake, monkeypatch):
    fake.model_id = 768
    fake.busy_ends = 10**6
    monkeypatch.setattr(niimbot, "PRINT_TIMEOUT", 0.1)
    with pytest.raises(PrinterTimeout, match="did not finish"):
        Niimbot().print_label(_image(384, 20), "printer", PrintOptions())


def test_unsupported_requests(fake):
    with niimbot.session("printer") as s:
        with pytest.raises(niimbot.NotSupported):
            s.request(0x59, expect=(0x69,))  # paper info: the fake does not know it
        assert s.optional(0x59, expect=(0x69,)) is None


def test_garbage_answers_do_not_confuse_a_job(fake):
    fake.garbage = b"\xaa\xaa\x55" + Packet(0xDB, b"\x00").to_bytes()  # error 0: none
    Niimbot().print_label(_image(), "printer", PrintOptions(copies=2))
    assert fake.printed == 2


def test_a_real_address_goes_to_the_serial_port(monkeypatch):
    opened = []

    class Port:
        def __init__(self, device, wait):
            opened.append((device, wait))
            raise UsbNotFound("gone")

    monkeypatch.setattr(serialport, "Port", Port)
    with pytest.raises(UsbNotFound):
        Niimbot().query("ttyACM3")
    assert opened == [(Path("/dev/ttyACM3"), serialport.BUSY_WAIT)]
    with pytest.raises(PrinterError):
        Niimbot().print_label(_image(), "/dev/ttyACM3", PrintOptions())
    assert opened[-1] == (Path("/dev/ttyACM3"), serialport.PRINT_WAIT)


def test_map_error():
    assert isinstance(niimbot.map_error(OSError(5, "I/O error")), PrinterError)
    error = CoverOpen("x")
    assert niimbot.map_error(error) is error


def test_prepare_in_printer_orientation(png_b64):
    backend = Niimbot()
    label = Label(printer="niimbot", tape_mm=12, auto_length=False, length_mm=40)
    label.orientation = "along"  # a D11 label: 12 mm across the head, 40 along the feed
    image = backend.prepare(label, PrintOptions())
    assert image.size == (96, 320) and image.info["niimbot_dpi"] == niimbot.DPI_203


def test_registered():
    backend = get_backend("niimbot")
    assert isinstance(backend, Niimbot)
    caps = backend.capabilities
    assert not caps.continuous and caps.max_width_mm == 106
    assert caps.label_sizes[0].width_mm == 50


# --- the contract every backend passes --------------------------------------------------


class TestContract(BackendContract):
    backend_id = "niimbot"

    @pytest.fixture
    def harness(self, fake, tty_sysfs):
        tty_sysfs("ttyACM0", manufacturer="NIIMBOT", product="B1")

        def fail():
            fake.error = 1  # cover open

        return SimpleNamespace(backend=Niimbot(), fail=fail, printed=lambda: fake.printed)
