"""NIIMBOT label printers (B1, B21, B3S, B4, D11, D110, D101 ...) on USB
serial (see docs/niimbot.md).

The printers speak a small binary protocol of packets
55 55 CMD LEN DATA CHK AA AA, the same over USB (a CDC ACM serial port,
/dev/ttyACM0) and Bluetooth; Etiketto uses USB only. A job sets density and
label type, then sends the label row by row (blank rows, rows with a few
black pixels and full bitmap rows each have their own packet) and polls the
printer until it has printed all copies. Which commands a model takes
depends on its model id and protocol version, which the printer reports.

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
    Choice,
    CoverOpen,
    FoundPrinter,
    HeadOpen,
    JobCanceled,
    LabelNotFound,
    LabelSize,
    LabelTooLong,
    LabelTooWide,
    Level,
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
    Setting,
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

HEAD = b"\x55\x55"
TAIL = b"\xaa\xaa"
CONNECT_PREFIX = b"\x03"  # the printer wants it in front of a Connect packet

# seconds: an ordinary answer, a status poll (and the wait for the next page
# index), the answer after a page of rows
REQUEST_TIMEOUT = 1.0
STATUS_TIMEOUT = 5.0
PAGE_TIMEOUT = 10.0
POLL_INTERVAL = 0.3  # between two status polls while printing
RETRY_INTERVAL = 0.5  # between two tries of a page start or page end (B21 L2B)
PAGE_START_TRIES = 5
PAGE_END_TRIES = 20
PRINT_TIMEOUT = 60.0  # without progress while waiting for a job to finish
WRITE_TIMEOUT = 5.0  # without the port taking data

MAX_REPEAT = 255  # the repeat field of a row packet is one byte
MAX_INDEXED = 6  # rows with up to this many black pixels are sent as positions
MAX_ROWS = 0xFFFF  # the row number is 16 bits
# a check point after each 200 rows (rows 199, 399, ...): runs of identical
# rows end there for every model, the B21 sends a check line there
CHECK_LINE_EVERY = 200

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
CHECK_LINE = 0x86
# rows; the printer does not answer them
ROW_BITMAP = 0x85
ROW_INDEXED = 0x83
ROW_EMPTY = 0x84
ROWS = (ROW_BITMAP, ROW_INDEXED, ROW_EMPTY)
# answers that can come instead of the expected one, or unasked
ERROR = 0xDB
NOT_SUPPORTED = 0x00
PAGE_INDEX = 0xE0  # old D11: the number of the label just printed

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
    CHECK_LINE: (0xD3,),
}
HEARTBEAT_ANSWERS = {1: 0xDD, 2: 0xDE, 3: 0xDF, 4: 0xD9}
INFO_MODEL_ID = 8
INFO_FIRMWARE = 9
CONNECTED_NEW = 2  # Connect answer: protocol 1
CONNECTED_V3 = 3  # Connect answer: the status data tell the protocol

# label types by the values of the "media" setting (ui/texts.CHOICE_NAMES)
LABEL_TYPES = {"gap": 1, "mark": 2, "continuous": 3, "transparent": 5}

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
LOW_BATTERY_PERCENT = 10  # the heartbeat's charge at or below this is "low"

_sleep = time.sleep  # replaced in tests


# --- models -------------------------------------------------------------------------


@dataclass(frozen=True)
class Density:
    """The print density (darkness) a model takes per job: lowest to
    highest step, and the one it uses by default."""

    lowest: int
    highest: int
    default: int

    def level(self) -> Level:
        """As the printer setting "density" (None: the default)."""
        return Level("density", self.lowest, self.highest)


@dataclass(frozen=True)
class Model:
    """What a model can do, by the ids it reports (NIIMBOT's devices.json
    through niimbluelib; the widest label from Thermalith's printers.json)."""

    name: str
    ids: tuple[int, ...]
    dpi: float
    head_px: int  # pixels across the print head
    media: tuple[str, ...]  # label types, the default first
    density: Density
    # command sequence (see task_for); "" = none of its own
    task: str
    widest_mm: float  # the widest label (stock), a little wider than the head prints

    @property
    def head_mm(self) -> float:
        return self.head_px * 25.4 / self.dpi


GBT = ("gap", "mark", "transparent")
GBCT = ("gap", "mark", "continuous", "transparent")
GT = ("gap", "transparent")
D5 = Density(1, 5, 3)
D3 = Density(1, 3, 2)
B21C2B_MEDIA = ("gap", "continuous", "transparent", "mark")

MODELS = (
    Model("B1", (4096,), DPI_203, 384, GBT, D5, "b1", 50),
    Model("B1 PRO", (4097,), DPI_300, 567, GBT, D5, "v4", 50),
    Model("B1 SE", (4098,), DPI_203, 384, GBT, D5, "", 54),
    Model("B21", (768,), DPI_203, 384, GBCT, D5, "b21", 50),
    Model("B21 PRO", (785,), DPI_300, 591, GBCT, D5, "v4", 50),
    Model("B21 C2B", (771, 775), DPI_203, 384, B21C2B_MEDIA, D5, "b1", 50),
    Model("B21 L2B", (769,), DPI_203, 384, GBT, D5, "b21_l2b", 50),
    Model("B21S", (777,), DPI_203, 384, GBCT, D5, "d110", 50),
    Model("B203", (2816,), DPI_203, 400, GBT, D5, "", 50),
    Model("B3", (52993,), DPI_203, 600, GBCT, D5, "", 75),
    Model("B31", (5632,), DPI_203, 600, GBT, D5, "", 77),
    Model("B32", (2049,), DPI_300, 851, GT, Density(1, 15, 10), "", 75),
    Model("B3S", (256, 260, 262), DPI_203, 576, GBCT, D5, "", 75),
    # B3S P and B4: the B1 sequence on any protocol, tried on hardware (Thermalith)
    Model("B3S P", (272,), DPI_203, 576, GBCT, D5, "b1", 75),
    Model("B4", (6656,), DPI_203, 832, GBT, D5, "b1", 108),
    Model("D11", (512,), DPI_203, 96, GT, D3, "d11", 15),
    Model("D11S", (514,), DPI_203, 96, GT, D3, "d11", 15),
    Model("D11 H", (528,), DPI_300, 142, GT, D5, "v4", 15),
    Model("D110", (2304, 2305), DPI_203, 96, GT, D3, "d110", 15),
    Model("D110 M", (2320,), DPI_203, 96, GT, D5, "b1", 15),
    Model("D101", (2560,), DPI_203, 192, GT, D3, "b1", 25),
    Model("B18", (3584,), DPI_203, 96, ("gap", "transparent", "continuous"), D3, "", 15),
    Model("H1S", (4352,), DPI_203, 96, ("gap", "continuous", "transparent"), D3, "h1s", 15),
)
BY_ID = {model_id: model for model in MODELS for model_id in model.ids}
BY_NAME = {model.name: model for model in MODELS}
# models whose lid byte in the heartbeat means "open" where others mean "closed"
INVERTED_LID = frozenset({512, 514, 513, 2304, 1792, 3584, 5120, 2560, 3840, 4352, 272, 273, 274})

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
    """The command sequence for a model and protocol version, as niimbluelib
    picks it: the D11 (not the D11S) speaks the D110 sequence on protocol 1
    and 2, the D110 M
    the protocol 4 one on protocol 4; a model without a sequence of its own
    takes protocol 4 from version 4 on, else the B1 one (as Thermalith)."""
    if model.name == "D11" and protocol in (1, 2):
        return "d110"
    if model.name == "D110 M" and protocol == 4:
        return "v4"
    if not model.task:
        return "v4" if protocol >= 4 else "b1"
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
        frame = HEAD + body + bytes((checksum(body),)) + TAIL
        return CONNECT_PREFIX + frame if self.command == CONNECT else frame


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
            del self.buffer[: 7 + len(frame.data)]
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
    """Software version: a 16 bit number in hundredths ("7.81")."""
    if len(data) != 2:
        return ""
    return "%.2f" % ((data[0] << 8 | data[1]) / 100)


@dataclass(frozen=True)
class PrintStatus:
    page: int  # labels printed so far
    printed: int = 100  # percent of the current label printed
    fed: int = 100  # percent of the current label fed out
    error: int = 0


def parse_print_status(data: bytes) -> PrintStatus:
    """page u16, print and feed progress; the 10 byte form has an error at 6."""
    if len(data) < 4:
        raise PrinterError("short print status %s" % data.hex())
    error = data[6] if len(data) == 10 else 0
    return PrintStatus(data[0] << 8 | data[1], data[2], data[3], error)


@dataclass(frozen=True)
class Heartbeat:
    """What a heartbeat says; None where this answer does not tell."""

    lid_closed: bool | None = None
    paper: bool | None = None
    battery: int | None = None  # percent

    def error(self) -> str:
        """The PrinterError code of a problem it shows, or ""."""
        if self.lid_closed is False:
            return CoverOpen.code
        if self.paper is False:
            return OutOfLabels.code
        if self.battery is not None and 0 < self.battery <= LOW_BATTERY_PERCENT:
            return LowBattery.code
        return ""


# heartbeat type 1 (answer 0xDD): offset of the lid byte by length, then battery, paper
_ADVANCED1 = {10: (8, False), 13: (9, True), 19: (15, True)}


def _percent(level: int) -> int:
    """Charge as 0 to 4 steps on some printers, in percent on others."""
    return level * 25 if level <= 4 else level


def parse_heartbeat(command: int, data: bytes, model: int) -> Heartbeat:
    """Heartbeat answers: type 1 (protocol below 3) or type 4. Lengths the
    sources do not know tell nothing."""
    if command == HEARTBEAT_ANSWERS[4]:
        if len(data) < 9:
            return Heartbeat()
        return Heartbeat(data[4] == 0, data[5] == 0, _percent(data[2]))
    if len(data) == 20:
        return Heartbeat(paper=data[18] == 0)
    if len(data) not in _ADVANCED1:
        return Heartbeat()
    at, has_paper = _ADVANCED1[len(data)]
    lid = (data[at] == 0) != (model in INVERTED_LID)
    paper = data[at + 2] == 0 if has_paper else None
    return Heartbeat(lid, paper, _percent(data[at + 1]))


# --- rows ---------------------------------------------------------------------------


def position_on_head(image: Image.Image, head_px: int) -> Image.Image:
    """Exactly the head's width, the label in the middle: the protocol has no
    offset, so a narrower label is padded with white on both sides and a
    wider one is cut to the middle."""
    if image.width == head_px:
        return image
    left = (head_px - image.width) // 2
    page = Image.new("RGB", (head_px, image.height), "white")
    page.paste(image.convert("RGB"), (left, 0))
    return page


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
    """Black pixels in each third of the head (one byte each: more than 255
    wrap, as in niimbluelib), or the total as [0, low byte, high byte] where
    the row does not fit the thirds."""
    chunk = head_px // 24
    if not total and chunk and len(row) <= 3 * chunk:
        thirds = (row[i * chunk : (i + 1) * chunk] for i in range(3))
        return bytes(sum(b.bit_count() for b in part) & 0xFF for part in thirds)
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


def runs(
    rows: list[bytes], check_every: int = CHECK_LINE_EVERY
) -> Iterator[tuple[int, bytes, int]]:
    """(first row, content, count) of identical rows. A run ends at each
    check point (after rows 199, 399, ...) and after MAX_REPEAT rows; it
    goes on at the next row number."""
    start = 0
    while start < len(rows):
        end = start + 1
        while (
            end < len(rows)
            and end - start < MAX_REPEAT
            and not (check_every and end % check_every == 0)
            and rows[end] == rows[start]
        ):
            end += 1
        yield start, rows[start], end - start
        start = end


def row_packets(
    rows: list[bytes], head_px: int, total: bool = False, check_lines: bool = False
) -> list[Packet]:
    """The packets of one page; `check_lines` adds a check line at each
    check point (B21), numbered by the row it follows."""
    packets = []
    for start, row, count in runs(rows):
        packets.append(row_packet(start, row, count, pixel_counts(row, head_px, total)))
        last = start + count - 1
        if check_lines and last % CHECK_LINE_EVERY == CHECK_LINE_EVERY - 1:
            packets.append(Packet(CHECK_LINE, u16(last) + b"\x01"))
    return packets


# --- jobs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    task: str
    rows: int
    cols: int  # pixels per row: the head's width
    page: tuple[Packet, ...]  # the rows of one label (and its check lines)
    copies: int
    density: int
    label_type: int


def make_job(
    image: Image.Image, model: Model, task: str, copies: int, density: int, label_type: int
) -> Job:
    """Rows of an image in printer orientation, placed on the head."""
    page = position_on_head(image, model.head_px)
    rows = ink_rows(page)
    checks = task in ("b21", "b21_l2b")
    packets = row_packets(rows, model.head_px, total=checks, check_lines=checks)
    return Job(task, len(rows), page.width, tuple(packets), copies, density, label_type)


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
        self.page_index = 0  # the last 0xE0 the printer sent (old D11)

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
                self._check(packet)
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

    def heartbeat(self, kind: int) -> Packet | None:
        return self.optional(HEARTBEAT, bytes((kind,)), expect=(HEARTBEAT_ANSWERS[kind],))

    def cancel(self) -> None:
        """Drop the job, so that the printer is ready for the next one."""
        with suppress(PrinterError, OSError):
            self.request(CANCEL)

    def _check(self, packet: Packet) -> None:
        """An answer nobody waits for: errors raise, a page index is kept,
        the rest is dropped."""
        if packet.command == ERROR:
            code = packet.data[0] if packet.data else 0
            if code and code not in NON_FATAL:
                raise error_for(code)
        elif packet.command == NOT_SUPPORTED:
            raise NotSupported("request not supported: %s" % packet.data.hex())
        elif packet.command == PAGE_INDEX:
            self.note_page_index(packet)

    def note_page_index(self, packet: Packet) -> None:
        if len(packet.data) == 2:
            self.page_index = max(self.page_index, model_id(packet.data))


def session(host: str, wait: float = serialport.BUSY_WAIT) -> AbstractContextManager[Session]:
    return open_session(lambda: _connect(host, wait), Session, map_error)


@dataclass(frozen=True)
class Identity:
    model_id: int
    model: Model | None
    protocol: int = 0
    firmware: str = ""


def identify(s: Session, with_firmware: bool = False) -> Identity:
    """Connect (which tells the protocol), then ask for the model (and firmware)."""
    protocol = 0
    connected = s.optional(CONNECT)
    result = connected.data[0] if connected and connected.data else 0
    if result == CONNECTED_NEW:
        protocol = 1
    elif result == CONNECTED_V3:
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
    elif job.task == "h1s":
        s.request(PRINT_START, u16(job.copies))
    else:
        s.request(PRINT_START)


def _page_size(job: Job, copies: int) -> bytes:
    rows, size = u16(job.rows), u16(job.rows) + u16(job.cols)
    sizes = {
        "d11": rows,  # rows only
        "b1": size + u16(copies),
        "h1s": size + u16(copies) + bytes(3),  # cut height, cut type
        # cut height, cut type, 0, send all, part height
        "v4": size + u16(copies) + bytes(7),
    }
    return sizes.get(job.task, size)


def _rows(s: Session, job: Job) -> None:
    """The rows of one label; a check line waits for its answer."""
    chunk: list[bytes] = []
    for packet in job.page:
        if packet.command == CHECK_LINE:
            s.port.write(b"".join(chunk), PAGE_TIMEOUT)
            chunk = []
            s.request(CHECK_LINE, packet.data, timeout=PAGE_TIMEOUT)
        else:
            chunk.append(packet.to_bytes())
    s.port.write(b"".join(chunk), PAGE_TIMEOUT)


def _until_true(s: Session, command: int, tries: int) -> None:
    """Repeat a request until the printer answers 1 (B21 L2B), like
    niimbluelib's doUntilTrue: a refusal waits RETRY_INTERVAL, a timeout or
    an error answer is tried again at once; the last problem is raised."""
    problem: PrinterError = PrinterBusy("the printer refused 0x%02X %d times" % (command, tries))
    for _ in range(tries):
        try:
            if s.request(command).data[:1] == b"\x01":
                return
        except (PrinterTimeout, NotSupported) as e:
            problem = e
            continue
        except PrinterError as e:
            if "reported" not in e.params:
                raise
            problem = e
            continue
        _sleep(RETRY_INTERVAL)
    raise problem


def _page(s: Session, job: Job, copies: int) -> None:
    """One label: its frame and rows; the printer prints `copies`."""
    if job.task in ("d110", "d11"):
        s.request(PRINT_CLEAR)
    if job.task == "v4":  # no page start: a status request nobody waits for
        s.send(PRINT_STATUS)
    elif job.task == "b21_l2b":
        _until_true(s, PAGE_START, PAGE_START_TRIES)
    else:
        s.request(PAGE_START)
    s.request(SET_PAGE_SIZE, _page_size(job, copies))
    if job.task in ("d110", "d11"):
        s.request(PRINT_QUANTITY, u16(copies))
    _rows(s, job)
    s.request(PAGE_END, timeout=PAGE_TIMEOUT)


def _with_count(error: PrinterError, done: int, total: int) -> PrinterError:
    if done:
        error.params.update(printed=done, copies=total)
    return error


def _finished(status: PrintStatus, total: int, feed: bool) -> bool:
    """All labels counted, the last one printed to its end (and fed out)."""
    done = status.printed >= 100 and (status.fed >= 100 or not feed)
    return status.page >= total and done


@dataclass
class _Polls:
    """What the status polls of a job said so far. The first status may
    still be the last job's: a finished one counts once the job was seen
    unfinished, or when the next poll is finished too."""

    total: int
    feed: bool
    done: int = 0  # labels printed, as far as accepted
    unfinished: bool = False
    was_finished: bool = False

    def accept(self, status: PrintStatus) -> bool:
        """Note a status; True when the job is done. A finished status not
        yet accepted does not count labels (it may be a stale one)."""
        finished = _finished(status, self.total, self.feed)
        ended = finished and (self.unfinished or self.was_finished)
        if ended or not finished:
            self.done = max(self.done, min(status.page, self.total))
        self.unfinished = self.unfinished or not finished
        self.was_finished = finished
        return ended


def _wait_pages(s: Session, total: int, progress: ProgressCallback | None, feed: bool) -> None:
    """Poll until the printer has printed `total` labels and the last one
    to the end (and fed it out, unless `feed` is False: protocol 4 printers
    may never report that). The timeout restarts with every change."""
    polls = _Polls(total, feed)
    last = None
    deadline = time.monotonic() + PRINT_TIMEOUT
    try:
        while True:
            status = parse_print_status(s.request(PRINT_STATUS, timeout=STATUS_TIMEOUT).data)
            before = polls.done
            ended = polls.accept(status)
            if polls.done > before:
                report_progress(progress, min(total, polls.done + 1), total)
            if status.error and status.error not in NON_FATAL:
                raise error_for(status.error)
            if ended:
                return
            if status != last:
                last, deadline = status, time.monotonic() + PRINT_TIMEOUT
            elif time.monotonic() > deadline:
                raise PrinterTimeout("printed %d of %d labels, then nothing" % (polls.done, total))
            _sleep(POLL_INTERVAL)
    except PrinterError as e:
        raise _with_count(e, polls.done, total) from None


def _wait_page_index(s: Session, total: int, progress: ProgressCallback | None) -> None:
    """Old D11: the printer sends the number of each label it printed, also
    while Etiketto still waits for another answer (kept in the session)."""
    done = 0
    try:
        while True:
            if s.page_index > done:
                done = s.page_index
                report_progress(progress, min(total, done + 1), total)
            if done >= total:
                return
            s.note_page_index(s.receive((PAGE_INDEX,), STATUS_TIMEOUT))
    except PrinterError as e:
        raise _with_count(e, done, total) from None


def _wait_print_end(s: Session, copies: int) -> None:
    """B21 (protocol 1): asked to end, the printer says 1 once it is done.
    Its answer has no progress to restart a timeout with, so the job may
    take PRINT_TIMEOUT per label."""
    limit = PRINT_TIMEOUT * copies
    deadline = time.monotonic() + limit
    while s.request(PRINT_END, timeout=STATUS_TIMEOUT).data[:1] != b"\x01":
        if time.monotonic() > deadline:
            raise PrinterTimeout("the printer did not finish within %g s" % limit)
        _sleep(POLL_INTERVAL)


def _end(s: Session) -> None:
    """The labels are out: an answer to the end that comes late or not at
    all does not make the job fail."""
    with suppress(PrinterTimeout):
        s.request(PRINT_END, timeout=STATUS_TIMEOUT)


def run_job(s: Session, job: Job, progress: ProgressCallback | None) -> None:
    _start(s, job)
    if job.task in ("b21", "b21_l2b"):  # no copies field: one page per copy
        for n in range(1, job.copies + 1):
            report_progress(progress, n, job.copies)
            _page(s, job, 1)
        if job.task == "b21":
            _wait_print_end(s, job.copies)
            return
        _until_true(s, PAGE_END, PAGE_END_TRIES)
        _end(s)
        return
    _page(s, job, job.copies)
    if job.task == "d11":
        _wait_page_index(s, job.copies, progress)
    else:
        _wait_pages(s, job.copies, progress, feed=job.task != "v4")
    _end(s)
    if job.task == "v4":
        s.send(HEARTBEAT)  # protocol 4 printers expect one after the end


# --- the backend --------------------------------------------------------------------


# USB vendor id of NIIMBOT printers with a native USB serial port (Thermalith,
# seen on a B3S P)
VENDOR_IDS = frozenset({0x3513})


def is_niimbot(device: serialport.SerialDevice) -> bool:
    """By the vendor id, or "NIIMBOT" in the USB maker or product name."""
    text = (device.manufacturer + " " + device.product).casefold()
    return "niimbot" in text or device.vendor_id in VENDOR_IDS


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
        settings=(Choice("media", GBCT), D5.level()),
    )
    # the connected model's density range and label types (settings())
    query_before_render = True

    def __init__(self) -> None:
        self.model: Model | None = None  # of the printer last asked

    def _model(self, info: PrinterInfo | None) -> Model | None:
        return BY_NAME.get(info.model) if info else self.model

    def display_name(self, info: PrinterInfo | None) -> str:
        if info and info.model:
            return "NIIMBOT " + info.model
        return self.name

    def settings(self, info: PrinterInfo | None) -> tuple[Setting, ...]:
        """The label types and density range of the connected model."""
        model = self._model(info)
        if model is None:
            return self.capabilities.settings
        return (Choice("media", model.media), model.density.level())

    def migrate_settings(self, entry: dict) -> None:
        """Earlier versions stored the density as 0 for the default and the
        label type with black marks as "black_mark"."""
        if entry.get("density") == 0:
            del entry["density"]
        if entry.get("media") == "black_mark":
            entry["media"] = "mark"

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
        """Model and firmware, and cover, labels and battery from the
        heartbeat; a problem the printer reports is part of the answer,
        not a failure."""
        error = ""
        identity = None
        with session(host) as s:
            try:
                identity = identify(s, with_firmware=True)
                error = _heartbeat(s, identity).error()
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


def _heartbeat(s: Session, identity: Identity) -> Heartbeat:
    """Type 4 from protocol 3 on, else type 1; nothing if it does not answer."""
    kind = 4 if identity.protocol >= 3 else 1
    packet = s.heartbeat(kind)
    if packet is None:
        return Heartbeat()
    return parse_heartbeat(packet.command, packet.data, identity.model_id)


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
    media = Choice("media", model.media).value(options.settings.get("media"))
    density = model.density.level().value(options.settings.get("density"))
    if density is None:  # "as set on the printer": the model's default
        density = model.density.default
    return make_job(image, model, task, copies, density, LABEL_TYPES[media])
