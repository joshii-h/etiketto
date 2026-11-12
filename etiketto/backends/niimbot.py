"""NIIMBOT label printers (B1, B21, B3S, B4, D11, D110, D101 ...) on USB
serial (see docs/niimbot.md).

The printers speak a small binary protocol of packets
55 55 CMD LEN DATA CHK AA AA, the same over USB (a CDC ACM serial port,
/dev/ttyACM0) and Bluetooth; Etiketto uses USB only. A job sets density and
label type, then sends the label row by row (blank rows, rows with a few
black pixels and full bitmap rows each have their own packet) and polls the
printer until it has printed all copies. Which commands a model takes
depends on its model id, which the printer reports.

Protocol knowledge from niimbluelib (MultiMote, MIT) and Thermalith /
Niimbot.Net (EvilGeniusLabs, GPL-3.0-or-later), written anew for Etiketto.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, replace
from functools import reduce

from PIL import Image

from etiketto.backends import serialport
from etiketto.backends.base import (
    LIMIT_SLACK_MM,
    Backend,
    Capabilities,
    CoverOpen,
    Density,
    FoundPrinter,
    HeadOpen,
    JobCanceled,
    LabelNotFound,
    LabelSize,
    LabelTooLong,
    LabelTooWide,
    LowBattery,
    OutOfLabels,
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
    RibbonError,
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

HEAD = b"\x55\x55"
TAIL = b"\xaa\xaa"

# seconds: an ordinary answer, a status poll, the answer after a page of rows
REQUEST_TIMEOUT = 1.0
STATUS_TIMEOUT = 5.0
PAGE_TIMEOUT = 10.0
POLL_INTERVAL = 0.3  # between two status polls while printing
PRINT_TIMEOUT = 60.0  # without a printed label while waiting for a job to finish
WRITE_TIMEOUT = 5.0  # without the port taking data

MAX_REPEAT = 255  # the repeat field of a row packet is one byte
MAX_INDEXED = 6  # rows with up to this many black pixels are sent as positions
MAX_ROWS = 0xFFFF  # the row number is 16 bits
CHECK_LINE_EVERY = 200  # B21 (protocol 1): a check line after this many rows

DPI_203 = 8 * 25.4  # 8 dots per mm exactly; "203 dpi" is rounded
DPI_300 = 300.0

# commands (requests); the answer ids are in ANSWERS
CONNECT = 0xC1
STATUS_DATA = 0xA5
PRINTER_INFO = 0x40
HEARTBEAT = 0xDC
SET_DENSITY = 0x21
SET_LABEL_TYPE = 0x23
PRINT_START = 0x01
PAGE_START = 0x03
SET_PAGE_SIZE = 0x13
PRINT_QUANTITY = 0x15
PRINT_CLEAR = 0x20
PAGE_END = 0xE3
PRINT_STATUS = 0xA3
PRINT_END = 0xF3
CANCEL = 0xDA
# rows; the printer does not answer them
ROW_BITMAP = 0x85
ROW_INDEXED = 0x83
ROW_EMPTY = 0x84
CHECK_LINE = 0x86
# answers that can come instead of the expected one
ERROR = 0xDB
NOT_SUPPORTED = 0x00

ANSWERS = {
    CONNECT: (0xC2,),
    STATUS_DATA: (0xB5,),
    SET_DENSITY: (0x31,),
    SET_LABEL_TYPE: (0x33,),
    PRINT_START: (0x02,),
    PAGE_START: (0x04,),
    SET_PAGE_SIZE: (0x14,),
    PRINT_QUANTITY: (0x16,),
    PRINT_CLEAR: (0x30,),
    PAGE_END: (0xE4,),
    PRINT_STATUS: (0xB3,),
    PRINT_END: (0xF4,),
    CANCEL: (0xD0,),
}
HEARTBEAT_ANSWERS = {1: 0xDD, 2: 0xDE, 3: 0xDF, 4: 0xD9}
INFO_MODEL_ID = 8
INFO_FIRMWARE = 9

# label types by Etiketto's media names (base.MEDIA_TYPES)
LABEL_TYPES = {"gap": 1, "black_mark": 2, "continuous": 3, "transparent": 5}

# error codes in 0xDB answers and in print status -> error class, text for bug reports
ERRORS: dict[int, tuple[type[PrinterError], str]] = {
    1: (CoverOpen, "cover open"),
    2: (OutOfLabels, "no paper"),
    3: (LowBattery, "low battery"),
    4: (LowBattery, "battery exception"),
    5: (JobCanceled, "canceled at the printer"),
    6: (PrinterError, "data error"),
    7: (PrinterError, "overheated"),
    8: (OutOfLabels, "paper-out exception"),
    9: (PrinterBusy, "printer busy"),
    10: (HeadOpen, "no print head"),
    11: (PrinterError, "temperature too low"),
    12: (HeadOpen, "print head loose"),
    13: (RibbonError, "no ribbon"),
    14: (RibbonError, "wrong ribbon"),
    15: (RibbonError, "ribbon used up"),
    16: (PrinterError, "wrong paper"),
    27: (PaperJam, "abnormal paper output"),
    28: (LabelNotFound, "check the paper"),
    32: (PrinterError, "label material error"),
    33: (PrinterError, "material not supported"),
    52: (PrinterError, "timeout receiving data"),
}
# reading or writing the roll's RFID tag failed: the printer prints anyway
NON_FATAL = frozenset({0x14, 0x1D, 0x22})

_sleep = time.sleep  # replaced in tests


# --- models -------------------------------------------------------------------------


@dataclass(frozen=True)
class Model:
    """What a model can do, by the ids it reports (NIIMBOT's devices.json
    through niimbluelib). Task: which command sequence it takes."""

    name: str
    ids: tuple[int, ...]
    dpi: float
    head_px: int  # pixels across the print head
    media: tuple[str, ...]  # label types, the default first
    density: Density
    task: str  # "b1", "d110", "b21" (B21, protocol 1) or "v4" (protocol 4)
    max_label_mm: float = 0.0  # widest label; 0: the head width + 2 mm

    @property
    def head_mm(self) -> float:
        return self.head_px * 25.4 / self.dpi

    @property
    def widest_mm(self) -> float:
        """The widest label taken: a label is a little wider than the head
        can print (B1: 50 mm labels, a 48 mm head), the rest is cut off."""
        return self.max_label_mm or math.ceil(self.head_mm) + 2.0


GBT = ("gap", "black_mark", "transparent")
GBCT = ("gap", "black_mark", "continuous", "transparent")
GT = ("gap", "transparent")
D5 = Density(1, 5, 3)
D3 = Density(1, 3, 2)

MODELS = (
    Model("B1", (4096,), DPI_203, 384, GBT, D5, "b1", 50),
    Model("B1 PRO", (4097,), DPI_300, 567, GBT, D5, "v4", 50),
    Model("B1 SE", (4098,), DPI_203, 384, GBT, D5, "b1", 50),
    Model("B21", (768,), DPI_203, 384, GBCT, D5, "b21", 50),
    Model("B21 PRO", (785,), DPI_300, 591, GBCT, D5, "v4", 50),
    Model(
        "B21 C2B",
        (771, 775),
        DPI_203,
        384,
        ("gap", "continuous", "transparent", "black_mark"),
        D5,
        "b1",
        50,
    ),
    Model("B21 L2B", (769,), DPI_203, 384, GBT, D5, "b1", 50),
    Model("B21S", (777,), DPI_203, 384, GBCT, D5, "d110", 50),
    Model("B203", (2816,), DPI_203, 400, GBT, D5, "b1"),
    Model("B3", (52993,), DPI_203, 600, GBCT, D5, "b1"),
    Model("B31", (5632,), DPI_203, 600, GBT, D5, "b1"),
    Model("B32", (2049,), DPI_300, 851, GT, Density(1, 15, 10), "b1"),
    Model("B3S", (256, 260, 262), DPI_203, 576, GBCT, D5, "b1", 75),
    Model("B3S P", (272,), DPI_203, 576, GBCT, D5, "b1", 75),
    Model("B4", (6656,), DPI_203, 832, GBT, D5, "b1"),
    Model("D11", (512,), DPI_203, 96, GT, D3, "d110", 15),
    Model("D11 H", (528,), DPI_300, 142, GT, D5, "v4", 15),
    Model("D110", (2304, 2305), DPI_203, 96, GT, D3, "d110", 15),
    Model("D110 M", (2320,), DPI_203, 96, GT, D5, "b1", 15),
    Model("D101", (2560,), DPI_203, 192, GT, D3, "b1", 25),
    Model("B18", (3584,), DPI_203, 96, ("gap", "transparent", "continuous"), D3, "b1", 15),
    Model("H1S", (4352,), DPI_203, 96, ("gap", "continuous", "transparent"), D3, "b1", 15),
)
BY_ID = {model_id: model for model in MODELS for model_id in model.ids}
BY_NAME = {model.name: model for model in MODELS}

# common label sizes (width across the head × height along the feed), the
# default first: B1/B21 rolls, larger ones for B3S/B4, the narrow D11/D110 rolls
LABEL_SIZES = (
    LabelSize(50, 30),
    LabelSize(40, 30),
    LabelSize(40, 20),
    LabelSize(30, 15),
    LabelSize(50, 50),
    LabelSize(75, 50),
    LabelSize(100, 150),
    LabelSize(12, 40),
    LabelSize(12, 22),
    LabelSize(15, 30),
)


def task_for(model: Model, protocol: int) -> str:
    """The command sequence: D110 M speaks protocol 4 with newer firmware."""
    if model.task == "b1" and protocol >= 4 and model.name == "D110 M":
        return "v4"
    return model.task


# --- packets ------------------------------------------------------------------------


def checksum(body: bytes) -> int:
    """XOR of command, length and data."""
    return reduce(lambda a, b: a ^ b, body, 0)


def u16(value: int) -> bytes:
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


@dataclass(frozen=True)
class Packet:
    command: int
    data: bytes = b"\x01"  # requests without data carry a single 1

    def to_bytes(self) -> bytes:
        if len(self.data) > 0xFF:
            raise ValueError("packet data too long: %d bytes" % len(self.data))
        body = bytes((self.command, len(self.data))) + self.data
        return HEAD + body + bytes((checksum(body),)) + TAIL


class Reader:
    """Packets from a byte stream that may split, join or garble them."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self.buffer += data
        packets = []
        while (packet := self._next()) is not None:
            if isinstance(packet, Packet):
                packets.append(packet)
        return packets

    def _next(self) -> Packet | bool | None:
        """A packet, False for skipped garbage, None for "need more bytes"."""
        start = self.buffer.find(HEAD)
        if start < 0:  # keep a last 0x55 that may start a header
            del self.buffer[: len(self.buffer) - (self.buffer[-1:] == b"\x55")]
            return None
        del self.buffer[:start]
        frame = self._frame(0)
        if isinstance(frame, Packet):
            del self.buffer[: len(frame.to_bytes())]
            return frame
        if frame is False:
            del self.buffer[:1]  # not a packet after all: look for the next header
            return False
        # incomplete, or garbage that starts like a header and claims a long
        # length: a complete packet further on means the latter
        later = self.buffer.find(HEAD, 1)
        while later >= 0:
            if isinstance(self._frame(later), Packet):
                del self.buffer[:later]
                return False
            later = self.buffer.find(HEAD, later + 1)
        return None

    def _frame(self, pos: int) -> Packet | bool | None:
        """The packet at pos, False if it is none, None if bytes are missing."""
        if len(self.buffer) < pos + 4:
            return None
        length = self.buffer[pos + 3]
        end = pos + 4 + length + 3
        if len(self.buffer) < end:
            return None
        body = bytes(self.buffer[pos + 2 : pos + 4 + length])
        if self.buffer[pos + 4 + length] == checksum(body) and self.buffer[end - 2 : end] == TAIL:
            return Packet(body[0], body[2:])
        return False


# --- answers ------------------------------------------------------------------------


def error_for(code: int) -> PrinterError:
    """The PrinterError for a printer error code; `reported` marks it as the
    printer's own answer (the status query shows it instead of failing)."""
    cls, text = ERRORS.get(code, (PrinterError, "error"))
    return cls("NIIMBOT error %d: %s" % (code, text), reported=code)


def model_id(data: bytes) -> int:
    if len(data) >= 2:
        return data[0] << 8 | data[1]
    return data[0] << 8 if data else 0


def protocol_version(data: bytes) -> int:
    """From the printer status data: 3, 4, 5, or 0 if it does not say."""
    if len(data) < 13:
        return 0
    n = data[11] * 100 + data[12]
    if n >= 302:
        return 5
    if n >= 300:
        return 4
    return 3 if n >= 204 else 0


def firmware(data: bytes) -> str:
    """Software version: a 16 bit number in hundredths ("5.14")."""
    if len(data) < 2:
        return ""
    return "%.2f" % ((data[0] << 8 | data[1]) / 100)


@dataclass(frozen=True)
class PrintStatus:
    page: int  # labels printed so far
    error: int = 0


def parse_print_status(data: bytes) -> PrintStatus:
    if len(data) < 2:
        raise PrinterError("short print status %s" % data.hex())
    return PrintStatus(data[0] << 8 | data[1], data[6] if len(data) >= 7 else 0)


# --- rows ---------------------------------------------------------------------------


def fit_head(image: Image.Image, head_px: int) -> Image.Image:
    """Wider than the print head: keep the middle (labels are centred)."""
    if image.width <= head_px:
        return image
    left = (image.width - head_px) // 2
    return image.crop((left, 0, left + head_px, image.height))


def ink_rows(image: Image.Image) -> list[bytes]:
    """One bytes object per row along the feed: the leftmost pixel in the
    highest bit, 1 = black, padded to whole bytes."""
    ink = image.convert("L").point(lambda v: 255 if v < 128 else 0)
    bits = ink.convert("1", dither=Image.Dither.NONE).tobytes()
    per_row = (image.width + 7) // 8
    return [bits[i : i + per_row] for i in range(0, len(bits), per_row)]


def black_pixels(row: bytes) -> list[int]:
    return [i * 8 + bit for i, byte in enumerate(row) for bit in range(8) if byte & 0x80 >> bit]


def pixel_counts(row: bytes, head_px: int, total: bool = False) -> bytes:
    """Black pixels in each third of the head, or the total as
    [0, low byte, high byte] where the row does not fit the thirds."""
    chunk = head_px // 24
    if not total and chunk and len(row) <= 3 * chunk:
        thirds = (row[i * chunk : (i + 1) * chunk] for i in range(3))
        return bytes(min(0xFF, sum(b.bit_count() for b in part)) for part in thirds)
    count = sum(b.bit_count() for b in row)
    return bytes((0, count & 0xFF, (count >> 8) & 0xFF))


def row_packet(number: int, row: bytes, repeat: int, counts: bytes) -> Packet:
    """A blank row, a row of black pixel positions, or the full bitmap."""
    if not any(row):
        return Packet(ROW_EMPTY, u16(number) + bytes((repeat,)))
    black = black_pixels(row)
    head = u16(number) + counts + bytes((repeat,))
    if len(black) <= MAX_INDEXED:
        return Packet(ROW_INDEXED, head + b"".join(u16(x) for x in black))
    return Packet(ROW_BITMAP, head + row)


def runs(rows: list[bytes]) -> Iterator[tuple[int, bytes, int]]:
    """(first row, content, count) of identical rows, at most MAX_REPEAT
    each: a longer run goes on at the next row number."""
    start = 0
    while start < len(rows):
        end = start + 1
        while end < len(rows) and end - start < MAX_REPEAT and rows[end] == rows[start]:
            end += 1
        yield start, rows[start], end - start
        start = end


def row_packets(
    rows: list[bytes], head_px: int, total: bool = False, check_lines: bool = False
) -> list[Packet]:
    """The packets of one page; `check_lines` adds a check line after each
    CHECK_LINE_EVERY rows (B21, protocol 1)."""
    packets = []
    for start, row, count in runs(rows):
        packets.append(row_packet(start, row, count, pixel_counts(row, head_px, total)))
        if check_lines:
            lines = range(start + 1, start + count + 1)
            packets.extend(
                Packet(CHECK_LINE, u16(line) + b"\x01")
                for line in lines
                if line % CHECK_LINE_EVERY == 0
            )
    return packets


# --- jobs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    task: str
    rows: int
    cols: int  # bits per row, a multiple of 8
    page: bytes  # the row packets of one label
    copies: int
    density: int
    label_type: int


def make_job(
    image: Image.Image, model: Model, task: str, copies: int, density: int, label_type: int
) -> Job:
    """Rows of an image already fit to the head, in printer orientation."""
    rows = ink_rows(image)
    packets = row_packets(rows, model.head_px, total=task == "b21", check_lines=task == "b21")
    return Job(
        task,
        len(rows),
        (image.width + 7) // 8 * 8,
        b"".join(p.to_bytes() for p in packets),
        copies,
        density,
        label_type,
    )


# --- connection ---------------------------------------------------------------------


class NotSupported(PrinterError):
    """The printer answered 0x00: it does not know the request."""


def _connect(host: str, wait: float) -> serialport.Port:
    return serialport.Port(serialport.device_path(host), wait)


def map_error(error: Exception) -> PrinterError:
    mapped = network_error(error)
    return mapped if mapped else PrinterError(error_text(error))


class Session:
    """Requests and answers on an open port, one at a time."""

    def __init__(self, port: serialport.Port):
        self.port = port
        self.reader = Reader()
        self.pending: list[Packet] = []

    def send(self, command: int, data: bytes = b"\x01") -> None:
        self.port.write(Packet(command, data).to_bytes(), WRITE_TIMEOUT)

    def receive(self, expect: tuple[int, ...], timeout: float) -> Packet:
        """The next answer with one of the expected ids; others are
        dropped, error answers raise. PrinterTimeout if none comes."""
        deadline = time.monotonic() + timeout
        while True:
            while self.pending:
                packet = self.pending.pop(0)
                if packet.command in expect:
                    return packet
                _check(packet)
            chunk = self.port.read(deadline)
            if not chunk:
                raise PrinterTimeout(
                    "no answer %s within %g s" % ("/".join("0x%02X" % e for e in expect), timeout)
                )
            self.pending.extend(self.reader.feed(chunk))

    def request(
        self,
        command: int,
        data: bytes = b"\x01",
        timeout: float | None = None,
        expect: tuple[int, ...] = (),
    ) -> Packet:
        """Send and wait for the answer (REQUEST_TIMEOUT unless given)."""
        self.send(command, data)
        return self.receive(expect or ANSWERS[command], timeout or REQUEST_TIMEOUT)

    def optional(self, command: int, data: bytes = b"\x01", expect: tuple[int, ...] = ()):
        """A request some printers do not answer: None then."""
        try:
            return self.request(command, data, expect=expect)
        except (PrinterTimeout, NotSupported):
            return None

    def info(self, kind: int) -> Packet:
        return self.request(PRINTER_INFO, bytes((kind,)), expect=(PRINTER_INFO + kind,))

    def heartbeat(self, kind: int = 2) -> Packet | None:
        return self.optional(HEARTBEAT, bytes((kind,)), expect=(HEARTBEAT_ANSWERS[kind],))

    def cancel(self) -> None:
        """Drop the job, so that the printer is ready for the next one."""
        with suppress(PrinterError, OSError):
            self.request(CANCEL)


def _check(packet: Packet) -> None:
    """An answer nobody waits for: errors raise, the rest is dropped."""
    if packet.command == ERROR:
        code = packet.data[0] if packet.data else 0
        if code and code not in NON_FATAL:
            raise error_for(code)
    elif packet.command == NOT_SUPPORTED:
        raise NotSupported("request not supported: %s" % packet.data.hex())


def session(host: str, wait: float = serialport.BUSY_WAIT) -> AbstractContextManager[Session]:
    return open_session(lambda: _connect(host, wait), Session, map_error)


@dataclass(frozen=True)
class Identity:
    model_id: int
    model: Model | None
    protocol: int = 0
    firmware: str = ""


def identify(s: Session, with_firmware: bool = False) -> Identity:
    """Connect, then ask for the model (and firmware)."""
    protocol = 0
    connected = s.optional(CONNECT)
    if connected and connected.data[:1] == b"\x03":  # protocol 3 or newer
        status = s.optional(STATUS_DATA)
        protocol = protocol_version(status.data) if status else 0
    number = model_id(s.info(INFO_MODEL_ID).data)
    version = ""
    if with_firmware:
        with suppress(PrinterTimeout, NotSupported):
            version = firmware(s.info(INFO_FIRMWARE).data)
    return Identity(number, BY_ID.get(number), protocol, version)


def _start(s: Session, job: Job) -> None:
    """Density, label type and the job's start; protocol 4 sets the label
    type first and announces pages, colour and speed."""
    settings = [
        (SET_DENSITY, bytes((job.density,))),
        (SET_LABEL_TYPE, bytes((job.label_type,))),
    ]
    for command, data in settings[::-1] if job.task == "v4" else settings:
        s.request(command, data)
    if job.task == "b1":
        s.request(PRINT_START, u16(job.copies) + bytes(5))  # 0, 0, 0, 0, one colour
    elif job.task == "v4":
        # pages, 0, 0, 0, 0, one colour, speed 1 (the default), flag 0
        s.request(PRINT_START, u16(job.copies) + bytes(5) + b"\x01\x00")
        s.send(PRINT_STATUS)  # protocol 4 expects it before the first page
    else:
        s.request(PRINT_START)


def _page_size(job: Job, copies: int) -> bytes:
    size = u16(job.rows) + u16(job.cols)
    if job.task == "b1":
        return size + u16(copies)
    if job.task == "v4":
        # copies, cut height, cut type, 0, send all, part height
        return size + u16(copies) + bytes(7)
    return size


def _page(s: Session, job: Job, copies: int) -> None:
    """One label: rows and their frame; the printer prints `copies`."""
    if job.task == "d110":
        s.request(PRINT_CLEAR)
    s.request(PAGE_START)
    s.request(SET_PAGE_SIZE, _page_size(job, copies))
    if job.task == "d110":
        s.request(PRINT_QUANTITY, u16(copies))
    s.port.write(job.page, PAGE_TIMEOUT)
    s.request(PAGE_END, timeout=PAGE_TIMEOUT)


def _wait_pages(s: Session, total: int, progress: ProgressCallback | None) -> None:
    """Poll until the printer has printed `total` labels; the timeout
    restarts with every label. Errors say how many came out."""
    done = 0
    deadline = time.monotonic() + PRINT_TIMEOUT
    try:
        while True:
            status = parse_print_status(s.request(PRINT_STATUS, timeout=STATUS_TIMEOUT).data)
            if status.page > done:
                done = status.page
                deadline = time.monotonic() + PRINT_TIMEOUT
                report_progress(progress, min(total, done + 1), total)
            if status.error and status.error not in NON_FATAL:
                raise error_for(status.error)
            if done >= total:
                return
            if time.monotonic() > deadline:
                raise PrinterTimeout("printed %d of %d labels, then nothing" % (done, total))
            _sleep(POLL_INTERVAL)
    except PrinterError as e:
        if done:
            e.params.update(printed=done, copies=total)
        raise


def _wait_print_end(s: Session) -> None:
    """B21 (protocol 1): asked to end, the printer says 1 once it is done."""
    deadline = time.monotonic() + PRINT_TIMEOUT
    while s.request(PRINT_END, timeout=STATUS_TIMEOUT).data[:1] != b"\x01":
        if time.monotonic() > deadline:
            raise PrinterTimeout("the printer did not finish within %g s" % PRINT_TIMEOUT)
        _sleep(POLL_INTERVAL)


def run_job(s: Session, job: Job, progress: ProgressCallback | None) -> None:
    _start(s, job)
    if job.task == "b21":  # no copies field: one page per copy
        for n in range(1, job.copies + 1):
            report_progress(progress, n, job.copies)
            _page(s, job, 1)
        _wait_print_end(s)
        return
    _page(s, job, job.copies)
    _wait_pages(s, job.copies, progress)
    s.request(PRINT_END)
    if job.task == "v4":
        s.send(HEARTBEAT)  # protocol 4 printers expect one after the end


# --- the backend --------------------------------------------------------------------


def is_niimbot(device: serialport.SerialDevice) -> bool:
    """By the USB strings: NIIMBOT names itself as maker or in the product
    [assumed: to be checked on a printer], or by its vendor id."""
    text = (device.manufacturer + " " + device.product).casefold()
    return "niimbot" in text or device.vendor_id in VENDOR_IDS


# USB vendor ids of NIIMBOT printers [assumed: not in the sources, verify with lsusb]
VENDOR_IDS = frozenset({0x3513})


class Niimbot(Backend):
    id = "niimbot"
    name = "NIIMBOT B1, B21, B3S, B4, D11, D110, D101"
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI_203,
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=max(model.widest_mm for model in MODELS),
        data_formats=("png",),
        density=D5,
        media_types=GBCT,
    )

    def __init__(self) -> None:
        self.model: Model | None = None  # of the printer last asked

    def _model(self, info: PrinterInfo | None) -> Model | None:
        return BY_NAME.get(info.model) if info else self.model

    def display_name(self, info: PrinterInfo | None) -> str:
        if info and info.model:
            return "NIIMBOT " + info.model
        return self.name

    def density(self, info: PrinterInfo | None) -> Density | None:
        model = self._model(info)
        return model.density if model else self.capabilities.density

    def media_types(self, info: PrinterInfo | None) -> tuple[str, ...]:
        model = self._model(info)
        return model.media if model else self.capabilities.media_types

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """NIIMBOT printers on USB serial; nothing is sent to find them."""
        return [
            FoundPrinter(
                "%s (USB)" % (device.product or "NIIMBOT"), device.address, str(device.device)
            )
            for device in serialport.devices()
            if is_niimbot(device)
        ]

    def query(self, host: str) -> PrinterInfo:
        """Model and firmware; a problem the printer reports (cover open, no
        labels) is part of the answer, not a failure."""
        error = ""
        identity = None
        with session(host) as s:
            try:
                identity = identify(s, with_firmware=True)
                s.heartbeat()
            except PrinterError as e:
                if "reported" not in e.params:
                    raise
                error = e.code
        if identity:
            self.model = identity.model or self.model
        return PrinterInfo(
            model=_model_name(identity, self.model),
            tape_mm=None,
            remaining_mm=None,
            total_mm=None,
            state="ERROR" if error else "IDLE",
            error=error,
            firmware=identity.firmware if identity else "",
        )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(self.model.dpi if self.model else self.capabilities.dpi)

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """In printer orientation: x across the print head, y along the
        feed. The resolution it was made for travels with the image."""
        dpi = self.raster(label.tape_mm, options).dpi
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info["niimbot_dpi"] = dpi
        return image

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        rendered = float(image.info.get("niimbot_dpi", self.capabilities.dpi))
        copies = max(1, min(options.copies, 0xFFFF))
        with session(host, serialport.PRINT_WAIT) as s:
            report_progress(progress, 1, copies)
            identity = identify(s)
            model = identity.model
            if model is None:
                raise PrinterError("unknown NIIMBOT model id %d" % identity.model_id)
            self.model = model
            task = task_for(model, identity.protocol)
            job = _job_for(image, rendered, model, task, options, copies)
            try:
                run_job(s, job, progress)
            except (PrinterError, OSError):
                s.cancel()
                raise
        return None


def _model_name(identity: Identity | None, known: Model | None) -> str:
    """The model's name; an unknown one by its id, none if the printer did not say."""
    if identity is None:
        return known.name if known else ""
    return identity.model.name if identity.model else "model %d" % identity.model_id


def _job_for(
    image: Image.Image,
    rendered: float,
    model: Model,
    task: str,
    options: PrintOptions,
    copies: int,
) -> Job:
    """Checks and rows for the connected model."""
    # as designed; a pixel more is rounding (50 mm at 300 dpi are 591 px)
    width_mm = image.width * 25.4 / rendered
    if width_mm > model.widest_mm + max(LIMIT_SLACK_MM, 25.4 / rendered):
        raise LabelTooWide(
            "%.1f mm is wider than %.1f mm (NIIMBOT %s)" % (width_mm, model.widest_mm, model.name),
            max_mm=model.widest_mm,
        )
    if not math.isclose(rendered, model.dpi):  # made before the printer was asked
        factor = model.dpi / rendered
        size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
        image = image.resize(size, Image.Resampling.NEAREST)
    if image.height > MAX_ROWS:
        limit = MAX_ROWS * 25.4 / model.dpi
        raise LabelTooLong("%d rows" % image.height, max_mm=round(limit, 1))
    media = options.media if options.media in model.media else model.media[0]
    return make_job(
        fit_head(image, model.head_px),
        model,
        task,
        copies,
        model.density.clamp(options.density),
        LABEL_TYPES[media],
    )
