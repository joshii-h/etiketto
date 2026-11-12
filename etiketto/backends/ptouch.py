"""Brother P-touch tape printers (TZe tape, HSe heat-shrink tube): Brother's
raster commands on USB or TCP port 9100 (see docs/ptouch.md).

Written anew from the facts collected in docs/ptouch.md: Brother's raster
command references for the PT-E550W/P750W/P710BT and the PT-P900 series, and
the model flags and quirks documented by ptouch-print (Dominic Radermacher)
and ptouch (Nicolai Buchwitz). No code of either project is included.

A job is a stream of commands. Each raster line is one column of the label
across the tape: the image is turned so that its height runs across the
print head, one bit per pin.
"""

from __future__ import annotations

import re
import socket
import struct
from contextlib import AbstractContextManager
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from PIL import Image

from etiketto.backends import mdns, usb
from etiketto.backends.base import (
    Backend,
    BadCassette,
    Capabilities,
    CassetteInfo,
    CoverOpen,
    EditorLite,
    FoundPrinter,
    LabelNotFound,
    LabelTooLong,
    LowBattery,
    NoCassette,
    Overheated,
    PaperJam,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterStalled,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    Raster,
    UnknownModel,
    UnsupportedModel,
    UsbNotFound,
    check_length,
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, render

DEFAULT_PORT = 9100
BROTHER = 0x04F9  # USB vendor ID
SOCKET_TIMEOUT = 5.0  # connecting and each status answer
REPLY_WAIT = 0.5  # an answer that may or may not come (after switching to raster)
# a printer stops reading while it prints a page, and reports a page only
# once it is done: wait this long plus the time a page takes at MIN_SPEED
PRINT_TIMEOUT = 30.0
MIN_SPEED_MM_S = 10.0  # [assumed] slower than any P-touch (Brother: 20 to 60 mm/s)
GARBAGE_LIMIT = 4096  # bytes without a status in them: not a P-touch
THRESHOLD = 128  # grey levels below this print; no dithering, codes stay sharp
MAX_LENGTH_MM = 1000.0  # Brother: TZe tape up to 1000 mm
MAX_TUBE_MM = 500.0  # Brother: heat-shrink tube up to 500 mm
MIN_LENGTH_MM = 4.4  # Brother: the shortest print data (4 to 4.4 mm by model and media)
DEFAULT_TAPE_MM = 12  # the most common TZe width, for a new label
# blank tape before and after a label: min, max, default; Brother's minimum
# is 2 mm on the 128-pin models and 1 mm on the PT-P900 series (Model.min_feed_mm)
FEED_MM = (1.0, 127.0, 2.0)
SEND_CHUNK = 65536

ESC = b"\x1b"
INIT = ESC + b"@"
STATUS_REQUEST = ESC + b"iS"
RASTER_MODE = ESC + b"ia\x01"  # switch to raster mode (Brother reference)
NOTIFY = ESC + b"i!\x00"  # ESC i !: status notifications while printing on
GRAPHICS_RASTER = ESC + b"iR\x01"  # older models: select raster graphics transfer
PRINT_PAGE = b"\x0c"  # print, no feed: more pages follow, or chain printing
PRINT_LAST = b"\x1a"  # print the last page, feed and cut
EMPTY_LINE = b"Z"
LINE = b"G"

# --- tapes and print area ----------------------------------------------------

TAPE_WIDTHS_MM: tuple[float, ...] = (3.5, 6, 9, 12, 18, 24, 36)
LAMINATED = 0x01
HIGH_RES_LAMINATED = 0x09  # ESC i z n2 for high resolution on the PT-P900 series

# The print area per tape width: (blank pins from the first byte of a raster
# line, printable pins); the rest of the head stays blank. Brother's tables
# (2.3.5 Raster line, "Total number of pins") give "pins for left margin",
# "print area" and "pins for right margin", with the right margin at the
# first byte, so the first number here is Brother's right margin. 128 pins
# at 180 dpi: PT-E550W/P750W/P710BT reference; 560 pins at 360 dpi: PT-P900
# reference (left 248/240/219/197/155/112/45, right as here).
TZE_128: dict[float, tuple[int, int]] = {
    3.5: (52, 24),
    6: (48, 32),
    9: (39, 50),
    12: (29, 70),
    18: (8, 112),
    24: (0, 128),
}
TZE_560: dict[float, tuple[int, int]] = {
    3.5: (264, 48),
    6: (256, 64),
    9: (235, 106),
    12: (213, 150),
    18: (171, 234),
    24: (128, 320),
    36: (61, 454),
}
HEAT_SHRINK_2, HEAT_SHRINK_3 = 0x11, 0x17  # media types in the status
TUBES = (HEAT_SHRINK_2, HEAT_SHRINK_3)
# heat-shrink tube by (media type, width in the status). Print areas as
# Brother's tables; the position from ptouch, 1 or 2 pins off Brother's
# numbers: on 560 pins taken from an analysis of Brother's own software,
# on 128 pins [assumed] (Brother, from the first byte:
# 50/28, 40/48, 31/66, 11/106, 0/128, 54/20, 42/44, 39/50, 4/120 on 128
# pins; 260/56, 240/96, 222/132, 182/212, 160/256, 268/40, 244/88,
# 238/100, 168/240, 108/360 on 560 pins)
HSE_128: dict[tuple[int, int], tuple[int, int]] = {
    (HEAT_SHRINK_2, 6): (52, 28),
    (HEAT_SHRINK_2, 9): (42, 48),
    (HEAT_SHRINK_2, 12): (33, 66),
    (HEAT_SHRINK_2, 18): (13, 106),
    (HEAT_SHRINK_2, 24): (0, 128),
    (HEAT_SHRINK_3, 5): (56, 20),
    (HEAT_SHRINK_3, 9): (44, 44),
    (HEAT_SHRINK_3, 11): (41, 50),
    (HEAT_SHRINK_3, 21): (6, 120),
}
HSE_560: dict[tuple[int, int], tuple[int, int]] = {
    (HEAT_SHRINK_2, 6): (261, 56),
    (HEAT_SHRINK_2, 9): (241, 96),
    (HEAT_SHRINK_2, 12): (223, 132),
    (HEAT_SHRINK_2, 18): (183, 212),
    (HEAT_SHRINK_2, 24): (161, 256),
    (HEAT_SHRINK_3, 5): (269, 40),
    (HEAT_SHRINK_3, 9): (245, 88),
    (HEAT_SHRINK_3, 11): (239, 100),
    (HEAT_SHRINK_3, 21): (169, 240),
    (HEAT_SHRINK_3, 31): (109, 360),
}
FLE = 0x13  # FLe die-cut labels, 21 x 45 mm, PT-P900/P900W/P950NW only
# Brother lists no print area for FLe labels: that of the 21 mm tube of the
# same width, from Brother's table (left 152, print 240, right 168) [assumed]
FLE_560 = {21: (168, 240)}
TZE_BY_PINS = {128: TZE_128, 560: TZE_560}
HSE_BY_PINS = {128: HSE_128, 560: HSE_560}

# media type byte -> CassetteInfo.media (the interface names them)
MEDIA = {
    LAMINATED: "laminated",
    0x03: "non_laminated",
    0x04: "fabric",
    HEAT_SHRINK_2: "heat_shrink_2",
    FLE: "fle",
    0x14: "flexible",
    0x15: "satin",
    HEAT_SHRINK_3: "heat_shrink_3",
}
MEDIA_CODES = {name: code for code, name in MEDIA.items()}
INCOMPATIBLE = 0xFF


def tape_from_status(width: int) -> float:
    """The status says 4 for 3.5 mm tape."""
    return 3.5 if width == 4 else float(width)


def status_width(tape_mm: float) -> int:
    return 4 if tape_mm == 3.5 else round(tape_mm)


def nearest_tape(width_mm: float) -> float:
    return min(TAPE_WIDTHS_MM, key=lambda w: abs(w - width_mm))


def tube_width(media: int, nominal_mm: float) -> int | None:
    """The width in the status of a tube that is designed on nominal_mm tape
    (an 11.2 mm tube says 11 and is designed on 12 mm)."""
    widths = {w for (m, w) in HSE_560 if m == media}
    return next((w for w in sorted(widths) if nearest_tape(w) == nominal_mm), None)


# --- models ------------------------------------------------------------------


@dataclass(frozen=True)
class Model:
    """A P-touch model and how to talk to it.

    `documented`: a model of Brother's raster references (PT-E550W/P750W/
    P710BT, PT-P900 series) with the full command set: print information,
    cut and half cut, chain printing, margin, high resolution. The others
    get the command subset ptouch-print uses for them, controlled by the
    flags packbits (TIFF compression needed), p700_init (ESC i a, answer to
    drop), info_cmd (ESC i z), auto_cut (cutter: ESC i M 40) and d460
    (PT-D460BT family: fixed margin bytes, n9 = 2)."""

    name: str
    usb_ids: tuple[int, ...] = ()
    pins: int = 128
    dpi: int = 180
    packbits: bool = False
    p700_init: bool = False
    info_cmd: bool = False
    auto_cut: bool = False
    d460: bool = False
    documented: bool = False
    half_cut: bool = False
    heat_shrink: bool = False
    network: bool = False
    high_res: bool = False
    high_res_laminated_only: bool = False  # PT-P900 series: n2 = 09h, laminated tape only
    page_cut: bool = False  # ESC i A (cut every n labels); not on the PT-P710BT
    notify: bool = False  # ESC i ! 00 needed for status while printing
    last_page: bool = False  # ESC i z n9 = 2 on the last page (PT-P900 series)
    min_feed_mm: float = 2.0
    invalidate: int = 100  # NUL bytes that clear the buffer (2.1); 200 on the PT-P900 series
    status_code: int | None = None  # model byte of the status
    unsupported: str = ""  # "editor_lite" or "raster": refused with a clear error
    lead_px: int = 0  # blank columns the model needs before the content

    @property
    def line_bytes(self) -> int:
        return self.pins // 8

    @property
    def p900_series(self) -> bool:
        """PT-P900/P900W/P950NW/P910BT: extended error byte, FLe labels."""
        return self.documented and self.pins == 560


def _documented(name: str, usb_id: int | None, code: int | None, **flags) -> Model:
    defaults = {
        "auto_cut": True,
        "half_cut": True,
        "heat_shrink": True,
        "high_res": True,
        "page_cut": True,
    }
    return Model(
        name,
        (usb_id,) if usb_id is not None else (),
        documented=True,
        status_code=code,
        **(defaults | flags),
    )


def _p900(name: str, usb_id: int | None, code: int | None, **flags) -> Model:
    p900 = {
        "pins": 560,
        "dpi": 360,
        "high_res_laminated_only": True,
        "last_page": True,
        "min_feed_mm": 1.0,
        "invalidate": 200,
    }
    return _documented(name, usb_id, code, **(p900 | flags))


# USB product IDs and model bytes of the documented models as Brother's
# references give them (Appendix A: USB Specifications; status offset 4);
# the PT-P710BT's model byte is not in the reference [assumed]
MODELS: tuple[Model, ...] = (
    Model("PT-9200DX", (0x2001, 0x2002), pins=384, dpi=360, packbits=True, auto_cut=True),
    Model(
        "PT-9700PC",
        (0x203C,),
        pins=384,
        dpi=360,
        packbits=True,
        auto_cut=True,
        p700_init=True,
        info_cmd=True,
    ),
    Model("PT-2300", (0x2004,), pins=112, packbits=True, auto_cut=True),
    Model("PT-2420PC", (0x2007,), packbits=True),
    Model("PT-2450PC", (0x2011,), packbits=True),
    Model("PT-1950", (0x2019,), pins=112, packbits=True),
    Model("PT-2700", (0x201F,), auto_cut=True),
    # ptouch-print notes a quirk ("needs more than 76 pixels, the first 32
    # blank") whose meaning is unclear; it is not applied [open]
    Model("PT-1230PC", (0x202C,)),
    Model("PT-2430PC", (0x202D,), auto_cut=True),
    Model("PT-2730", (0x2041,), lead_px=48),  # [assumed] 48 blank columns first
    Model("PT-H500", (0x205E,), packbits=True, auto_cut=True),
    Model("PT-E500", (0x205F,), packbits=True),
    _documented("PT-E550W", 0x2060, 0x66, packbits=True, network=True),
    Model("PT-P700", (0x2061,), packbits=True, p700_init=True, auto_cut=True),
    _documented("PT-P750W", 0x2062, 0x68, packbits=True, network=True),
    Model("PT-D450", (0x2073,), info_cmd=True),
    Model("PT-D600", (0x2074,), packbits=True),
    _p900("PT-P900", 0x2083, 0x71),
    _p900("PT-P900W", 0x2085, 0x69, network=True),
    _p900("PT-P950NW", 0x2086, 0x70, network=True),
    _documented(
        "PT-P710BT",
        0x20AF,
        0x76,
        packbits=True,
        half_cut=False,
        heat_shrink=False,
        page_cut=False,
        notify=True,
    ),
    _p900("PT-P910BT", 0x20C7, 0x78, heat_shrink=False, high_res=False, notify=True),
    Model("PT-D410", (0x20DF,), info_cmd=True, auto_cut=True, d460=True),
    Model("PT-D460BT", (0x20E0,), p700_init=True, info_cmd=True, auto_cut=True, d460=True),
    Model("PT-D610BT", (0x20E1,), p700_init=True, info_cmd=True, auto_cut=True, d460=True),
    Model("PT-E310BT", (0x2201,), p700_init=True, info_cmd=True, d460=True),
    Model("PT-E560BT", (0x2203,), p700_init=True, info_cmd=True, d460=True),
)
# a printer in Editor Lite mode is a USB drive: it cannot print until the
# Editor Lite switch is off (product IDs from ptouch-print; Brother gives
# 2062 for the PT-P750W printer, so 2065 is its drive)
EDITOR_LITE: tuple[Model, ...] = (
    Model("PT-1230PC", (0x2030,), unsupported="editor_lite"),
    Model("PT-2430PC", (0x2031,), unsupported="editor_lite"),
    Model("PT-P700", (0x2064,), unsupported="editor_lite"),
    Model("PT-P750W", (0x2065,), unsupported="editor_lite"),
)
# a network printer whose status names no model known here [assumed]
GENERIC = "P-touch"
GENERIC_128 = _documented(GENERIC, None, None, packbits=True, network=True)
GENERIC_560 = _p900(GENERIC, None, None, network=True)


def _key(name: str) -> str:
    """ "Brother PT-P750W" -> "PTP750W": model names as printers spell them."""
    return re.sub(r"[^A-Z0-9]", "", name.upper().replace("BROTHER", ""))


_BY_KEY = {_key(m.name): m for m in MODELS}
_BY_USB_ID = {usb_id: m for m in MODELS for usb_id in m.usb_ids}
_BY_CODE = {m.status_code: m for m in MODELS if m.status_code is not None}


def model_by_name(name: str) -> Model | None:
    """The model a name stands for, also "PT-P900W series": the longest
    known model name the name starts with."""
    key = _key(name)
    found = [m for k, m in _BY_KEY.items() if key.startswith(k)]
    return max(found, key=lambda m: len(_key(m.name))) if found else None


def model_by_usb_id(product: int | None) -> Model | None:
    return _BY_USB_ID.get(product) if product is not None else None


def generic_model(width_mm: float) -> Model:
    """For a printer of unknown model: 36 mm tape only fits the wide head."""
    return GENERIC_560 if width_mm > 24 else GENERIC_128


def model_of(info: PrinterInfo | None) -> Model | None:
    """The model a query reported (PrinterInfo.model)."""
    if info is None:
        return None
    if info.model == GENERIC:
        return generic_model(info.tape_mm or 0)
    return model_by_name(info.model)


def print_area(model: Model, width_mm: float, media: int = 0) -> tuple[int, int] | None:
    """(blank pins from the first byte, printable pins) for a tape on this
    model; None if the model cannot print on it."""
    if media in TUBES:
        table = HSE_BY_PINS.get(model.pins) if model.heat_shrink else None
        return table.get((media, round(width_mm))) if table else None
    if media == FLE:
        return FLE_560.get(round(width_mm)) if model.p900_series and model.heat_shrink else None
    known = TZE_BY_PINS.get(model.pins)
    if known is not None:
        return known.get(width_mm)
    # other heads (112, 384 pins): Brother's printable width at the head's
    # resolution, centred and at most the head [assumed, as ptouch-print]
    area = (TZE_128 if model.dpi == 180 else TZE_560).get(width_mm)
    if area is None:
        return None
    pins = min(area[1], model.pins)
    return (model.pins - pins) // 2, pins


# --- status --------------------------------------------------------------------

STATUS_SIZE = 32
STATUS_START = b"\x80\x20B"  # print head mark, size 32, "B"rother
REPLY, COMPLETED, ERROR = 0x00, 0x01, 0x02  # status type
PHASE_PRINTING = 0x01

# (status byte: error information 1 or 2, bit, class, text for bug
# reports); the first that is set wins, so the order is what to fix first.
# Brother's references; 8/0x10 and 8/0x80 are not used there [assumed]
ERRORS: tuple[tuple[int, int, type[PrinterError], str], ...] = (
    (9, 0x10, CoverOpen, "cover open"),
    (8, 0x01, NoCassette, "no media"),
    (8, 0x02, NoCassette, "end of media"),
    (9, 0x01, BadCassette, "replace media"),
    (8, 0x04, PaperJam, "cutter jam"),
    (9, 0x40, LabelNotFound, "black marking not detected"),
    (9, 0x20, Overheated, "overheating"),
    (8, 0x08, LowBattery, "weak batteries"),
    (8, 0x10, PrinterBusy, "printer in use"),
    (8, 0x40, PrinterError, "high-voltage adapter"),
    (8, 0x80, PrinterError, "fan motor error"),
    (9, 0x02, PrinterError, "expansion buffer full"),
    (9, 0x04, PrinterError, "communication error"),
    (9, 0x08, PrinterError, "communication buffer full"),
    (9, 0x80, PrinterError, "system error"),
)
# extended error (status byte 7, PT-P900 series)
EXTENDED_ERRORS: dict[int, tuple[type[PrinterError], str]] = {
    0x10: (NoCassette, "FLe tape end"),
    0x1D: (PrinterError, "high-resolution/draft printing error"),
    0x1E: (PrinterError, "adapter pull/insert error"),
    0x21: (BadCassette, "incompatible media"),
}


@dataclass(frozen=True)
class Status:
    """The 32 bytes the printer sends on ESC i S, on its own after a page,
    after a phase change and on errors."""

    model_code: int
    width: int  # mm, 4 for 3.5 mm tape, 0 without tape
    media: int
    status_type: int
    phase: int
    tape_colour: int
    text_colour: int
    raw: bytes

    @property
    def tape_mm(self) -> float | None:
        return tape_from_status(self.width) if self.width else None


def parse_status(frame: bytes) -> Status:
    if len(frame) != STATUS_SIZE or not frame.startswith(STATUS_START):
        raise PrinterError("unexpected status %r" % frame[:STATUS_SIZE])
    return Status(
        model_code=frame[4],
        width=frame[10],
        media=frame[11],
        status_type=frame[18],
        phase=frame[19],
        tape_colour=frame[24],
        text_colour=frame[25],
        raw=bytes(frame),
    )


def status_error(status: Status, extended: bool = False) -> PrinterError | None:
    """What keeps the printer from printing, or None. `extended`: the
    model has the extended error byte (PT-P900 series)."""
    detail = "status %s" % status.raw.hex(" ")
    for offset, bit, cls, text in ERRORS:
        if status.raw[offset] & bit:
            return cls("%s: %s" % (text, detail))
    code = status.raw[7] if extended else 0
    if code:
        cls, text = EXTENDED_ERRORS.get(code, (PrinterError, "extended error"))
        return cls("%s: %s" % (text, detail))
    if status.media == INCOMPATIBLE:
        return BadCassette("incompatible media: " + detail)
    if status.status_type == ERROR:
        return PrinterError("error status: " + detail)
    if not status.width:
        return NoCassette("no tape width: " + detail)
    return None


# --- raster data ---------------------------------------------------------------


def packbits(data: bytes) -> bytes:
    """TIFF PackBits: runs of 2 to 128 equal bytes as (257 - n, byte), the
    rest as literal pieces of 1 to 128 bytes (n - 1, bytes)."""
    out = bytearray()
    i, size = 0, len(data)
    while i < size:
        run = 1
        while i + run < size and run < 128 and data[i + run] == data[i]:
            run += 1
        if run > 1:
            out += bytes((257 - run, data[i]))
            i += run
            continue
        begin = i
        i += 1
        # up to the next three equal bytes, which a run packs better (a
        # run of two would cost as much as it saves)
        while i < size and i - begin < 128 and data[i : i + 3] != bytes((data[i],)) * 3:
            i += 1
        out.append(i - begin - 1)
        out += data[begin:i]
    return bytes(out)


def threshold(image: Image.Image) -> Image.Image:
    """Ink where the image is darker than THRESHOLD, as mode "1" with ink = 1."""
    return image.convert("L").point(lambda v: 255 if v < THRESHOLD else 0, mode="1")


def columns(image: Image.Image, model: Model, before: int) -> list[bytes]:
    """One raster line per image column: pin `before + y` (counted from the
    first byte's highest bit) prints image row y."""
    ink = threshold(image)
    head = Image.new("1", (ink.width + model.lead_px, model.pins), 0)
    head.paste(ink, (model.lead_px, before))
    data = head.transpose(Image.Transpose.TRANSPOSE).tobytes()
    size = model.line_bytes
    return [data[i : i + size] for i in range(0, len(data), size)]


def raster_lines(lines: list[bytes], model: Model) -> bytes:
    """Documented models: PackBits with Z for empty lines (or plain lines);
    the others as ptouch-print sends them: with TIFF one literal piece per
    line, else the plain line."""
    out = bytearray()
    for line in lines:
        if model.documented and model.packbits:
            if not any(line):
                out += EMPTY_LINE
                continue
            payload = packbits(line)
        elif model.packbits:
            payload = bytes((len(line) - 1,)) + line
        else:
            payload = line
        out += LINE + struct.pack("<H", len(payload)) + payload
    return bytes(out)


# --- the job --------------------------------------------------------------------


@dataclass(frozen=True)
class JobSettings:
    cut: str = "full"  # "full", "half" or "none", as the model can
    chain: bool = False
    high_res: bool = False
    feed_dots: int = 14  # at the head's resolution (doubled for high resolution)
    width: int = 12  # the status's tape width byte
    media: int = LAMINATED  # the status's media type


def start(model: Model) -> bytes:
    """Once per job: clear the buffer (100 bytes, 200 on the PT-P900
    series, as Brother says), initialise; older models switch to raster here."""
    begin = bytes(model.invalidate) + INIT
    if model.documented:
        return begin
    begin += b"M\x02" if model.packbits else b""
    return begin + (RASTER_MODE if model.p700_init else GRAPHICS_RASTER)


def _info(flags: int, kind: int, settings: JobSettings, lines: int, page: int) -> bytes:
    """ESC i z: print information (n1 flags, n2 media, n3 width, n5-n8 lines, n9 page)."""
    return (
        ESC
        + b"iz"
        + bytes((flags, kind, settings.width, 0))
        + struct.pack("<I", lines)
        + bytes((page, 0))
    )


def page_number(model: Model, index: int, count: int) -> int:
    """ESC i z n9: 0 for the first page, 1 for the others; the PT-P900
    series says 2 on the last page, also when it is the only one."""
    if model.last_page and index == count - 1:
        return 2
    return 0 if index == 0 else 1


def _page_info(model: Model, settings: JobSettings, lines: int, page: int) -> bytes:
    if settings.media in TUBES:
        return _info(0x86, settings.media, settings, lines, page)  # recover, width, media valid
    high_res = settings.high_res and model.high_res_laminated_only
    # recover and width valid; the PT-P900 series wants 09h for high resolution
    return _info(0x84, HIGH_RES_LAMINATED if high_res else 0, settings, lines, page)


def _advanced(settings: JobSettings) -> int:
    """ESC i K: 0x04 half cut, 0x08 no chain printing (feed and cut at the
    end), 0x40 high resolution."""
    return (
        (0x04 if settings.cut == "half" else 0)
        | (0 if settings.chain else 0x08)
        | (0x40 if settings.high_res else 0)
    )


def documented_page(
    model: Model, lines: list[bytes], settings: JobSettings, index: int = 0, count: int = 1
) -> bytes:
    """A page with Brother's control codes, without the print command."""
    out = RASTER_MODE + (NOTIFY if model.notify else b"")
    out += _page_info(model, settings, len(lines), page_number(model, index, count))
    full = settings.cut == "full"
    out += ESC + b"iM" + (b"\x40" if full else b"\x00")  # auto cut
    if full and model.page_cut:
        out += ESC + b"iA\x01"  # cut after every label
    out += ESC + b"iK" + bytes((_advanced(settings),))
    out += ESC + b"id" + struct.pack("<H", settings.feed_dots)
    out += b"M" + (b"\x02" if model.packbits else b"\x00")
    return out + raster_lines(lines, model)


def legacy_page(model: Model, lines: list[bytes], settings: JobSettings) -> bytes:
    out = b""
    if model.info_cmd:
        out += _info(0, 0, settings, len(lines), 2 if model.d460 else 0)
    if model.d460:
        # margin 1 dot, then M 00 (no compression): what these models need
        out += ESC + b"id\x01\x00" + b"M\x00"
    if model.auto_cut and settings.cut == "full":
        out += ESC + b"iM\x40"
    if model.d460 and settings.chain:
        out += ESC + b"iK\x00"
    return out + raster_lines(lines, model)


def page_end(model: Model, settings: JobSettings, last: bool) -> bytes:
    """0C between labels, 1A after the last (feed and cut); with chain
    printing on older models 0C at the end too. The PT-D460BT family always
    gets 1A, as ptouch-print sends it, and chains with ESC i K 00."""
    if model.d460:
        return PRINT_LAST
    if not last or (settings.chain and not model.documented):
        return PRINT_PAGE
    return PRINT_LAST


def build_job(model: Model, lines: list[bytes], settings: JobSettings, copies: int) -> list[bytes]:
    """The job in pieces: the start, then each page with its print command.
    One job for all copies, initialised once."""
    copies = max(1, copies)
    pieces = [start(model)]
    for n in range(copies):
        if model.documented:
            page = documented_page(model, lines, settings, n, copies)
        else:
            page = legacy_page(model, lines, settings)
        pieces.append(page + page_end(model, settings, n == copies - 1))
    return pieces


def print_timeout(length_mm: float, high_res: bool) -> float:
    """How long a page may take: no status (or no data taken) for longer
    means the printer is stuck."""
    return PRINT_TIMEOUT + length_mm / MIN_SPEED_MM_S * (2 if high_res else 1)


# --- connection -------------------------------------------------------------------


def editor_lite_plugged() -> Model | None:
    """A P-touch in Editor Lite mode on USB (a drive, not a printer)."""
    lite = {usb_id: m for m in EDITOR_LITE for usb_id in m.usb_ids}
    return next((lite[p] for p in usb.other_devices(BROTHER) if p in lite), None)


_browse = mdns.browse  # replaced in tests: they never search the network


class _TcpLink:
    """A socket used like usb.Connection: sendall() fails only when the
    printer takes no data for the timeout (it stops reading while it prints
    a page), a silent printer raises PrinterTimeout."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.timeout = SOCKET_TIMEOUT
        sock.settimeout(SOCKET_TIMEOUT)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout
        self.sock.settimeout(timeout)

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                sent = self.sock.send(view[:SEND_CHUNK])
            except TimeoutError as e:
                raise PrinterStalled("printer took no data for %g s" % self.timeout) from e
            view = view[sent:]

    def recv(self, size: int) -> bytes:
        try:
            return self.sock.recv(size)
        except TimeoutError as e:
            raise PrinterTimeout("printer did not answer within %g s" % self.timeout) from e

    def close(self) -> None:
        self.sock.close()


def _connect(host: str, port: int, wait: float = usb.BUSY_WAIT):
    """The USB device file, or TCP port 9100: the same commands."""
    if usb.is_usb(host):
        try:
            return usb.connect(host, SOCKET_TIMEOUT, wait)
        except UsbNotFound as e:
            lite = editor_lite_plugged()
            if lite:
                raise EditorLite("%s in Editor Lite mode: %s" % (lite.name, e)) from e
            raise
    return _TcpLink(socket.create_connection((mdns.resolve_host(host), port), SOCKET_TIMEOUT))


def map_error(error: Exception) -> PrinterError:
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    if isinstance(error, OSError) or "resolve" in text.lower():
        return PrinterNotFound(text)
    return PrinterError(text)


class ConnectionClosed(PrinterTimeout):
    """The printer (or the network) closed the connection: not a printer
    that is merely silent."""


def _same_host(a: str, b: str) -> bool:
    """Host names as DNS compares them: any case, with or without the final dot."""
    return a.casefold().rstrip(".") == b.casefold().rstrip(".")


class _Session:
    def __init__(self, link):
        self.link = link
        self.buffer = b""  # read beyond the last status (two came at once)

    def status(self) -> Status:
        # the model is not known yet: the longer invalidate of the PT-P900
        # series (only NUL bytes, as many as the printer takes)
        self.link.sendall(bytes(200) + INIT + STATUS_REQUEST)
        return self.read_status()

    def read_status(self) -> Status:
        """The next status; bytes before it (an answer cut short, 16-byte
        answers of some models) are skipped."""
        while True:
            begin = self.buffer.find(STATUS_START)
            if begin >= 0 and len(self.buffer) - begin >= STATUS_SIZE:
                frame = self.buffer[begin : begin + STATUS_SIZE]
                self.buffer = self.buffer[begin + STATUS_SIZE :]
                return parse_status(frame)
            if len(self.buffer) > GARBAGE_LIMIT:
                raise PrinterError("no status in the answer %r" % self.buffer[:64])
            chunk = self.link.recv(256)
            if not chunk:
                raise ConnectionClosed("connection closed after %r" % self.buffer[:64])
            self.buffer += chunk

    def drop_reply(self, timeout: float) -> None:
        """Read the answer some models give after switching to raster, if any."""
        self.link.settimeout(REPLY_WAIT)
        try:
            self.read_status()
        except (PrinterError, OSError):
            pass  # silence is fine
        finally:
            self.link.settimeout(timeout)

    def send_job(
        self,
        model: Model,
        pieces: list[bytes],
        timeout: float,
        progress: ProgressCallback | None,
        waits: bool,
        network: bool,
    ) -> bool:
        """The job, page by page; the printer may stop reading for as long
        as a page takes to print. `waits`: after each page, read the
        status until it is printed (Brother's flow 5.1); on the network a
        printer that does not report pages gets the rest without waiting.
        True if the printer reported every page printed."""
        copies = len(pieces) - 1
        self.link.settimeout(timeout)
        try:
            self.link.sendall(pieces[0])
            if model.p700_init:
                self.drop_reply(timeout)
            for n, page in enumerate(pieces[1:], 1):
                report_progress(progress, n, copies)
                self.link.sendall(page)
                if waits:
                    waits = self._printed(model, n - 1, copies, network)
        finally:
            self.link.settimeout(SOCKET_TIMEOUT)
        return waits

    def _printed(self, model: Model, done: int, copies: int, network: bool) -> bool:
        """Until the printer reports the page printed; its errors at once.
        False if a network printer stays silent (keep sending, stop waiting)."""
        try:
            while True:
                status = self.read_status()
                if status.status_type == COMPLETED:
                    return True
                if status.status_type == ERROR:
                    raise status_error(status, model.p900_series) or PrinterError(
                        "error status %s" % status.raw.hex(" ")
                    )
        except ConnectionClosed as e:  # gone, not silent: say what came out
            raise _printed_before(e, done, copies) from None
        except PrinterTimeout:
            if network:  # status over TCP is not documented [open]
                return False
            raise _printed_before(PrinterTimeout("no status for the page"), done, copies) from None
        except PrinterError as e:
            raise _printed_before(e, done, copies) from None


def _printed_before(error: PrinterError, done: int, copies: int) -> PrinterError:
    if done:
        error.params.update(printed=done, copies=copies)
    return error


def session(
    host: str, port: int = DEFAULT_PORT, wait: float = usb.BUSY_WAIT
) -> AbstractContextManager[_Session]:
    return open_session(lambda: _connect(host, port, wait), _Session, map_error)


# --- the backend ------------------------------------------------------------------


def _usb_model(address: str) -> Model | None:
    """The model of a usb:// address: by its name, else by the USB product ID."""
    try:
        name = unquote(urlsplit(address).path.lstrip("/"))
    except ValueError:
        return None
    model = model_by_name(name)
    if model:
        return model
    try:
        return model_by_usb_id(usb.product_id(usb.find(address), BROTHER))
    except PrinterError:
        return None


def _is_network_ptouch(name: str) -> bool:
    key = _key(name)
    return any(m.network and _key(m.name) in key for m in MODELS)


def _network_model(host: str, info: PrinterInfo | None) -> Model | None:
    """A network printer that gives no status: the model it reported
    before, its host name ("PT-P900W.local"), or its DNS-SD name."""
    known = model_of(info)
    if known and known.name != GENERIC:
        return known
    model = model_by_name(host.split(".")[0])
    if model:
        return model
    for found in _browse("_pdl-datastream._tcp", _is_network_ptouch, 1.0):
        if _same_host(host, found.host) or _same_host(host, found.address):
            return model_by_name(found.name.removeprefix("Brother").strip())
    return None


def identify(
    host: str, status: Status | None, tape_mm: float = 0, info: PrinterInfo | None = None
) -> Model:
    """The model at host: on USB by its name or ID, on TCP by the model
    byte of the status; without one by its name (else a generic model)."""
    if usb.is_usb(host):
        model = _usb_model(host)
        if model is None:
            raise UnknownModel("unknown P-touch model at %s" % host)
    elif status is None:
        model = _network_model(host, info) or generic_model(tape_mm)
    else:
        model = _BY_CODE.get(status.model_code) or generic_model(status.tape_mm or 0)
    if model.unsupported == "editor_lite":
        raise EditorLite("%s is in Editor Lite mode" % model.name)
    if model.unsupported:
        raise UnsupportedModel("%s does not print raster graphics" % model.name)
    return model


def _blind_status(tape_mm: float) -> Status:
    """No status over TCP: the label's tape, laminated, as if reported."""
    frame = bytearray(STATUS_SIZE)
    frame[0:4] = STATUS_START + b"0"
    frame[10], frame[11] = status_width(tape_mm), LAMINATED
    return parse_status(bytes(frame))


class PTouch(Backend):
    id = "ptouch"
    name = "Brother P-touch"
    capabilities = Capabilities(
        tape_widths_mm=TAPE_WIDTHS_MM,
        dpi=180,
        modes=(PrintMode("standard", 180),),
        color_model="mono",
        cut_modes=("full", "half", "none"),
        data_formats=("raster",),
        max_length_mm=MAX_LENGTH_MM,
        min_length_mm=MIN_LENGTH_MM,
        extras=("chain", "high_res", "feed"),
        feed_mm=FEED_MM,
        default_tape_mm=DEFAULT_TAPE_MM,
    )

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port

    # --- what the connected model can do ---------------------------------------------

    def display_name(self, info: PrinterInfo | None) -> str:
        return "Brother " + info.model if info and info.model else self.name

    def cut_modes(self, info: PrinterInfo | None) -> tuple[str, ...]:
        model = model_of(info)
        return _cut_modes(model) if model else self.capabilities.cut_modes

    def extras(self, info: PrinterInfo | None) -> tuple[str, ...]:
        model = model_of(info)
        return _extras(model) if model else self.capabilities.extras

    def feed_mm(self, info: PrinterInfo | None) -> tuple[float, float, float]:
        """Brother's margins: from 2 mm on the 128-pin models, 1 mm on the
        PT-P900 series."""
        model = model_of(info)
        low, high, default = self.capabilities.feed_mm
        return (model.min_feed_mm, high, default) if model else (low, high, default)

    # --- finding and asking ------------------------------------------------------------

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """P-touch printers on USB (also one in Editor Lite mode, which then
        explains itself), then those on the network."""
        plugged = []
        for p in usb.printers():
            if not p.manufacturer.casefold().startswith("brother"):
                continue
            model = model_by_name(p.model) or model_by_usb_id(usb.product_id(p, BROTHER))
            if model:
                plugged.append(
                    FoundPrinter("Brother %s (USB)" % model.name, p.address, str(p.device))
                )
        lite = editor_lite_plugged()
        if lite and not plugged:
            # no device file to name; asking it explains Editor Lite
            address = usb.SCHEME + "Brother/" + quote(lite.name, safe="")
            plugged.append(FoundPrinter("Brother %s (USB)" % lite.name, address, "USB"))
        return plugged + _browse("_pdl-datastream._tcp", _is_network_ptouch, timeout)

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            status = s.status()
        return self.info(identify(host, status), status)

    def info(self, model: Model, status: Status) -> PrinterInfo:
        error = status_error(status, model.p900_series)
        tape = status.tape_mm
        cassette = None
        if tape and status.media != INCOMPATIBLE:
            area = print_area(model, tape, status.media)
            cassette = CassetteInfo(
                printable_mm=round(area[1] / model.dpi * 25.4, 1) if area else None,
                media=MEDIA.get(status.media, ""),
            )
        busy = status.phase == PHASE_PRINTING
        state = "ERROR" if error else "BUSY" if busy else "IDLE"
        return PrinterInfo(
            model=model.name,
            # FLe labels are 21 mm, no tape width of their own
            tape_mm=(round(tape) if status.media == FLE else nearest_tape(tape)) if tape else None,
            remaining_mm=None,
            total_mm=None,
            state=state,
            error=error.code if error else "",
            cassette=cassette,
        )

    # --- rendering -----------------------------------------------------------------

    @staticmethod
    def _area(model: Model, tape_mm: float, info: PrinterInfo | None) -> tuple[int, int]:
        """The print area for the label's tape: of the inserted tube if the
        printer reported one for this width, else of the tape; a tape the
        model has no area for gets the whole head."""
        cassette = info.cassette if info else None
        media = MEDIA_CODES.get(cassette.media, 0) if cassette else 0
        if info and info.tape_mm == tape_mm and media in TUBES:
            width = tube_width(media, tape_mm)
            area = print_area(model, width, media) if width else None
            if area:
                return area
        return print_area(model, tape_mm) or (0, model.pins)

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        """Rendered for the print area of the model last asked; at high
        resolution twice as fine in both directions, prepare() then halves
        it across the tape."""
        info = options.info
        model = model_of(info) or generic_model(tape_mm)
        media = info.cassette.media if info and info.cassette else ""
        high_res = _high_res(model, MEDIA_CODES.get(media, LAMINATED), options.high_res)
        factor = 2 if high_res else 1
        pins = self._area(model, tape_mm, info)[1]
        return Raster(model.dpi * factor, tape_px=pins * factor, width_px=pins)

    def preview(self, image: Image.Image, mode: str = "") -> Image.Image:
        """Black where the printer prints: a threshold, no dithering."""
        return image.convert("L").point(lambda v: 0 if v < THRESHOLD else 255).convert("RGB")

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        raster = self.raster(label.tape_mm, options)
        image = render(
            label,
            dpi=raster.dpi,
            safe_mm=safe_mm,
            tape_px=raster.tape_px,
            min_length_mm=self.capabilities.min_length_mm,
        ).image
        pins = raster.width_px or image.height
        if image.height != pins:
            image = image.convert("L").resize((image.width, pins), Image.Resampling.BOX)
        image = self.preview(image)
        image.info["lpi"] = raster.dpi  # along the tape: the length of a column
        image.info["tape_mm"] = label.tape_mm
        return image

    # --- printing --------------------------------------------------------------------

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        rendered_lpi = float(image.info.get("lpi", self.capabilities.dpi))
        length_mm = image.width / rendered_lpi * 25.4
        check_length(length_mm, self.capabilities)
        copies = max(1, options.copies)
        with session(host, self.port, usb.PRINT_WAIT) as s:
            status = _first_status(s, host)
            if status is None:  # TCP without status: print without waiting [open]
                status = _blind_status(float(image.info.get("tape_mm", DEFAULT_TAPE_MM)))
                model = identify(host, None, status.tape_mm or 0, options.info)
                waits = False
            else:
                model = identify(host, status)
                waits = model.documented
            area = _check_media(model, status, length_mm)
            settings = job_settings(model, options, status)
            lpi = model.dpi * (2 if settings.high_res else 1)
            lines = columns(_fit(image, area[1], rendered_lpi, lpi), model, area[0])
            timeout = print_timeout(length_mm, settings.high_res)
            pieces = build_job(model, lines, settings, copies)
            confirmed = s.send_job(model, pieces, timeout, progress, waits, not usb.is_usb(host))
        if model.documented and not confirmed:
            # sent, but the printer did not say it printed: no "Printed"
            return PrintResult(queued=True)
        return None


def _first_status(s: _Session, host: str) -> Status | None:
    """The status before a job; None if a network printer does not answer
    (Brother's references do not say whether there is status over TCP) [open]."""
    try:
        return s.status()
    except PrinterTimeout:
        if usb.is_usb(host):
            raise
        return None


def _check_media(model: Model, status: Status, length_mm: float) -> tuple[int, int]:
    """The print area for the loaded media; its problems as PrinterErrors."""
    error = status_error(status, model.p900_series)
    if error:
        raise error
    tape = status.tape_mm or 0
    area = print_area(model, tape, status.media)
    if area is None:
        raise BadCassette(
            "%s cannot print on %g mm, media 0x%02x" % (model.name, tape, status.media)
        )
    if status.media in TUBES and length_mm > MAX_TUBE_MM:
        raise LabelTooLong(
            "%.1f mm is longer than %.0f mm of tube" % (length_mm, MAX_TUBE_MM),
            max_mm=MAX_TUBE_MM,
        )
    return area


def _cut_modes(model: Model) -> tuple[str, ...]:
    if not model.auto_cut:
        return ()  # a cutter lever: the user cuts
    if model.documented and model.half_cut:
        return ("full", "half", "none")
    return ("full", "none")


def _extras(model: Model) -> tuple[str, ...]:
    if model.documented:
        return ("chain", "high_res", "feed") if model.high_res else ("chain", "feed")
    return ("chain",)


def _high_res(model: Model, media: int, wanted: bool) -> bool:
    """High resolution if wanted and possible: the PT-P900 series only on
    laminated tape, the PT-P910BT not at all."""
    if not (wanted and model.high_res):
        return False
    return media == LAMINATED or not model.high_res_laminated_only


def job_settings(model: Model, options: PrintOptions, status: Status) -> JobSettings:
    """What the model can do of the options."""
    cuts = _cut_modes(model)
    extras = _extras(model)
    high_res = _high_res(model, status.media, options.high_res)
    low, high, default = FEED_MM
    feed = default if options.feed_mm is None else options.feed_mm
    feed = min(high, max(low, model.min_feed_mm, feed))
    # Brother: 14 dots are 2 mm at 180 dpi and 1 mm at 360 dpi
    dots = round(feed * model.dpi / 25.4) * (2 if high_res else 1)
    return JobSettings(
        cut=options.cut if options.cut in cuts else (cuts[0] if cuts else "none"),
        chain=options.chain and "chain" in extras,
        high_res=high_res,
        feed_dots=dots,
        width=status.width,
        media=status.media,
    )


def _fit(image: Image.Image, pins: int, rendered_lpi: float, lpi: float) -> Image.Image:
    """Made for another tape or resolution: scaled to the print area instead
    of failing (the interface warns about a different tape first)."""
    width = max(1, round(image.width * lpi / rendered_lpi))
    if image.size == (width, pins):
        return image
    return image.convert("L").resize((width, pins), Image.Resampling.LANCZOS)
