"""Brother QL label printers (QL-500 to QL-1115NWB): Brother's raster
language on TCP port 9100 or USB (see docs/brother-ql.md).

The protocol knowledge comes from brother_ql by Philipp Klaus and its fork
by LunarEclipse (both GPL-3.0); this module is written anew from notes on
them, no code is copied. Two backends share it: continuous rolls (the
label grows with its content) and die-cut labels (fixed sizes), as the
interface designs these two differently. The loaded roll is read from the
printer's 32-byte status before every job.
"""

from __future__ import annotations

import re
import socket
import time
from abc import abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageChops

from tavletta.backends import mdns, net, usb
from tavletta.backends.base import (
    Backend,
    Capabilities,
    CassetteInfo,
    CoverOpen,
    FoundPrinter,
    LabelSize,
    LabelTooLong,
    OutOfLabels,
    OutOfPaper,
    PaperJam,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    Raster,
    UnsupportedSize,
    WrongMedia,
    check_length,
    check_width,
    map_error,
    open_session,
    report_progress,
)
from tavletta.model import Label
from tavletta.render import DEFAULT_SAFE_MM, mm_to_px

DEFAULT_PORT = 9100
DPI = 300  # across the print head; 600 along the feed in high resolution
SOCKET_TIMEOUT = 10.0  # connecting, sending; on USB also without progress
STATUS_WAIT = 3.0  # for the answer to a status request
FIRST_NOTICE = 10.0  # for the first notice after a job, like brother_ql
PAGE_TIMEOUT = 60.0  # between two notices while printing
BUSY_TIMEOUT = 180.0  # waiting for another job to finish
POLL_INTERVAL = 0.5
MAX_JUNK = 4096  # bytes before a status packet that are not one

ESC = b"\x1b"
STATUS_SIZE = 32
STATUS_HEAD = b"\x80\x20\x42"  # print head mark, size, Brother
STATUS_REQUEST = ESC + b"iS"
# NULs clear a half-received command; no ESC @ here, it would stop a print
QUERY = b"\x00" * 400 + STATUS_REQUEST
# a QL takes a job on TCP at printing pace (90 bytes a line, hundreds of
# lines a second); a job that trickles in far slower counts as stalled
MIN_RATE = 4_000

# status type (byte 18) and phase (byte 19)
REPLY, COMPLETED, ERROR_OCCURRED, TURNED_OFF = 0x00, 0x01, 0x02, 0x04
NOTIFICATION, PHASE_CHANGE = 0x05, 0x06
PHASE_WAITING, PHASE_PRINTING = 0x00, 0x01
# media type in the print information; the status of the QL-710W, QL-720NW
# and QL-800 series reports 0x4A and 0x4B instead
CONTINUOUS_TYPE, DIE_CUT_TYPE = 0x0A, 0x0B
CONTINUOUS_TYPES, DIE_CUT_TYPES = (0x0A, 0x4A), (0x0B, 0x4B)

BLACK = PrintMode("black", DPI)
RED = PrintMode("red", DPI)  # red and black on DK-22251 (QL-8xx only)
QUALITIES = ("standard", "high")

_sleep = time.sleep  # replaced in tests
_clock = time.monotonic


# --- models -----------------------------------------------------------------------


@dataclass(frozen=True)
class Model:
    """What a QL model takes; numbers in dots at 300 dpi (docs/brother-ql.md)."""

    name: str
    min_dots: int = 150  # shortest label along the feed
    max_dots: int = 11811  # longest label (1 m)
    wide: bool = False  # 1296 pins (162 bytes a line) instead of 720 (90)
    mode_setting: bool = True  # knows ESC i a (else always in raster mode)
    cutting: bool = True
    expanded: bool = True  # knows ESC i K (cut at end, 600 dpi, two colours)
    compression: bool = True  # takes PackBits lines over USB and the network
    two_color: bool = False
    invalidate: int = 200  # NULs that clear the command buffer
    high_res: bool = True  # 600 dpi along the feed (a bit of ESC i K)
    round12_margin: bool = False  # 35 dots feed margin for 12 mm round labels

    @property
    def bytes_per_row(self) -> int:
        return 162 if self.wide else 90


def _old(name: str, min_dots: int = 150, **flags) -> Model:
    """The QL-500 to QL-700: no ESC i a, no PackBits, the feed margin of
    12 mm round labels."""
    flags.setdefault("high_res", False)
    return Model(
        name, min_dots, mode_setting=False, compression=False, round12_margin=True, **flags
    )


def _wide(name: str, min_dots: int, max_dots: int, **flags) -> Model:
    return Model(name, min_dots, max_dots, wide=True, **flags)


def _two_color(name: str, compression: bool = True) -> Model:
    return Model(name, compression=compression, two_color=True, invalidate=400)


MODELS = {
    model.name: model
    for model in (
        _old("QL-500", 295, cutting=False, expanded=False),
        _old("QL-550", 295),
        _old("QL-560", 295),
        _old("QL-570", high_res=True),
        Model("QL-580N", round12_margin=True),
        Model("QL-600", high_res=False),  # only in the fork: nothing known
        # PackBits only over its serial port, which Tavletta does not use
        Model("QL-650TD", 295, compression=False, high_res=False),
        _old("QL-700", high_res=True),
        Model("QL-710W"),
        Model("QL-720NW"),
        _two_color("QL-800", compression=False),
        _two_color("QL-810W"),
        _two_color("QL-820NWB"),
        _wide("QL-1050", 295, 35433, high_res=False, invalidate=350),
        _wide("QL-1060N", 295, 35433, high_res=False),
        _wide("QL-1100", 301, 35434, invalidate=350),
        _wide("QL-1100NWB", 301, 35434, invalidate=350),
        _wide("QL-1110NWB", 301, 35434, invalidate=350),
        _wide("QL-1115NWB", 301, 35434, invalidate=350),
    )
}
# an unknown model: what every model takes (no compression, no 600 dpi, the
# longest minimum length); wide rolls only fit the 4 inch models
NARROW = Model("QL", 301, compression=False, invalidate=400, high_res=False)
WIDE = replace(NARROW, max_dots=35433, wide=True)

# model code in status byte 4 (Brother's raster command references); the
# QL-500 and QL-550 share 0x4F: taken as the QL-500, which has no cutter,
# unless the USB device ID or the network name says QL-550
MODEL_CODES = {
    0x4F: "QL-500",
    0x31: "QL-560",
    0x32: "QL-570",
    0x33: "QL-580N",
    0x51: "QL-650TD",
    0x35: "QL-700",
    0x36: "QL-710W",
    0x37: "QL-720NW",
    0x38: "QL-800",
    0x39: "QL-810W",
    0x41: "QL-820NWB",
    0x50: "QL-1050",
    0x34: "QL-1060N",
    0x43: "QL-1100",
    0x44: "QL-1110NWB",
    0x45: "QL-1115NWB",
}

_MODEL_RE = re.compile(r"QL-?(\d{3,4}[A-Z]*)", re.IGNORECASE)


def model_named(text: str) -> Model | None:
    """The model a name mentions ("Brother QL-820NWB", "QL820NWB")."""
    match = _MODEL_RE.search(text or "")
    return MODELS.get("QL-" + match.group(1).upper()) if match else None


# --- media ------------------------------------------------------------------------


@dataclass(frozen=True)
class Media:
    """A DK roll: the size printed on the box (length 0: continuous), the
    printable dots across and along, the right margin in pins on the 720 and
    the 1296 pin head, the feed margin (ESC i d) and other lengths the
    status of some models reports."""

    name: str
    width_mm: int
    length_mm: int
    dots: tuple[int, int]
    offset_r: int
    offset_wide: int
    feed_margin: int = 0
    lengths: tuple[int, ...] = ()
    round: bool = False

    @property
    def die_cut(self) -> bool:
        return self.length_mm > 0

    @property
    def wide(self) -> bool:
        return self.dots[0] + self.offset_r > 720

    def offset(self, model: Model) -> int:
        return self.offset_wide if model.wide else self.offset_r

    def margin(self, model: Model) -> int:
        if self.name == "d12":
            return 35 if model.round12_margin else 0
        return self.feed_margin

    def size(self) -> LabelSize:
        return LabelSize(self.width_mm, self.length_mm)


# Brother's raster command references; the 18 mm roll only in brother_ql's fork
CONTINUOUS = (
    Media("12", 12, 0, (106, 0), 29, 74, 35),
    Media("18", 18, 0, (234, 0), 171, 215, 14),
    Media("29", 29, 0, (306, 0), 6, 50, 35),
    Media("38", 38, 0, (413, 0), 12, 56, 35),
    Media("50", 50, 0, (554, 0), 12, 56, 35),
    Media("54", 54, 0, (590, 0), 0, 44, 35),
    Media("62", 62, 0, (696, 0), 12, 56, 35),
    Media("90", 90, 0, (1027, 0), 12, 56, 35),  # QL-1100 series [assumed: geometry]
    Media("102", 102, 0, (1164, 0), 12, 56, 35),
    Media("103", 104, 0, (1200, 0), 12, 38, 35),
)
# the first is the default label size in the interface
DIE_CUT = (
    Media("62x29", 62, 29, (696, 271), 12, 56),
    Media("29x90", 29, 90, (306, 991), 6, 50),
    Media("62x100", 62, 100, (696, 1109), 12, 56),
    Media("17x54", 17, 54, (165, 566), 0, 44),
    Media("17x87", 17, 87, (165, 956), 0, 44),
    Media("23x23", 23, 23, (236, 202), 42, 85),
    Media("29x42", 29, 42, (306, 425), 6, 50),
    Media("38x90", 38, 90, (413, 991), 12, 56),
    Media("39x48", 39, 48, (425, 495), 6, 50),
    Media("52x29", 52, 29, (578, 271), 0, 44),
    Media("54x29", 54, 29, (602, 271), 59, 59),
    Media("60x86", 60, 86, (672, 954), 24, 68, lengths=(87,)),
    Media("102x51", 102, 51, (1164, 526), 12, 56),
    Media("102x152", 102, 152, (1164, 1660), 12, 56, lengths=(153,)),
    Media("103x164", 104, 164, (1200, 1822), 12, 38),
    Media("d12", 12, 12, (94, 94), 113, 156, round=True),
    Media("d24", 24, 24, (236, 236), 42, 85, round=True),
    Media("d58", 58, 58, (618, 618), 51, 94, round=True),
)
DK_GAP_MM = 3.0  # between two die-cut labels on the roll [assumed]
# on DK-22251 the red parts print red; it reports itself as 62 mm continuous
RED_ROLL = CONTINUOUS[6]


def continuous_media(width_mm: float) -> Media | None:
    return next((m for m in CONTINUOUS if m.width_mm == width_mm), None)


def die_cut_media(width_mm: float, length_mm: float) -> Media | None:
    """By the size on the box or a length some model reports instead."""
    return next(
        (
            m
            for m in DIE_CUT
            if abs(m.width_mm - width_mm) < 0.5
            and any(abs(n - length_mm) < 0.5 for n in (m.length_mm, *m.lengths))
        ),
        None,
    )


# --- status -----------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    model_code: int = 0
    error1: int = 0
    error2: int = 0
    media_width: int = 0
    media_type: int = 0
    media_length: int = 0
    status_type: int = REPLY
    phase: int = PHASE_WAITING
    colour: int = 0  # byte 25, reserved in Brother's references
    raw: bytes = b""

    @property
    def red_roll_hint(self) -> bool:
        """Bit 7 of byte 25: 0x81 with the red and black roll on a QL-8xx
        [assumed: seen by other projects, not in Brother's references]."""
        return bool(self.colour & 0x80)

    @property
    def media(self) -> Media | None:
        """The loaded roll, None if there is none or it is not known."""
        if self.media_type in CONTINUOUS_TYPES:
            return continuous_media(self.media_width)
        if self.media_type in DIE_CUT_TYPES:
            return die_cut_media(self.media_width, self.media_length)
        return None

    @property
    def continuous(self) -> bool:
        return self.media_type in CONTINUOUS_TYPES

    @property
    def model(self) -> Model | None:
        name = MODEL_CODES.get(self.model_code)
        return MODELS[name] if name else None


def parse_status(data: bytes) -> Status:
    """A 32-byte status packet; PrinterError for anything else."""
    if len(data) != STATUS_SIZE or not data.startswith(STATUS_HEAD):
        raise PrinterError("unexpected status answer %s" % data[:STATUS_SIZE].hex())
    return Status(
        model_code=data[4],
        error1=data[8],
        error2=data[9],
        media_width=data[10],
        media_type=data[11],
        media_length=data[17],
        status_type=data[18],
        phase=data[19],
        colour=data[25],
        raw=bytes(data),
    )


# (byte, bit) of the error information -> error class; text for bug reports
STATUS_ERRORS: tuple[tuple[int, int, type[PrinterError], str], ...] = (
    (1, 0x01, OutOfLabels, "no media"),
    (1, 0x02, OutOfLabels, "end of media"),
    (1, 0x04, PaperJam, "cutter jam"),
    (1, 0x10, PrinterBusy, "main unit in use"),
    (1, 0x20, PrinterError, "printer turned off"),
    (1, 0x40, PrinterError, "high-voltage adapter"),
    (1, 0x80, PrinterError, "fan does not work"),
    (2, 0x01, WrongMedia, "replace media"),
    (2, 0x02, PrinterError, "expansion buffer full"),
    (2, 0x04, PrinterError, "communication error"),
    (2, 0x08, PrinterBusy, "communication buffer full"),
    (2, 0x10, CoverOpen, "cover open"),
    (2, 0x20, PrinterError, "cancel key"),
    (2, 0x40, PaperJam, "media cannot be fed"),
    (2, 0x80, PrinterError, "system error"),
)


def status_error(status: Status) -> PrinterError | None:
    """The first problem the status reports, or None. "Media cannot be fed"
    on a continuous roll is its end (Brother: also when the media end is
    detected)."""
    for byte, bit, cls, text in STATUS_ERRORS:
        if (status.error1 if byte == 1 else status.error2) & bit:
            if (byte, bit) == (2, 0x40) and status.continuous:
                cls, text = OutOfPaper, "end of the continuous roll"
            return cls("status %s: %s" % (status.raw.hex(), text))
    if status.status_type == ERROR_OCCURRED:
        return PrinterError("status %s: error without a reason" % status.raw.hex())
    if status.status_type == TURNED_OFF:
        return PrinterError("status %s: printer turned off" % status.raw.hex())
    return None


# --- raster -----------------------------------------------------------------------


def packbits(data: bytes) -> bytes:
    """TIFF PackBits: runs of 3 to 128 equal bytes as (1 - n, byte), the
    rest as literals of up to 128 bytes (n - 1, bytes). Pairs stay in the
    literal: a line never grows by more than one byte per 128, so a
    162-byte line fits the one-byte length."""
    out = bytearray()
    literal = bytearray()
    i = 0
    while i < len(data):
        run = 1
        while i + run < len(data) and run < 128 and data[i + run] == data[i]:
            run += 1
        if run > 2:
            _flush(out, literal)
            out += bytes((257 - run, data[i]))
            i += run
            continue
        literal.append(data[i])
        if len(literal) == 128:
            _flush(out, literal)
        i += 1
    _flush(out, literal)
    return bytes(out)


def _flush(out: bytearray, literal: bytearray) -> None:
    if literal:
        out.append(len(literal) - 1)
        out += literal
        literal.clear()


def _mask(image: Image.Image, keep) -> Image.Image:
    """A 1-bit image, set (255) where keep(value) is true."""
    return image.point(lambda v: 255 if keep(v) else 0, "1")


def threshold(image: Image.Image) -> Image.Image:
    """Black where the image is darker than mid-grey: no dithering, so that
    codes and text keep sharp edges (brother_ql's default is a threshold too)."""
    return _mask(image.convert("L"), lambda v: v < 128)


def red_mask(image: Image.Image) -> Image.Image:
    """Red-ish pixels (hue, saturation and value as brother_ql splits them)."""
    hue, saturation, value = image.convert("RGB").convert("HSV").split()
    red = ImageChops.logical_and(
        _mask(hue, lambda v: v < 40 or v > 210), _mask(saturation, lambda v: v > 100)
    )
    return ImageChops.logical_and(red, _mask(value, lambda v: v > 80))


def ink(image: Image.Image, two_color: bool) -> tuple[Image.Image, Image.Image | None]:
    """The black and (two colours) the red layer, set bits print."""
    if not two_color:
        return threshold(image), None
    red = red_mask(image)
    black = ImageChops.subtract(threshold(image).convert("L"), red.convert("L"))
    return _mask(black, lambda v: v > 0), red


def raster_rows(layer: Image.Image, model: Model, offset_r: int) -> list[bytes]:
    """Each image row as one raster line across the print head. The image
    sits `offset_r` dots from the head's right end; the first byte holds
    the rightmost pins (the image is mirrored, as the printer expects)."""
    width = model.bytes_per_row * 8
    if layer.width + offset_r > width:
        raise UnsupportedSize("%d dots + offset %d > %d pins" % (layer.width, offset_r, width))
    canvas = Image.new("1", (width, layer.height), 0)
    canvas.paste(layer, (width - layer.width - offset_r, 0))
    data = canvas.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes()
    size = model.bytes_per_row
    return [data[i : i + size] for i in range(0, len(data), size)]


def raster_line(row: bytes, compress: bool, plane: int = 0) -> bytes:
    """g 0x00 n data (one colour) or w plane n data (1 black, 2 red)."""
    data = packbits(row) if compress else row
    head = b"g\x00" if not plane else b"w" + bytes((plane,))
    return head + bytes((len(data),)) + data


@dataclass(frozen=True)
class Job:
    """What one job prints: the rows of one label, copies times."""

    model: Model
    media: Media
    black: list[bytes]
    red: list[bytes] | None = None
    copies: int = 1
    cut: str = "full"  # "full" after every label, "end" after the last, "none"
    high_res: bool = False
    compress: bool = False
    length_mm: int | None = None  # die-cut: the length the status reported, if any


def _print_info(job: Job, page: int) -> bytes:
    media = job.media
    flags = 0x80 | 0x02 | 0x04 | 0x08  # media type, width and length valid
    if job.red is None:
        flags |= 0x40  # quality first (Brother: not for two colours)
    kind = DIE_CUT_TYPE if media.die_cut else CONTINUOUS_TYPE
    length = media.length_mm if job.length_mm is None else job.length_mm
    lines = len(job.black).to_bytes(4, "little")
    return (
        ESC
        + b"iz"
        + bytes((flags, kind, media.width_mm, length))
        + lines
        + (b"\x01\x00" if page else b"\x00\x00")
    )


def _page_setup(job: Job) -> bytes:
    """Cut, expanded mode, feed margin and compression of a page."""
    model = job.model
    cut = job.cut if model.cutting else "none"
    out = b""
    if model.cutting:
        out += ESC + b"iM" + (b"\x40" if cut == "full" else b"\x00")
        if cut == "full":
            out += ESC + b"iA\x01"  # cut after every label
    if model.expanded:
        flags = (0x08 if cut != "none" else 0) | (0x40 if job.high_res else 0)
        out += ESC + b"iK" + bytes((flags | (0x01 if job.red is not None else 0),))
    out += ESC + b"id" + job.media.margin(model).to_bytes(2, "little")
    if job.compress:
        out += b"M\x02"
    return out


def _lines(job: Job) -> bytes:
    if job.red is None:
        return b"".join(raster_line(row, job.compress) for row in job.black)
    return b"".join(
        raster_line(black, job.compress, 1) + raster_line(red, job.compress, 2)
        for black, red in zip(job.black, job.red, strict=True)
    )


def build_job(job: Job) -> bytes:
    """The bytes of a whole job: init, then each copy as a page; 0x0C ends
    a page, 0x1A the last one."""
    model = job.model
    mode = ESC + b"ia\x01" if model.mode_setting else b""
    head = mode + b"\x00" * model.invalidate + ESC + b"@" + mode + STATUS_REQUEST
    setup, lines = _page_setup(job), _lines(job)
    pages = [
        _print_info(job, n) + setup + lines + (b"\x1a" if n == job.copies - 1 else b"\x0c")
        for n in range(job.copies)
    ]
    return head + b"".join(pages)


# --- connection -------------------------------------------------------------------


def _connect(host: str, port: int, wait: float = usb.BUSY_WAIT):
    """TCP to the printer's port, or its USB device: the same language
    (net.connect). Replaced in tests."""
    return net.connect(host, port, SOCKET_TIMEOUT, wait, MIN_RATE)


class _Session:
    """Status packets from one connection; bytes that are not part of one
    (a half packet of an earlier session) are dropped."""

    def __init__(self, link, on_usb: bool):
        self.link = link
        self.on_usb = on_usb
        self.buffer = b""
        self.junk = 0
        self.answered = False  # the printer answered a status request

    def send(self, data: bytes) -> None:
        if isinstance(self.link, socket.socket):
            # the timeout restarts with each bit of progress (a printer whose
            # buffer is full takes nothing for a while)
            net.send_all(self.link, data, SOCKET_TIMEOUT, MIN_RATE)
        else:  # usb.Connection has its own progress-based timeout
            self.link.sendall(data)

    def ask(self) -> Status | None:
        """The printer's status; None if it does not answer on the network
        (older models only answer on USB [assumed])."""
        self.send(QUERY)
        try:
            status = self.read(STATUS_WAIT)
        except PrinterTimeout:
            if self.on_usb:
                raise
            return None
        self.answered = True
        return status

    def drop(self) -> None:
        """Forget what was read but not used yet."""
        self.buffer = b""
        self.junk = 0

    def poll(self) -> Status:
        """The status while a job may run: no NULs, they would end it."""
        self.send(STATUS_REQUEST)
        return self.read(STATUS_WAIT)

    def read(self, timeout: float) -> Status:
        deadline = _clock() + timeout
        try:
            while True:
                packet = self._packet()
                if packet:
                    return parse_status(packet)
                remaining = deadline - _clock()
                if remaining <= 0:
                    raise PrinterTimeout("no status within %g s" % timeout)
                self.link.settimeout(remaining)
                self.buffer += self._recv()
        finally:
            self.link.settimeout(SOCKET_TIMEOUT)

    def _recv(self) -> bytes:
        try:
            chunk = self.link.recv(256)
        except TimeoutError as e:  # socket.timeout; usb raises PrinterTimeout
            raise PrinterTimeout("no status: %s" % e) from e
        if not chunk:
            raise PrinterTimeout("connection closed by the printer")
        return chunk

    def _packet(self) -> bytes | None:
        start = self.buffer.find(STATUS_HEAD)
        if start < 0:  # keep what may be the start of a header
            start = max(0, len(self.buffer) - len(STATUS_HEAD) + 1)
        self.junk += start
        self.buffer = self.buffer[start:]
        if self.junk > MAX_JUNK:
            raise PrinterError("unexpected answer: %d bytes without a status" % self.junk)
        if len(self.buffer) < STATUS_SIZE:
            return None
        packet, self.buffer = self.buffer[:STATUS_SIZE], self.buffer[STATUS_SIZE:]
        return packet


def session(
    host: str, port: int = DEFAULT_PORT, wait: float = usb.BUSY_WAIT
) -> AbstractContextManager[_Session]:
    return open_session(
        lambda: _connect(host, port, wait),
        lambda link: _Session(link, usb.is_usb(host)),
        map_error,
    )


def _checked(status: Status, printed: int = 0, copies: int = 0) -> Status:
    error = status_error(status)
    if error:
        if printed:
            error.params.update(printed=printed, copies=copies)
        raise error
    return status


def _wait_idle(s: _Session, status: Status) -> Status:
    """Another job is printing: wait until the printer takes a new one."""
    deadline = _clock() + BUSY_TIMEOUT
    while status.phase == PHASE_PRINTING:
        if _clock() > deadline:
            raise PrinterBusy("printer still printing after %.0f s" % BUSY_TIMEOUT)
        _sleep(POLL_INTERVAL)
        status = _checked(s.poll())
    return status


def _wait_done(
    s: _Session, progress: ProgressCallback | None, copies: int, sync: bool = False
) -> int:
    """Count the "printing completed" notices; returns how many came. A
    printer that sends no notice within FIRST_NOTICE (answers to status
    requests do not count) does not report on this connection: the job
    was sent, that has to do.

    `sync` (a retry): what comes before the answer to the job's own status
    request belongs to the refused job and is skipped. If that answer never
    comes after packets were skipped, one more status request decides."""
    done = skipped = 0
    deadline = _clock() + FIRST_NOTICE
    notified, synced = False, not sync
    while done < copies:
        try:
            status = s.read(max(0.0, deadline - _clock()))
        except PrinterTimeout as e:
            if not synced and skipped:
                _checked(s.poll())  # the retry's answer never came
            _give_up(s, e, notified, done, copies)
            break
        if not synced:
            synced = status.status_type == REPLY
            if not synced:
                skipped += 1
                continue
        _checked(status, done, copies)
        if status.status_type == REPLY:
            continue
        notified = True
        deadline = _clock() + PAGE_TIMEOUT
        if status.status_type == COMPLETED:
            done += 1
            if done < copies:
                report_progress(progress, done + 1, copies)
    report_progress(progress, copies, copies)
    return done


def _give_up(s: _Session, error: PrinterTimeout, notified: bool, done: int, copies: int):
    """No more notices: fine if none ever came (the printer does not report
    here) or the printer says it is done; else the timeout, with the labels
    printed so far."""
    if notified and not _finished(s):
        error.params.update(printed=done, copies=copies)
        raise error


def _finished(s: _Session) -> bool:
    """Notices stopped coming: asked once, a printer waiting for data
    without an error has printed the job."""
    try:
        status = s.poll()
    except PrinterError:
        return False
    return status.phase == PHASE_WAITING and status_error(status) is None


# --- geometry ---------------------------------------------------------------------


def _scale(image: Image.Image) -> int:
    """2 for an image rendered for 600 dpi (high resolution), else 1."""
    return 2 if image.info.get("quality") == "high" else 1


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """The image scaled to fit width × height, centred on white. One that
    is a pixel off (a render made a multiple of 2 for a later halving) is
    placed at the corner instead, not scaled: its pixel pairs stay pairs."""
    if image.size == (width, height):
        return image
    if abs(image.width - width) <= 1 and abs(image.height - height) <= 1:
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(image.convert("RGB"), (0, 0))
        return canvas
    factor = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(
        image.convert("RGB").resize(size, Image.Resampling.LANCZOS),
        ((width - size[0]) // 2, (height - size[1]) // 2),
    )
    return canvas


def _crop_centre(image: Image.Image, width: int, height: int, align: int = 1) -> Image.Image:
    """The centre width × height; the corner on a multiple of `align`, so
    pixel pairs of a render for a later halving stay pairs."""
    left, top = (image.width - width) // 2, (image.height - height) // 2
    left, top = left - left % align, top - top % align
    return image.crop((left, top, left + width, top + height))


def _resolution(image: Image.Image, scale: int, high_res: bool) -> Image.Image:
    """Printer orientation at 300 dpi across the head, 300 or 600 along."""
    width = image.width // scale
    height = image.height * (2 if high_res else 1) // scale
    if (width, height) == image.size:
        return image
    # halving the render made for it (Raster.across: codes on whole pairs of
    # pixels): averaging each pair keeps them black, no ringing at the edges
    exact = image.width % scale == 0 and image.height % scale == 0
    method = Image.Resampling.BOX if exact else Image.Resampling.LANCZOS
    return image.resize((max(1, width), max(1, height)), method)


def _length_checked(image: Image.Image, model: Model, high_res: bool) -> Image.Image:
    """Continuous rolls: no shorter than the model's minimum (white added at
    both ends), refused if longer than its maximum."""
    factor = 2 if high_res else 1
    rows = image.height
    if rows > model.max_dots * factor:
        limit = model.max_dots / DPI * 25.4
        raise LabelTooLong("%d lines > %d" % (rows, model.max_dots * factor), max_mm=limit)
    if rows >= model.min_dots * factor:
        return image
    canvas = Image.new("RGB", (image.width, model.min_dots * factor), "white")
    canvas.paste(image, (0, (canvas.height - rows) // 2))
    return canvas


def preview_image(image: Image.Image, mode: str = "") -> Image.Image:
    """What the print head makes of it: black, or red and black."""
    black, red = ink(image, mode == RED.id)
    out = Image.new("RGB", image.size, "white")
    out.paste((0, 0, 0), mask=black)
    if red is not None:
        out.paste((255, 0, 0), mask=red)
    return out


# --- the backends -----------------------------------------------------------------


@dataclass
class _Plan:
    """What a job will be, decided from the label and the printer's status."""

    model: Model
    media: Media
    image: Image.Image  # printer orientation, printable area, final resolution
    two_color: bool
    high_res: bool
    length_mm: int | None = None  # die-cut: as the status reported it


# what was learned about a printer, by host, for both backends: the model
# its network name says, and whether the red and black roll is loaded (the
# status does not tell; a job of the wrong kind is refused)
_found_models: dict[str, Model] = {}
_two_color_rolls: dict[str, bool] = {}


class _BrotherQL(Backend):
    """What both kinds of rolls share: connection, status, the job."""

    die_cut = False
    # one printer, one setup: host, cut, colours and quality for both kinds
    settings_id = "brother_ql"

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port

    # --- the printer ---------------------------------------------------------

    def display_name(self, info: PrinterInfo | None) -> str:
        if info and info.model.upper().startswith("QL-"):
            return "Brother " + info.model
        return self.name

    def cut_modes(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """None for a model without a cutter (QL-500)."""
        model = MODELS.get(info.model) if info else None
        return () if model and not model.cutting else self.capabilities.cut_modes

    @staticmethod
    def model_for(host: str, status: Status | None) -> Model | None:
        """From the USB address, the network search, then the status code."""
        if usb.is_usb(host):
            found = model_named(unquote(urlsplit(host).path))
            if found:
                return found
        return _found_models.get(host) or (status.model if status else None)

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """QL printers on USB, then on the network."""
        found = [
            FoundPrinter("Brother %s (USB)" % p.model, p.address, str(p.device))
            for p in usb.printers()
            if p.manufacturer.casefold().startswith("brother") and model_named(p.model)
        ]
        hosts = set()
        for service in ("_pdl-datastream._tcp", "_printer._tcp"):
            for printer in mdns.browse(service, _is_ql, timeout):
                if printer.host in hosts:
                    continue
                hosts.add(printer.host)
                model = model_named(printer.name)
                if model:
                    _found_models[printer.host] = model
                found.append(printer)
        return found

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            status = s.ask()
        return self.info(self.model_for(host, status), status, host)

    def info(self, model: Model | None, status: Status | None, host: str = "") -> PrinterInfo:
        name = model.name if model else "QL"
        if status is None:  # connected, but no answer
            return PrinterInfo(name, None, None, None, "UNKNOWN")
        media, error = self._loaded(status)
        info = PrinterInfo(
            name,
            None,
            None,
            None,
            _state(status, error),
            error=error.code if error else "",
            modes=_modes(model, media, _two_color_rolls.get(host), status.red_roll_hint),
            qualities=_qualities(model),
        )
        if status.media and not media:  # the other kind: the other backend fits
            info.media_backend = BrotherQL.id if self.die_cut else BrotherQLDieCut.id
        if media:
            _describe(info, media)
        return info

    def _loaded(self, status: Status) -> tuple[Media | None, PrinterError | None]:
        """The loaded roll if it is of this backend's kind, and the problem
        the printer reports; die-cut labels on the continuous side (or the
        other way round) are a problem of their own."""
        loaded, error = status.media, status_error(status)
        if loaded is None or loaded.die_cut == self.die_cut:
            return loaded, error
        return None, error or WrongMedia("loaded: %s" % loaded.name)

    def preview(self, image: Image.Image, mode: str = "") -> Image.Image:
        return preview_image(image, mode)

    def renders_red(self, mode: str = "") -> bool:
        return mode == RED.id  # red and black: red parts stay red for red_mask()

    # --- printing ------------------------------------------------------------

    @abstractmethod
    def label_media(self, image: Image.Image) -> Media:
        """The roll the image was made for."""

    @abstractmethod
    def fit(self, image: Image.Image, made_for: Media, media: Media) -> Image.Image:
        """The image (printer orientation) on the loaded roll's printable area."""

    def to_printer(self, image: Image.Image) -> Image.Image:
        return image

    def plan(
        self,
        image: Image.Image,
        options: PrintOptions,
        host: str,
        status,
        two_color: bool | None = None,
    ) -> _Plan:
        """`two_color`: the kind of job to send, else the roll decides (as
        far as known), else the choice in the dialog."""
        made_for = self.label_media(image)
        media = _roll(made_for, status)
        model = self.model_for(host, status) or (WIDE if media.wide else NARROW)
        two_color = _two_color_job(model, media, two_color, _two_color_rolls.get(host), options)
        high_res = options.quality == "high" and model.high_res and not two_color
        scale = _scale(image)
        printed = self.fit(self.to_printer(image), made_for, media)
        printed = _resolution(printed, scale, high_res)
        if not media.die_cut:
            printed = _length_checked(printed, model, high_res)
        length = status.media_length if status and media.die_cut else None
        return _Plan(model, media, printed, two_color, high_res, length)

    def job(self, plan: _Plan, options: PrintOptions) -> Job:
        black, red = ink(plan.image, plan.two_color)
        offset = plan.media.offset(plan.model)
        return Job(
            model=plan.model,
            media=plan.media,
            black=raster_rows(black, plan.model, offset),
            red=raster_rows(red, plan.model, offset) if red is not None else None,
            copies=max(1, options.copies),
            cut=options.cut if options.cut in self.capabilities.cut_modes else "full",
            high_res=plan.high_res,
            compress=plan.model.compression,
            length_mm=plan.length_mm,
        )

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        copies = max(1, options.copies)
        with session(host, self.port, usb.PRINT_WAIT) as s:
            status = s.ask()
            if status is not None:
                status = _wait_idle(s, _checked(status))
            plan = self.plan(image, options, host, status)
            report_progress(progress, 1, copies)
            try:
                done = self._send(s, plan, options, progress)
            except WrongMedia as e:
                if e.params.get("printed") or not _other_roll_kind(plan, status):
                    raise
                # the 62 mm roll is the other one (red and black or plain):
                # the printer refused the job before printing, send it the
                # other way [assumed: refused before anything is printed]
                plan = self.plan(image, options, host, status, not plan.two_color)
                s.drop()  # the rest of the refused job's notices
                try:
                    done = self._send(s, plan, options, progress, retry=True)
                except WrongMedia:
                    _two_color_rolls.pop(host, None)  # it was something else
                    raise
        if done and plan.media == RED_ROLL and plan.model.two_color:
            # a label came out: this is what the roll takes
            _two_color_rolls[host] = plan.two_color
        return _result(options, plan)

    def _send(
        self, s: _Session, plan: _Plan, options: PrintOptions, progress, retry: bool = False
    ) -> int:
        """Send the job; the labels the printer reports printed (0 if it
        reports nothing)."""
        copies = max(1, options.copies)
        s.send(build_job(self.job(plan, options)))
        if s.answered:
            return _wait_done(s, progress, copies, sync=retry)
        report_progress(progress, copies, copies)  # sent is all we know
        return 0


def _two_color_job(
    model: Model, media: Media, wanted: bool | None, guess: bool | None, options: PrintOptions
) -> bool:
    """Two colours: only on the 62 mm roll of a QL-8xx; as asked by the
    caller, else as a printed job showed the roll, else the dialog's choice
    (which the status' hint only preselects)."""
    if not (model.two_color and media == RED_ROLL):
        return False
    if wanted is not None:
        return wanted
    return options.mode == RED.id if guess is None else guess


def _state(status: Status, error: PrinterError | None) -> str:
    if error:
        return "ERROR"
    return "BUSY" if status.phase == PHASE_PRINTING else "IDLE"


def _qualities(model: Model | None) -> tuple[str, ...]:
    return QUALITIES if model and model.high_res else QUALITIES[:1]


def _describe(info: PrinterInfo, media: Media) -> None:
    """The loaded roll in the printer's info."""
    info.cassette = CassetteInfo(printable_mm=round(media.dots[0] / DPI * 25.4, 1))
    if media.die_cut:
        info.label_mm = (media.width_mm, media.length_mm)
        info.pitch_mm = media.length_mm + DK_GAP_MM
    else:
        info.tape_mm = media.width_mm


def _other_roll_kind(plan: _Plan, status: Status | None) -> bool:
    """A refused job on the 62 mm roll of a two-colour model: the roll is
    the other kind than the job."""
    return status is not None and plan.model.two_color and plan.media == RED_ROLL


def _result(options: PrintOptions, plan: _Plan) -> PrintResult | None:
    """What differs from the choice in the dialog, and why."""
    used = RED.id if plan.two_color else BLACK.id
    wanted = options.mode if options.mode in (RED.id, BLACK.id) else BLACK.id
    if used == wanted:
        return None
    # the roll decided; on a model without two colours red is not available
    reason = "roll" if plan.model.two_color and plan.media == RED_ROLL else ""
    return PrintResult(mode=used, reason=reason)


def _roll(made_for: Media, status: Status | None) -> Media:
    """The loaded roll; the label's own if the printer does not tell."""
    media = status.media if status else None
    if media is None:
        return made_for
    if media.die_cut != made_for.die_cut:
        raise WrongMedia("label for %s, loaded: %s" % (made_for.name, media.name))
    return media


def _modes(
    model: Model | None,
    media: Media | None,
    two_color_roll: bool | None = None,
    hint: bool = False,
) -> tuple[PrintMode, ...]:
    """Red and black: a QL-8xx with the 62 mm roll (DK-22251 or plain);
    once a job showed which roll it is, only what that roll prints. The
    status' hint for the red roll only puts it first (preselected)."""
    if not (model and model.two_color and media == RED_ROLL):
        return (BLACK,)
    if two_color_roll is None:
        return (RED, BLACK) if hint else (BLACK, RED)
    return (RED,) if two_color_roll else (BLACK,)


def _is_ql(name: str) -> bool:
    return model_named(name) is not None


MIN_LENGTH_MM = 150 / DPI * 25.4  # the shortest any model prints (12.7 mm)
MAX_LENGTH_MM = 11811 / DPI * 25.4  # the longest every model prints (1 m)
QL_CUT_MODES = ("full", "end", "none")


class BrotherQL(_BrotherQL):
    """Continuous DK rolls: the label grows with its content."""

    id = "brother_ql"
    name = "Brother QL"
    capabilities = Capabilities(
        tape_widths_mm=tuple(m.width_mm for m in CONTINUOUS),
        dpi=DPI,
        modes=(BLACK,),
        color_model="mono",
        cut_modes=QL_CUT_MODES,
        data_formats=("raster",),
        min_length_mm=MIN_LENGTH_MM,
        max_length_mm=MAX_LENGTH_MM,
        qualities=QUALITIES,
        media="roll",
    )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        nearest = self.capabilities.nearest_tape(tape_mm)
        media = next(m for m in CONTINUOUS if m.width_mm == nearest)
        scale = 2 if options.quality == "high" else 1
        # high quality: 600 dpi along the feed, the head's 300 across; the
        # render's 600 across is halved when printing (_resolution). Along
        # too, when the job ends up at 300 (two colours, a model without
        # 600 dpi): codes on pixel pairs both ways stay whole either way
        return Raster(DPI * scale, tape_px=media.dots[0] * scale, across=scale, along_step=scale)

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        image = super().prepare(label, options, safe_mm)
        image.info.update(tape_mm=label.tape_mm, quality=options.quality)
        return image

    def label_media(self, image: Image.Image) -> Media:
        media = continuous_media(image.info.get("tape_mm", 0))
        if media is None:  # not made by prepare(): by its height
            across = image.height // _scale(image)
            media = next((m for m in CONTINUOUS if m.dots[0] == across), None)
        if media is None:
            raise UnsupportedSize("no roll is %d dots wide" % image.height)
        return media

    def to_printer(self, image: Image.Image) -> Image.Image:
        """Label space (x along the roll) to raster lines across the head."""
        return image.rotate(90, expand=True)

    def fit(self, image: Image.Image, made_for: Media, media: Media) -> Image.Image:
        """Another width loaded ("print anyway"): scaled to it, like the VC-500W."""
        scale = _scale(image)
        across = media.dots[0] * scale
        if image.width == across:
            return image
        length = max(1, round(image.height * across / image.width))
        return image.convert("RGB").resize((across, length), Image.Resampling.LANCZOS)

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        dpi = DPI * _scale(image)
        check_length(image.width / dpi * 25.4, self.capabilities)
        return super().print_label(image, host, options, progress)


class BrotherQLDieCut(_BrotherQL):
    """Die-cut DK labels of fixed sizes, content across or along the roll."""

    id = "brother_ql_diecut"
    name = "Brother QL"
    die_cut = True
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI,
        modes=(BLACK,),
        color_model="mono",
        cut_modes=QL_CUT_MODES,
        continuous=False,
        label_sizes=tuple(m.size() for m in DIE_CUT),
        max_width_mm=max(m.width_mm for m in DIE_CUT),
        data_formats=("raster",),
        qualities=QUALITIES,
    )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(DPI * (2 if options.quality == "high" else 1))

    def label_raster(self, label: Label, options: PrintOptions) -> Raster:
        """High quality: rendered at 600 dpi, the head's axis halved when
        printing, the feed's too when the job ends up at 300 dpi (two
        colours, a model without 600 dpi): codes on pixel pairs both ways."""
        raster = self.raster(label.tape_mm, options)
        scale = 2 if options.quality == "high" else 1
        return replace(raster, across=scale, along_step=scale)

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """In printer orientation (x across the head), the whole label; the
        unprintable rim is cut off when printing."""
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(90, expand=True)
        image.info.update(size_mm=label.media_size(), quality=options.quality)
        return image

    def label_media(self, image: Image.Image) -> Media:
        size = image.info.get("size_mm")
        if size is None:  # not made by prepare(): by its size in dots
            dpi = DPI * _scale(image)
            size = (image.width / dpi * 25.4, image.height / dpi * 25.4)
        media = die_cut_media(*size)
        if media is None:
            raise UnsupportedSize("no die-cut label is %.1f × %.1f mm" % size)
        return media

    def fit(self, image: Image.Image, made_for: Media, media: Media) -> Image.Image:
        """The label at the loaded size (scaled if other labels are loaded:
        "print anyway"), then its printable area."""
        scale = _scale(image)
        dpi = DPI * scale
        image = _fit(image, mm_to_px(media.width_mm, dpi), mm_to_px(media.length_mm, dpi))
        return _crop_centre(image, media.dots[0] * scale, media.dots[1] * scale, scale)

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        dpi = DPI * _scale(image)
        check_width(image.width / dpi * 25.4, self.capabilities)
        return super().print_label(image, host, options, progress)
