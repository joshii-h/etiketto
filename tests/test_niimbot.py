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


def b(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# --- packets --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "packet, expected",
    [
        (Packet(0xDC, b"\x02"), "55 55 DC 01 02 DF AA AA"),  # heartbeat, basic
        (Packet(0x21, b"\x03"), "55 55 21 01 03 23 AA AA"),  # density 3
        (Packet(0x40, b"\x08"), "55 55 40 01 08 49 AA AA"),  # the probe: model id
        (Packet(0xC1), "03 55 55 C1 01 01 C1 AA AA"),  # connect: prefixed with 03
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
    assert niimbot.firmware(niimbot.u16(781)) == "7.81"  # Thermalith's B3S P
    assert niimbot.firmware(b"\x02") == ""
    assert niimbot.firmware(b"\x01\x02\x03") == ""


def test_print_status():
    four = b"\x00\x03\x64\x32"
    assert niimbot.parse_print_status(four) == niimbot.PrintStatus(3, 100, 50, 0)
    ten = b"\x00\x01\x10\x20\x00\x00\x02\x00\x00\x00"
    assert niimbot.parse_print_status(ten) == niimbot.PrintStatus(1, 16, 32, 2)
    eight = b"\x00\x01\x64\x64\x00\x00\x02\x00"  # only the 10 byte form has an error
    assert niimbot.parse_print_status(eight).error == 0
    with pytest.raises(PrinterError):
        niimbot.parse_print_status(b"\x00\x01\x64")


@pytest.mark.parametrize(
    "command, data, model, expected",
    [
        # type 1, D110 form: lid, battery (steps of 25 %)
        (0xDD, bytes(8) + b"\x00\x03", 2320, niimbot.Heartbeat(True, None, 75)),
        # the D110 (2304) reports its lid the other way round
        (0xDD, bytes(8) + b"\x00\x03", 2304, niimbot.Heartbeat(False, None, 75)),
        # type 1, B1 form: lid, battery, paper, RFID
        (0xDD, bytes(9) + b"\x00\x04\x01\x01", 4096, niimbot.Heartbeat(True, False, 100)),
        (0xDD, bytes(15) + b"\x01\x50\x00\x01", 4096, niimbot.Heartbeat(False, True, 80)),
        (0xDD, bytes(18) + b"\x00\x01", 4096, niimbot.Heartbeat(None, True, None)),
        (0xDD, bytes(5), 4096, niimbot.Heartbeat()),  # a length nobody knows
        # type 4: 2 bytes, battery, temperature, lid, paper, ...
        (0xD9, b"\x00\x00\x08\x20\x00\x00\x01\x01\x00", 2320, niimbot.Heartbeat(True, True, 8)),
        (0xD9, b"\x00\x00\x02", 2320, niimbot.Heartbeat()),
    ],
)
def test_heartbeat(command, data, model, expected):
    assert niimbot.parse_heartbeat(command, data, model) == expected


@pytest.mark.parametrize(
    "heartbeat, code",
    [
        (niimbot.Heartbeat(True, True, 100), ""),
        (niimbot.Heartbeat(), ""),
        (niimbot.Heartbeat(False, True, 100), "cover_open"),
        (niimbot.Heartbeat(True, False, 100), "out_of_labels"),
        (niimbot.Heartbeat(True, True, 8), "battery"),
        (niimbot.Heartbeat(True, True, 0), ""),  # 0: no battery or unknown
    ],
)
def test_heartbeat_problems(heartbeat, code):
    assert heartbeat.error() == code


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
    # B4: a third of 34 bytes holds 272 black pixels, one byte wraps as upstream
    assert pixel_counts(bytes([0xFF] * 102), 832) == bytes([16, 16, 16])
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


def test_runs_end_at_each_check_point():
    """As niimbluelib's encoder: rows 0-199, 200-399, ... never share a run."""
    blank = row_packets([bytes(48)] * 450, 384)
    assert blank == [
        Packet(niimbot.ROW_EMPTY, b"\x00\x00\xc8"),  # rows 0 to 199
        Packet(niimbot.ROW_EMPTY, b"\x00\xc8\xc8"),  # 200 to 399
        Packet(niimbot.ROW_EMPTY, b"\x01\x90\x32"),  # 400 to 449
    ]


def test_long_runs_are_split_at_255():
    rows = [bytes(1)] * 600
    assert [(start, n) for start, _row, n in niimbot.runs(rows, check_every=0)] == [
        (0, 255),
        (255, 255),
        (510, 90),
    ]


def test_different_rows_are_not_merged():
    rows = [bytes([1]), bytes([2]), bytes([2]), bytes([1])]
    assert [(start, count) for start, _row, count in niimbot.runs(rows)] == [
        (0, 1),
        (1, 2),
        (3, 1),
    ]


def test_check_lines_after_rows_199_and_399():
    packets = row_packets([bytes(48)] * 450, 384, total=True, check_lines=True)
    assert [p.command for p in packets] == [
        niimbot.ROW_EMPTY,
        niimbot.CHECK_LINE,
        niimbot.ROW_EMPTY,
        niimbot.CHECK_LINE,
        niimbot.ROW_EMPTY,
    ]
    lines = [p.data for p in packets if p.command == niimbot.CHECK_LINE]
    assert lines == [niimbot.u16(199) + b"\x01", niimbot.u16(399) + b"\x01"]


def test_position_on_head_centres_and_cuts():
    narrow = Image.new("RGB", (320, 4), "white")
    narrow.putpixel((0, 0), (0, 0, 0))
    page = niimbot.position_on_head(narrow, 384)
    assert page.size == (384, 4) and page.getpixel((32, 0)) == (0, 0, 0)
    assert page.getpixel((0, 0)) == (255, 255, 255)
    wide = Image.new("RGB", (400, 10), "white")
    wide.putpixel((8, 0), (0, 0, 0))  # the first pixel the 384 px head prints
    cut = niimbot.position_on_head(wide, 384)
    assert cut.size == (384, 10) and cut.getpixel((0, 0)) == (0, 0, 0)
    exact = Image.new("RGB", (384, 1), "white")
    assert niimbot.position_on_head(exact, 384) is exact


# --- models ---------------------------------------------------------------------------


def test_model_table():
    ids = [i for model in MODELS for i in model.ids]
    assert len(ids) == len(set(ids))  # each id names one model
    b1 = niimbot.BY_ID[4096]
    assert (b1.name, b1.head_px, b1.task, b1.widest_mm) == ("B1", 384, "b1", 50)
    assert b1.head_mm == pytest.approx(48)
    assert niimbot.BY_ID[512].density == Density(1, 3, 2)  # D11
    assert niimbot.BY_ID[514].name == "D11S"
    assert niimbot.BY_ID[2560].head_px == 192  # D101
    assert niimbot.BY_ID[6656].widest_mm == 108  # B4: 108 mm stock, 104 mm head
    for model in MODELS:
        # no NIIMBOT printer has 229 dpi: 8 dots/mm or 300 dpi, never computed
        assert model.dpi in (niimbot.DPI_203, niimbot.DPI_300), model.name
        assert set(model.media) <= set(niimbot.LABEL_TYPES)
        assert model.task in ("", "b1", "d110", "d11", "b21", "b21_l2b", "h1s", "v4")
        assert model.density.lowest <= model.density.default <= model.density.highest
        assert model.head_mm <= model.widest_mm + 0.1  # B21 PRO: 591 px are 50.04 mm
    assert pytest.approx(8) == niimbot.DPI_203 / 25.4


@pytest.mark.parametrize(
    "name, protocol, task",
    [
        ("D11", 0, "d11"),  # the old sequence by default
        ("D11", 1, "d110"),
        ("D11", 2, "d110"),
        ("D11", 3, "d11"),
        ("D11S", 0, "d11"),
        ("D11S", 1, "d11"),  # only the D11 switches
        ("D110 M", 0, "b1"),
        ("D110 M", 4, "v4"),
        ("D110 M", 5, "b1"),  # exactly 4, as niimbluelib
        ("B4", 4, "b1"),  # tried on hardware with the B1 sequence (Thermalith)
        ("B3S P", 4, "b1"),
        ("B1 SE", 0, "b1"),  # no sequence of its own
        ("B1 SE", 4, "v4"),
        ("B1", 5, "b1"),
        ("H1S", 0, "h1s"),
        ("B21 L2B", 0, "b21_l2b"),
    ],
)
def test_task_for_protocol(name, protocol, task):
    assert niimbot.task_for(niimbot.BY_NAME[name], protocol) == task


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
        (" ttyUSB2", "/dev/ttyUSB2"),
        ("/dev/serial/by-id/usb-NIIMBOT_B1-if00", "/dev/serial/by-id/usb-NIIMBOT_B1-if00"),
    ],
)
def test_device_path(host, path):
    assert serialport.device_path(host) == Path(path)


@pytest.mark.parametrize(
    "host",
    [
        "printer.local",
        "192.0.2.1",
        "tty/../x",
        "",
        "/dev/pts/0",
        "/dev/ttyS0",
        "ttyS1",
        "/etc/passwd",
    ],
)
def test_device_path_refuses_other_addresses(host):
    with pytest.raises(UsbNotFound):
        serialport.device_path(host)


def test_device_path_needs_a_device(tmp_path, monkeypatch):
    monkeypatch.setattr(serialport, "DEV", tmp_path)
    (tmp_path / "ttyACM5").write_text("")
    with pytest.raises(UsbNotFound, match="not a device"):
        serialport.device_path("ttyACM5")
    assert serialport.device_path("ttyACM6") == tmp_path / "ttyACM6"  # not plugged in


def test_device_path_follows_a_stable_name(tmp_path, monkeypatch):
    monkeypatch.setattr(serialport, "DEV", tmp_path)
    monkeypatch.setattr(serialport.stat, "S_ISCHR", lambda mode: True)  # a file stands in
    (tmp_path / "ttyACM0").write_text("")
    by_id = tmp_path / "serial" / "by-id"
    by_id.mkdir(parents=True)
    link = by_id / "usb-NIIMBOT_B1-if00"
    link.symlink_to("../../ttyACM0")
    assert serialport.device_path(str(link)) == link
    (tmp_path / "pts").mkdir()
    (by_id / "elsewhere").symlink_to("../../pts")  # a link to no USB serial port
    with pytest.raises(UsbNotFound):
        serialport.device_path(str(by_id / "elsewhere"))


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
        device = port.device
        port.device = tmp_path / "gone"  # hung up because it was unplugged
        with pytest.raises(UsbNotFound, match="is gone"):
            port.read(deadline=serialport.time.monotonic() + 5)
        port.device = device
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

V4_MODELS = {4097, 785, 528}  # protocol 4 printers by their id (niimbluelib)


class FakeNiimbot:
    """Answers like a NIIMBOT printer on the other end of a pseudo terminal,
    as niimbluelib and Thermalith describe it; records every packet.

    - Connect is only answered with the 03 prefix in front.
    - Protocol 4 printers drop the first packet after a print start and
      after a print end (niimbluelib: the B21 PRO does).
    - A check line is answered with 0xC6, then 0xD3.
    - Heartbeats are answered with the printer's state, never with an error.
    - Each label takes two status polls: printing (50 %), then done.
    - An old D11 (a page size of rows only) sends the number of each
      printed label (0xE0) by itself."""

    def __init__(self, model_id: int = 4096):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.model_id = model_id
        self.connect_result = 1
        self.status_data = b""
        self.firmware = niimbot.u16(781)
        self.lid_closed = True
        self.paper = True
        self.battery = 4  # steps of 25 %
        self.feeds = True  # reports the label fed out (protocol 4 may not)
        self.status_length = 10  # of a print status; only 10 bytes carry an error
        self.counts_pages = True  # an old D11 sends 0xE0 for each label
        self.index_first = False  # ... even before its answer to the page end
        self.error = 0  # answered with 0xDB to every request but heartbeats
        self.errors: dict[int, int] = {}  # command -> error code instead of the answer
        self.status_errors: dict[int, int] = {}  # labels printed -> error in the status
        self.silent: set[int] = set()
        self.garbage = b""  # sent before every answer
        self.busy_ends = 0  # PRINT_END answers 0 this many times
        self.refused_starts = 0  # PAGE_START answers 0 this many times
        self.busy_page_ends = 0  # PAGE_END answers 0 this many times
        self.log: list[Packet] = []
        self.raw = bytearray()
        self.copies = 1  # of the page being sent
        self.page_index = False  # the page size had rows only: report 0xE0
        self.queued = 0  # labels sent
        self.printed = 0
        self.halfway = False  # the current label is half printed
        self.dropping = False  # the next packet gets lost
        self.stale = None  # the first status answer, left over from a last job
        self._stop = threading.Event()
        self._reader = Reader()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def v4(self) -> bool:
        return self.model_id in V4_MODELS or (self.model_id == 2320 and self._protocol() == 4)

    def _protocol(self) -> int:
        return niimbot.protocol_version(self.status_data) if self.connect_result == 3 else 0

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

    def commands(self) -> list[int]:
        return [p.command for p in self.log if p.command not in niimbot.ROWS]

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
            self.raw += data
            for packet in self._reader.feed(data):
                self.log.append(packet)
                self._handle(packet)

    def _answer(self, command: int, data: bytes = b"\x01") -> None:
        os.write(self.master, self.garbage + Packet(command, data).to_bytes())

    def _handle(self, packet: Packet) -> None:
        command = packet.command
        if command in niimbot.ROWS or command in self.silent:
            return
        if self.dropping:
            self.dropping = False
            return
        self.dropping = self.v4 and command in (niimbot.PRINT_START, niimbot.PRINT_END)
        code = self.errors.get(command, self.error)
        if code and command != niimbot.HEARTBEAT:
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
        prefixed = Packet(niimbot.CONNECT).to_bytes()
        if prefixed in self.raw:  # without the prefix the printer stays silent
            self._answer(0xC2, bytes([self.connect_result]))

    def _on_a5(self, data):
        self._answer(0xB5, self.status_data)

    def _on_40(self, data):
        kind = data[0]
        value = {8: niimbot.u16(self.model_id), 9: self.firmware}.get(kind, b"\x00")
        self._answer(0x40 + kind, value)

    def _on_dc(self, data):
        inverted = self.model_id in niimbot.INVERTED_LID
        lid = int(self.lid_closed == inverted)  # 0 = closed, the other way when inverted
        paper = 0 if self.paper else 1
        if data[0] == 4:
            self._answer(
                0xD9, bytes([0, 0, self.battery, 30, int(not self.lid_closed), paper, 1, 1, 0])
            )
        elif data[0] == 1:
            self._answer(0xDD, bytes(9) + bytes([lid, self.battery, paper, 1]))
        else:
            self._answer(niimbot.HEARTBEAT_ANSWERS[data[0]], bytes(10))

    def _on_13(self, data):
        self.copies = data[4] << 8 | data[5] if len(data) >= 6 else 1
        self.page_index = len(data) == 2
        self._answer(0x14)

    def _on_15(self, data):
        self.copies = data[0] << 8 | data[1]
        self._answer(0x16)

    def _on_03(self, data):
        if self.refused_starts:
            self.refused_starts -= 1
            self._answer(0x04, b"\x00")
            return
        self._answer(0x04)

    def _on_e3(self, data):
        if self.index_first:
            self._answer(niimbot.PAGE_INDEX, niimbot.u16(1))
        if self.busy_page_ends:
            self.busy_page_ends -= 1
            self._answer(0xE4, b"\x00")
        else:
            self._answer(0xE4)
        if self.log[-2].command != niimbot.PAGE_END:  # not a repeated page end
            self.queued += self.copies
        if self.page_index and self.counts_pages:
            for n in range(self.printed + 1, self.queued + 1):
                self._answer(niimbot.PAGE_INDEX, niimbot.u16(n))
            self.printed = self.queued

    def _on_86(self, data):
        self._answer(0xC6)
        self._answer(0xD3)

    def _on_a3(self, data):
        if self.stale is not None:
            self._answer(0xB3, self.stale)
            self.stale = None
            return
        if self.halfway:
            self.halfway = False
        elif self.printed < self.queued:
            self.printed += 1
            self.halfway = True
        error = self.status_errors.get(self.printed, 0)
        progress = 50 if self.halfway else 100
        fed = 0 if self.halfway or not self.feeds else 100
        body = niimbot.u16(self.printed) + bytes([progress, fed, 0, 0, error, 0, 0, 0])
        self._answer(0xB3, body[: self.status_length])

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
    monkeypatch.setattr(niimbot, "STATUS_TIMEOUT", 0.5)
    yield printer
    printer.close()


def _image(width=320, height=240) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 60, 40), fill="black")
    draw.line((0, height - 1, width - 1, height - 1), fill="black")
    return image


START = [niimbot.CONNECT, niimbot.PRINTER_INFO]


def test_query_a_b1(fake):
    backend = Niimbot()
    info = backend.query("/dev/ttyACM0")
    assert (info.model, info.firmware, info.state, info.error) == ("B1", "7.81", "IDLE", "")
    assert backend.display_name(info) == "NIIMBOT B1"
    assert fake.commands() == [0xC1, 0x40, 0x40, 0xDC]
    assert fake.sent(0x40) == [b"\x08", b"\x09"]
    assert fake.sent(0xDC) == [b"\x01"]  # protocol below 3: heartbeat type 1
    assert backend.raster(50, PrintOptions()).dpi == niimbot.DPI_203


def test_query_survives_garbage_and_unasked_answers(fake):
    fake.garbage = b"\x00\xff" + Packet(0xB3, bytes(4)).to_bytes() + b"\x55\x55\x13"
    info = Niimbot().query("printer")
    assert info.model == "B1" and info.firmware == "7.81"


@pytest.mark.parametrize(
    "model, change, code",
    [
        (4096, {"lid_closed": False}, "cover_open"),
        (512, {"lid_closed": False}, "cover_open"),  # a D11 says it the other way round
        (512, {}, ""),
        (4096, {"paper": False}, "out_of_labels"),
    ],
)
def test_query_reads_the_heartbeat(fake, model, change, code):
    fake.model_id = model
    for name, value in change.items():
        setattr(fake, name, value)
    info = Niimbot().query("printer")
    assert (info.state, info.error) == ("ERROR" if code else "IDLE", code)


def test_query_reports_an_error_answer_as_state(fake):
    fake.errors = {0xC1: 2}  # already when connecting: the model is the one asked before
    backend = Niimbot()
    info = backend.query("printer")
    assert (info.model, info.state, info.error) == ("", "ERROR", "out_of_labels")
    assert backend.display_name(info) == backend.name


def test_query_without_firmware_heartbeat_or_connect(fake):
    fake.silent = {0xC1, 0xDC}
    fake.firmware = b""
    info = Niimbot().query("printer")
    assert info.model == "B1" and info.firmware == "" and info.error == ""
    fake.silent = {0x40}
    with pytest.raises(PrinterTimeout):
        Niimbot().query("printer")


def test_connect_needs_its_prefix(fake, monkeypatch):
    plain = Packet(niimbot.CONNECT).to_bytes()[1:]
    monkeypatch.setattr(Packet, "to_bytes", lambda self: plain)  # as if sent bare
    with niimbot.session("printer") as s:
        assert s.optional(niimbot.CONNECT) is None  # the printer stays silent


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


def test_query_a_d110_m_with_protocol_4(fake):
    fake.model_id = 2320  # D110 M
    fake.connect_result = 3
    fake.status_data = bytes(11) + bytes([3, 0])  # 300: protocol 4
    fake.battery = 8  # percent
    backend = Niimbot()
    info = backend.query("printer")
    assert info.model == "D110 M" and info.error == "battery"
    assert fake.commands()[:3] == [0xC1, 0xA5, 0x40]
    assert fake.sent(0xDC) == [b"\x04"]  # protocol 3 and up: heartbeat type 4
    assert backend.density(info) == Density(1, 5, 3)
    assert backend.media_types(info) == ("gap", "transparent")


def test_print_on_a_b1(fake):
    events = []
    image = _image(384, 240)  # 48 × 30 mm at 8 dots/mm
    image.info["niimbot_dpi"] = niimbot.DPI_203
    Niimbot().print_label(image, "printer", PrintOptions(copies=2, density=4), events.append)
    assert fake.commands() == START + [0x21, 0x23, 0x01, 0x03, 0x13, 0xE3] + [0xA3] * 4 + [0xF3]
    assert fake.sent(0x21) == [b"\x04"] and fake.sent(0x23) == [b"\x01"]
    assert fake.sent(0x01) == [b"\x00\x02\x00\x00\x00\x00\x00"]
    assert fake.sent(0x13) == [niimbot.u16(240) + niimbot.u16(384) + niimbot.u16(2)]
    rows = [p for p in fake.log if p.command in niimbot.ROWS]
    assert rows[0].command == niimbot.ROW_EMPTY and rows[0].data == b"\x00\x00\x0a"
    assert rows[-1].data[:2] == niimbot.u16(239)  # the last row
    assert fake.printed == 2
    assert [(e.copy, e.copies) for e in events] == [(1, 2), (2, 2), (2, 2)]


def test_a_narrow_label_is_centred_on_the_head(fake):
    image = Image.new("RGB", (320, 2), "white")  # 40 mm on the 48 mm head of a B1
    image.putpixel((0, 0), (0, 0, 0))
    Niimbot().print_label(image, "printer", PrintOptions())
    assert fake.sent(0x13)[0][2:4] == niimbot.u16(384)  # the head's width
    row = [p for p in fake.log if p.command == niimbot.ROW_INDEXED][0]
    assert row.data[-2:] == niimbot.u16(32)  # 32 px of white on the left


def test_print_on_a_d110(fake):
    fake.model_id = 2304
    image = _image(96, 320)  # 12 × 40 mm
    Niimbot().print_label(image, "printer", PrintOptions(copies=3, density=5, media="continuous"))
    page = [0x20, 0x03, 0x13, 0x15, 0xE3]
    assert fake.commands() == START + [0x21, 0x23, 0x01] + page + [0xA3] * 6 + [0xF3]
    assert fake.sent(0x21) == [b"\x03"]  # D110: density 1 to 3
    assert fake.sent(0x23) == [b"\x01"]  # no continuous rolls: gaps
    assert fake.sent(0x01) == [b"\x01"]
    assert fake.sent(0x13) == [niimbot.u16(320) + niimbot.u16(96)]
    assert fake.sent(0x15) == [b"\x00\x03"]
    assert fake.printed == 3


def test_print_on_an_old_d11(fake):
    fake.model_id = 512
    events = []
    Niimbot().print_label(_image(96, 160), "printer", PrintOptions(copies=2), events.append)
    page = [0x20, 0x03, 0x13, 0x15, 0xE3]
    assert fake.commands() == START + [0x21, 0x23, 0x01] + page + [0xF3]  # no status polls
    assert fake.sent(0x13) == [niimbot.u16(160)]  # rows only
    assert fake.printed == 2 and {e.copy for e in events} == {1, 2}


def test_an_old_d11_that_counts_before_it_answers(fake):
    fake.model_id = 512
    fake.index_first = True
    Niimbot().print_label(_image(96, 160), "printer", PrintOptions())
    assert fake.commands()[-1] == niimbot.PRINT_END


def test_a_d11_on_protocol_1_takes_the_d110_sequence(fake):
    fake.model_id = 512
    fake.connect_result = 2  # connected, protocol 1
    Niimbot().print_label(_image(96, 160), "printer", PrintOptions())
    assert fake.sent(0x13) == [niimbot.u16(160) + niimbot.u16(96)]
    assert niimbot.PRINT_STATUS in fake.commands()


def test_an_old_d11_that_stops_counting(fake):
    fake.model_id = 512
    fake.counts_pages = False
    with pytest.raises(PrinterTimeout):
        Niimbot().print_label(_image(96, 160), "printer", PrintOptions())


def test_print_on_a_b21(fake):
    fake.model_id = 768
    fake.busy_ends = 2
    events = []
    image = _image(384, 450)
    Niimbot().print_label(
        image, "printer", PrintOptions(copies=2, media="black_mark"), events.append
    )
    page = [0x03, 0x13, 0x86, 0x86, 0xE3]  # check lines after rows 199 and 399
    assert fake.commands() == START + [0x21, 0x23, 0x01] + page * 2 + [0xF3] * 3
    assert fake.sent(0x23) == [b"\x02"]
    assert fake.sent(0x13) == [niimbot.u16(450) + niimbot.u16(384)] * 2
    lines = fake.sent(niimbot.CHECK_LINE)
    assert lines == [niimbot.u16(199) + b"\x01", niimbot.u16(399) + b"\x01"] * 2
    bitmap = [p for p in fake.log if p.command == niimbot.ROW_BITMAP][0]
    assert bitmap.data[2] == 0  # counts as a total
    assert fake.printed == 2 and {e.copy for e in events} == {1, 2}


def test_print_on_a_b21_l2b(fake):
    fake.model_id = 769
    fake.refused_starts = 1
    fake.busy_page_ends = 3
    Niimbot().print_label(_image(384, 100), "printer", PrintOptions(copies=2))
    first = [0x03, 0x03, 0x13, 0xE3]  # the page start is tried again
    assert fake.commands() == START + [0x21, 0x23, 0x01] + first + [0x03, 0x13, 0xE3] + [
        0xE3,
        0xE3,
        0xF3,
    ]
    assert fake.printed == 2


def test_a_b21_l2b_tries_again_after_timeouts_and_errors(fake, monkeypatch):
    fake.model_id = 769
    answers = iter([None, 9, 0])  # silent, "busy", then accepted

    def page_start(data):
        what = next(answers, 0)
        if what == 9:
            fake._answer(niimbot.ERROR, b"\x09")
        elif what == 0:
            fake._answer(0x04)

    monkeypatch.setattr(fake, "_on_03", page_start)
    Niimbot().print_label(_image(384, 100), "printer", PrintOptions())
    assert fake.commands().count(niimbot.PAGE_START) == 3 and fake.printed == 1


def test_a_b21_l2b_that_stays_busy(fake, monkeypatch):
    fake.model_id = 769
    monkeypatch.setattr(fake, "_on_03", lambda data: fake._answer(niimbot.ERROR, b"\x09"))
    with pytest.raises(PrinterBusy, match="error 9"):
        Niimbot().print_label(_image(384, 100), "printer", PrintOptions())


def test_a_b21_l2b_that_keeps_refusing(fake):
    fake.model_id = 769
    fake.refused_starts = 10
    with pytest.raises(PrinterBusy, match="refused 0x03 5 times"):
        Niimbot().print_label(_image(384, 100), "printer", PrintOptions())


def test_print_on_an_h1s(fake):
    fake.model_id = 4352
    Niimbot().print_label(_image(96, 200), "printer", PrintOptions(copies=2))
    assert fake.commands() == START + [0x21, 0x23, 0x01, 0x03, 0x13, 0xE3] + [0xA3] * 4 + [0xF3]
    assert fake.sent(0x01) == [b"\x00\x02"]
    assert fake.sent(0x13) == [niimbot.u16(200) + niimbot.u16(96) + b"\x00\x02" + bytes(3)]
    assert fake.printed == 2


def test_a_stale_status_does_not_end_a_job(fake):
    fake.stale = niimbot.u16(1) + bytes([100, 100, 0, 0, 0, 0, 0, 0])  # the last job's
    Niimbot().print_label(_image(), "printer", PrintOptions())
    assert fake.printed == 1
    assert fake.commands().count(niimbot.PRINT_STATUS) == 3  # stale, printing, done


def test_a_finished_status_counts_on_the_second_poll(fake, monkeypatch):
    done = niimbot.u16(1) + bytes([100, 100, 0, 0, 0, 0, 0, 0])
    # a fast printer: the label is out before the first poll
    monkeypatch.setattr(fake, "_on_a3", lambda data: fake._answer(0xB3, done))
    Niimbot().print_label(_image(), "printer", PrintOptions())
    assert fake.commands().count(niimbot.PRINT_STATUS) == 2


def test_a_stale_status_counts_no_labels(fake, monkeypatch):
    """A status left over from a job of 5 labels, then no paper at once."""
    answers = iter([niimbot.u16(5) + bytes([100, 100, 0, 0, 0, 0, 0, 0])])
    no_paper = niimbot.u16(0) + bytes([10, 0, 0, 0, 2, 0, 0, 0])
    monkeypatch.setattr(fake, "_on_a3", lambda data: fake._answer(0xB3, next(answers, no_paper)))
    events = []
    with pytest.raises(OutOfLabels) as caught:
        Niimbot().print_label(_image(), "printer", PrintOptions(copies=3), events.append)
    assert "printed" not in caught.value.params
    assert [(e.copy, e.copies) for e in events] == [(1, 3)]


def test_a_finished_status_that_keeps_changing_still_ends(fake, monkeypatch):
    answers = iter(
        [
            niimbot.u16(1) + bytes([100, 100, 0, 0, 0, 0, 0, 0]),
            niimbot.u16(2) + bytes([100, 100, 0, 0, 0, 0, 0, 0]),  # counts on
        ]
    )
    monkeypatch.setattr(fake, "_on_a3", lambda data: fake._answer(0xB3, next(answers)))
    events = []
    Niimbot().print_label(_image(), "printer", PrintOptions(), events.append)
    assert fake.commands().count(niimbot.PRINT_STATUS) == 2
    assert [(e.copy, e.copies) for e in events] == [(1, 1), (1, 1)]


def test_print_on_a_b1_pro_with_protocol_4(fake):
    fake.model_id = 4097
    fake.feeds = False  # finished on the page count and print progress alone
    image = _image(567, 300)
    image.info["niimbot_dpi"] = niimbot.DPI_300
    backend = Niimbot()
    backend.print_label(image, "printer", PrintOptions(density=2, media="transparent"))
    fake.wait_for(lambda: 0xDC in fake.commands())  # sent last, without an answer
    after_start = [0xA3, 0x13, 0xE3, 0xA3, 0xA3, 0xF3, 0xDC]  # no page start
    assert fake.commands() == START + [0x23, 0x21, 0x01] + after_start
    assert fake.sent(0x23) == [b"\x05"]
    assert fake.sent(0x01) == [b"\x00\x01" + bytes(5) + b"\x01\x00"]
    assert fake.sent(0x13) == [niimbot.u16(300) + niimbot.u16(567) + niimbot.u16(1) + bytes(7)]
    assert fake.sent(0xDC) == [b"\x01"]
    assert backend.raster(50, PrintOptions()).dpi == niimbot.DPI_300


def test_a_d110_m_on_protocol_4(fake):
    fake.model_id = 2320
    fake.connect_result = 3
    fake.status_data = bytes(11) + bytes([3, 0])
    Niimbot().print_label(_image(96, 100), "printer", PrintOptions())
    assert niimbot.PAGE_START not in fake.commands()
    assert len(fake.sent(0x13)[0]) == 13


def test_print_rescales_an_image_made_for_another_resolution(fake):
    fake.model_id = 4097  # 300 dpi
    fake.feeds = False
    image = _image(400, 240)  # 50 × 30 mm at 8 dots/mm
    Niimbot().print_label(image, "printer", PrintOptions())
    assert fake.sent(0x13)[0][:4] == niimbot.u16(354) + niimbot.u16(567)  # 30 mm, the head


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
    fake.status_errors = {1: 2}  # no paper while the first label prints
    with pytest.raises(OutOfLabels) as caught:
        Niimbot().print_label(_image(), "printer", PrintOptions(copies=3))
    assert caught.value.params == {"reported": 2, "printed": 1, "copies": 3}
    assert fake.commands()[-1] == niimbot.CANCEL


def test_an_error_byte_needs_the_long_status(fake):
    """Only a status of 10 bytes has an error byte (8 bytes: none)."""
    fake.status_errors = {1: 2}
    fake.status_length = 8
    Niimbot().print_label(_image(), "printer", PrintOptions())
    assert fake.printed == 1


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


def test_a_late_answer_to_the_end_is_no_failure(fake):
    fake.silent = {0xF3}
    Niimbot().print_label(_image(), "printer", PrintOptions())
    assert fake.printed == 1 and niimbot.CANCEL not in fake.commands()


def test_a_printer_that_stops_answering_times_out(fake):
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
    assert not caps.continuous and caps.max_width_mm == 108
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
