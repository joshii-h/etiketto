"""The Brother QL backends against FakeQL, on the byte level.

No QL printer was at hand: the expected bytes follow the command sequence
of brother_ql (see docs/brother-ql.md), written down here independently of
the backend's code. FakeQL takes a job the way the printer's raster
interpreter would and answers status requests with 32-byte packets. It also
runs behind a real TCP socket on 127.0.0.1 and behind a pseudo terminal
standing in for /dev/usb/lp0; nothing leaves this computer.
"""

from __future__ import annotations

import os
import socket
import threading
import tty
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from etiketto.backends import brother_ql as ql
from etiketto.backends import mdns, net, usb
from etiketto.backends.base import (
    CoverOpen,
    FoundPrinter,
    LabelTooLong,
    LabelTooWide,
    OutOfLabels,
    OutOfPaper,
    PaperJam,
    PrinterBusy,
    PrinterError,
    PrinterNotFound,
    PrinterTimeout,
    PrintOptions,
    PrintResult,
    UnsupportedSize,
    WrongMedia,
)
from etiketto.backends.brother_ql import BrotherQL, BrotherQLDieCut
from etiketto.model import Label, TextElement

REAL_CONNECT = ql._connect  # before conftest's guard replaces it
MAX_SLEEPS = 5000
ESC = b"\x1b"


def packet(
    model=0x41,
    err1=0,
    err2=0,
    width=62,
    kind=0x0A,
    length=0,
    status_type=0,
    phase=0,
    colour=0,
) -> bytes:
    """A status packet as the printer sends it."""
    data = bytearray(32)
    data[0:3] = b"\x80\x20\x42"
    data[3], data[4], data[5] = 0x34, model, 0x30
    data[8], data[9], data[10], data[11] = err1, err2, width, kind
    data[17], data[18], data[19] = length, status_type, phase
    data[25] = colour
    return bytes(data)


# --- the fake printer ----------------------------------------------------------------


def _unpack(data: bytes) -> bytes:
    """PackBits, decoded here independently of the backend's code."""
    out, i = bytearray(), 0
    while i < len(data):
        n = data[i]
        if n < 128:
            out += data[i + 1 : i + 2 + n]
            i += n + 2
        else:
            out += bytes([data[i + 1]]) * (257 - n)
            i += 2
    return bytes(out)


class FakeQL:
    """The raster interpreter of a QL: commands in, status packets out.
    A page is recorded at 0x0C or 0x1A; with `notify` the printer reports
    each page (phase change, printing completed, phase change)."""

    def __init__(self, model=0x41, width=62, kind=0x4A, length=0):
        self.model, self.width, self.kind, self.length = model, width, kind, length
        self.err1 = self.err2 = 0
        self.busy = 0  # status requests that still show another job printing
        self.notify = True
        self.silent = False  # answers nothing at all
        self.fail_on_page = None  # an error packet instead of this page's notices
        # the 62 mm roll: True red and black, False plain; a job of the other
        # kind (ESC i K bit 0) is refused with "replace media"; None: any
        self.red_roll = None
        self.refused = 0
        self.colour = 0  # status byte 25
        self.answer_in_job = True  # answers the ESC i S inside a job
        self.in_job = False
        self.buffer = b""
        self.out = b""
        self.pages: list[dict] = []
        self.requests = 0
        self.nuls = 0
        self.inits = 0
        self.page: dict = self._new_page()

    def status(self, status_type=0, phase=0) -> bytes:
        if self.busy:
            self.busy -= 1
            phase = 1
        return packet(
            self.model,
            self.err1,
            self.err2,
            self.width,
            self.kind,
            self.length,
            status_type,
            phase,
            self.colour,
        )

    @staticmethod
    def _new_page() -> dict:
        return {"commands": [], "black": [], "red": [], "compressed": False}

    def printed(self) -> int:
        return len(self.pages)

    def feed(self, data: bytes) -> None:
        self.buffer += data
        while self.buffer:
            used = self._command(self.buffer)
            if not used:
                return  # wait for the rest
            self.buffer = self.buffer[used:]

    def _command(self, data: bytes) -> int:  # noqa: C901 - one branch per command
        first = data[0]
        if first == 0:
            self.nuls += 1
            return 1
        if data.startswith(ESC + b"@"):
            self.in_job = True  # the next ESC i S is the job's own
            self.inits += 1
            self.page = self._new_page()
            return 2
        if data.startswith(ESC + b"i"):
            return self._esc_i(data)
        if first == ord("M"):
            if len(data) < 2:
                return 0
            self.page["compressed"] = data[1] == 2
            self.page["commands"].append(data[:2])
            return 2
        if first in (ord("g"), ord("w")):
            return self._line(data)
        if first in (0x0C, 0x1A):
            self._page_done(last=first == 0x1A)
            return 1
        raise AssertionError("unknown command %s" % data[:8].hex())

    def _esc_i(self, data: bytes) -> int:
        if len(data) < 3:
            return 0
        size = {b"S": 3, b"a": 4, b"M": 4, b"A": 4, b"K": 4, b"d": 5, b"z": 13}[data[2:3]]
        if len(data) < size:
            return 0
        if data[2:3] == b"S":
            self.requests += 1
            in_job, self.in_job = self.in_job, False
            if not self.silent and (self.answer_in_job or not in_job):
                self.out += self.status()
        else:
            self.page["commands"].append(data[:size])
        return size

    def _line(self, data: bytes) -> int:
        if len(data) < 3 or len(data) < 3 + data[2]:
            return 0
        row = data[3 : 3 + data[2]]
        if self.page["compressed"]:
            row = _unpack(row)
        plane = "red" if data[0] == ord("w") and data[1] == 2 else "black"
        self.page[plane].append(row)
        return 3 + data[2]

    def _page_done(self, last: bool) -> None:
        number = len(self.pages)
        expanded = [c for c in self.page["commands"] if c.startswith(ESC + b"iK")]
        two_color = bool(expanded and expanded[-1][3] & 0x01)
        if self.red_roll is not None and two_color != self.red_roll:
            self.refused += 1
            self.page = self._new_page()
            self.out += packet(self.model, 0, 0x01, self.width, self.kind, self.length, 0x02)
            return
        self.pages.append(self.page)
        commands = [c for c in self.page["commands"] if not c.startswith(ESC + b"ia")]
        self.page = self._new_page()
        self.page["commands"] = commands  # settings stay for the next page
        if self.silent or not self.notify:
            return
        if self.fail_on_page == number:
            self.pages.pop()
            self.out += packet(self.model, 0x04, 0, self.width, self.kind, self.length, 0x02)
            return
        self.out += self.status(0x06, 1) + self.status(0x01, 0)
        if last:
            self.out += self.status(0x06, 0)


class FakeLink:
    """A socket to FakeQL in the same thread."""

    def __init__(self, device: FakeQL):
        self.device = device
        self.timeouts: list[float] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.device.feed(data)

    def recv(self, size: int) -> bytes:
        device = self.device
        if not device.out:
            raise TimeoutError("timed out")
        chunk, device.out = device.out[:size], device.out[size:]
        return chunk

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _forget(monkeypatch):
    """What the backend learned about printers stays in the test."""
    monkeypatch.setattr(ql, "_found_models", {})
    monkeypatch.setattr(ql, "_two_color_rolls", {})


@pytest.fixture
def printer(monkeypatch):
    device = FakeQL()
    calls, links, sleeps = [], [], []
    now = [1000.0]

    def connect(host, port, wait=None):
        calls.append((host, port, wait))
        links.append(FakeLink(device))
        return links[-1]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) > MAX_SLEEPS:
            raise AssertionError("endless polling")

    monkeypatch.setattr(ql, "_connect", connect)
    monkeypatch.setattr(ql, "_sleep", sleep)
    monkeypatch.setattr(ql, "_clock", lambda: now[0])
    return SimpleNamespace(device=device, calls=calls, links=links, sleeps=sleeps, now=now)


def _continuous(tape=62, **kwargs) -> Label:
    kwargs.setdefault("elements", [TextElement(text="QL")])
    return Label(printer="brother_ql", tape_mm=tape, **kwargs)


def _die_cut(width=62, height=29, orientation="across") -> Label:
    label = Label(printer="brother_ql_diecut", auto_length=False, orientation=orientation)
    label.set_media_size(width, height)
    return label


def _pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    return [image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)]


# --- golden bytes ----------------------------------------------------------------------

BLANK_62 = Image.new("1", (696, 20), 0)


def _job(model, media=None, rows=None, **kwargs) -> bytes:
    media = media or ql.continuous_media(62)
    black = rows if rows is not None else ql.raster_rows(BLANK_62, model, media.offset(model))
    return ql.build_job(ql.Job(model, media, black, **kwargs))


def test_golden_ql720nw_matches_brother_ql():
    """The example of docs/brother-ql.md: 62 mm continuous, 20 empty lines,
    QL-720NW, cut, uncompressed, quality first."""
    expected = (
        ESC
        + b"ia\x01"
        + b"\x00" * 200
        + ESC
        + b"@"
        + ESC
        + b"ia\x01"
        + ESC
        + b"iS"
        + ESC
        + b"iz\xce\x0a\x3e\x00\x14\x00\x00\x00\x00\x00"
        + ESC
        + b"iM\x40"
        + ESC
        + b"iA\x01"
        + ESC
        + b"iK\x08"
        + ESC
        + b"id\x23\x00"
        + (b"g\x00\x5a" + b"\x00" * 90) * 20
        + b"\x1a"
    )
    assert _job(ql.MODELS["QL-720NW"], compress=False) == expected


def test_golden_ql500_has_no_mode_switch_cut_or_expanded_mode():
    expected = (
        b"\x00" * 200
        + ESC
        + b"@"
        + ESC
        + b"iS"
        + ESC
        + b"iz\xce\x0a\x3e\x00\x14\x00\x00\x00\x00\x00"
        + ESC
        + b"id\x23\x00"
        + (b"g\x00\x5a" + b"\x00" * 90) * 20
        + b"\x1a"
    )
    assert _job(ql.MODELS["QL-500"]) == expected


def test_golden_ql820nwb_compresses_each_line():
    job = _job(ql.MODELS["QL-820NWB"], compress=True, cut="none")
    head = ESC + b"ia\x01" + b"\x00" * 400 + ESC + b"@" + ESC + b"ia\x01" + ESC + b"iS"
    setup = ESC + b"iM\x00" + ESC + b"iK\x00" + ESC + b"id\x23\x00" + b"M\x02"
    info = ESC + b"iz\xce\x0a\x3e\x00\x14\x00\x00\x00\x00\x00"
    # 90 zero bytes: one run, 1 - 90 = -89 = 0xa7
    assert job == head + info + setup + b"g\x00\x02\xa7\x00" * 20 + b"\x1a"


def test_golden_ql800_two_colours_send_both_planes_per_line():
    model = ql.MODELS["QL-800"]
    black = Image.new("1", (696, 2), 0)
    black.putpixel((0, 0), 255)  # leftmost dot of the image, first line
    red = Image.new("1", (696, 2), 0)
    red.putpixel((695, 1), 255)  # rightmost dot, second line
    rows_black = ql.raster_rows(black, model, 12)
    rows_red = ql.raster_rows(red, model, 12)
    job = ql.build_job(ql.Job(model, ql.RED_ROLL, rows_black, rows_red, compress=False))
    # image x 0 is head pin 707 from the left of 720 (720 - 696 - 12 = 12 dots
    # from the left edge), mirrored: byte 1, bit 3; x 695 is byte 88, bit 4
    left = bytearray(90)
    left[88] = 0x10
    right = bytearray(90)
    right[1] = 0x08
    zero = bytes(90)
    assert rows_black == [bytes(left), zero] and rows_red == [zero, bytes(right)]
    lines = (
        b"w\x01\x5a" + bytes(left) + b"w\x02\x5a" + zero + b"w\x01\x5a" + zero + b"w\x02\x5a"
    ) + bytes(right)
    assert job.endswith(ESC + b"iK\x09" + ESC + b"id\x23\x00" + lines + b"\x1a")
    assert job.count(b"\x00" * 400) == 1 and b"M\x02" not in job


def test_golden_ql1100_wide_rows_and_offset():
    model = ql.MODELS["QL-1100"]
    media = ql.continuous_media(102)
    image = Image.new("1", (1164, 1), 0)
    image.putpixel((0, 0), 255)
    rows = ql.raster_rows(image, model, media.offset(model))
    # 1296 pins: the image starts 1296 - 1164 - 56 = 76 dots from the left,
    # mirrored to pin 1219: byte 152, bit 7 - 3
    expected = bytearray(162)
    expected[152] = 0x10
    assert rows == [bytes(expected)]
    job = ql.build_job(ql.Job(model, media, rows, compress=False))
    assert ESC + b"iz\xce\x0a\x66\x00\x01\x00\x00\x00\x00\x00" in job
    assert job.endswith(b"g\x00\xa2" + bytes(expected) + b"\x1a")


def test_golden_high_resolution_and_cut_at_end():
    job = _job(ql.MODELS["QL-710W"], high_res=True, cut="end")
    assert ESC + b"iM\x00" + ESC + b"iK\x48" in job
    assert ESC + b"iA" not in job


def test_golden_die_cut_label():
    media = ql.die_cut_media(62, 29)
    rows = ql.raster_rows(Image.new("1", (696, 271), 0), ql.MODELS["QL-720NW"], 12)
    job = ql.build_job(ql.Job(ql.MODELS["QL-720NW"], media, rows))
    assert ESC + b"iz\xce\x0b\x3e\x1d\x0f\x01\x00\x00\x00\x00" in job  # 271 lines
    assert ESC + b"id\x00\x00" in job  # no feed margin on die-cut labels


def test_golden_copies_are_pages_of_one_job():
    rows = [bytes(90)] * 3
    job = ql.build_job(ql.Job(ql.MODELS["QL-700"], ql.RED_ROLL, rows, copies=3))
    assert job.count(ESC + b"@") == 1  # one job
    assert job.count(ESC + b"iz") == 3
    assert job.count(b"\x0c") == 2 and job.endswith(b"\x1a")
    first, second, third = job.split(ESC + b"iz")[1:]
    assert first[8] == 0 and second[8] == 1 and third[8] == 1  # n9: following pages


def test_raster_rows_refuse_an_image_wider_than_the_head():
    with pytest.raises(UnsupportedSize):
        ql.raster_rows(Image.new("1", (720, 1)), ql.MODELS["QL-700"], 12)


def test_every_roll_fits_the_print_head():
    for media in ql.CONTINUOUS + ql.DIE_CUT:
        assert media.dots[0] + media.offset_wide <= 1296, media
        assert media.wide or media.dots[0] + media.offset_r <= 720, media
        assert media.wide == (media.width_mm > 62)
    assert ql.die_cut_media(39, 48).name == "39x48"
    assert ql.die_cut_media(10, 10) is None and ql.continuous_media(13) is None


# --- PackBits --------------------------------------------------------------------------


def test_packbits_like_apple_tn1023():
    raw = bytes.fromhex("aaaaaa80002aaaaaaaaa80002a22aaaaaaaaaaaaaaaaaaaa")
    packed = bytes.fromhex("feaa0280002afdaa0380002a22f7aa")
    assert ql.packbits(raw) == packed
    assert ql.unpackbits(packed) == raw


@given(st.binary(max_size=600))
def test_packbits_roundtrip(data):
    assert ql.unpackbits(ql.packbits(data)) == data
    assert _unpack(ql.packbits(data)) == data


@given(st.binary(min_size=162, max_size=162))
def test_packed_lines_fit_the_length_byte(row):
    assert len(ql.packbits(row)) <= 164 < 256


def test_packbits_long_runs_and_literals():
    assert ql.packbits(b"") == b""
    assert ql.packbits(b"\x05" * 300) == b"\x81\x05\x81\x05\xd5\x05"
    literal = bytes(range(130))
    assert ql.packbits(literal) == b"\x7f" + literal[:128] + b"\x01" + literal[128:]
    assert ql.unpackbits(b"\x80\x00\x41") == b"A"  # 0x80 is a no-op


@pytest.mark.parametrize("data", [b"\x02AB", b"\xfe"])
def test_unpackbits_refuses_cut_off_data(data):
    with pytest.raises(ValueError):
        ql.unpackbits(data)


# --- status ----------------------------------------------------------------------------


def test_parse_status_fields():
    status = ql.parse_status(packet(0x38, width=29, kind=0x0B, length=90, status_type=6, phase=1))
    assert (status.model_code, status.media_width, status.media_type) == (0x38, 29, 0x0B)
    assert (status.media_length, status.status_type, status.phase) == (90, 6, 1)
    assert status.media.name == "29x90" and status.model.name == "QL-800"
    assert ql.status_error(status) is None


@pytest.mark.parametrize(
    "data",
    [b"", packet()[:31], packet() + b"\x00", b"\x81" + packet()[1:], b"hello" * 7],
)
def test_parse_status_refuses_garbage(data):
    with pytest.raises(PrinterError, match="unexpected status"):
        ql.parse_status(data)


@pytest.mark.parametrize(
    "fields, name",
    [
        ({"width": 62, "kind": 0x0A}, "62"),
        ({"width": 104, "kind": 0x0A}, "103"),
        ({"width": 62, "kind": 0x0B, "length": 100}, "62x100"),
        ({"width": 24, "kind": 0x0B, "length": 24}, "d24"),
        ({"width": 0, "kind": 0x00}, None),
        ({"width": 33, "kind": 0x0A}, None),  # not a known roll
        ({"width": 62, "kind": 0x01}, None),  # a P-touch tape
    ],
)
def test_media_from_status(fields, name):
    media = ql.parse_status(packet(**fields)).media
    assert (media.name if media else None) == name


@pytest.mark.parametrize(
    "err1, err2, cls",
    [
        (0x01, 0, OutOfLabels),
        (0x02, 0, OutOfLabels),
        (0x04, 0, PaperJam),
        (0x10, 0, PrinterBusy),
        (0x80, 0, PrinterError),
        (0, 0x01, WrongMedia),
        (0, 0x10, CoverOpen),
        (0, 0x40, OutOfPaper),  # on a continuous roll: its end
        (0, 0x80, PrinterError),
        (0x01, 0x10, OutOfLabels),  # the first one counts
    ],
)
def test_status_errors(err1, err2, cls):
    error = ql.status_error(ql.parse_status(packet(err1=err1, err2=err2)))
    assert type(error) is cls and error.details.startswith("status 802042")


def test_error_packet_without_a_reason_is_still_an_error():
    error = ql.status_error(ql.parse_status(packet(status_type=0x02)))
    assert type(error) is PrinterError


def test_unknown_model_code():
    assert ql.parse_status(packet(model=0x99)).model is None


class Chunks:
    """Answers in the given pieces, then a timeout."""

    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def sendall(self, data):
        pass

    def recv(self, size):
        if not self.chunks:
            raise TimeoutError("timed out")
        return self.chunks.pop(0)

    def settimeout(self, timeout):
        pass


def test_status_in_pieces_after_junk():
    whole = packet(width=29)
    s = ql._Session(Chunks(b"\x13\x80", whole[:5], whole[5:20], whole[20:] + packet()[:3]), False)
    assert s.read(1).media_width == 29
    assert s.buffer == packet()[:3]  # the start of the next one stays


def test_endless_junk_is_an_error():
    s = ql._Session(Chunks(*[b"\x55" * 1000] * 6), False)
    with pytest.raises(PrinterError, match="without a status"):
        s.read(1)


def test_closed_connection_is_a_timeout():
    s = ql._Session(Chunks(b""), False)
    with pytest.raises(PrinterTimeout, match="closed"):
        s.read(1)


def test_silent_printer_on_the_network_has_no_status():
    assert ql._Session(Chunks(), False).ask() is None
    with pytest.raises(PrinterTimeout):
        ql._Session(Chunks(), True).ask()  # on USB every QL answers


# --- models ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, name",
    [
        ("Brother QL-820NWB", "QL-820NWB"),
        ("Brother QL-720NW (USB)", "QL-720NW"),
        ("ql1110nwb", "QL-1110NWB"),
        ("QL-600", "QL-600"),
        ("Brother QL-9999", None),
        ("Brother VC-500W", None),
        ("", None),
    ],
)
def test_model_named(text, name):
    model = ql.model_named(text)
    assert (model.name if model else None) == name


def test_model_table():
    models = ql.MODELS
    assert not models["QL-500"].cutting and not models["QL-500"].expanded
    assert not models["QL-700"].mode_setting and not models["QL-700"].compression
    assert models["QL-800"].two_color and not models["QL-800"].compression
    assert models["QL-820NWB"].invalidate == 400 and models["QL-720NW"].invalidate == 200
    assert models["QL-1100"].bytes_per_row == 162 and models["QL-1100"].invalidate == 350
    assert not ql.NARROW.compression and not ql.NARROW.high_res
    assert ql.WIDE.bytes_per_row == 162
    assert all(name in models for name in ql.MODEL_CODES.values())


# --- prepare and preview ---------------------------------------------------------------


def test_preview_is_black_and_white_or_red(png_b64):
    image = Image.new("RGB", (4, 1))
    for x, colour in enumerate([(0, 0, 0), (200, 0, 0), (120, 120, 120), (230, 230, 230)]):
        image.putpixel((x, 0), colour)
    assert _pixels(ql.preview_image(image)) == [(0, 0, 0), (0, 0, 0), (0, 0, 0), (255,) * 3]
    two = ql.preview_image(image, "red")
    assert _pixels(two) == [(0, 0, 0), (255, 0, 0), (0, 0, 0), (255,) * 3]


def test_continuous_prepare_renders_the_printable_width():
    backend = BrotherQL()
    image = backend.prepare(_continuous(62), PrintOptions())
    assert image.height == 696 and image.info["tape_mm"] == 62
    high = backend.prepare(_continuous(62), PrintOptions(quality="high"))
    assert high.height == 1392 and high.info["quality"] == "high"
    assert high.width == pytest.approx(2 * image.width, abs=2)


@pytest.mark.parametrize("orientation", ["across", "along"])
def test_die_cut_prepare_in_printer_orientation(orientation):
    image = BrotherQLDieCut().prepare(_die_cut(62, 29, orientation), PrintOptions())
    assert image.size == (732, 343) and image.info["size_mm"] == (62, 29)


# --- the backend against FakeQL ---------------------------------------------------------


def test_query_continuous_roll(printer):
    info = BrotherQL().query("ql.local")
    assert info.model == "QL-820NWB" and info.tape_mm == 62 and info.state == "IDLE"
    assert [m.id for m in info.modes] == ["black", "red"]  # a QL-8xx with a 62 mm roll
    assert info.qualities == ("standard", "high")
    assert info.cassette.printable_mm == 58.9 and not info.error
    assert BrotherQL().display_name(info) == "Brother QL-820NWB"
    # no ESC @ in a status query: it would stop a print
    assert printer.device.nuls == 400 and printer.device.inits == 0
    assert printer.links[0].closed and printer.calls == [("ql.local", 9100, usb.BUSY_WAIT)]


def test_query_other_rolls_and_models(printer):
    device = printer.device
    device.model, device.width = 0x37, 29  # QL-720NW
    info = BrotherQL().query("ql.local")
    assert (
        info.model == "QL-720NW" and info.tape_mm == 29 and [m.id for m in info.modes] == ["black"]
    )
    device.model = 0x99  # unknown: no high resolution, a generic name
    info = BrotherQL().query("ql.local")
    assert info.model == "QL" and info.qualities == ("standard",)
    assert BrotherQL().display_name(info) == "Brother QL"


def test_query_die_cut_labels(printer):
    device = printer.device
    device.kind, device.width, device.length = 0x0B, 29, 90
    info = BrotherQLDieCut().query("ql.local")
    assert info.tape_mm is None and info.pitch_mm == 93.0 and not info.error
    assert info.cassette.printable_mm == 25.9
    # the same roll on the continuous side: the wrong kind
    assert BrotherQL().query("ql.local").error == "wrong_media"
    device.kind, device.length = 0x0A, 0
    assert BrotherQLDieCut().query("ql.local").error == "wrong_media"


def test_query_reports_problems_and_busy(printer):
    printer.device.err2 = 0x10
    info = BrotherQL().query("ql.local")
    assert info.state == "ERROR" and info.error == "cover_open"
    printer.device.err2 = 0
    printer.device.busy = 1
    assert BrotherQL().query("ql.local").state == "BUSY"


def test_query_a_printer_that_does_not_answer_on_the_network(printer):
    printer.device.silent = True
    info = BrotherQL().query("ql.local")
    assert info.state == "UNKNOWN" and info.tape_mm is None and not info.error


def _sent_rows(page) -> Image.Image:
    """The black rows of a page as an image (1 = printed), unmirrored."""
    rows = page["black"]
    image = Image.frombytes("1", (len(rows[0]) * 8, len(rows)), b"".join(rows))
    return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)


def test_print_continuous_label(printer):
    backend = BrotherQL()
    label = _continuous(62, auto_length=False, length_mm=40, background="#000000")
    options = PrintOptions(copies=2, cut="full")
    image = backend.prepare(label, options)
    events = []
    assert backend.print_label(image, "ql.local", options, events.append) is None
    device = printer.device
    assert device.printed() == 2 and [e.copy for e in events] == [1, 2, 2]
    page = device.pages[0]
    assert len(page["black"]) == image.width  # one line per dot along the roll
    assert page["compressed"]  # the QL-820NWB takes PackBits
    assert ESC + b"iM\x40" in page["commands"] and ESC + b"iK\x08" in page["commands"]
    sent = _sent_rows(page)
    # the black background fills exactly the printable 696 dots, 12 from the right
    assert sent.getbbox() == (720 - 12 - 696, 0, 720 - 12, image.width)
    assert printer.calls[0][2] == usb.PRINT_WAIT


@pytest.mark.parametrize(
    "cut, commands",
    [
        ("end", [ESC + b"iM\x00", ESC + b"iK\x08"]),
        ("none", [ESC + b"iM\x00", ESC + b"iK\x00"]),
        ("half", [ESC + b"iM\x40", ESC + b"iA\x01"]),  # not on a QL: a full cut
    ],
)
def test_cut_modes(printer, cut, commands):
    backend = BrotherQL()
    options = PrintOptions(cut=cut)
    backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert all(c in printer.device.pages[0]["commands"] for c in commands)


def test_print_two_colours(printer):
    backend = BrotherQL()
    label = _continuous(62, elements=[TextElement(text="R", color="#d00000")])
    options = PrintOptions(mode="red")
    assert backend.print_label(backend.prepare(label, options), "ql.local", options) is None
    page = printer.device.pages[0]
    assert ESC + b"iK\x09" in page["commands"]
    assert any(any(row) for row in page["red"]) and not any(any(r) for r in page["black"])
    assert len(page["red"]) == len(page["black"])


def test_two_colours_without_the_red_roll_print_black(printer):
    printer.device.width = 29
    backend = BrotherQL()
    options = PrintOptions(mode="red")
    image = backend.prepare(_continuous(29), options)
    assert backend.print_label(image, "ql.local", options) == PrintResult(mode="black")
    assert not printer.device.pages[0]["red"]


def test_print_high_resolution(printer):
    backend = BrotherQL()
    options = PrintOptions(quality="high")
    image = backend.prepare(_continuous(62, auto_length=False, length_mm=30), options)
    backend.print_label(image, "ql.local", options)
    page = printer.device.pages[0]
    assert ESC + b"iK\x48" in page["commands"]
    assert len(page["black"]) == image.width  # 600 lines per inch along the roll
    assert len(page["black"][0]) == 90  # still 300 dpi across


def test_high_resolution_on_a_model_without_it(printer):
    printer.device.model = 0x99  # unknown model: 300 dpi only
    backend = BrotherQL()
    options = PrintOptions(quality="high")
    image = backend.prepare(_continuous(62, auto_length=False, length_mm=30), options)
    backend.print_label(image, "ql.local", options)
    page = printer.device.pages[0]
    assert len(page["black"]) == image.width // 2
    assert not page["compressed"]  # an unknown model gets no PackBits


def test_short_labels_are_extended_to_the_model_minimum(printer):
    printer.device.model = 0x4F  # QL-500: at least 295 lines
    backend = BrotherQL()
    image = Image.new("RGB", (100, 696), "black")
    backend.print_label(image, "ql.local", PrintOptions())
    rows = printer.device.pages[0]["black"]
    assert len(rows) == 295
    assert not any(rows[0]) and any(rows[150])  # white added at both ends


def test_too_long_label(printer):
    backend = BrotherQL()
    with pytest.raises(LabelTooLong):
        backend.print_label(Image.new("RGB", (12000, 696)), "ql.local", PrintOptions())
    assert not printer.calls  # refused before connecting
    plan_image = Image.new("RGB", (11900, 696))  # within 1 m + slack, longer than 11811
    with pytest.raises(LabelTooLong) as raised:
        backend.print_label(plan_image, "ql.local", PrintOptions())
    assert raised.value.params["max_mm"] == pytest.approx(1000, abs=0.1)


def test_another_width_loaded_is_scaled_to_it(printer):
    printer.device.width = 29
    backend = BrotherQL()
    image = backend.prepare(_continuous(62, background="#000000"), PrintOptions())
    backend.print_label(image, "ql.local", PrintOptions())
    sent = _sent_rows(printer.device.pages[0])
    assert sent.getbbox()[2] - sent.getbbox()[0] == 306
    assert len(printer.device.pages[0]["black"]) == round(image.width * 306 / 696)


def test_die_cut_roll_for_a_continuous_label(printer):
    printer.device.kind, printer.device.length = 0x0B, 29
    backend = BrotherQL()
    with pytest.raises(WrongMedia):
        backend.print_label(
            backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions()
        )
    assert printer.device.printed() == 0


def test_image_for_no_roll(printer):
    with pytest.raises(UnsupportedSize):
        BrotherQL().print_label(Image.new("RGB", (300, 123)), "ql.local", PrintOptions())


def test_print_die_cut_label(printer):
    device = printer.device
    device.kind, device.length = 0x0B, 29
    backend = BrotherQLDieCut()
    label = _die_cut(62, 29)
    label.background = "#000000"
    image = backend.prepare(label, PrintOptions())
    backend.print_label(image, "ql.local", PrintOptions())
    page = device.pages[0]
    assert len(page["black"]) == 271  # the printable length
    assert ESC + b"iz\xce\x0b\x3e\x1d\x0f\x01\x00\x00\x00\x00" in page["commands"]
    assert _sent_rows(page).getbbox() == (12, 0, 708, 271)


def test_die_cut_other_size_loaded_is_fitted(printer):
    device = printer.device
    device.kind, device.width, device.length = 0x0B, 29, 90
    backend = BrotherQLDieCut()
    label = _die_cut(62, 29)
    label.background = "#000000"
    backend.print_label(backend.prepare(label, PrintOptions()), "ql.local", PrintOptions())
    rows = device.pages[0]["black"]
    assert len(rows) == 991 and len(rows[0]) == 90


def test_die_cut_without_status_uses_the_label_size(printer):
    printer.device.silent = True
    backend = BrotherQLDieCut()
    events = []
    image = backend.prepare(_die_cut(29, 90, "along"), PrintOptions(quality="high"))
    backend.print_label(image, "ql.local", PrintOptions(copies=3), events.append)
    page = printer.device.pages[0]
    assert ESC + b"iz\xce\x0b\x1d\x5a\xdf\x03\x00\x00\x00\x00" in page["commands"]  # 991
    assert [e.copy for e in events] == [1, 3]


def test_die_cut_size_without_a_label(printer):
    image = BrotherQLDieCut().prepare(_die_cut(50, 25), PrintOptions())
    with pytest.raises(UnsupportedSize):
        BrotherQLDieCut().print_label(image, "ql.local", PrintOptions())
    with pytest.raises(LabelTooWide):
        BrotherQLDieCut().print_label(Image.new("RGB", (1300, 300)), "ql.local", PrintOptions())


def test_waits_for_another_job(printer):
    printer.device.busy = 3
    backend = BrotherQL()
    backend.print_label(backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions())
    assert printer.device.printed() == 1 and printer.sleeps == [ql.POLL_INTERVAL] * 3


def test_busy_for_too_long(printer, monkeypatch):
    printer.device.busy = 10**6
    backend = BrotherQL()
    with pytest.raises(PrinterBusy):
        backend.print_label(
            backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions()
        )
    assert printer.device.printed() == 0


def test_error_before_printing(printer):
    printer.device.err1 = 0x01
    with pytest.raises(OutOfLabels):
        BrotherQL().print_label(Image.new("RGB", (300, 696)), "ql.local", PrintOptions())
    assert printer.device.printed() == 0 and printer.links[0].closed


def test_error_while_printing_says_how_many_were_printed(printer):
    printer.device.fail_on_page = 1
    backend = BrotherQL()
    options = PrintOptions(copies=3)
    with pytest.raises(PaperJam) as raised:
        backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert raised.value.params == {"printed": 1, "copies": 3}


def test_a_printer_that_stops_reporting(printer, monkeypatch):
    device = printer.device
    backend = BrotherQL()
    options = PrintOptions(copies=2)
    image = backend.prepare(_continuous(), options)
    real_done = device._page_done

    def only_first(last):
        real_done(last)
        if len(device.pages) == 2:
            # the answer to the job's status request, the first page's notices
            device.out = device.out[: 32 * 3]
            device.busy = 1  # asked once more, the printer is still printing

    monkeypatch.setattr(device, "_page_done", only_first)
    with pytest.raises(PrinterTimeout) as raised:
        backend.print_label(image, "ql.local", options)
    assert raised.value.params == {"printed": 1, "copies": 2}
    assert device.requests == 3  # the query, the job's own, once more


def test_notices_stop_but_the_printer_is_done(printer, monkeypatch):
    device = printer.device
    backend = BrotherQL()
    options = PrintOptions(copies=2)
    image = backend.prepare(_continuous(), options)
    real_done = device._page_done

    def only_first(last):
        real_done(last)
        if len(device.pages) == 2:
            device.out = device.out[: 32 * 3]

    monkeypatch.setattr(device, "_page_done", only_first)
    events = []
    backend.print_label(image, "ql.local", options, events.append)  # waiting, no error
    assert events[-1].copy == 2 and device.requests == 3


def test_a_printer_without_notices(printer):
    printer.device.notify = False
    backend = BrotherQL()
    events = []
    backend.print_label(
        backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions(), events.append
    )
    assert printer.device.printed() == 1 and [e.copy for e in events] == [1, 1]


def test_connection_errors(monkeypatch):
    def refuse(host, port, wait=None):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(ql, "_connect", refuse)
    with pytest.raises(PrinterError) as raised:
        BrotherQL().query("ql.local")
    assert raised.value.code == "port_closed"
    assert type(ql.map_error(OSError("unreachable"))) is PrinterNotFound
    assert type(ql.map_error(ValueError("cannot resolve x"))) is PrinterNotFound
    assert type(ql.map_error(ValueError("odd"))) is PrinterError


# --- discovery and models --------------------------------------------------------------


def test_discover_on_usb_and_the_network(tmp_path, monkeypatch, printer):
    root = tmp_path / "usbmisc"
    for name, ieee in (
        ("lp0", "MFG:Brother;CMD:PT-CBP;MDL:QL-820NWB;SERN:000F1Z;"),
        ("lp1", "MFG:Brother;MDL:VC-500W;"),  # another Brother: not this backend
        ("lp2", "MFG:cab;MDL:EOS2/300;"),
    ):
        (root / name / "device").mkdir(parents=True)
        (root / name / "device" / "ieee1284_id").write_text(ieee)
    monkeypatch.setattr(usb, "SYSFS", root)
    monkeypatch.setattr(usb, "DEV", Path("/dev/usb"))
    seen = []
    services = {
        "_pdl-datastream._tcp": [FoundPrinter("Brother QL-720NW", "QL720.local", "192.0.2.5")],
        "_printer._tcp": [
            FoundPrinter("Brother QL-720NW", "QL720.local", "192.0.2.5"),
            FoundPrinter("Brother QL-9999", "QL9.local", "192.0.2.6"),
        ],
    }

    def browse(service_type, match, timeout):
        seen.append(service_type)
        return [p for p in services[service_type] if match(p.name)]

    monkeypatch.setattr(mdns, "browse", browse)
    backend = BrotherQL()
    found = backend.discover(timeout=0.01)
    assert [f.name for f in found] == [
        "Brother QL-820NWB (USB)",
        "Brother QL-720NW",
    ]
    assert found[0].host == "usb://Brother/QL-820NWB?serial=000F1Z"
    assert seen == ["_pdl-datastream._tcp", "_printer._tcp"]
    # the model found on the network wins over the status code
    assert backend.query("QL720.local").model == "QL-720NW"
    assert backend.model_for("usb://Brother/QL-1100?serial=1", None).name == "QL-1100"
    assert backend.model_for("usb://Brother/LabelWriter", None) is None


# --- contract ----------------------------------------------------------------------------


class TestContract(BackendContract):
    backend_id = "brother_ql"

    @pytest.fixture
    def harness(self, printer, monkeypatch):
        found = [FoundPrinter("Brother QL-820NWB", "QL820.local", "192.0.2.7")]
        monkeypatch.setattr(mdns, "browse", lambda service_type, match, timeout: found)

        def fail():
            printer.device.err2 = 0x10

        return SimpleNamespace(backend=BrotherQL(), fail=fail, printed=printer.device.printed)


class TestDieCutContract(BackendContract):
    backend_id = "brother_ql_diecut"

    @pytest.fixture
    def harness(self, printer, monkeypatch):
        monkeypatch.setattr(mdns, "browse", lambda service_type, match, timeout: [])
        device = printer.device
        device.kind, device.length = 0x0B, 29

        def fail():
            device.err1 = 0x04

        return SimpleNamespace(backend=BrotherQLDieCut(), fail=fail, printed=device.printed)


# --- a fake printer behind real transports ----------------------------------------------


@pytest.fixture
def tcp_printer(monkeypatch):
    """FakeQL behind a TCP server on 127.0.0.1 (never the LAN)."""
    device = FakeQL(model=0x37)  # QL-720NW
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(0.05)
                while not stop.is_set():
                    try:
                        data = conn.recv(65536)
                    except TimeoutError:
                        data = None
                    if data == b"":
                        break
                    if data:
                        device.feed(data)
                    if device.out:
                        out, device.out = device.out, b""
                        conn.sendall(out)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(ql, "_connect", REAL_CONNECT)
    yield SimpleNamespace(device=device, port=server.getsockname()[1])
    stop.set()
    server.close()
    thread.join(5)


def test_over_tcp(tcp_printer):
    backend = BrotherQL(port=tcp_printer.port)
    info = backend.query("127.0.0.1")
    assert info.model == "QL-720NW" and info.tape_mm == 62
    options = PrintOptions(copies=2)
    events = []
    backend.print_label(
        backend.prepare(_continuous(), options), "127.0.0.1", options, events.append
    )
    assert tcp_printer.device.printed() == 2 and {e.copy for e in events} == {1, 2}


@pytest.fixture
def usb_printer(monkeypatch):
    """FakeQL behind a pseudo terminal that stands in for /dev/usb/lp0."""
    master, slave = os.openpty()
    tty.setraw(slave)
    path = Path(os.ttyname(slave))
    device = FakeQL(model=0x99, width=29)  # the model only from the USB device ID
    stop = threading.Event()

    def serve():
        import select

        while not stop.is_set():
            if select.select([master], [], [], 0.05)[0]:
                try:
                    device.feed(os.read(master, 65536))
                except OSError:
                    return
            if device.out:
                out, device.out = device.out, b""
                os.write(master, out)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    printer = usb.UsbPrinter(path, "Brother", "QL-820NWB", "000F1Z")
    monkeypatch.setattr(usb, "find", lambda address: printer)
    monkeypatch.setattr(ql, "_connect", REAL_CONNECT)
    yield SimpleNamespace(device=device, address=printer.address)
    stop.set()
    thread.join(5)
    os.close(master)
    os.close(slave)


def test_over_usb(usb_printer):
    backend = BrotherQL()
    info = backend.query(usb_printer.address)
    assert info.model == "QL-820NWB" and info.tape_mm == 29 and info.state == "IDLE"
    options = PrintOptions(cut="end")
    backend.print_label(backend.prepare(_continuous(29), options), usb_printer.address, options)
    page = usb_printer.device.pages[0]
    assert page["compressed"] and ESC + b"iK\x08" in page["commands"]


def test_no_time_left_for_a_status():
    with pytest.raises(PrinterTimeout, match="no status within 0 s"):
        ql._Session(Chunks(packet()), False).read(0)


def test_model_maximum_length():
    image = Image.new("RGB", (696, 11812))
    with pytest.raises(LabelTooLong) as raised:
        ql._length_checked(image, ql.MODELS["QL-700"], high_res=False)
    assert raised.value.params["max_mm"] == pytest.approx(999.99, abs=0.01)
    assert ql._length_checked(image, ql.MODELS["QL-1100"], high_res=False) is image


def test_die_cut_image_without_its_size(printer):
    printer.device.kind, printer.device.length = 0x0B, 29
    BrotherQLDieCut().print_label(Image.new("RGB", (732, 343)), "ql.local", PrintOptions())
    assert len(printer.device.pages[0]["black"]) == 271


# --- what Brother's references say (review against them) ------------------------------


@pytest.mark.parametrize(
    "kind, name",
    [(0x0A, "62"), (0x4A, "62"), (0x0B, "62x29"), (0x4B, "62x29"), (0x1A, None)],
)
def test_both_media_type_codes(kind, name):
    """QL-1100 and the old series report 0x0A/0x0B, the QL-710W, QL-720NW
    and QL-800 series 0x4A/0x4B."""
    length = 29 if kind & 0x0F == 0x0B else 0
    media = ql.parse_status(packet(kind=kind, length=length)).media
    assert (media.name if media else None) == name


def test_high_resolution_models():
    with_600 = {name for name, model in ql.MODELS.items() if model.high_res}
    assert with_600 == {
        "QL-570",
        "QL-580N",
        "QL-700",
        "QL-710W",
        "QL-720NW",
        "QL-800",
        "QL-810W",
        "QL-820NWB",
        "QL-1100",
        "QL-1100NWB",
        "QL-1110NWB",
        "QL-1115NWB",
    }


@pytest.mark.parametrize(
    "width, length, name",
    [
        (60, 86, "60x86"),
        (60, 87, "60x86"),
        (102, 152, "102x152"),
        (102, 153, "102x152"),
        (38, 90, "38x90"),
    ],
)
def test_die_cut_lengths_of_all_references(width, length, name):
    assert ql.die_cut_media(width, length).name == name


def test_label_sizes_as_on_the_box():
    sizes = BrotherQLDieCut.capabilities.label_sizes
    assert (60, 86) in [(s.width_mm, s.height_mm) for s in sizes]
    assert (102, 152) in [(s.width_mm, s.height_mm) for s in sizes]


def test_print_info_uses_the_reported_length(printer):
    device = printer.device
    device.model, device.kind, device.width, device.length = 0x43, 0x0B, 60, 87  # QL-1100
    backend = BrotherQLDieCut()
    label = _die_cut(60, 86)
    label.background = "#000000"
    backend.print_label(backend.prepare(label, PrintOptions()), "ql.local", PrintOptions())
    info = next(c for c in device.pages[0]["commands"] if c.startswith(ESC + b"iz"))
    assert info[5:7] == bytes((60, 87))
    assert _sent_rows(device.pages[0]).getbbox()[2] == 1296 - 68  # right margin 68 pins


def test_ninety_mm_roll_on_the_wide_models(printer):
    device = printer.device
    device.model, device.kind, device.width = 0x44, 0x0A, 90  # QL-1110NWB
    info = BrotherQL().query("ql.local")
    assert info.tape_mm == 90 and 90 in BrotherQL.capabilities.tape_widths_mm


@pytest.mark.parametrize(
    "model, media, offset",
    [
        ("QL-1100", "103", 38),
        ("QL-1100", "62", 56),
        ("QL-1050", "12", 74),
        ("QL-820NWB", "60x86", 24),
        ("QL-820NWB", "54x29", 59),
        ("QL-700", "23x23", 42),
    ],
)
def test_right_margins_from_brother(model, media, offset):
    found = next(m for m in ql.CONTINUOUS + ql.DIE_CUT if m.name == media)
    assert found.offset(ql.MODELS[model]) == offset
    assert ql.die_cut_media(23, 23).dots == (236, 202)


def test_feed_margin_of_12_mm_round_labels():
    d12 = ql.die_cut_media(12, 12)
    assert d12.margin(ql.MODELS["QL-700"]) == 35
    assert d12.margin(ql.MODELS["QL-580N"]) == 35
    assert d12.margin(ql.MODELS["QL-820NWB"]) == 0
    assert d12.margin(ql.MODELS["QL-1100"]) == 0


def test_no_compression_on_the_ql650td_and_invalidate_lengths():
    assert not ql.MODELS["QL-650TD"].compression
    job = _job(ql.MODELS["QL-1100"], ql.continuous_media(102), rows=[bytes(162)])
    assert job.startswith(ESC + b"ia\x01" + b"\x00" * 350 + ESC + b"@")


def test_quality_first_not_with_two_colours():
    rows = [bytes(90)]
    red = ql.build_job(ql.Job(ql.MODELS["QL-800"], ql.RED_ROLL, rows, rows))
    black = ql.build_job(ql.Job(ql.MODELS["QL-800"], ql.RED_ROLL, rows))
    assert ESC + b"iz\x8e" in red and ESC + b"iz\xce" in black


def test_end_of_continuous_roll_and_turned_off():
    end = ql.status_error(ql.parse_status(packet(err2=0x40, kind=0x4A)))
    assert type(end) is OutOfPaper
    stuck = ql.status_error(ql.parse_status(packet(err2=0x40, kind=0x4B, length=29)))
    assert type(stuck) is PaperJam
    off = ql.status_error(ql.parse_status(packet(status_type=0x04)))
    assert type(off) is PrinterError and "turned off" in off.details


def test_shared_model_code_is_the_ql500():
    assert ql.parse_status(packet(model=0x4F)).model.name == "QL-500"
    info = BrotherQL().info(ql.MODELS["QL-500"], ql.parse_status(packet(model=0x4F)))
    assert BrotherQL().cut_modes(info) == ()  # no cutter: no cut row
    assert BrotherQL().cut_modes(None) == ("full", "end", "none")
    ql550 = BrotherQL().info(ql.MODELS["QL-550"], ql.parse_status(packet(model=0x4F)))
    assert BrotherQL().cut_modes(ql550) == ("full", "end", "none")


def test_both_backends_share_the_settings_and_the_search(printer, monkeypatch):
    assert BrotherQL().settings_id == BrotherQLDieCut().settings_id == "brother_ql"
    found = [FoundPrinter("Brother QL-720NW", "QL720.local", "192.0.2.5")]
    monkeypatch.setattr(mdns, "browse", lambda service_type, match, timeout: found)
    BrotherQL().discover(timeout=0.01)
    assert BrotherQLDieCut().query("QL720.local").model == "QL-720NW"


def test_the_other_kind_of_roll_offers_the_other_backend(printer):
    device = printer.device
    device.kind, device.length = 0x4B, 29
    info = BrotherQL().query("ql.local")
    assert info.error == "wrong_media" and info.media_backend == "brother_ql_diecut"
    die_cut = BrotherQLDieCut().query("ql.local")
    assert not die_cut.media_backend and die_cut.label_mm == (62, 29)
    device.kind, device.length = 0x4A, 0
    assert BrotherQLDieCut().query("ql.local").media_backend == "brother_ql"


def test_the_red_and_black_roll_needs_a_two_colour_job(printer):
    device = printer.device
    device.red_roll = True
    backend = BrotherQL()
    options = PrintOptions(mode="black")
    result = backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert result == PrintResult(mode="red", reason="roll")
    assert device.refused == 1 and device.printed() == 1
    assert ESC + b"iK\x09" in device.pages[0]["commands"]
    # remembered: only red and black from now on, no second refusal
    assert [m.id for m in backend.query("ql.local").modes] == ["red"]
    backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert device.refused == 1 and device.printed() == 2


def test_a_plain_roll_with_red_and_black_chosen(printer):
    device = printer.device
    device.red_roll = False
    backend = BrotherQL()
    options = PrintOptions(mode="red", quality="high")
    result = backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert result == PrintResult(mode="black", reason="roll")
    assert ESC + b"iK\x48" in device.pages[0]["commands"]  # 600 dpi again in black
    assert [m.id for m in backend.query("ql.local").modes] == ["black"]


def test_a_refused_job_on_another_roll_is_an_error(printer):
    device = printer.device
    device.model, device.red_roll = 0x37, True  # a QL-720NW never prints two colours
    backend = BrotherQL()
    with pytest.raises(WrongMedia):
        backend.print_label(
            backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions()
        )
    assert device.refused == 1


def test_red_on_a_model_without_two_colours():
    plan = SimpleNamespace(two_color=False, model=ql.MODELS["QL-720NW"], media=ql.RED_ROLL)
    assert ql._result(PrintOptions(mode="red"), plan) == PrintResult(mode="black")
    assert ql._result(PrintOptions(mode="vivid"), plan) is None


class Pipe:
    """A socket whose other end reads slowly or not at all."""

    def __init__(self, read: bool):
        self.ours, self.theirs = socket.socketpair()
        self.ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.got = bytearray()
        if read:
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while chunk := self.theirs.recv(65536):
            self.got += chunk

    def close(self):
        self.ours.close()
        self.theirs.close()


def test_send_in_pieces():
    pipe = Pipe(read=True)
    data = bytes(range(256)) * 2000
    ql._Session(pipe.ours, False).send(data)
    pipe.ours.shutdown(socket.SHUT_WR)
    for _ in range(200):
        if len(pipe.got) == len(data):
            break
        threading.Event().wait(0.01)
    assert bytes(pipe.got) == data
    pipe.close()


def test_a_printer_with_a_full_buffer_stalls():
    pipe = Pipe(read=False)
    from etiketto.backends.base import PrinterStalled

    with pytest.raises(PrinterStalled):
        net.send_all(pipe.ours, bytes(10_000_000), 0.2, ql.MIN_RATE)
    pipe.close()


def test_sessions_on_tcp_send_in_pieces(monkeypatch):
    sent = []
    pipe = Pipe(read=True)
    monkeypatch.setattr(net, "send_all", lambda link, data, timeout, rate: sent.append(len(data)))
    ql._Session(pipe.ours, False).send(b"job")
    assert sent == [3]
    pipe.close()


def test_no_answer_when_asked_whether_the_job_is_done():
    assert not ql._finished(ql._Session(Chunks(), False))


# --- the red and black roll: retries, memory, the status' hint ----------------------


@pytest.mark.parametrize("red_roll, mode, used", [(True, "black", "red"), (False, "red", "black")])
def test_retry_with_copies(printer, red_roll, mode, used):
    """Every page of the refused job is refused: those notices must not be
    taken for the retry's."""
    device = printer.device
    device.red_roll = red_roll
    backend = BrotherQL()
    options = PrintOptions(copies=3, mode=mode)
    events = []
    result = backend.print_label(
        backend.prepare(_continuous(), options), "ql.local", options, events.append
    )
    assert result == PrintResult(mode=used, reason="roll")
    assert device.refused == 3 and device.printed() == 3
    assert [e.copy for e in events] == [1, 2, 3, 3]
    assert ql._two_color_rolls == {"ql.local": red_roll}


def test_a_retry_refused_too_forgets_the_roll(printer):
    device = printer.device
    ql._two_color_rolls["ql.local"] = False

    def refuse(last):
        device.refused += 1
        device.page = device._new_page()
        device.out += packet(device.model, 0, 0x01, 62, 0x4A, 0, 0x02)

    device._page_done = refuse
    backend = BrotherQL()
    with pytest.raises(WrongMedia):
        backend.print_label(
            backend.prepare(_continuous(), PrintOptions()), "ql.local", PrintOptions()
        )
    assert device.refused == 2 and "ql.local" not in ql._two_color_rolls


def test_the_roll_is_remembered_only_once_a_label_came_out(printer):
    printer.device.notify = False
    backend = BrotherQL()
    options = PrintOptions(mode="red")
    backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert ql._two_color_rolls == {}


def test_status_hint_for_the_red_and_black_roll(printer):
    device = printer.device
    device.red_roll, device.colour = True, 0x81
    backend = BrotherQL()
    assert ql.parse_status(device.status()).red_roll_hint
    # the hint only preselects red and black; the dialog's choice wins
    assert [m.id for m in backend.query("ql.local").modes] == ["red", "black"]
    options = PrintOptions(mode="red")
    assert (
        backend.print_label(backend.prepare(_continuous(), options), "ql.local", options) is None
    )
    assert device.refused == 0
    ql._two_color_rolls.clear()
    options = PrintOptions(mode="black")
    result = backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert result == PrintResult(mode="red", reason="roll") and device.refused == 1
    # what a printed job showed wins over the hint
    ql._two_color_rolls["ql.local"] = False
    assert [m.id for m in backend.query("ql.local").modes] == ["black"]


def test_ql1050_invalidates_with_350_bytes():
    assert ql.MODELS["QL-1050"].invalidate == 350 and ql.MODELS["QL-1060N"].invalidate == 200


def test_notices_before_the_jobs_own_answer_are_skipped():
    stale = packet(err2=0x01, status_type=0x02)  # the refused job's next page
    s = ql._Session(Chunks(stale, packet(), packet(status_type=0x01)), True)
    assert ql._wait_done(s, None, 1, sync=True) == 1
    # a first job does not skip: the same error is its own
    s = ql._Session(Chunks(stale, packet(), packet(status_type=0x01)), True)
    with pytest.raises(WrongMedia):
        ql._wait_done(s, None, 1)


def test_retry_without_its_own_answer_asks_once_more():
    """Packets were skipped and the job's answer never came: an error the
    printer then reports is raised, not taken as success."""
    notice = packet(status_type=0x06, phase=1)
    s = ql._Session(Chunks(notice, b"", packet(err1=0x04)), True)
    with pytest.raises(PaperJam):
        ql._wait_done(s, None, 2, sync=True)
    # nothing wrong when asked: as before, no notices counted means "sent"
    s = ql._Session(Chunks(notice, b"", packet()), True)
    assert ql._wait_done(s, None, 2, sync=True) == 0


def test_first_job_on_a_printer_without_the_jobs_answer(printer):
    """A printer that answers the session's status request but not the one
    inside a job: a first job counts its notices as before."""
    device = printer.device
    device.answer_in_job = False
    backend = BrotherQL()
    options = PrintOptions(copies=3)
    events = []
    backend.print_label(
        backend.prepare(_continuous(), options), "ql.local", options, events.append
    )
    assert device.printed() == 3 and [e.copy for e in events] == [1, 2, 3, 3]
    device.fail_on_page = 4  # the second page of the next job
    with pytest.raises(PaperJam) as raised:
        backend.print_label(backend.prepare(_continuous(), options), "ql.local", options)
    assert raised.value.params == {"printed": 1, "copies": 3}
