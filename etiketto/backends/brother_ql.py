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

from etiketto.backends import mdns, usb
from etiketto.backends.base import (
    Backend,
    Capabilities,
    CassetteInfo,
    CoverOpen,
    FoundPrinter,
    LabelSize,
    LabelTooLong,
    OutOfLabels,
    PaperJam,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
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
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, mm_to_px

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
# clears a half-received job: the printer drops NULs in command mode
QUERY = b"\x00" * 400 + ESC + b"@" + STATUS_REQUEST

# status type (byte 18) and phase (byte 19)
REPLY, COMPLETED, ERROR_OCCURRED, NOTIFICATION, PHASE_CHANGE = 0x00, 0x01, 0x02, 0x05, 0x06
PHASE_WAITING, PHASE_PRINTING = 0x00, 0x01
CONTINUOUS_TYPE, DIE_CUT_TYPE = 0x0A, 0x0B

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
    bytes_per_row: int = 90  # 720 pins; 162 = 1296 pins on the 4 inch models
    offset_r: int = 0  # added to the roll's right offset
    mode_setting: bool = True  # knows ESC i a (else always in raster mode)
    cutting: bool = True
    expanded: bool = True  # knows ESC i K (cut at end, 600 dpi, two colours)
    compression: bool = True  # takes PackBits lines
    two_color: bool = False
    invalidate: int = 200  # NULs that clear the command buffer
    # 600 dpi along the feed, a bit of ESC i K (brother_ql's dpi_600 option)
    high_res: bool = True


def _old(name: str, min_dots: int = 150, **flags) -> Model:
    """The models without ESC i a and compression."""
    return Model(name, min_dots, mode_setting=False, compression=False, **flags)


def _wide(name: str, min_dots: int, max_dots: int) -> Model:
    """The 4 inch models: 1296 pins, 44 dots further right."""
    return Model(name, min_dots, max_dots, bytes_per_row=162, offset_r=44)


def _two_color(name: str, compression: bool = True) -> Model:
    return Model(name, compression=compression, two_color=True, invalidate=400)


MODELS = {
    model.name: model
    for model in (
        _old("QL-500", 295, cutting=False, expanded=False, high_res=False),
        _old("QL-550", 295),
        _old("QL-560", 295),
        _old("QL-570"),
        Model("QL-580N"),
        Model("QL-600"),
        Model("QL-650TD", 295),
        _old("QL-700"),
        Model("QL-710W"),
        Model("QL-720NW"),
        _two_color("QL-800", compression=False),
        _two_color("QL-810W"),
        _two_color("QL-820NWB"),
        _wide("QL-1050", 295, 35433),
        _wide("QL-1060N", 295, 35433),
        _wide("QL-1100", 301, 35434),
        _wide("QL-1100NWB", 301, 35434),
        _wide("QL-1110NWB", 301, 35434),
        _wide("QL-1115NWB", 301, 35434),
    )
}
# an unknown model: what every model takes (no compression, no 600 dpi, the
# longest minimum length); wide rolls only fit the 4 inch models
NARROW = Model("QL", 301, compression=False, invalidate=400, high_res=False)
WIDE = replace(NARROW, max_dots=35433, bytes_per_row=162, offset_r=44)

# model code in status byte 4, from Brother's raster command references
# [assumed: not in brother_ql, not checked on a printer]
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
    """A DK roll: width × length in mm as the status reports it (length 0:
    continuous), the printable dots across and along, the right offset in
    dots and the feed margin (ESC i d)."""

    name: str
    width_mm: int
    length_mm: int
    dots: tuple[int, int]
    offset_r: int
    feed_margin: int
    round: bool = False

    @property
    def die_cut(self) -> bool:
        return self.length_mm > 0

    @property
    def wide(self) -> bool:
        return self.dots[0] + self.offset_r > 720

    def size(self) -> LabelSize:
        return LabelSize(self.width_mm, self.length_mm)


CONTINUOUS = (
    Media("12", 12, 0, (106, 0), 29, 35),
    Media("18", 18, 0, (234, 0), 171, 14),
    Media("29", 29, 0, (306, 0), 6, 35),
    Media("38", 38, 0, (413, 0), 12, 35),
    Media("50", 50, 0, (554, 0), 12, 35),
    Media("54", 54, 0, (590, 0), 0, 35),
    Media("62", 62, 0, (696, 0), 12, 35),
    Media("102", 102, 0, (1164, 0), 12, 35),
    Media("103", 104, 0, (1200, 0), 12, 35),
)
# the first is the default label size in the interface
DIE_CUT = (
    Media("62x29", 62, 29, (696, 271), 12, 0),
    Media("29x90", 29, 90, (306, 991), 6, 0),
    Media("62x100", 62, 100, (696, 1109), 12, 0),
    Media("17x54", 17, 54, (165, 566), 0, 0),
    Media("17x87", 17, 87, (165, 956), 0, 0),
    Media("23x23", 23, 23, (202, 202), 42, 0),
    Media("29x42", 29, 42, (306, 425), 6, 0),
    Media("39x90", 38, 90, (413, 991), 12, 0),
    Media("39x48", 39, 48, (425, 495), 6, 0),
    Media("52x29", 52, 29, (578, 271), 0, 0),
    Media("54x29", 54, 29, (598, 271), 60, 0),
    Media("60x86", 60, 87, (672, 954), 18, 0),
    Media("102x51", 102, 51, (1164, 526), 12, 0),
    Media("102x152", 102, 153, (1164, 1660), 12, 0),
    Media("103x164", 104, 164, (1200, 1822), 12, 0),
    Media("d12", 12, 12, (94, 94), 113, 35, round=True),
    Media("d24", 24, 24, (236, 236), 42, 0, round=True),
    Media("d58", 58, 58, (618, 618), 51, 0, round=True),
)
DK_GAP_MM = 3.0  # between two die-cut labels on the roll [assumed]
# on DK-22251 the red parts print red; it reports itself as 62 mm continuous
RED_ROLL = CONTINUOUS[6]


def continuous_media(width_mm: float) -> Media | None:
    return next((m for m in CONTINUOUS if m.width_mm == width_mm), None)


def die_cut_media(width_mm: float, length_mm: float) -> Media | None:
    return next(
        (
            m
            for m in DIE_CUT
            if abs(m.width_mm - width_mm) < 0.5 and abs(m.length_mm - length_mm) < 0.5
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
    raw: bytes = b""

    @property
    def media(self) -> Media | None:
        """The loaded roll, None if there is none or it is not known."""
        if self.media_type == CONTINUOUS_TYPE:
            return continuous_media(self.media_width)
        if self.media_type == DIE_CUT_TYPE:
            return die_cut_media(self.media_width, self.media_length)
        return None

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
    """The first problem the status reports, or None."""
    for byte, bit, cls, text in STATUS_ERRORS:
        if (status.error1 if byte == 1 else status.error2) & bit:
            return cls("status %s: %s" % (status.raw.hex(), text))
    if status.status_type == ERROR_OCCURRED:
        return PrinterError("status %s: error without a reason" % status.raw.hex())
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


def unpackbits(data: bytes) -> bytes:
    """The inverse of packbits(); ValueError for data cut off in the middle."""
    out = bytearray()
    i = 0
    while i < len(data):
        header = data[i]
        if header < 128:
            chunk = data[i + 1 : i + 2 + header]
            if len(chunk) != header + 1:
                raise ValueError("PackBits literal cut off")
            out += chunk
            i += header + 2
        elif header > 128:
            if i + 1 >= len(data):
                raise ValueError("PackBits run cut off")
            out += bytes((data[i + 1],)) * (257 - header)
            i += 2
        else:  # 128: no operation
            i += 1
    return bytes(out)


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


def _print_info(job: Job, page: int) -> bytes:
    media = job.media
    flags = 0x80 | 0x02 | 0x04 | 0x08 | 0x40  # kind, width, length valid; quality first
    kind = DIE_CUT_TYPE if media.die_cut else CONTINUOUS_TYPE
    lines = len(job.black).to_bytes(4, "little")
    return (
        ESC
        + b"iz"
        + bytes((flags, kind, media.width_mm, media.length_mm))
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
    out += ESC + b"id" + job.media.feed_margin.to_bytes(2, "little")
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
    """TCP to port 9100, or the printer's USB device: the same raster
    language. `wait`: on USB, how long to wait while another session uses it."""
    if usb.is_usb(host):
        return usb.connect(host, SOCKET_TIMEOUT, wait)
    return socket.create_connection((mdns.resolve_host(host), port), SOCKET_TIMEOUT)


def map_error(error: Exception) -> PrinterError:
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    if isinstance(error, OSError) or "resolve" in text.lower():
        return PrinterNotFound(text)
    return PrinterError(text)


class _Session:
    """Status packets from one connection; bytes that are not part of one
    (a half packet of an earlier session) are dropped."""

    def __init__(self, link, on_usb: bool):
        self.link = link
        self.on_usb = on_usb
        self.buffer = b""
        self.junk = 0

    def send(self, data: bytes) -> None:
        self.link.sendall(data)

    def ask(self) -> Status | None:
        """The printer's status; None if it does not answer on the network
        (older models only answer on USB [assumed])."""
        self.send(QUERY)
        try:
            return self.read(STATUS_WAIT)
        except PrinterTimeout:
            if self.on_usb:
                raise
            return None

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


def _wait_done(s: _Session, progress: ProgressCallback | None, copies: int) -> None:
    """Count the "printing completed" notices. A printer that sends none
    within FIRST_NOTICE (answers to status requests do not count) does not
    report on this connection: the job was sent, that has to do."""
    done = 0
    deadline = _clock() + FIRST_NOTICE
    notified = False
    while done < copies:
        try:
            status = s.read(max(0.0, deadline - _clock()))
        except PrinterTimeout as e:
            if not notified:
                break
            e.params.update(printed=done, copies=copies)
            raise
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


# --- geometry ---------------------------------------------------------------------


def _scale(image: Image.Image) -> int:
    """2 for an image rendered for 600 dpi (high resolution), else 1."""
    return 2 if image.info.get("quality") == "high" else 1


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """The image scaled to fit width × height, centred on white."""
    if image.size == (width, height):
        return image
    factor = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(
        image.convert("RGB").resize(size, Image.Resampling.LANCZOS),
        ((width - size[0]) // 2, (height - size[1]) // 2),
    )
    return canvas


def _crop_centre(image: Image.Image, width: int, height: int) -> Image.Image:
    left, top = (image.width - width) // 2, (image.height - height) // 2
    return image.crop((left, top, left + width, top + height))


def _resolution(image: Image.Image, scale: int, high_res: bool) -> Image.Image:
    """Printer orientation at 300 dpi across the head, 300 or 600 along."""
    width = image.width // scale
    height = image.height * (2 if high_res else 1) // scale
    if (width, height) == image.size:
        return image
    return image.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)


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


class _BrotherQL(Backend):
    """What both kinds of rolls share: connection, status, the job."""

    die_cut = False

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.models: dict[str, Model] = {}  # host -> model found by the search

    # --- the printer ---------------------------------------------------------

    def display_name(self, info: PrinterInfo | None) -> str:
        if info and info.model.upper().startswith("QL-"):
            return "Brother " + info.model
        return self.name

    def model_for(self, host: str, status: Status | None) -> Model | None:
        """From the USB address, the network search, then the status code."""
        if usb.is_usb(host):
            found = model_named(unquote(urlsplit(host).path))
            if found:
                return found
        return self.models.get(host) or (status.model if status else None)

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
                    self.models[printer.host] = model
                found.append(printer)
        return found

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            status = s.ask()
        return self.info(self.model_for(host, status), status)

    def info(self, model: Model | None, status: Status | None) -> PrinterInfo:
        name = model.name if model else "QL"
        if status is None:  # connected, but no answer
            return PrinterInfo(name, None, None, None, "UNKNOWN")
        media, error = self._loaded(status)
        state = "ERROR" if error else "BUSY" if status.phase == PHASE_PRINTING else "IDLE"
        info = PrinterInfo(
            name,
            None,
            None,
            None,
            state,
            error=error.code if error else "",
            modes=_modes(model, media),
            qualities=QUALITIES if model and model.high_res else QUALITIES[:1],
        )
        if media:
            info.cassette = CassetteInfo(printable_mm=round(media.dots[0] / DPI * 25.4, 1))
            if media.die_cut:
                info.pitch_mm = media.length_mm + DK_GAP_MM
            else:
                info.tape_mm = media.width_mm
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

    # --- printing ------------------------------------------------------------

    @abstractmethod
    def label_media(self, image: Image.Image) -> Media:
        """The roll the image was made for."""

    @abstractmethod
    def fit(self, image: Image.Image, made_for: Media, media: Media) -> Image.Image:
        """The image (printer orientation) on the loaded roll's printable area."""

    def to_printer(self, image: Image.Image) -> Image.Image:
        return image

    def plan(self, image: Image.Image, options: PrintOptions, host: str, status) -> _Plan:
        made_for = self.label_media(image)
        media = _roll(made_for, status)
        model = self.model_for(host, status) or (WIDE if media.wide else NARROW)
        two_color = options.mode == RED.id and model.two_color and media == RED_ROLL
        high_res = options.quality == "high" and model.high_res and not two_color
        scale = _scale(image)
        printed = self.fit(self.to_printer(image), made_for, media)
        printed = _resolution(printed, scale, high_res)
        if not media.die_cut:
            printed = _length_checked(printed, model, high_res)
        return _Plan(model, media, printed, two_color, high_res)

    def job(self, plan: _Plan, options: PrintOptions) -> Job:
        black, red = ink(plan.image, plan.two_color)
        offset = plan.media.offset_r + plan.model.offset_r
        return Job(
            model=plan.model,
            media=plan.media,
            black=raster_rows(black, plan.model, offset),
            red=raster_rows(red, plan.model, offset) if red is not None else None,
            copies=max(1, options.copies),
            cut=options.cut if options.cut in self.capabilities.cut_modes else "full",
            high_res=plan.high_res,
            compress=plan.model.compression,
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
            s.send(build_job(self.job(plan, options)))
            if status is None:  # a printer that does not answer: sent is all we know
                report_progress(progress, copies, copies)
            else:
                _wait_done(s, progress, copies)
        if options.mode == RED.id and not plan.two_color:
            return PrintResult(mode=BLACK.id)
        return None


def _roll(made_for: Media, status: Status | None) -> Media:
    """The loaded roll; the label's own if the printer does not tell."""
    media = status.media if status else None
    if media is None:
        return made_for
    if media.die_cut != made_for.die_cut:
        raise WrongMedia("label for %s, loaded: %s" % (made_for.name, media.name))
    return media


def _modes(model: Model | None, media: Media | None) -> tuple[PrintMode, ...]:
    """Red and black: a QL-8xx with the 62 mm roll (DK-22251 or plain)."""
    if model and model.two_color and media == RED_ROLL:
        return (BLACK, RED)
    return (BLACK,)


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
    )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        nearest = self.capabilities.nearest_tape(tape_mm)
        media = next(m for m in CONTINUOUS if m.width_mm == nearest)
        scale = 2 if options.quality == "high" else 1
        return Raster(DPI * scale, tape_px=media.dots[0] * scale)

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
        return _crop_centre(image, media.dots[0] * scale, media.dots[1] * scale)

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
