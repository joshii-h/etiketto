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
    LowBattery,
    NoCassette,
    Overheated,
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
PRINT_TIMEOUT = 60.0  # seconds without any status while a job prints
GARBAGE_LIMIT = 4096  # bytes without a status in them: not a P-touch
THRESHOLD = 128  # grey levels below this print; no dithering, codes stay sharp
MAX_LENGTH_MM = 1000.0  # [assumed] the longest label Etiketto sends
FEED_MM = (2.0, 127.0, 2.0)  # blank tape before and after a label: min, max, default

ESC = b"\x1b"
INIT = ESC + b"@"
STATUS_REQUEST = ESC + b"iS"
RASTER_MODE = ESC + b"ia\x01"  # switch to raster mode (Brother reference)
GRAPHICS_RASTER = ESC + b"iR\x01"  # older models: select raster graphics transfer
PRINT_PAGE = b"\x0c"  # print, no feed: more pages follow, or chain printing
PRINT_LAST = b"\x1a"  # print the last page, feed and cut
EMPTY_LINE = b"Z"
LINE = b"G"

# --- tapes and print area ----------------------------------------------------

TAPE_WIDTHS_MM: tuple[float, ...] = (3.5, 6, 9, 12, 18, 24, 36)

# Brother's print area: (blank pins before, printable pins) for each tape
# width; the rest of the head stays blank. 128 pins at 180 dpi
# (PT-E550W/P750W/P710BT reference, 2.3 Print Area) and 560 pins at 360 dpi
# (PT-P900 reference, 2.3.5 Raster line).
TZE_128: dict[float, tuple[int, int]] = {
    3.5: (52, 24),
    6: (48, 32),
    9: (39, 50),
    12: (29, 70),
    18: (8, 112),
    24: (0, 128),
}
TZE_560: dict[float, tuple[int, int]] = {
    3.5: (248, 48),
    6: (240, 64),
    9: (219, 106),
    12: (197, 150),
    18: (155, 234),
    24: (112, 320),
    36: (45, 454),
}
HEAT_SHRINK_2, HEAT_SHRINK_3 = 0x11, 0x17  # media types in the status
# heat-shrink tube by (media type, width in the status); ptouch's values
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
TZE_BY_PINS = {128: TZE_128, 560: TZE_560}
HSE_BY_PINS = {128: HSE_128, 560: HSE_560}

# media type byte -> CassetteInfo.media (the interface names them)
MEDIA = {
    0x01: "laminated",
    0x03: "non_laminated",
    0x04: "fabric",
    HEAT_SHRINK_2: "heat_shrink_2",
    0x13: "flexible",
    0x14: "flexible",
    0x15: "satin",
    HEAT_SHRINK_3: "heat_shrink_3",
}
INCOMPATIBLE = 0xFF


def tape_from_status(width: int) -> float:
    """The status says 4 for 3.5 mm tape."""
    return 3.5 if width == 4 else float(width)


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
    status_code: int | None = None  # model byte of the status [assumed]
    unsupported: str = ""  # "editor_lite" or "raster": refused with a clear error
    lead_px: int = 0  # blank columns the model needs before the content
    min_px: int = 0  # at least this many columns of data

    @property
    def high_res(self) -> bool:
        return self.documented

    @property
    def line_bytes(self) -> int:
        return self.pins // 8


def _documented(name: str, usb_id: int | None, code: int | None, **flags) -> Model:
    return Model(
        name,
        (usb_id,) if usb_id is not None else (),
        documented=True,
        auto_cut=True,
        half_cut=flags.pop("half_cut", True),
        heat_shrink=flags.pop("heat_shrink", True),
        status_code=code,
        **flags,
    )


def _p900(name: str, usb_id: int | None, code: int | None, **flags) -> Model:
    return _documented(name, usb_id, code, pins=560, dpi=360, **flags)


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
    # [assumed] ptouch-print: needs more than 76 columns, the first 32 stay blank
    Model("PT-1230PC", (0x202C,), lead_px=32, min_px=77),
    Model("PT-2430PC", (0x202D,), auto_cut=True),
    Model("PT-2730", (0x2041,), lead_px=48),
    Model("PT-H500", (0x205E,), packbits=True, auto_cut=True),
    Model("PT-E500", (0x205F,), packbits=True),
    _documented("PT-E550W", 0x2060, 0x65, packbits=True, network=True),
    Model("PT-P700", (0x2061,), packbits=True, p700_init=True, auto_cut=True),
    # ptouch-print lists 2062, ptouch 2065 (which ptouch-print calls its
    # Editor Lite mode): a printer on /dev/usb/lp is never a drive, so
    # 2065 names the printer here and Editor Lite is only a 2065 without it
    _documented("PT-P750W", 0x2062, 0x68, packbits=True, p700_init=True, network=True),
    Model("PT-D450", (0x2073,), info_cmd=True),
    Model("PT-D600", (0x2074,), packbits=True),
    _p900("PT-P900", 0x2083, 0x71),
    _p900("PT-P900W", 0x2085, 0x69, p700_init=True, network=True),
    _p900("PT-P950NW", 0x2086, 0x70, network=True),
    _documented("PT-P710BT", 0x20AF, 0x76, packbits=True, half_cut=False, heat_shrink=False),
    _p900("PT-P910BT", 0x20C7, 0x78, heat_shrink=False),
    Model("PT-D410", (0x20DF,), info_cmd=True, auto_cut=True, d460=True),
    Model("PT-D460BT", (0x20E0,), p700_init=True, info_cmd=True, auto_cut=True, d460=True),
    Model("PT-D610BT", (0x20E1,), p700_init=True, info_cmd=True, auto_cut=True, d460=True),
    Model("PT-E310BT", (0x2201,), p700_init=True, info_cmd=True, d460=True),
    Model("PT-E560BT", (0x2203,), p700_init=True, info_cmd=True, d460=True),
)
# a printer in Editor Lite mode is a USB drive: it cannot print until the
# Editor Lite switch is off
EDITOR_LITE: tuple[Model, ...] = (
    Model("PT-1230PC", (0x2030,), unsupported="editor_lite"),
    Model("PT-2430PC", (0x2031,), unsupported="editor_lite"),
    Model("PT-P700", (0x2064,), unsupported="editor_lite"),
    Model("PT-P750W", (0x2065,), unsupported="editor_lite"),
)
# a network printer whose status names no model known here [assumed]
GENERIC_128 = _documented("P-touch", None, None, packbits=True, network=True)
GENERIC_560 = _p900("P-touch", None, None, network=True)


def _key(name: str) -> str:
    """ "Brother PT-P750W" -> "PTP750W": model names as printers spell them."""
    return re.sub(r"[^A-Z0-9]", "", name.upper().replace("BROTHER", ""))


_BY_KEY = {_key(m.name): m for m in MODELS}
_BY_USB_ID = {usb_id: m for m in MODELS for usb_id in m.usb_ids}
_BY_USB_ID[0x2065] = _BY_KEY["PTP750W"]  # see the PT-P750W above
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


def print_area(model: Model, width_mm: float, media: int = 0) -> tuple[int, int] | None:
    """(blank pins before, printable pins) for a tape on this model; None if
    the model cannot print on it."""
    if media in (HEAT_SHRINK_2, HEAT_SHRINK_3):
        table = HSE_BY_PINS.get(model.pins) if model.heat_shrink else None
        return table.get((media, round(width_mm))) if table else None
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
# reports); the first that is set wins, so the order is what to fix first
ERRORS: tuple[tuple[int, int, type[PrinterError], str], ...] = (
    (9, 0x10, CoverOpen, "cover open"),
    (8, 0x01, NoCassette, "no media"),
    (8, 0x02, NoCassette, "end of media"),
    (9, 0x01, BadCassette, "replace media"),
    (8, 0x04, PaperJam, "cutter jam"),
    (9, 0x40, PaperJam, "feeding error"),
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


def status_error(status: Status) -> PrinterError | None:
    """What keeps the printer from printing, or None."""
    detail = "status %s" % status.raw.hex(" ")
    for offset, bit, cls, text in ERRORS:
        if status.raw[offset] & bit:
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
        start = i
        i += 1
        # up to the next three equal bytes, which a run packs better (a
        # run of two would cost as much as it saves)
        while i < size and i - start < 128 and data[i : i + 3] != bytes((data[i],)) * 3:
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


def threshold(image: Image.Image) -> Image.Image:
    """Ink where the image is darker than THRESHOLD, as mode "1" with ink = 1."""
    return image.convert("L").point(lambda v: 255 if v < THRESHOLD else 0, mode="1")


def columns(image: Image.Image, model: Model, left: int) -> list[bytes]:
    """One raster line per image column: pin left + y prints image row y,
    pins counted from the first byte's highest bit."""
    ink = threshold(image)
    width = max(ink.width + model.lead_px, model.min_px)
    head = Image.new("1", (width, model.pins), 0)
    head.paste(ink, (model.lead_px, left))
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
    media: int = 0x01  # the status's media type


def start(model: Model) -> bytes:
    """Clear the buffer, initialise, switch to raster."""
    if model.documented:
        return bytes(200) + INIT + RASTER_MODE
    begin = bytes(100) + INIT + (b"M\x02" if model.packbits else b"")
    return begin + (RASTER_MODE if model.p700_init else GRAPHICS_RASTER)


def _info(flags: int, settings: JobSettings, kind: int, lines: int, page: int) -> bytes:
    """ESC i z: print information (n1 flags, n2 media, n3 width, n5-n8 lines, n9 page)."""
    return (
        ESC
        + b"iz"
        + bytes((flags, kind, settings.width, 0))
        + struct.pack("<I", lines)
        + bytes((page, 0))
    )


def documented_page(model: Model, lines: list[bytes], settings: JobSettings, first: bool) -> bytes:
    heat_shrink = settings.media in (HEAT_SHRINK_2, HEAT_SHRINK_3)
    kind = settings.media if heat_shrink else 0
    # 0x86: recover, width and media valid; page 0 is the first, 1 the others [assumed]
    out = _info(0x86, settings, kind, len(lines), 0 if first else 1)
    full = settings.cut == "full"
    out += ESC + b"iM" + (b"\x40" if full else b"\x00")  # auto cut
    if full:
        out += ESC + b"iA\x01"  # cut after every label
    advanced = (
        (0x04 if settings.cut == "half" else 0)
        | (0 if settings.chain else 0x08)  # 0x08: no chain printing, feed and cut at the end
        | (0x40 if settings.high_res else 0)
    )
    out += ESC + b"iK" + bytes((advanced,))
    out += ESC + b"id" + struct.pack("<H", settings.feed_dots)
    out += b"M" + (b"\x02" if model.packbits else b"\x00")
    return out + raster_lines(lines, model)


def legacy_page(model: Model, lines: list[bytes], settings: JobSettings) -> bytes:
    out = b""
    if model.info_cmd:
        out += _info(0, settings, 0, len(lines), 2 if model.d460 else 0)
    if model.d460:
        # margin 1 dot, then M 00 (no compression): what these models need
        out += ESC + b"id\x01\x00" + b"M\x00"
    if model.auto_cut and settings.cut == "full":
        out += ESC + b"iM\x40"
    if model.d460 and settings.chain:
        out += ESC + b"iK\x00"
    return out + raster_lines(lines, model)


def legacy_end(model: Model, settings: JobSettings, last: bool) -> bytes:
    if last and settings.chain and not model.d460:
        return PRINT_PAGE  # chain printing: no feed after the last label
    return PRINT_LAST if last or settings.cut == "full" else PRINT_PAGE


def build_jobs(
    model: Model, lines: list[bytes], settings: JobSettings, copies: int
) -> list[bytes]:
    """What to send: documented models get one job with a page per copy;
    the others one job per copy, each as ptouch-print sends a label."""
    copies = max(1, copies)
    if model.documented:
        pages = [documented_page(model, lines, settings, n == 0) for n in range(copies)]
        return [start(model) + PRINT_PAGE.join(pages) + PRINT_LAST]
    return [
        start(model)
        + legacy_page(model, lines, settings)
        + legacy_end(model, settings, n == copies - 1)
        for n in range(copies)
    ]


# --- connection -------------------------------------------------------------------


def editor_lite_plugged() -> Model | None:
    """A P-touch in Editor Lite mode on USB (a drive, not a printer)."""
    lite = {usb_id: m for m in EDITOR_LITE for usb_id in m.usb_ids}
    return next((lite[p] for p in usb.other_devices(BROTHER) if p in lite), None)


_browse = mdns.browse  # replaced in tests: they never search the network


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
    def __init__(self, link):
        self.link = link
        self.buffer = b""  # read beyond the last status (two came at once)

    def status(self) -> Status:
        self.link.sendall(bytes(100) + INIT + STATUS_REQUEST)
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
                raise PrinterTimeout("connection closed after %r" % self.buffer[:64])
            self.buffer += chunk

    def drop_reply(self) -> None:
        """Read the answer some models give after switching to raster, if any."""
        self.link.settimeout(REPLY_WAIT)
        try:
            self.read_status()
        except (PrinterError, OSError):
            pass  # TimeoutError on TCP, PrinterTimeout on USB
        finally:
            self.link.settimeout(SOCKET_TIMEOUT)

    def send(self, data: bytes) -> None:
        self.link.sendall(data)

    def wait_pages(self, copies: int, progress: ProgressCallback | None) -> None:
        """Until the printer reports every page printed; its errors at once."""
        self.link.settimeout(PRINT_TIMEOUT)
        done = 0
        try:
            while done < copies:
                status = self.read_status()
                if status.status_type == COMPLETED:
                    done += 1
                    if done < copies:
                        report_progress(progress, done + 1, copies)
                    continue
                error = status_error(status) if status.status_type == ERROR else None
                if error:
                    raise error
        except PrinterError as e:
            if done:
                e.params.update(printed=done, copies=copies)
            raise
        finally:
            self.link.settimeout(SOCKET_TIMEOUT)


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
        extras=("chain", "high_res", "feed"),
        feed_mm=FEED_MM,
    )

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.model: Model | None = None  # of the printer last asked
        self.media: tuple[int, int] | None = None  # (media type, width) it reported

    # --- what the connected model can do ---------------------------------------------

    def _model_of(self, info: PrinterInfo | None) -> Model | None:
        if info is None:
            return None
        if self.model and self.model.name == info.model:
            return self.model
        return model_by_name(info.model)

    def display_name(self, info: PrinterInfo | None) -> str:
        return "Brother " + info.model if info and info.model else self.name

    def cut_modes(self, info: PrinterInfo | None) -> tuple[str, ...]:
        model = self._model_of(info)
        return _cut_modes(model) if model else self.capabilities.cut_modes

    def extras(self, info: PrinterInfo | None) -> tuple[str, ...]:
        model = self._model_of(info)
        return _extras(model) if model else self.capabilities.extras

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

    def _identify(self, host: str, status: Status) -> Model:
        if usb.is_usb(host):
            model = _usb_model(host)
            if model is None:
                raise UnsupportedModel("unknown P-touch model at %s" % host)
        else:
            model = _BY_CODE.get(status.model_code) or generic_model(status.tape_mm or 0)
        if model.unsupported == "editor_lite":
            raise EditorLite("%s is in Editor Lite mode" % model.name)
        if model.unsupported:
            raise UnsupportedModel("%s does not print raster graphics" % model.name)
        self.model = model
        self.media = (status.media, status.width)
        return model

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            status = s.status()
        model = self._identify(host, status)
        return self.info(model, status)

    def info(self, model: Model, status: Status) -> PrinterInfo:
        error = status_error(status)
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
            tape_mm=self.capabilities.nearest_tape(tape) if tape else None,
            remaining_mm=None,
            total_mm=None,
            state=state,
            error=error.code if error else "",
            cassette=cassette,
        )

    # --- rendering -----------------------------------------------------------------

    def _area(self, model: Model, tape_mm: float) -> tuple[int, int]:
        """The print area for the label's tape: of the inserted tube if that
        is what the printer reported for this width, else of the tape; a tape
        the model has no area for gets the whole head."""
        reported = self.media
        if reported and reported[0] in (HEAT_SHRINK_2, HEAT_SHRINK_3):
            tube = tape_from_status(reported[1])
            area = print_area(model, tube, reported[0])
            if area and self.capabilities.nearest_tape(tube) == tape_mm:
                return area
        return print_area(model, tape_mm) or (0, model.pins)

    def _profile(self, tape_mm: float) -> Model:
        return self.model or generic_model(tape_mm)

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        """Rendered for the print area; at high resolution twice as fine
        in both directions, prepare() then halves it across the tape."""
        model = self._profile(tape_mm)
        factor = 2 if options.high_res and model.high_res else 1
        pins = self._area(model, tape_mm)[1]
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
        check_length(image.width / rendered_lpi * 25.4, self.capabilities)
        copies = max(1, options.copies)
        with session(host, self.port, usb.PRINT_WAIT) as s:
            status = s.status()
            model = self._identify(host, status)
            error = status_error(status)
            if error:
                raise error
            tape = status.tape_mm or 0
            area = print_area(model, tape, status.media)
            if area is None:
                raise BadCassette(
                    "%s cannot print on %g mm, media 0x%02x" % (model.name, tape, status.media)
                )
            settings = job_settings(model, options, status)
            lpi = model.dpi * (2 if settings.high_res else 1)
            lines = columns(_fit(image, area[1], rendered_lpi, lpi), model, area[0])
            jobs = build_jobs(model, lines, settings, copies)
            for n, job in enumerate(jobs, 1):
                report_progress(progress, n if len(jobs) > 1 else 1, copies)
                _send(s, model, job)
            if model.documented:
                s.wait_pages(copies, progress)
        return None


def _send(s: _Session, model: Model, job: bytes) -> None:
    """A job; models that answer the switch to raster get it read and dropped."""
    begin = start(model)
    if not model.p700_init:
        s.send(job)
        return
    s.send(begin)
    s.drop_reply()
    s.send(job[len(begin) :])


def _cut_modes(model: Model) -> tuple[str, ...]:
    if not model.auto_cut:
        return ()  # a cutter lever: the user cuts
    if model.documented and model.half_cut:
        return ("full", "half", "none")
    return ("full", "none")


def _extras(model: Model) -> tuple[str, ...]:
    if model.documented:
        return ("chain", "high_res", "feed")
    return ("chain",)


def job_settings(model: Model, options: PrintOptions, status: Status) -> JobSettings:
    """What the model can do of the options."""
    cuts = _cut_modes(model)
    extras = _extras(model)
    high_res = options.high_res and "high_res" in extras
    low, high, default = FEED_MM
    feed = min(high, max(low, default if options.feed_mm is None else options.feed_mm))
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
