"""Brother P-touch: Brother's raster commands, checked byte for byte and
against a fake printer that reads them back (see docs/ptouch.md). No test
talks to a real printer or searches the network; the fake answers in
memory, on a local TCP socket or on a pseudo terminal standing in for
/dev/usb/lp0."""

from __future__ import annotations

import os
import select
import socket
import threading
import time
import tty
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from etiketto.backends import ptouch, usb
from etiketto.backends.base import (
    CassetteInfo,
    FoundPrinter,
    PrinterError,
    PrinterInfo,
    PrinterTimeout,
    PrintOptions,
    Progress,
)
from etiketto.backends.ptouch import (
    HSE_128,
    HSE_560,
    MODELS,
    TZE_128,
    TZE_560,
    JobSettings,
    PTouch,
    build_job,
    documented_page,
    model_by_name,
    model_by_usb_id,
    packbits,
    parse_status,
    print_area,
    status_error,
)
from etiketto.model import Label, TextElement

REAL_CONNECT = ptouch._connect  # before conftest's guard replaces it
ESC = b"\x1b"


def unpackbits(data: bytes) -> bytes:
    """TIFF PackBits, decoded: what the printer does with a compressed line."""
    out = bytearray()
    i = 0
    while i < len(data):
        n = data[i]
        i += 1
        if n < 128:
            out += data[i : i + n + 1]
            i += n + 1
        elif n > 128:
            out += bytes([data[i]]) * (257 - n)
            i += 1
    return bytes(out)


def status_frame(
    width=12, media=0x01, err1=0, err2=0, kind=0, phase=0, code=0x68, tape=0x01, text=0x08
) -> bytes:
    frame = bytearray(32)
    frame[0:4] = b"\x80\x20B0"
    frame[4], frame[5] = code, ord("0")
    frame[8], frame[9], frame[10], frame[11] = err1, err2, width, media
    frame[18], frame[19] = kind, phase
    frame[24], frame[25] = tape, text
    return bytes(frame)


def model(name: str) -> ptouch.Model:
    found = model_by_name(name)
    assert found is not None and found.name == name
    return found


# --- a fake P-touch, reading the command stream back -------------------------------------


class FakePTouch:
    """Parses what Etiketto sends like a printer would and records each
    printed page; answers ESC i S, and after each page (if `completes`)."""

    def __init__(self, line_bytes=16, code=0x68, width=12, media=0x01, completes=True):
        self.line_bytes = line_bytes
        self.code, self.width, self.media = code, width, media
        self.completes = completes
        self.answers_raster = False  # a status after ESC i a, as P700-type models do
        self.errors = (0, 0)
        self.fail_after: int | None = None  # pages, then a cover-open error
        self.buffer = bytearray()
        self.out = bytearray()
        self.pages: list[dict] = []
        self.settings: dict = {}
        self.lines: list[bytes] = []
        self.inits = 0
        self.close_after: int | None = None  # pages, then the connection closes
        self.closed = False
        self.unread_at_page: list[int] = []  # answer bytes nobody had read at ESC i a

    def status(self, kind=0) -> bytes:
        err1, err2 = self.errors
        return status_frame(self.width, self.media, err1, err2, kind, code=self.code)

    def feed(self, data: bytes) -> None:
        self.buffer += data
        while self.buffer:
            used = self._command(bytes(self.buffer[:4096]))
            if not used:
                return  # the rest of the command is still on its way
            del self.buffer[:used]

    def _command(self, b: bytes) -> int:
        if b[0] == 0:
            return len(b) - len(b.lstrip(b"\x00"))
        if b[0] == 0x1B:
            return self._escape(b)
        if b[0] == ord("M"):
            if len(b) < 2:
                return 0
            self.settings["compression"] = b[1]
            return 2
        if b[0] == ord("G"):
            return self._line(b)
        if b[0] == ord("Z"):
            self.lines.append(bytes(self.line_bytes))
            return 1
        if b[0] in (0x0C, 0x1A):
            self._page(b[0])
            return 1
        raise AssertionError("unexpected byte %r" % b[:8])

    def _escape(self, b: bytes) -> int:
        if len(b) < 2:
            return 0
        if b[1] == ord("@"):
            self.inits += 1
            self.settings = {}
            return 2
        assert b[1] == ord("i"), b[:4]
        if len(b) < 3:
            return 0
        code = chr(b[2])
        size = {"S": 3, "a": 4, "!": 4, "R": 4, "M": 4, "A": 4, "K": 4, "d": 5, "z": 13}[code]
        if len(b) < size:
            return 0
        if code == "S":
            self.out += self.status()
        elif code == "a":
            self.unread_at_page.append(len(self.out))
        if code == "a" and self.answers_raster:
            self.out += self.status(kind=0x06)
        self.settings[code] = b[3:size]
        return size

    def _line(self, b: bytes) -> int:
        if len(b) < 3:
            return 0
        size = b[1] | b[2] << 8
        if len(b) < 3 + size:
            return 0
        payload = b[3 : 3 + size]
        line = unpackbits(payload) if self.settings.get("compression") == 2 else payload
        assert len(line) == self.line_bytes, (len(line), self.line_bytes)
        self.lines.append(line)
        return 3 + size

    def _page(self, end: int) -> None:
        self.pages.append(dict(self.settings, lines=self.lines, end=end))
        self.lines = []
        if self.close_after is not None and len(self.pages) >= self.close_after:
            self.closed = True  # hangs up instead of reporting the page
        elif self.fail_after is not None and len(self.pages) >= self.fail_after:
            self.errors = (0, 0x10)
            self.out += self.status(kind=0x02)
        elif self.completes:
            self.out += self.status(kind=0x01)


class FakeLink:
    """A link to the fake printer, in memory; silent means a timeout."""

    def __init__(self, printer: FakePTouch, chunk: int = 7):
        self.printer = printer
        self.chunk = chunk
        self.timeouts: list[float] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.printer.feed(data)

    def recv(self, size: int) -> bytes:
        out = self.printer.out
        if not out and self.printer.closed:
            return b""  # like a socket the other side closed
        if not out:  # like _TcpLink and usb.Connection
            raise PrinterTimeout("printer did not answer")
        piece = bytes(out[: min(size, self.chunk)])
        del out[: len(piece)]
        return piece

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    """A PT-P750W behind ptouch._connect."""
    printer = FakePTouch()
    links: list[FakeLink] = []

    def connect(host, port, wait=usb.BUSY_WAIT):
        links.append(FakeLink(printer))
        return links[-1]

    monkeypatch.setattr(ptouch, "_connect", connect)
    monkeypatch.setattr(ptouch, "_browse", lambda service_type, match, timeout: [])
    return SimpleNamespace(printer=printer, links=links)


def label(tape=12, text="AB") -> Label:
    return Label(tape_mm=tape, printer="ptouch", elements=[TextElement(text=text)])


def strip(*columns: list[int], height: int = 70) -> Image.Image:
    """An image of len(columns) columns, black at the given rows."""
    image = Image.new("RGB", (len(columns), height), "white")
    for x, rows in enumerate(columns):
        for y in rows:
            image.putpixel((x, y), (0, 0, 0))
    return image


# --- the contract ------------------------------------------------------------------------


class TestContract(BackendContract):
    backend_id = "ptouch"

    @pytest.fixture
    def harness(self, fake):
        return SimpleNamespace(
            backend=PTouch(),
            fail=lambda: setattr(fake.printer, "errors", (0, 0x10)),
            printed=lambda: len(fake.printer.pages),
        )


# --- PackBits ----------------------------------------------------------------------------


def test_packbits_known_answers():
    assert packbits(bytes(16)) == b"\xf1\x00"
    assert packbits(b"") == b""
    assert packbits(b"\x07") == b"\x00\x07"
    # Apple's example from the TIFF specification (Technical Note TN1023)
    raw = bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22AAAAAAAAAAAAAAAAAAAA")
    assert packbits(raw) == bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA")
    # at most 128 bytes per run or literal piece
    assert packbits(bytes(130)) == b"\x81\x00\xff\x00"
    assert packbits(bytes(range(130)))[0] == 127 and packbits(bytes(range(130)))[129] == 1


@given(st.binary(max_size=600))
def test_packbits_roundtrip(data):
    packed = packbits(data)
    assert unpackbits(packed) == data
    assert len(packed) <= len(data) + (len(data) + 127) // 128  # never much longer


@given(st.lists(st.sampled_from([0, 0xFF, 0x0F]), max_size=70))
def test_packbits_roundtrip_runs(values):
    data = bytes(values)
    assert unpackbits(packbits(data)) == data


# --- pins and tapes: Brother's tables ------------------------------------------------------

# Brother's raster references, 2.3.5 "Raster line": (tape, pins for left
# margin, print area pins, pins for right margin); the right margin is at the
# first byte of a raster line
BROTHER_TZE_128 = [
    (3.5, 52, 24, 52),
    (6, 48, 32, 48),
    (9, 39, 50, 39),
    (12, 29, 70, 29),
    (18, 8, 112, 8),
    (24, 0, 128, 0),
]
BROTHER_TZE_560 = [
    (3.5, 248, 48, 264),
    (6, 240, 64, 256),
    (9, 219, 106, 235),
    (12, 197, 150, 213),
    (18, 155, 234, 171),
    (24, 112, 320, 128),
    (36, 45, 454, 61),
]
# heat-shrink tube: (media, width in the status, left, print, right)
BROTHER_HSE_128 = [
    (0x11, 6, 50, 28, 50),
    (0x11, 9, 40, 48, 40),
    (0x11, 12, 31, 66, 31),
    (0x11, 18, 11, 106, 11),
    (0x11, 24, 0, 128, 0),
    (0x17, 5, 54, 20, 54),
    (0x17, 9, 42, 44, 42),
    (0x17, 11, 39, 50, 39),
    (0x17, 21, 4, 120, 4),
]
BROTHER_HSE_560 = [
    (0x11, 6, 244, 56, 260),
    (0x11, 9, 224, 96, 240),
    (0x11, 12, 206, 132, 222),
    (0x11, 18, 166, 212, 182),
    (0x11, 24, 144, 256, 160),
    (0x17, 5, 252, 40, 268),
    (0x17, 9, 228, 88, 244),
    (0x17, 11, 222, 100, 238),
    (0x17, 21, 152, 240, 168),
    (0x17, 31, 92, 360, 108),
]


@pytest.mark.parametrize(
    "table, head, brother", [(TZE_128, 128, BROTHER_TZE_128), (TZE_560, 560, BROTHER_TZE_560)]
)
def test_tze_print_area_is_brothers(table, head, brother):
    assert set(table) == {row[0] for row in brother}
    for tape, left, pins, right in brother:
        assert left + pins + right == head
        assert table[tape] == (right, pins)  # counted from the first byte


@pytest.mark.parametrize(
    "table, head, brother", [(HSE_128, 128, BROTHER_HSE_128), (HSE_560, 560, BROTHER_HSE_560)]
)
def test_heat_shrink_print_area(table, head, brother):
    assert set(table) == {(m, w) for m, w, *_ in brother}
    for media, width, left, pins, right in brother:
        assert left + pins + right == head
        before, printable = table[(media, width)]
        assert printable == pins
        # ptouch's position, corrected on printers: 1 or 2 pins off Brother's
        assert 0 <= before - right <= 2 and before + pins <= head


def test_print_area_per_head():
    p750w, p900w = model("PT-P750W"), model("PT-P900W")
    assert print_area(p750w, 12) == (29, 70)
    assert print_area(p750w, 3.5) == (52, 24)
    assert print_area(p750w, 36) is None  # no 36 mm tape in a 128-pin printer
    assert print_area(p900w, 36) == (61, 454)
    assert print_area(p900w, 24) == (128, 320)
    assert print_area(p750w, 12, 0x11) == (33, 66)  # heat-shrink tube 2:1
    assert print_area(p750w, 11, 0x17) == (41, 50)
    assert print_area(p750w, 31, 0x17) is None
    assert print_area(p900w, 31, 0x17) == (109, 360)
    # no heat-shrink tube on the PT-P710BT and PT-P910BT
    assert print_area(model("PT-P710BT"), 12, 0x11) is None
    assert print_area(model("PT-P910BT"), 12, 0x11) is None
    # other heads: Brother's width at their resolution, centred, at most the head
    assert print_area(model("PT-2300"), 12) == (21, 70)
    assert print_area(model("PT-2300"), 24) == (0, 112)
    assert print_area(model("PT-9700PC"), 12) == (117, 150)
    assert print_area(model("PT-9700PC"), 36) == (0, 384)
    assert print_area(model("PT-2300"), 36) is None


def test_columns_put_image_rows_on_pins():
    p750w = model("PT-P750W")
    lines = ptouch.columns(strip([], [0], [69, 1]), p750w, 29)
    assert lines[0] == bytes(16)
    assert lines[1] == bytes(3) + b"\x04" + bytes(12)  # pin 29: byte 3, bit 2
    # pin 30 and pin 98 (byte 12, bit 5)
    assert lines[2] == bytes(3) + b"\x02" + bytes(8) + b"\x20" + bytes(3)


def test_columns_on_the_p900_head():
    """Brother: the right margin (213 pins on 12 mm) is at the first byte,
    the left margin (197 pins) at the last."""
    p900w = model("PT-P900W")
    full = Image.new("L", (1, 150), 0)  # all ink
    (line,) = ptouch.columns(full, p900w, *print_area(p900w, 12)[:1])
    bits = "".join(f"{b:08b}" for b in line)
    assert len(bits) == 560
    assert bits.index("1") == 213 and bits.rindex("1") == 560 - 197 - 1


def test_columns_threshold_grey():
    image = Image.new("L", (3, 70), 255)
    image.putpixel((0, 0), 127)  # dark enough
    image.putpixel((1, 0), 128)  # too light
    lines = ptouch.columns(image, model("PT-P750W"), 29)
    assert lines[0][3] == 0x04 and not any(lines[1])


def test_quirk_padding():
    # PT-2730: 48 blank columns first [assumed]; PT-1230PC: no quirk applied
    lines = ptouch.columns(strip([0]), model("PT-2730"), 29)
    assert len(lines) == 49 and not any(b"".join(lines[:48])) and any(lines[48])
    assert len(ptouch.columns(strip([0]), model("PT-1230PC"), 29)) == 1


# --- the job, byte for byte --------------------------------------------------------------

SETTINGS = JobSettings(cut="full", chain=False, high_res=False, feed_dots=14, width=12, media=1)
LINE_29 = bytes(3) + b"\x04" + bytes(12)  # pin 29 of 128


def two_lines(left=29, line_bytes=16) -> list[bytes]:
    """An empty column, then one with the first printable pin."""
    marked = bytearray(line_bytes)
    marked[left // 8] = 0x80 >> (left % 8)
    return [bytes(line_bytes), bytes(marked)]


def job(*args) -> bytes:
    return b"".join(build_job(*args))


def test_brothers_examples():
    """The command examples of Brother's references, byte for byte."""
    # print information for 100 mm on 24 mm tape: 0x84, n2 0, n3 24, 0x2AA lines
    settings = JobSettings(width=24, media=1)
    page = documented_page(model("PT-P750W"), [bytes(16)] * 0x2AA, settings)
    assert page.startswith(
        ESC + b"ia\x01" + bytes.fromhex("1b697a 84 00 18 00 aa 02 00 00 00 00".replace(" ", ""))
    )
    # PT-P900 series: half cut, no chain printing; 1 mm margin at 360 dpi
    settings = JobSettings(cut="half", width=24, media=1, feed_dots=14)
    page = documented_page(model("PT-P900W"), [bytes(70)], settings)
    assert ESC + b"iK\x0c" in page and ESC + b"id\x0e\x00" in page
    assert (
        ptouch.job_settings(
            model("PT-P900W"), PrintOptions(feed_mm=1), parse_status(status_frame(width=24))
        ).feed_dots
        == 14
    )
    # PackBits: Brother's example (2.2.3 compression)
    raw = bytes(20) + bytes.fromhex("222223babfa2222b")
    assert packbits(raw) == bytes.fromhex("ed00ff2205 23babfa2222b".replace(" ", ""))


def test_golden_documented_128():
    """PT-P750W: Brother's sequence with PackBits and Z for empty lines."""
    assert job(model("PT-P750W"), two_lines(), SETTINGS, 1) == (
        bytes(100)
        + ESC
        + b"@"
        + ESC
        + b"ia\x01"
        + ESC
        + b"iz\x84\x00\x0c\x00\x02\x00\x00\x00\x00\x00"
        + ESC
        + b"iM\x40"
        + ESC
        + b"iA\x01"
        + ESC
        + b"iK\x08"
        + ESC
        + b"id\x0e\x00"
        + b"M\x02"
        + b"Z"
        + b"G\x06\x00\xfe\x00\x00\x04\xf5\x00"
        + b"\x1a"
    )


def test_golden_documented_two_pages_half_cut_chain_high_res_tube():
    settings = JobSettings(
        cut="half", chain=True, high_res=True, feed_dots=28, width=9, media=0x11
    )
    pieces = build_job(model("PT-E550W"), [LINE_29], settings, 2)
    page = ESC + b"iM\x00" + ESC + b"iK\x44" + ESC + b"id\x1c\x00" + b"M\x02" + b"G\x06\x00"
    page += b"\xfe\x00\x00\x04\xf5\x00"
    info = ESC + b"ia\x01" + ESC + b"iz\x86\x11\x09\x00\x01\x00\x00\x00"
    assert pieces == [
        bytes(100) + ESC + b"@",
        info + b"\x00\x00" + page + b"\x0c",
        info + b"\x01\x00" + page + b"\x1a",
    ]


def test_golden_documented_560_uncompressed():
    """PT-P900W: 70 bytes per line, no compression, empty lines in full,
    n9 = 2 on a single page."""
    settings = JobSettings(cut="none", feed_dots=14, width=24, media=1)
    marked = bytearray(70)
    marked[16] = 0x80  # pin 128, Brother's right margin on 24 mm
    assert job(model("PT-P900W"), two_lines(128, 70), settings, 1) == (
        bytes(200)  # the PT-P900 series: 200 bytes invalidate (2.1)
        + ESC
        + b"@"
        + ESC
        + b"ia\x01"
        + ESC
        + b"iz\x84\x00\x18\x00\x02\x00\x00\x00\x02\x00"
        + ESC
        + b"iM\x00"
        + ESC
        + b"iK\x08"
        + ESC
        + b"id\x0e\x00"
        + b"M\x00"
        + b"G\x46\x00"
        + bytes(70)
        + b"G\x46\x00"
        + bytes(marked)
        + b"\x1a"
    )


def test_p900_page_numbers_and_high_res():
    """n9: 0, 1, ..., 2 on the last page; high resolution on laminated tape
    with n2 = 09h."""
    settings = JobSettings(cut="full", high_res=True, feed_dots=28, width=12, media=1)
    pieces = build_job(model("PT-P900"), [bytes(70)], settings, 3)
    infos = [p[p.index(ESC + b"iz") + 3 : p.index(ESC + b"iz") + 13] for p in pieces[1:]]
    assert [i[1] for i in infos] == [0x09] * 3 and [i[8] for i in infos] == [0, 1, 2]
    assert all(ESC + b"iK\x48" in p for p in pieces[1:])


def test_p910bt_and_p710bt_get_status_notifications():
    """ESC i ! 00 on every page, right after ESC i a (PT-P910BT, PT-P710BT);
    no ESC i A on the PT-P710BT."""
    for name, size in (("PT-P910BT", 70), ("PT-P710BT", 16)):
        pieces = build_job(model(name), [bytes(size)], SETTINGS, 2)
        assert all(
            p.startswith(ESC + b"ia\x01" + ESC + b"i!\x00" + ESC + b"iz") for p in pieces[1:]
        )
    assert ESC + b"iA" not in job(model("PT-P710BT"), [bytes(16)], SETTINGS, 1)
    assert ESC + b"iA\x01" in job(model("PT-P750W"), [bytes(16)], SETTINGS, 1)
    assert ESC + b"i!" not in job(model("PT-P750W"), [bytes(16)], SETTINGS, 1)


def test_golden_legacy_packbits():
    """PT-2420PC: M 02 before ESC i R, each line one literal piece."""
    assert job(model("PT-2420PC"), two_lines(), SETTINGS, 1) == (
        bytes(100)
        + ESC
        + b"@"
        + b"M\x02"
        + ESC
        + b"iR\x01"
        + b"G\x11\x00\x0f"
        + bytes(16)
        + b"G\x11\x00\x0f"
        + LINE_29
        + b"\x1a"
    )


def test_golden_legacy_copies_are_one_job():
    """PT-2430PC: initialised once, 0C between the copies, 1A at the end."""
    page = ESC + b"iM\x40" + b"G\x10\x00" + LINE_29
    assert build_job(model("PT-2430PC"), [LINE_29], SETTINGS, 2) == [
        bytes(100) + ESC + b"@" + ESC + b"iR\x01",
        page + b"\x0c",
        page + b"\x1a",
    ]
    # without cut no ESC i M; chain printing: 0C at the end too
    pieces = build_job(model("PT-2430PC"), [LINE_29], JobSettings(cut="none"), 2)
    assert [p[-1:] for p in pieces[1:]] == [b"\x0c", b"\x1a"] and ESC + b"iM" not in pieces[1]
    pieces = build_job(model("PT-2430PC"), [LINE_29], JobSettings(cut="none", chain=True), 2)
    assert [p[-1:] for p in pieces[1:]] == [b"\x0c", b"\x0c"]


def test_golden_p700():
    """PT-P700: ESC i a instead of ESC i R, PackBits, cutter."""
    assert job(model("PT-P700"), [LINE_29], SETTINGS, 1) == (
        bytes(100)
        + ESC
        + b"@"
        + b"M\x02"
        + ESC
        + b"ia\x01"
        + ESC
        + b"iM\x40"
        + b"G\x11\x00\x0f"
        + LINE_29
        + b"\x1a"
    )


def test_golden_d460bt():
    """PT-D460BT: print information with n9 = 2, the fixed margin bytes,
    chain printing via ESC i K, 1A after every label."""
    settings = JobSettings(cut="full", chain=True, width=12, media=1)
    page = (
        ESC
        + b"iz\x00\x00\x0c\x00\x01\x00\x00\x00\x02\x00"
        + ESC
        + b"id\x01\x00M\x00"
        + ESC
        + b"iM\x40"
        + ESC
        + b"iK\x00"
        + b"G\x10\x00"
        + LINE_29
        + b"\x1a"
    )
    assert build_job(model("PT-D460BT"), [LINE_29], settings, 2) == [
        bytes(100) + ESC + b"@" + ESC + b"ia\x01",
        page,
        page,
    ]


def test_golden_info_command():
    """PT-D450: ESC i z without valid flags, n9 = 0; no cutter."""
    assert job(model("PT-D450"), [LINE_29], SETTINGS, 1) == (
        bytes(100)
        + ESC
        + b"@"
        + ESC
        + b"iR\x01"
        + ESC
        + b"iz\x00\x00\x0c\x00\x01\x00\x00\x00\x00\x00"
        + b"G\x10\x00"
        + LINE_29
        + b"\x1a"
    )


def test_every_model_job_reads_back():
    """Each model's job, as the fake printer parses it: one page per copy."""
    for m in MODELS:
        printer = FakePTouch(line_bytes=m.line_bytes)
        left, pins = print_area(m, 12) or (0, m.pins)
        lines = ptouch.columns(strip([0], [], [pins - 1], height=pins), m, left)
        printer.feed(job(m, lines, JobSettings(cut="none"), 3))
        assert not printer.buffer, m.name
        assert len(printer.pages) == 3 and printer.inits == 1, m.name
        first = printer.pages[0]["lines"]
        assert first[m.lead_px] == lines[m.lead_px], m.name
        assert len(first) == len(lines)


# --- settings for the model --------------------------------------------------------------


def info_for(name, tape=12, media="laminated"):
    return PrinterInfo(name, tape, None, None, "IDLE", cassette=CassetteInfo(media=media))


def test_cut_modes_and_extras_per_model():
    backend = PTouch()
    assert backend.cut_modes(None) == ("full", "half", "none")
    assert backend.cut_modes(info_for("PT-P750W")) == ("full", "half", "none")
    assert backend.cut_modes(info_for("PT-P710BT")) == ("full", "none")  # no half cut
    assert backend.cut_modes(info_for("PT-P700")) == ("full", "none")
    assert backend.cut_modes(info_for("PT-2420PC")) == ()  # a cutter lever only
    assert backend.extras(None) == ("chain", "high_res", "feed")
    assert backend.extras(info_for("PT-P950NW")) == ("chain", "high_res", "feed")
    assert backend.extras(info_for("PT-P910BT")) == ("chain", "feed")  # no high resolution
    assert backend.extras(info_for("PT-D460BT")) == ("chain",)
    assert backend.extras(info_for("Something else")) == ("chain", "high_res", "feed")
    assert backend.extras(info_for("P-touch", 36)) == ("chain", "high_res", "feed")


def test_job_settings_follow_the_model():
    status = parse_status(status_frame(width=24, media=0x01))
    p750w = model("PT-P750W")
    plain = ptouch.job_settings(p750w, PrintOptions(), status)
    assert plain == JobSettings("full", False, False, 14, 24, 1)
    wanted = PrintOptions(cut="half", chain=True, high_res=True, feed_mm=5.0)
    assert ptouch.job_settings(p750w, wanted, status) == JobSettings("half", True, True, 70, 24, 1)
    # what the model cannot do is left out
    assert ptouch.job_settings(model("PT-P710BT"), wanted, status).cut == "full"
    assert ptouch.job_settings(model("PT-2420PC"), wanted, status) == JobSettings(
        "none", True, False, 35, 24, 1
    )
    # Brother's margins: 2 mm (14 dots) at least at 180 dpi, 1 mm (14 dots)
    # at 360 dpi, 127 mm at most; doubled at high resolution
    assert ptouch.job_settings(p750w, PrintOptions(feed_mm=0.1), status).feed_dots == 14
    assert ptouch.job_settings(p750w, PrintOptions(feed_mm=500), status).feed_dots == 900
    p900 = model("PT-P900")
    assert ptouch.job_settings(p900, PrintOptions(feed_mm=0.1), status).feed_dots == 14
    assert ptouch.job_settings(p900, PrintOptions(feed_mm=127), status).feed_dots == 1800
    hi = ptouch.job_settings(p900, PrintOptions(feed_mm=1, high_res=True), status)
    assert hi.high_res and hi.feed_dots == 28
    assert (
        ptouch.job_settings(p900, PrintOptions(feed_mm=127, high_res=True), status).feed_dots
        == 3600
    )
    # PT-P900 series: high resolution on laminated tape only; PT-P910BT never
    tube = parse_status(status_frame(width=12, media=0x11))
    assert not ptouch.job_settings(p900, wanted, tube).high_res
    assert ptouch.job_settings(p750w, wanted, tube).high_res
    assert not ptouch.job_settings(model("PT-P910BT"), wanted, status).high_res


# --- status ------------------------------------------------------------------------------


def test_parse_status():
    status = parse_status(status_frame(width=4, media=0x11, kind=1, phase=1, code=0x71))
    assert (status.model_code, status.width, status.media) == (0x71, 4, 0x11)
    assert (status.status_type, status.phase, status.tape_mm) == (1, 1, 3.5)
    assert (status.tape_colour, status.text_colour) == (1, 8)
    assert parse_status(status_frame(width=0)).tape_mm is None
    for junk in (b"", status_frame()[:31], b"\x81" + status_frame()[1:], bytes(32)):
        with pytest.raises(PrinterError, match="unexpected status"):
            parse_status(junk)


@pytest.mark.parametrize(
    "err1, err2, code",
    [
        (0x01, 0, "no_cassette"),
        (0x02, 0, "no_cassette"),
        (0x04, 0, "jam"),
        (0x08, 0, "battery"),
        (0x10, 0, "busy"),
        (0x40, 0, "generic"),
        (0x80, 0, "generic"),
        (0, 0x01, "bad_cassette"),
        (0, 0x02, "generic"),
        (0, 0x04, "generic"),
        (0, 0x08, "generic"),
        (0, 0x10, "cover_open"),
        (0, 0x20, "overheat"),
        (0, 0x40, "label_not_found"),  # PT-P900 series: black marking not detected
        (0, 0x80, "generic"),
        (0x08, 0x10, "cover_open"),  # what to fix first
        (0x01, 0x20, "no_cassette"),
    ],
)
def test_status_errors(err1, err2, code):
    error = status_error(parse_status(status_frame(err1=err1, err2=err2)))
    assert error is not None and error.code == code
    assert "80 20 42" in error.details  # the raw status for bug reports


@pytest.mark.parametrize(
    "extended, code",
    [
        (0x10, "no_cassette"),
        (0x1D, "generic"),
        (0x1E, "generic"),
        (0x21, "bad_cassette"),
        (0x55, "generic"),
    ],
)
def test_extended_errors(extended, code):
    """PT-P900 series: status byte 7; reserved (ignored) on the others."""
    frame = bytearray(status_frame())
    frame[7] = extended
    error = status_error(parse_status(bytes(frame)), extended=True)
    assert error is not None and error.code == code
    assert status_error(parse_status(bytes(frame))) is None
    p900, p750w = model("PT-P900W"), model("PT-P750W")
    status = parse_status(bytes(frame))
    assert PTouch().info(p900, status).error == code
    assert PTouch().info(p750w, status).error == ""


def test_status_without_error_bits():
    assert status_error(parse_status(status_frame())) is None
    assert status_error(parse_status(status_frame(media=0xFF))).code == "bad_cassette"
    assert status_error(parse_status(status_frame(kind=0x02))).code == "generic"
    assert status_error(parse_status(status_frame(width=0, media=0))).code == "no_cassette"


def reader(*chunks: bytes) -> ptouch._Session:
    pieces = list(chunks)

    class Link:
        def sendall(self, data):
            pass

        def recv(self, size):
            if not pieces:
                raise PrinterTimeout("silent")
            return pieces.pop(0)

        def settimeout(self, timeout):
            pass

    return ptouch._Session(Link())


def test_status_in_pieces_and_after_garbage():
    frame = status_frame(width=18)
    # a 16-byte answer some models give, junk, then the status in pieces
    s = reader(b"\x80\x10B0" + bytes(12), b"junk\x80", b"\x20", frame[:5], frame[5:])
    assert s.read_status().width == 18
    # two at once: the second is kept for the next read
    s = reader(frame + status_frame(kind=1))
    assert s.read_status().status_type == 0 and s.read_status().status_type == 1
    with pytest.raises(PrinterTimeout):
        s.read_status()


def test_status_garbage_and_silence():
    with pytest.raises(PrinterError, match="no status"):
        reader(*[b"\xff" * 256] * 20).read_status()
    with pytest.raises(PrinterTimeout, match="closed"):
        reader(b"\x80\x20", b"").read_status()  # the connection closed
    with pytest.raises(PrinterTimeout):
        reader().read_status()


# --- models ------------------------------------------------------------------------------


def test_model_names():
    assert model_by_name("PT-P750W").name == "PT-P750W"
    assert model_by_name("Brother PT-P900W series").name == "PT-P900W"  # not PT-P900
    assert model_by_name("pt p900").name == "PT-P900"
    assert model_by_name("PT-E550W").name == "PT-E550W"  # not PT-E500
    assert model_by_name("VC-500W") is None and model_by_name("") is None
    assert model_by_name("QL-820NWB") is None


def test_usb_ids():
    assert model_by_usb_id(0x2061).name == "PT-P700"
    assert model_by_usb_id(0x2062).name == "PT-P750W"
    # Brother (Appendix A): 2062 is the PT-P750W; 2065 is its Editor Lite drive
    assert model_by_usb_id(0x2065) is None
    assert ptouch.EDITOR_LITE[3].usb_ids == (0x2065,)
    # Brother's IDs of the documented models
    brother = {0x2060: "PT-E550W", 0x20AF: "PT-P710BT", 0x2083: "PT-P900", 0x2086: "PT-P950NW"}
    for usb_id, name in brother.items():
        assert model_by_usb_id(usb_id).name == name
    assert model_by_usb_id(0x20C7).name == "PT-P910BT"
    assert model_by_usb_id(0x2085).name == "PT-P900W"
    assert model_by_usb_id(0x20B0) is None  # the VC-500W
    assert model_by_usb_id(None) is None
    ids = [usb_id for m in MODELS for usb_id in m.usb_ids]
    assert len(ids) == len(set(ids))


def test_model_flags():
    assert model("PT-P750W").documented and model("PT-P750W").network
    assert model("PT-P900W").pins == 560 and model("PT-P900W").dpi == 360
    assert not model("PT-P900W").packbits  # Brother's PT-P900 series default
    assert model("PT-D460BT").d460 and model("PT-D460BT").info_cmd
    assert not model("PT-P710BT").half_cut and not model("PT-P710BT").heat_shrink
    network = sorted(m.name for m in MODELS if m.network)
    assert network == ["PT-E550W", "PT-P750W", "PT-P900W", "PT-P950NW"]
    assert ptouch._is_network_ptouch("Brother PT-P750W") and not ptouch._is_network_ptouch("cab")


# --- query and print through the fake -------------------------------------------------------


def test_query(fake):
    info = PTouch().query("ptouch.local")
    assert (info.model, info.tape_mm, info.state, info.error) == ("PT-P750W", 12, "IDLE", "")
    assert info.cassette.printable_mm == 9.9 and info.cassette.media == "laminated"
    assert PTouch().display_name(info) == "Brother PT-P750W"
    assert PTouch().display_name(None) == "Brother P-touch"
    assert fake.links[-1].closed


def test_query_heat_shrink_and_small_tape(fake):
    fake.printer.width, fake.printer.media = 11, 0x17
    info = PTouch().query("ptouch.local")
    assert info.tape_mm == 12 and info.cassette.media == "heat_shrink_3"
    assert info.cassette.printable_mm == 7.1  # 50 pins
    fake.printer.width, fake.printer.media = 4, 0x01
    assert PTouch().query("ptouch.local").tape_mm == 3.5


def test_query_reports_problems(fake):
    fake.printer.errors = (0, 0x10)
    info = PTouch().query("ptouch.local")
    assert (info.state, info.error) == ("ERROR", "cover_open")
    fake.printer.errors = (0, 0)
    fake.printer.width, fake.printer.media = 0, 0
    info = PTouch().query("ptouch.local")
    assert info.tape_mm is None and info.error == "no_cassette" and info.cassette is None
    fake.printer.width, fake.printer.media = 12, 0xFF
    assert PTouch().query("ptouch.local").error == "bad_cassette"


def test_query_unknown_network_model(fake):
    fake.printer.code = 0x99  # a model code not in the table [assumed]
    info = PTouch().query("ptouch.local")
    assert info.model == "P-touch" and info.tape_mm == 12
    fake.printer.width = 36
    info = PTouch().query("ptouch.local")
    assert ptouch.model_of(info).pins == 560  # 36 mm only fits the wide head


def test_print(fake):
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    assert image.height == 70 and image.info["lpi"] == 180
    events = []
    assert (
        backend.print_label(image, "ptouch.local", PrintOptions(copies=2), events.append) is None
    )
    assert events == [Progress(1, 2), Progress(2, 2)]
    pages = fake.printer.pages
    assert len(pages) == 2 and [p["end"] for p in pages] == [0x0C, 0x1A]
    assert pages[0]["M"] == b"\x40" and pages[0]["K"] == b"\x08" and pages[0]["d"] == b"\x0e\x00"
    # the content sits in the print area: pins 29 to 98
    ink = b"".join(pages[0]["lines"])
    rows = [i for i in range(len(ink) * 8) if ink[i // 8] & (0x80 >> i % 8)]
    pins = {i % 128 for i in rows}
    assert pins and min(pins) >= 29 and max(pins) <= 98
    assert len(pages[0]["lines"]) == image.width


def test_print_high_res_and_chain(fake):
    backend = PTouch()
    options = PrintOptions(high_res=True, chain=True, cut="half", feed_mm=3)
    image = backend.prepare(label(12), options)
    assert image.height == 70 and image.info["lpi"] == 360
    normal = backend.prepare(label(12), PrintOptions())
    # twice the columns for the same length
    assert abs(image.width / 360 - normal.width / 180) * 25.4 < 0.5
    backend.print_label(image, "ptouch.local", options)
    (page,) = fake.printer.pages
    assert page["K"] == b"\x44" and page["M"] == b"\x00"  # half cut, chain, high res
    assert page["d"] == bytes((42, 0))  # 3 mm = 21 dots, doubled
    lines = int.from_bytes(page["z"][4:8], "little")
    assert lines == len(page["lines"]) == image.width


def test_print_refits_for_the_inserted_tape(fake):
    backend = PTouch()
    image = backend.prepare(label(24), PrintOptions())  # 128 pins, before any query
    fake.printer.width = 9  # but 9 mm tape is inserted
    backend.print_label(image, "ptouch.local", PrintOptions())
    (page,) = fake.printer.pages
    assert page["z"][2] == 9
    # rendered for the printer as asked (PrintOptions.info), and its tube
    fake.printer.width, fake.printer.media = 11, 0x17
    info = backend.query("ptouch.local")
    assert info.tape_mm == 12
    assert backend.raster(12, PrintOptions(info=info)).width_px == 50  # 11.2 mm tube
    assert backend.raster(9, PrintOptions(info=info)).width_px == 50  # 9 mm tape
    assert backend.raster(6, PrintOptions(info=info)).width_px == 32


def test_backend_keeps_no_printer_state(fake):
    """One backend serves every window: a query changes nothing for others."""
    backend = PTouch()
    before = backend.raster(12, PrintOptions())
    fake.printer.code, fake.printer.line_bytes = 0x69, 70
    info = backend.query("ptouch.local")
    assert backend.raster(12, PrintOptions()) == before
    assert backend.raster(12, PrintOptions(info=info)).width_px == 150


def test_print_errors(fake):
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    fake.printer.errors = (0x01, 0)
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "ptouch.local", PrintOptions())
    assert raised.value.code == "no_cassette" and not fake.printer.pages
    fake.printer.errors = (0, 0)
    fake.printer.width = 36  # no print area on this model
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "ptouch.local", PrintOptions())
    assert raised.value.code == "bad_cassette"
    too_long = Image.new("RGB", (8000, 70), "white")
    too_long.info["lpi"] = 180
    with pytest.raises(PrinterError) as raised:
        backend.print_label(too_long, "ptouch.local", PrintOptions())
    assert raised.value.code == "too_long"


def test_error_while_printing_says_how_many_came_out(fake):
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    fake.printer.fail_after = 2
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "ptouch.local", PrintOptions(copies=3))
    assert raised.value.code == "cover_open"
    assert raised.value.params == {"printed": 1, "copies": 3}


def test_silent_printer_on_usb_times_out(fake, monkeypatch):
    """On USB Brother's flow reports every page: none is a problem."""
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: model("PT-P750W"))
    fake.printer.completes = False
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "usb://Brother/PT-P750W", PrintOptions(copies=2))
    assert raised.value.code == "timeout" and len(fake.printer.pages) == 1
    length = image.width / 180 * 25.4
    assert ptouch.print_timeout(length, False) in fake.links[-1].timeouts


def test_silent_printer_on_the_network_gets_the_job(fake):
    """Status over TCP is not documented: a printer that answers ESC i S
    but reports no pages gets every page, and the job counts as sent."""
    fake.printer.completes = False
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    events = []
    result = backend.print_label(image, "ptouch.local", PrintOptions(copies=3), events.append)
    assert len(fake.printer.pages) == 3 and [e.copy for e in events] == [1, 2, 3]
    # sent, not reported printed: the interface says so instead of "Printed"
    assert result == ptouch.PrintResult(queued=True)


@pytest.mark.parametrize("close_after, printed", [(1, None), (2, 1), (3, 2)])
def test_connection_closed_during_the_job(fake, close_after, printed):
    """A printer that hangs up is not silent: the job fails and says how
    many labels it reported printed, also after the last page."""
    fake.printer.close_after = close_after
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "ptouch.local", PrintOptions(copies=3))
    assert raised.value.code == "timeout" and "closed" in raised.value.details
    assert len(fake.printer.pages) == close_after  # nothing sent after it
    if printed:
        assert raised.value.params == {"printed": printed, "copies": 3}
    else:
        assert not raised.value.params


def test_all_pages_reported_is_printed(fake):
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    assert backend.print_label(image, "ptouch.local", PrintOptions(copies=2)) is None


def test_status_is_read_between_pages(fake, monkeypatch):
    """Brother's flow 5.1: each page's "printing completed" is read before
    the next page is sent, so no answer piles up."""
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: model("PT-P750W"))
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    events = []
    backend.print_label(image, "usb://Brother/PT-P750W", PrintOptions(copies=3), events.append)
    assert fake.printer.unread_at_page == [0, 0, 0] and not fake.printer.out
    assert [e.copy for e in events] == [1, 2, 3]


def test_print_timeout_grows_with_the_label():
    assert ptouch.print_timeout(0, False) == ptouch.PRINT_TIMEOUT
    assert ptouch.print_timeout(1000, False) == ptouch.PRINT_TIMEOUT + 100
    assert ptouch.print_timeout(1000, True) == ptouch.PRINT_TIMEOUT + 200


def test_network_printer_without_status_prints_without_waiting(fake):
    """Brother documents no status over TCP: without one, the job goes out
    for the label's tape and nothing is waited for."""
    backend = PTouch()
    image = backend.prepare(label(9), PrintOptions())
    fake.printer.status = lambda kind=0: b""  # never answers
    events = []
    backend.print_label(image, "ptouch.local", PrintOptions(copies=2), events.append)
    pages = fake.printer.pages
    assert len(pages) == 2 and pages[0]["z"][0] == 0x84 and pages[0]["z"][2] == 9
    assert events == [Progress(1, 2), Progress(2, 2)]
    # the status query itself still says the printer does not answer
    with pytest.raises(PrinterError) as raised:
        backend.query("ptouch.local")
    assert raised.value.code == "timeout"


def test_usb_printer_without_status_is_an_error(fake, monkeypatch):
    """On USB the status is documented: no answer is a problem, not a reason
    to print blind."""
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: model("PT-P750W"))
    fake.printer.status = lambda kind=0: b""
    image = PTouch().prepare(label(12), PrintOptions())
    with pytest.raises(PrinterError) as raised:
        PTouch().print_label(image, "usb://Brother/PT-P750W", PrintOptions())
    assert raised.value.code == "timeout" and not fake.printer.pages


def test_network_printer_without_status_is_named_by_its_host(fake, monkeypatch):
    """No status, so no model byte: the model reported before, the host
    name or its DNS-SD name choose the head, not the tape width."""
    fake.printer.status = lambda kind=0: b""
    image = Image.new("RGB", (100, 70), "white")
    image.info.update(lpi=180, tape_mm=12)
    backend = PTouch()
    backend.print_label(image, "ptouch.local", PrintOptions())  # nothing known: 128 pins
    assert len(fake.printer.pages[-1]["lines"][0]) == 16
    fake.printer.line_bytes = 70
    backend.print_label(image, "PT-P950NW.local", PrintOptions())
    assert len(fake.printer.pages[-1]["lines"][0]) == 70
    found = [FoundPrinter("Brother PT-P900W", "BRW0001.local", "192.0.2.9")]
    monkeypatch.setattr(ptouch, "_browse", lambda service_type, match, timeout: found)
    backend.print_label(image, "192.0.2.9", PrintOptions())
    assert len(fake.printer.pages[-1]["lines"][0]) == 70
    # host names in any case, with or without the final dot
    for host in ("brw0001.LOCAL.", "BRW0001.local"):
        fake.printer.line_bytes = 70
        backend.print_label(image, host, PrintOptions())
        assert len(fake.printer.pages[-1]["lines"][0]) == 70
    assert not ptouch._same_host("brw0001.local", "brw0002.local")
    assert fake.printer.pages[-1]["z"][8] == 2  # PT-P900 series: last page
    monkeypatch.setattr(ptouch, "_browse", lambda service_type, match, timeout: [])
    backend.print_label(image, "printer.lan", PrintOptions(info=info_for("PT-P900")))
    assert len(fake.printer.pages[-1]["lines"][0]) == 70


def test_fle_labels(fake):
    """FLe labels (21 x 45 mm, PT-P900 series): shown as 21 mm, printed in
    the print area of the 21 mm tube [assumed]."""
    fake.printer.code, fake.printer.line_bytes = 0x69, 70
    fake.printer.width, fake.printer.media = 21, 0x13
    backend = PTouch()
    info = backend.query("ptouch.local")
    assert info.tape_mm == 21 and isinstance(info.tape_mm, int)
    assert info.cassette.media == "fle" and info.error == ""
    assert info.cassette.printable_mm == 16.9  # 240 pins
    image = backend.prepare(label(24), PrintOptions(info=info))
    backend.print_label(image, "ptouch.local", PrintOptions())
    ink = b"".join(fake.printer.pages[0]["lines"])
    pins = {i % 560 for i in range(len(ink) * 8) if ink[i // 8] & (0x80 >> i % 8)}
    assert min(pins) >= 168 and max(pins) < 168 + 240
    assert print_area(model("PT-P910BT"), 21, 0x13) is None
    assert print_area(model("PT-P750W"), 21, 0x13) is None


def test_shortest_label():
    """Brother: at least 4.4 mm of print data; shorter labels are extended."""
    lab = Label(tape_mm=12, printer="ptouch", elements=[], margin_mm=0)
    image = PTouch().prepare(lab, PrintOptions())
    assert image.width / 180 * 25.4 >= 4.4 - 0.15


def test_feed_range_per_model():
    backend = PTouch()
    assert backend.feed_mm(None) == (1.0, 127.0, 2.0)
    assert backend.feed_mm(info_for("PT-P750W")) == (2.0, 127.0, 2.0)
    assert backend.feed_mm(info_for("PT-P900")) == (1.0, 127.0, 2.0)


def test_tube_is_at_most_500_mm(fake):
    fake.printer.width, fake.printer.media = 12, 0x11
    backend = PTouch()
    image = Image.new("RGB", (round(600 / 25.4 * 180), 66), "white")
    image.info["lpi"] = 180
    with pytest.raises(PrinterError) as raised:
        backend.print_label(image, "ptouch.local", PrintOptions())
    assert raised.value.code == "too_long" and raised.value.params["max_mm"] == 500
    image = image.crop((0, 0, round(400 / 25.4 * 180), 66))
    backend.print_label(image, "ptouch.local", PrintOptions())
    assert fake.printer.pages[0]["z"][:2] == b"\x86\x11"


def test_answer_after_switching_to_raster_is_dropped(fake, monkeypatch):
    """PT-P700 (ptouch-print's P700 flag): the answer is read and dropped."""
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: model("PT-P700"))
    fake.printer.answers_raster = True
    fake.printer.completes = False
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    backend.print_label(image, "usb://Brother/PT-P700", PrintOptions(copies=2))
    assert len(fake.printer.pages) == 2 and not fake.printer.out
    assert ptouch.REPLY_WAIT in fake.links[-1].timeouts
    fake.printer.answers_raster = False  # and none at all is fine too
    backend.print_label(image, "usb://Brother/PT-P700", PrintOptions())
    assert len(fake.printer.pages) == 3


def test_preview_is_black_and_white_without_dither():
    grey = Image.new("RGB", (4, 4), (120, 120, 120))
    assert PTouch().preview(grey).getcolors() == [(16, (0, 0, 0))]
    light = Image.new("RGB", (4, 4), (200, 200, 200))
    assert PTouch().preview(light).getcolors() == [(16, (255, 255, 255))]


def test_raster_for_each_tape():
    backend = PTouch()
    assert backend.raster(12, PrintOptions()) == ptouch.Raster(180, tape_px=70, width_px=70)
    assert backend.raster(12, PrintOptions(high_res=True)) == ptouch.Raster(
        360, tape_px=140, width_px=70
    )
    # 36 mm: only the PT-P900 series takes it, at 360 dpi
    assert backend.raster(36, PrintOptions()) == ptouch.Raster(360, tape_px=454, width_px=454)
    p750w = PrintOptions(info=info_for("PT-P750W"))
    assert backend.raster(36, p750w).width_px == 128  # the whole head
    # PT-P900 series: high resolution on laminated tape only, none on the PT-P910BT
    p900 = info_for("PT-P900")
    assert backend.raster(12, PrintOptions(high_res=True, info=p900)).dpi == 720
    tube = info_for("PT-P900", media="heat_shrink_2")
    assert backend.raster(12, PrintOptions(high_res=True, info=tube)).dpi == 360
    p910 = info_for("PT-P910BT")
    assert backend.raster(12, PrintOptions(high_res=True, info=p910)).dpi == 360


# --- refused models ----------------------------------------------------------------------


def test_unsupported_models_are_refused(fake, monkeypatch):
    unsupported = ptouch.Model("PT-X1", (0x2999,), unsupported="raster")
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: unsupported)
    with pytest.raises(PrinterError) as raised:
        PTouch().query("usb://Brother/PT-X1")
    assert raised.value.code == "unsupported_model"
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: None)
    with pytest.raises(PrinterError) as raised:
        PTouch().query("usb://Brother/PT-X2")
    assert raised.value.code == "unknown_model"
    lite = ptouch.EDITOR_LITE[2]
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: lite)
    with pytest.raises(PrinterError) as raised:
        PTouch().query("usb://Brother/PT-P700")
    assert raised.value.code == "editor_lite"


# --- USB: sysfs and a pseudo terminal as /dev/usb/lp0 -------------------------------------


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """Fake /sys/class/usbmisc, /sys/bus/usb/devices and /dev/usb."""
    misc, devices, dev = tmp_path / "usbmisc", tmp_path / "devices", tmp_path / "dev"
    for path in (misc, devices, dev):
        path.mkdir()
    monkeypatch.setattr(usb, "SYSFS", misc)
    monkeypatch.setattr(usb, "USB_DEVICES", devices)
    monkeypatch.setattr(usb, "DEV", dev)
    monkeypatch.setattr(ptouch, "_browse", lambda service_type, match, timeout: [])

    def add(name, ieee1284_id, product, vendor="04f9", target=None):
        node = misc / name
        (node / "device").mkdir(parents=True)
        (node / "device" / "ieee1284_id").write_text(ieee1284_id)
        (node / "idVendor").write_text(vendor + "\n")  # device/../idVendor
        (node / "idProduct").write_text(product + "\n")
        if target:
            (dev / name).symlink_to(target)

    def drive(name, product):
        """A USB device without a printer interface (Editor Lite: a drive)."""
        node = devices / name
        (node / (name + ":1.0")).mkdir(parents=True)
        (node / "idVendor").write_text("04f9\n")
        (node / "idProduct").write_text(product + "\n")

    def printer(name, product):
        node = devices / name
        (node / (name + ":1.0") / "usbmisc" / "lp0").mkdir(parents=True)
        (node / "idVendor").write_text("04f9\n")
        (node / "idProduct").write_text(product + "\n")

    return SimpleNamespace(add=add, drive=drive, printer=printer)


def test_discover_on_usb(sysfs):
    sysfs.add("lp0", "MFG:Brother;MDL:PT-P700;SN:A1;", "2061")
    sysfs.add("lp1", "MFG:Brother;MDL:VC-500W;", "20b0")  # not a P-touch
    sysfs.add("lp2", "MFG:Brother;MDL:P-touch;", "2085")  # named by its USB ID
    sysfs.add("lp3", "MFG:HP;MDL:PT-P700;", "2061")  # another maker
    assert PTouch().discover(timeout=0.01) == [
        FoundPrinter(
            "Brother PT-P700 (USB)", "usb://Brother/PT-P700?serial=A1", str(usb.DEV / "lp0")
        ),
        FoundPrinter("Brother PT-P900W (USB)", "usb://Brother/P-touch", str(usb.DEV / "lp2")),
    ]


def test_discover_on_the_network(sysfs, monkeypatch):
    found = FoundPrinter("Brother PT-P750W", "BRW1.local", "192.0.2.7")
    calls = []

    def browse(service_type, match, timeout):
        calls.append((service_type, match("Brother PT-P750W"), match("cab EOS2/300")))
        return [found]

    monkeypatch.setattr(ptouch, "_browse", browse)
    assert PTouch().discover(timeout=0.01) == [found]
    assert calls == [("_pdl-datastream._tcp", True, False)]


def test_editor_lite_is_found_and_explained(sysfs, monkeypatch):
    sysfs.drive("1-2", "2064")  # PT-P700 as a drive
    sysfs.printer("1-3", "2065")  # a PT-P750W that prints: no Editor Lite
    sysfs.drive("1-4", "1234")  # something else of Brother's
    assert usb.other_devices(0x04F9) == [0x2064, 0x1234]
    (found,) = PTouch().discover(timeout=0.01)
    assert found.name == "Brother PT-P700 (USB)" and found.host == "usb://Brother/PT-P700"
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    with pytest.raises(PrinterError) as raised:
        PTouch().query(found.host)
    assert raised.value.code == "editor_lite"


def test_usb_not_found_without_editor_lite(sysfs, monkeypatch):
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    with pytest.raises(PrinterError) as raised:
        PTouch().query("usb://Brother/PT-P700")
    assert raised.value.code == "usb_not_found"


def test_usb_model_by_id_when_the_name_is_unknown(sysfs):
    sysfs.add("lp0", "MFG:Brother;MDL:Label Printer;", "20af")
    assert ptouch._usb_model("usb://Brother/Label%20Printer").name == "PT-P710BT"
    assert ptouch._usb_model("usb://Brother/Other") is None  # not plugged in
    assert ptouch._usb_model("usb://[x") is None
    assert usb.product_id(usb.UsbPrinter(usb.DEV / "lp9", "Brother", "x"), 0x04F9) is None


class PtyPrinter(threading.Thread):
    """The fake printer on the master side of a pseudo terminal."""

    def __init__(self, master: int, printer: FakePTouch):
        super().__init__(daemon=True)
        self.master, self.printer = master, printer
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            self.printer.feed(data)
            if self.printer.out:
                os.write(self.master, bytes(self.printer.out))
                self.printer.out.clear()


@pytest.fixture
def pty_printer(sysfs, monkeypatch):
    master, slave = os.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    printer = FakePTouch(code=0x00)  # on USB the model comes from the device ID
    sysfs.add("lp0", "MFG:Brother;MDL:PT-P750W;SN:E1;", "2062", target=os.ttyname(slave))
    thread = PtyPrinter(master, printer)
    thread.start()
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    monkeypatch.setattr(usb, "DRAIN_QUIET", 0.02)
    yield printer
    thread.stopped.set()
    thread.join()
    os.close(master)
    os.close(slave)


def test_usb_query_and_print_on_a_device_file(pty_printer):
    backend = PTouch()
    (found,) = backend.discover(timeout=0.01)
    assert found.host == "usb://Brother/PT-P750W?serial=E1"
    info = backend.query(found.host)
    assert (info.model, info.tape_mm, info.error) == ("PT-P750W", 12, "")
    image = backend.prepare(label(12), PrintOptions())
    backend.print_label(image, found.host, PrintOptions(copies=2))
    assert len(pty_printer.pages) == 2 and pty_printer.pages[-1]["end"] == 0x1A


# --- TCP: a local socket ------------------------------------------------------------------


@pytest.fixture
def tcp_printer(monkeypatch):
    """The fake printer on a TCP port of 127.0.0.1, one connection at a time."""
    server = socket.create_server(("127.0.0.1", 0))
    printer = FakePTouch(code=0x69, line_bytes=70)  # a PT-P900W
    stop = threading.Event()

    def serve():
        server.settimeout(0.05)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(0.05)
                while not stop.is_set():
                    try:
                        data = conn.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    printer.feed(data)
                    if printer.out:
                        conn.sendall(bytes(printer.out))
                        printer.out.clear()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    yield SimpleNamespace(printer=printer, port=server.getsockname()[1])
    stop.set()
    thread.join()
    server.close()


def test_tcp_query_and_print(tcp_printer):
    backend = PTouch(port=tcp_printer.port)
    info = backend.query("127.0.0.1")
    assert (info.model, info.tape_mm) == ("PT-P900W", 12)
    assert info.cassette.printable_mm == 10.6  # 150 pins at 360 dpi
    image = backend.prepare(label(12), PrintOptions(info=info))
    assert image.height == 150 and image.info["lpi"] == 360
    backend.print_label(image, "127.0.0.1", PrintOptions(copies=2, cut="none"))
    pages = tcp_printer.printer.pages
    assert len(pages) == 2 and pages[0]["compression"] == 0 and pages[0]["M"] == b"\x00"


def test_tcp_printer_that_reads_slowly(monkeypatch):
    """The printer takes the job in pieces with pauses (it prints while it
    reads): sending goes on as long as each pause is shorter than the
    timeout, even when the whole job takes much longer."""
    server = socket.create_server(("127.0.0.1", 0))
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    printer = FakePTouch(code=0x69, line_bytes=70)
    pauses = []

    def serve():
        conn, _addr = server.accept()
        with conn:
            data = b""
            while ESC + b"iS" not in data:
                data += conn.recv(4096)
            printer.feed(data)
            conn.sendall(bytes(printer.out))
            printer.out.clear()
            conn.settimeout(2)
            try:
                while chunk := conn.recv(8192):
                    time.sleep(0.1)  # printing: not reading
                    pauses.append(len(chunk))
                    printer.feed(chunk)
                    if printer.out:
                        conn.sendall(bytes(printer.out))
                        printer.out.clear()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    connect = socket.create_connection

    def small_buffers(address, timeout):
        sock = connect(address, timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return sock

    monkeypatch.setattr(ptouch.socket, "create_connection", small_buffers)
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    monkeypatch.setattr(ptouch, "PRINT_TIMEOUT", 0.5)  # each pause is shorter
    monkeypatch.setattr(ptouch, "MIN_SPEED_MM_S", 1e9)
    monkeypatch.setattr(ptouch, "SEND_CHUNK", 4096)
    image = Image.new("RGB", (1200, 150), "black")  # 70 bytes a line, uncompressed
    image.info["lpi"] = 360
    begin = time.monotonic()
    PTouch(port=server.getsockname()[1]).print_label(
        image, "127.0.0.1", PrintOptions(copies=2, cut="none")
    )
    took = time.monotonic() - begin
    thread.join(5)
    server.close()
    assert len(printer.pages) == 2
    assert took > 2 * ptouch.PRINT_TIMEOUT  # far longer than one timeout


def test_tcp_printer_that_takes_nothing_stalls():
    class Stuck:
        def settimeout(self, timeout):
            pass

        def send(self, data):
            raise TimeoutError("timed out")

        def recv(self, size):
            raise TimeoutError("timed out")

    link = ptouch._TcpLink(Stuck())
    with pytest.raises(PrinterError) as raised:
        link.sendall(b"x")
    assert raised.value.code == "stalled"
    with pytest.raises(PrinterError) as raised:
        link.recv(1)
    assert raised.value.code == "timeout"


def test_tcp_port_closed(monkeypatch):
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()  # nobody listens there now
    monkeypatch.setattr(ptouch, "_connect", REAL_CONNECT)
    with pytest.raises(PrinterError) as raised:
        PTouch(port=port).query("127.0.0.1")
    assert raised.value.code == "port_closed"


def test_map_error():
    assert ptouch.map_error(OSError("unreachable")).code == "not_found"
    assert ptouch.map_error(ValueError("cannot resolve x")).code == "not_found"
    assert ptouch.map_error(ValueError("odd")).code == "generic"
    assert ptouch.map_error(TimeoutError("timed out")).code == "timeout"


def test_tests_never_reach_the_network():
    """conftest's guard is in place for every test of this module."""
    with pytest.raises(AssertionError, match="network"):
        ptouch._connect("printer.invalid", 9100)
    with pytest.raises(AssertionError, match="network"):
        ptouch._browse("_pdl-datastream._tcp", lambda name: True, 0.01)


def test_label_files_with_small_tape(tmp_path):
    lab = Label.new_for("ptouch", 3.5)
    assert lab.tape_mm == 3.5
    lab.save(tmp_path / "small.etiketto")
    assert Label.load(tmp_path / "small.etiketto").tape_mm == 3.5
    # nothing inserted: 12 mm, the most common tape (not the widest)
    assert Label.new_for("ptouch").tape_mm == 12
    assert isinstance(Label.new_for("ptouch").tape_mm, int)
    assert Path(tmp_path / "small.etiketto").exists()


def test_print_on_an_older_model(fake, monkeypatch):
    """PT-2430PC: one job for both copies, nothing to wait for afterwards."""
    monkeypatch.setattr(ptouch, "_usb_model", lambda address: model("PT-2430PC"))
    fake.printer.completes = False  # and it would not say anything anyway
    backend = PTouch()
    image = backend.prepare(label(12), PrintOptions())
    events = []
    backend.print_label(image, "usb://Brother/PT-2430PC", PrintOptions(copies=2), events.append)
    assert events == [Progress(1, 2), Progress(2, 2)]
    pages = fake.printer.pages
    assert [p["end"] for p in pages] == [0x0C, 0x1A] and pages[0]["M"] == b"\x40"
    assert fake.printer.inits == 2  # the status query, then the job once


def test_print_without_an_answer_to_raster_mode(fake):
    """PT-P900: no answer after ESC i a expected, 70 bytes per line."""
    fake.printer.code, fake.printer.line_bytes = 0x71, 70
    backend = PTouch()
    info = backend.query("ptouch.local")
    assert info.model == "PT-P900"
    image = backend.prepare(label(12), PrintOptions())
    backend.print_label(image, "ptouch.local", PrintOptions())
    assert len(fake.printer.pages) == 1
    assert ptouch.REPLY_WAIT not in fake.links[-1].timeouts


def test_settings_of_an_unknown_network_model(fake):
    fake.printer.code = 0x99
    backend = PTouch()
    info = backend.query("ptouch.local")
    assert backend.cut_modes(info) == ("full", "half", "none")  # the generic model's
    assert backend.extras(info) == ("chain", "high_res", "feed")
