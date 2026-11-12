"""Printers the system knows, through IPP: CUPS queues on this computer
(also USB printers) and IPP Everywhere printers on the network.

The label is one page of exactly its size, unscaled: PDF if the printer (or
CUPS) takes it, else PWG Raster (which every IPP Everywhere printer takes),
else JPEG. The printer's own setup decides everything else, as with the
other backends.
"""

from __future__ import annotations

import getpass
import io
import ipaddress
import math
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any
from urllib.parse import quote, urlsplit

from PIL import Image

from etiketto.backends import ipp, mdns, pdf, pwg, urf
from etiketto.backends.base import (
    Backend,
    Capabilities,
    CoverOpen,
    FoundPrinter,
    JobCanceled,
    JobHeld,
    LabelSize,
    Margins,
    MediaJam,
    NoFormat,
    OutOfPaper,
    PageTooLarge,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    QueuePaused,
    SuppliesEmpty,
    UnsupportedSize,
    check_codes,
    error_text,
    network_error,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, Rendered

CUPS_URI = "ipp://localhost/"
DPI = 300.0
TIMEOUT = 10.0  # one IPP request
PRINT_TIMEOUT = 300.0  # a printing job whose page counter moved: this long without a page
TIME_PER_LABEL = 30.0  # no page counter: PRINT_TIMEOUT plus this per label, for the whole job
RECOVERY_LIMIT = 300.0  # out of paper, cover open, jam: this long for the user to fix each
RECOVERY_TOTAL = 900.0  # and this long for all of them together, both since the last page
BUSY_LIMIT = 30.0  # a printer that answers Print-Job with "busy": asked again this long
BUSY_PAUSE = 3.0
BUSY = 0x0507  # server-error-busy: no job was made
PENDING_LIMIT = 30.0  # waiting behind other jobs: then it prints on its own
PAGES_PER_JOB = 10  # pages in one job: the document is made in memory
MAX_JOB_BYTES = 64 << 20  # and up to this much page data (one page at least)
POLL_INTERVAL = 1.0
POLL_FAILURES = 3  # a busy printer may miss an answer now and then
PROBLEM_POLLS = 3  # a problem must last this many answers: not a passing state
SIZE_TOLERANCE = 100  # hundredths of mm: 1 mm
MAX_PAGE_WIDTH_MM = 330.0  # A3+; also keeps the render of a file small

_sleep = time.sleep  # replaced in tests
_clock = time.monotonic

# paper sizes to start from
PAPER_SIZES = (
    LabelSize(210, 297),  # A4
    LabelSize(148, 210),  # A5
    LabelSize(105, 148),  # A6
    LabelSize(215.9, 279.4),  # US Letter
    LabelSize(101.6, 152.4),  # 4 × 6 in
    LabelSize(100, 150),
)

# printer-state, job-state (RFC 8011)
IDLE, PROCESSING, STOPPED = 3, 4, 5
JOB_PENDING, JOB_HELD, JOB_PROCESSING, JOB_STOPPED = 3, 4, 5, 6
JOB_CANCELED, JOB_ABORTED, JOB_COMPLETED = 7, 8, 9
JOB_STATES = range(JOB_PENDING, JOB_COMPLETED + 1)

# printer-state-reasons (without -error/-warning/-report) -> error class
REASONS: dict[str, type[PrinterError]] = {
    "media-empty": OutOfPaper,
    "media-needed": OutOfPaper,
    "media-jam": MediaJam,
    "door-open": CoverOpen,
    "cover-open": CoverOpen,
    "interlock-open": CoverOpen,
    "toner-empty": SuppliesEmpty,
    "marker-supply-empty": SuppliesEmpty,
    "paused": PrinterPaused,
    "moving-to-paused": PrinterPaused,
    "offline": PrinterTimeout,
    "timed-out": PrinterTimeout,
}
# problems the user fixes at the printer, which then goes on by itself:
# shown while Etiketto waits (the stage for the interface)
RECOVERABLE: dict[type[PrinterError], str] = {
    OutOfPaper: "OUT_OF_PAPER",
    CoverOpen: "COVER_OPEN",
    MediaJam: "MEDIA_JAM",
}
# states CUPS and printers report while all is well (CUPS writes them
# without a suffix): never a reason to stop
ROUTINE = ("connecting-to-device", "sending-to-device", "cups-")

PRINTER_ATTRIBUTES = [
    "printer-state",
    "printer-state-reasons",
    "printer-make-and-model",
    "printer-info",
    "printer-firmware-string-version",
    "document-format-supported",
    "print-scaling-supported",
    "media-size-supported",
    "pwg-raster-document-type-supported",
    "pwg-raster-document-resolution-supported",
    "printer-resolution-default",
    "printer-resolution-supported",
    "copies-supported",
    "operations-supported",
    "urf-supported",
]
STATE_ATTRIBUTES = ["printer-state", "printer-state-reasons"]
MARGIN_SIDES = ("top", "right", "bottom", "left")
# what the printer cannot print on: per paper size in media-col-ready (the
# paper loaded) and media-col-database (all it knows), else the margins it
# supports for any paper
MARGIN_ATTRIBUTES = [
    "media-col-ready",
    "media-col-database",
    *("media-%s-margin-supported" % side for side in MARGIN_SIDES),
]


# --- addresses ---------------------------------------------------------------------


def printer_uri(host: str) -> str:
    """The IPP address for what the user entered:
    - a printer URI (ipp, ipps; http and https as the CUPS web pages show them)
    - a CUPS queue name on this computer ("HP_LaserJet")
    - a network printer's host name or address, with or without port and
      path ("hp.local", "192.0.2.5:631", "2001:db8::5", "hp.local/ipp/print",
      "2001:db8::5/ipp/print")
    """
    host = host.strip()
    scheme, sep, rest = host.partition("://")
    if sep:
        known = {"ipp": "ipp", "ipps": "ipps", "http": "ipp", "https": "ipps"}
        return known.get(scheme.lower(), scheme) + "://" + rest
    name, slash, path = host.partition("/")
    if _is_ipv6(name):
        return "ipp://[%s]/%s" % (name, path if slash else "ipp/print")
    if slash:
        return "ipp://%s/%s" % (name, path)
    if "." in host or ":" in host:
        return "ipp://%s/ipp/print" % host
    return CUPS_URI + "printers/" + quote(host)


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%")[0]).version == 6
    except ValueError:
        return False


def _is_local_queue(uri: str) -> bool:
    return (urlsplit(uri).hostname or "") in ("localhost", "127.0.0.1", "::1")


# --- errors ------------------------------------------------------------------------


def map_error(error: Exception) -> PrinterError:
    if isinstance(error, ssl.SSLError):  # an OSError, but not "not found"
        return PrinterError(error_text(error))
    if isinstance(error, ConnectionRefusedError):  # no CUPS, or no IPP there
        return PrinterNotFound(error_text(error))
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    if isinstance(error, OSError) or "resolve" in text.lower():
        return PrinterNotFound(text)
    return PrinterError(text)


class IppNotFound(PrinterNotFound):
    """The printer answered "not found" (IPP status 0x0406): for a job, the
    printer no longer knows it; unlike a network error, it did answer."""


def _is_problem(reason: Any) -> bool:
    """Warnings, reports and the routine states CUPS reports are none; nor
    is a supply running low ("toner-low", also without a suffix)."""
    return (
        isinstance(reason, str)
        and reason not in ("", "none")
        and not reason.endswith(("-warning", "-report", "-low"))
        and not reason.startswith(ROUTINE)
    )


def reasons_error(reasons: list[Any], local_queue: bool = False) -> PrinterError | None:
    """The problem in printer-state-reasons, or None; a known problem wins
    over one Etiketto has no text for."""
    problems = [reason for reason in reasons if _is_problem(reason)]
    if not problems:
        return None
    keywords = [reason.removesuffix("-error") for reason in problems]
    known = [k for k in keywords if k in REASONS]
    keyword = known[0] if known else keywords[0]
    cls = REASONS.get(keyword, PrinterError)
    if cls is PrinterPaused and local_queue:
        cls = QueuePaused  # resumed in the system settings, not on the printer
    return cls("printer-state-reasons: " + ", ".join(problems))


def known_error(reasons: list[Any], local_queue: bool = False) -> PrinterError | None:
    """Only the problems Etiketto knows (out of paper, jam ...): while a job
    prints, an unknown keyword may be something the printer copes with."""
    known = [
        reason
        for reason in reasons
        if _is_problem(reason) and reason.removesuffix("-error") in REASONS
    ]
    return reasons_error(known, local_queue)


def _paused(local_queue: bool, details: str) -> PrinterError:
    return (QueuePaused if local_queue else PrinterPaused)(details)


def state_error(attrs: dict, local_queue: bool = False) -> PrinterError | None:
    """The problem in printer-state-reasons, else "paused" if the printer
    (or the queue) is stopped."""
    error = reasons_error(_texts(attrs, "printer-state-reasons"), local_queue)
    if error is None and _int(attrs, "printer-state", IDLE) == STOPPED:
        error = _paused(local_queue, "printer-state: stopped")
    return error


def _send(uri: str, operation: int, attributes: list[ipp.Attribute], **kwargs) -> ipp.Response:
    """The printer's answer, whatever its status."""
    try:
        body = ipp.encode(
            operation, 1, [ipp.Attribute(ipp.URI, "printer-uri", [uri]), *attributes], **kwargs
        )
        return ipp.send(uri, body, TIMEOUT)
    except Exception as e:  # noqa: BLE001 - network and protocol errors alike
        raise map_error(e) from e


def _request(uri: str, operation: int, attributes: list[ipp.Attribute], **kwargs) -> ipp.Response:
    """The printer's answer; PrinterError unless it is successful."""
    return _checked(_send(uri, operation, attributes, **kwargs))


def _checked(answer: ipp.Response) -> ipp.Response:
    if not answer.ok:
        message = _text(answer.group(ipp.OPERATION_GROUP), "status-message")
        if answer.status == 0x0406:  # client-error-not-found
            raise IppNotFound("IPP status 0x%04x %s" % (answer.status, message))
        raise PrinterError("IPP status 0x%04x %s" % (answer.status, message))
    return answer


# Printers on the network are not trusted to send what the standard says:
# a value of the wrong type is treated as missing.


def _value(attrs: dict, name: str, kind: type | tuple[type, ...], default: Any) -> Any:
    values = attrs.get(name)
    if isinstance(values, list) and values and isinstance(values[0], kind):
        return values[0]
    return default


def _text(attrs: dict, name: str) -> str:
    return _value(attrs, name, str, "")


def _int(attrs: dict, name: str, default: int | None = None) -> int | None:
    value = _value(attrs, name, int, default)
    return default if isinstance(value, bool) else value


def _texts(attrs: dict, name: str) -> list[str]:
    return [v for v in attrs.get(name) or [] if isinstance(v, str)]


def _user() -> ipp.Attribute:
    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers)
        user = "etiketto"
    return ipp.Attribute(ipp.NAME, "requesting-user-name", [user])


# --- unprintable margins -------------------------------------------------------------


def _media_margins(collection: Any) -> Margins | None:
    """The margins of one media-col entry, for its paper size; None for a
    borderless entry (all margins 0: only used when asked for) or one
    without a fixed size or without margins."""
    if not isinstance(collection, dict):
        return None
    size = _value(collection, "media-size", dict, {})
    width, height = _int(size, "x-dimension"), _int(size, "y-dimension")
    sides = [_int(collection, "media-%s-margin" % side) for side in MARGIN_SIDES]
    if not width or not height or None in sides or not any(sides):
        return None
    top, right, bottom, left = (v / 100 for v in sides if v is not None)
    return Margins(top, right, bottom, left, size_mm=(width / 100, height / 100))


def _smallest_margin(attrs: dict, side: str) -> int | None:
    """The smallest margin but 0 (borderless, only when asked for)."""
    values = [
        v
        for v in attrs.get("media-%s-margin-supported" % side) or []
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    ]
    if not values:
        return None
    return min((v for v in values if v), default=0)


def margins(attrs: dict) -> tuple[Margins, ...]:
    """The printer's unprintable margins: per paper size as media-col-ready
    and media-col-database list them (loaded paper first), then for any
    paper from media-*-margin-supported."""
    found = [
        m
        for name in ("media-col-ready", "media-col-database")
        for m in map(_media_margins, attrs.get(name) or [])
        if m is not None
    ]
    sides = [_smallest_margin(attrs, side) for side in MARGIN_SIDES]
    if None not in sides:
        top, right, bottom, left = (v / 100 for v in sides if v is not None)
        found.append(Margins(top, right, bottom, left))
    return tuple(found)


# --- page size -----------------------------------------------------------------------


def _hundredths(mm: float) -> int:
    return round(mm * 100)


def _candidate(value: Any, wanted: int) -> int | None:
    """What a listed dimension offers for wanted: the value itself, or the
    nearest point of a range (rangeOfInteger, custom sizes)."""
    if isinstance(value, tuple) and len(value) == 2:
        return min(max(wanted, value[0]), value[1])
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _nearest(values: Any, wanted: int) -> int | None:
    """The listed dimension nearest to wanted, if within SIZE_TOLERANCE."""
    if not isinstance(values, list):
        return None
    offers = [_candidate(v, wanted) for v in values]
    close = [o for o in offers if o is not None and abs(o - wanted) <= SIZE_TOLERANCE]
    return min(close, key=lambda o: abs(o - wanted), default=None)


def match_size(sizes: list[Any], width: int, height: int) -> tuple[int, int] | None:
    """The size from media-size-supported (hundredths of mm) to ask for:
    the listed one nearest to the label's, within 1 mm, so that the printer
    does not fall back to its default paper; None if none is close. An
    unknown list means the printer will cope: the label's own size."""
    collections = [s for s in sizes if isinstance(s, dict)]
    if not collections:
        return width, height
    matches = []
    for size in collections:
        x = _nearest(size.get("x-dimension"), width)
        y = _nearest(size.get("y-dimension"), height)
        if x is not None and y is not None:
            matches.append((x, y))
    return min(matches, key=lambda m: abs(m[0] - width) + abs(m[1] - height), default=None)


def _media(width: int, height: int) -> ipp.Attribute:
    """media-col with the page size in hundredths of millimetres."""
    size = {
        "x-dimension": ipp.Attribute(ipp.INTEGER, "x-dimension", [width]),
        "y-dimension": ipp.Attribute(ipp.INTEGER, "y-dimension", [height]),
    }
    member = ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size", [size])
    return ipp.Attribute(ipp.BEGIN_COLLECTION, "media-col", [{"media-size": member}])


# --- documents ---------------------------------------------------------------------


PDF, PWG_RASTER, URF, JPEG = "application/pdf", "image/pwg-raster", "image/urf", "image/jpeg"
RASTERS = (PWG_RASTER, URF)  # copies as pages, at the printer's resolution
MIN_DPI, MAX_DPI = 100, 1200  # raster resolutions taken from a printer
# a raster page at the printer's resolution larger than this is refused
# (PageTooLarge): A4 fits up to about 700 dpi
MAX_PAGE_PIXELS = 50_000_000


def document_format(attrs: dict) -> str:
    """The format to send: PDF, PWG Raster, Apple Raster (URF) or JPEG, the
    first the printer takes; a raster format only in a colour space
    Etiketto can make."""
    formats = _texts(attrs, "document-format-supported")
    if not formats:
        return PDF
    usable = {PWG_RASTER: _pwg_mode(attrs), URF: _urf_mode(attrs)}
    for mime in (PDF, PWG_RASTER, URF, JPEG):
        if mime in formats and usable.get(mime, "") is not None:
            return mime
    details = "document-format-supported: " + ", ".join(formats)
    if PWG_RASTER in formats:
        types = _texts(attrs, "pwg-raster-document-type-supported")
        details += "; pwg-raster-document-type-supported: " + ", ".join(types)
    if URF in formats:
        details += "; urf-supported: " + ", ".join(_urf_keywords(attrs))
    raise NoFormat(details)


def document(image: Image.Image, attrs: dict, pages: int = 1) -> tuple[bytes, str]:
    """The page in a format the printer takes: PDF, PWG Raster, Apple Raster
    or JPEG. Only the raster formats repeat the page (pages > 1)."""
    return document_of([image], 1, attrs, pages)


def document_of(
    images: Iterable[Image.Image], count: int, attrs: dict, repeat: int = 1, dpi: float = DPI
) -> tuple[bytes, str]:
    """count pages in a format the printer takes: PDF, PWG Raster or Apple
    Raster with all of them, JPEG with the first (one job per page). PDF
    and the raster formats without loss, JPEG at its best quality, without
    chroma subsampling, so that small text stays sharp. Each page is
    encoded when it comes. Only the raster formats repeat a single page
    (repeat > 1), compressed once. dpi: the resolution of the PDF's images
    (see pdf_resolution) for pages without a size in mm."""
    mime = document_format(attrs)
    if mime == PDF:
        return pdf.encode(images, dpi), mime
    if mime in RASTERS:
        mode, dpi = _raster_format(attrs, mime)
        pages = (_raster_page(image, mode, dpi) for image in images)
        encode = pwg.encode_pages if mime == PWG_RASTER else urf.encode
        data = encode(pages, dpi, count)
        return (join_documents([data] * repeat, mime) if repeat > 1 else data), mime
    buf = io.BytesIO()
    image = next(iter(images)).convert("RGB")
    image.save(buf, format="JPEG", quality=95, subsampling=0, dpi=(DPI, DPI))
    return buf.getvalue(), mime


def join_documents(documents: list[bytes], mime: str) -> bytes:
    """One raster document of the pages of several."""
    return (pwg.join if mime == PWG_RASTER else urf.join)(documents)


def _raster_format(attrs: dict, mime: str) -> tuple[str, int]:
    """The colour space ("RGB" or "L") and resolution for a raster format."""
    if mime == PWG_RASTER:
        return _pwg_mode(attrs) or "RGB", _resolution(_pwg_resolutions(attrs))
    return _urf_mode(attrs) or "L", _resolution(_urf_resolutions(attrs))


def _pwg_mode(attrs: dict) -> str | None:
    """The image mode for the PWG Raster colour space the printer lists:
    8-bit sRGB, else sGray; a printer that does not say takes sRGB."""
    types = _texts(attrs, "pwg-raster-document-type-supported")
    if not types or "srgb_8" in types:
        return "RGB"
    return "L" if "sgray_8" in types else None


def print_resolution(attrs: dict) -> float | None:
    """The resolution the label is rendered at for this printer (see
    _print): PDF at pdf_resolution, a raster format at the printer's, JPEG
    at 300 dpi; None if the printer takes no format Etiketto makes."""
    try:
        mime = document_format(attrs)
    except NoFormat:
        return None
    if mime == PDF:
        return float(pdf_resolution(attrs))
    if mime in RASTERS:
        return float(_raster_format(attrs, mime)[1])
    return DPI


def _pwg_resolutions(attrs: dict) -> list[int]:
    return [
        r[0]
        for r in attrs.get("pwg-raster-document-resolution-supported") or []
        if isinstance(r, tuple) and len(r) == 3 and r[0] == r[1] and r[2] == 3
    ]


def _urf_keywords(attrs: dict) -> list[str]:
    """urf-supported, also as one comma-joined value (some printers)."""
    return [
        keyword.strip().upper()
        for value in _texts(attrs, "urf-supported")
        for keyword in value.split(",")
        if keyword.strip()
    ]


URF_COLOURS = ("W8", "SRGB24", "ADOBERGB", "DEVW", "DEVRGB", "DEVCMYK")


def _urf_mode(attrs: dict) -> str | None:
    """The image mode for the Apple Raster colour space the printer lists:
    SRGB24, else W8, which every AirPrint printer must take (also one that
    lists no colour space)."""
    keywords = _urf_keywords(attrs)
    if _urf_lists(keywords, "SRGB24"):
        return "RGB"
    if _urf_lists(keywords, "W8") or not any(k.startswith(URF_COLOURS) for k in keywords):
        return "L"
    return None


def _urf_lists(keywords: list[str], name: str) -> bool:
    """Whether urf-supported lists a colour space, also with more bit
    depths after it: W8-16, SRGB24-48."""
    return any(k == name or k.startswith(name + "-") for k in keywords)


def _number(text: str) -> int | None:
    """A number of ASCII digits only ("600", not "6e2", "\u0666", "-1")."""
    return int(text) if text.isascii() and text.isdecimal() else None


def _urf_resolutions(attrs: dict) -> list[int]:
    """The resolutions in urf-supported: RS300, RS300-600 (a list, not a
    range: PAPPL writes its last two resolutions so)."""
    values = [
        _number(value)
        for keyword in _urf_keywords(attrs)
        if keyword.startswith("RS")
        for value in keyword[2:].split("-")
    ]
    return [value for value in values if value is not None]


def _urf_copies(attrs: dict) -> bool:
    """Whether urf-supported says the printer makes copies: CP<n>, n >= 1."""
    counts = [_number(k[2:]) for k in _urf_keywords(attrs) if k.startswith("CP")]
    return any(count is not None and count >= 1 for count in counts)


def _resolution(dpis: list[int]) -> int:
    """300 dpi if the printer takes it, else the lowest it has above (or
    its highest): never less sharp than the render. Resolutions outside
    MIN_DPI..MAX_DPI are not believed (and a page at more would not fit in
    memory)."""
    dpis = sorted(d for d in dpis if MIN_DPI <= d <= MAX_DPI)
    if not dpis or int(DPI) in dpis:
        return int(DPI)
    return next((d for d in dpis if d > DPI), dpis[-1])


def _raster_page(image: Image.Image, mode: str, dpi: int) -> Image.Image:
    """The page at dpi: as it is if it was rendered at dpi (see
    _at_resolution), else resized: pixel by pixel for a whole factor (600
    dpi from 300: no grey edges where there were none), smoothly else."""
    width_mm, height_mm = _size_mm(image)
    size = (round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi))
    if abs(image.width - size[0]) > 1 or abs(image.height - size[1]) > 1:
        whole = dpi % DPI == 0
        method = Image.Resampling.NEAREST if whole else Image.Resampling.LANCZOS
        if whole:
            size = (image.width * int(dpi // DPI), image.height * int(dpi // DPI))
        image = image.resize(size, method)
    return image.convert(mode)


def _dpi(resolution: Any) -> int | None:
    """A square IPP resolution in dots per inch (dots per cm converted:
    8 dots/mm is 203 dpi); None for anything else."""
    if not (isinstance(resolution, tuple) and len(resolution) == 3):
        return None
    x, y, units = resolution
    if x != y or x <= 0 or units not in (3, 4):
        return None
    return x if units == 3 else round(x * 2.54)


def pdf_resolution(attrs: dict) -> int:
    """The resolution of the PDF's page images: the printer's own
    (printer-resolution-default) where 300 dpi would not land on its dots
    whole (below 300, or no whole multiple or divisor of 300 up to 600:
    a 203 dpi thermal printer gets 203 dpi, so that CUPS or the printer
    never resamples a barcode or a hairline); else 300 dpi (600 and 1200
    dpi printers double every pixel exactly). Below MIN_DPI it is not
    believed."""
    default = attrs.get("printer-resolution-default")
    dpi = _dpi(default[0] if isinstance(default, list) and default else default)
    if dpi is None or dpi == DPI or dpi < MIN_DPI:
        return int(DPI)
    if dpi < DPI or (dpi <= 2 * DPI and dpi % DPI):
        return dpi
    return int(DPI)


# --- the backend -----------------------------------------------------------------


class SystemPrinter(Backend):
    query_before_render = True  # the resolution the printer takes (the check's)
    id = "system"
    name = "CUPS / IPP"  # the interface shows a translated name
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI,
        modes=(PrintMode("standard", round(DPI)),),
        color_model="color",
        cut_modes=(),
        continuous=False,
        pages=True,
        label_sizes=PAPER_SIZES,
        max_width_mm=MAX_PAGE_WIDTH_MM,
        data_formats=("pdf",),
    )

    def display_name(self, info: PrinterInfo | None) -> str:
        return info.model if info and info.model else self.name

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """CUPS queues on this computer (USB printers too), then network
        printers that announce IPP and are not a CUPS queue already."""
        found = _cups_queues()
        known = {f.name for f in found}
        for service_type, scheme in (("_ipp._tcp", "ipp"), ("_ipps._tcp", "ipps")):
            for s in mdns.browse_services(service_type, lambda name: True, timeout):
                if s.name in known:
                    continue
                known.add(s.name)
                path = s.txt.get("rp", "ipp/print")
                uri = "%s://%s:%d/%s" % (scheme, s.host, s.port, path)
                found.append(FoundPrinter(name=s.name, host=uri, address=s.address))
        return found

    def query(self, host: str) -> PrinterInfo:
        uri = printer_uri(host)
        attrs = _printer_attributes(uri, PRINTER_ATTRIBUTES + MARGIN_ATTRIBUTES)
        state = _int(attrs, "printer-state", IDLE)
        error = state_error(attrs, _is_local_queue(uri))
        return PrinterInfo(
            model=_text(attrs, "printer-make-and-model") or _text(attrs, "printer-info"),
            tape_mm=None,
            remaining_mm=None,
            total_mm=None,
            state="ERROR" if error else ("BUSY" if state == PROCESSING else "IDLE"),
            error=error.code if error else "",
            firmware=_text(attrs, "printer-firmware-string-version"),
            margins=margins(attrs),
            dpi=print_resolution(attrs),
        )

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """One page: x across the paper, y down the page. Its size in mm
        travels with it (info), exact for paper sizes like A4; and how to
        render it again at another resolution (info["render_at"](dpi)), for
        a printer that takes PWG Raster at another one."""
        return self._prepare_at(label, options, safe_mm, DPI)

    def render_label(
        self,
        label: Label,
        options: PrintOptions,
        safe_mm: float = DEFAULT_SAFE_MM,
        max_dpi: float | None = None,
        bleed: bool = True,
        dpi: float | None = None,
    ) -> Rendered:
        """As every backend, but a page too large for the resolution is
        refused before it is rendered (PageTooLarge), as the print refuses
        it: the command line's check would otherwise render 140 megapixels.
        The preview renders at a resolution it chose with label_dpi() and
        its own pixel budget."""
        at = dpi or self.label_dpi(label, options, max_dpi)
        check_size(*label.media_size(), at)
        return super().render_label(label, options, safe_mm, max_dpi, bleed, at)

    def label_dpi(
        self,
        label: Label,
        options: PrintOptions,
        max_dpi: float | None = None,
        max_pixels: float | None = None,
    ) -> float:
        """The printer's resolution, at most `max_dpi`; with `max_pixels`
        (the preview and the export) only as fine as the page takes at most
        that many pixels: an A3 page at 600 dpi would be 70 megapixels."""
        dpi = self.render_dpi(label.tape_mm, options, max_dpi)
        if max_pixels is None:
            return dpi
        return min(dpi, fitting_dpi(*label.media_size(), max_pixels))

    def _prepare_at(
        self, label: Label, options: PrintOptions, safe_mm: float, dpi: float
    ) -> Image.Image:
        label = replace(label, auto_length=False)
        rendered = self.render_label(label, options, safe_mm, dpi=dpi)  # the one path
        check_codes(rendered.warnings)  # at the resolution it is printed at
        image = rendered.image
        image = self.preview(image, options.mode)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info["size_mm"] = label.media_size()
        image.info["render_at"] = partial(self._prepare_at, label, options, safe_mm)
        return image

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        return self._print([image], host, max(1, options.copies), progress)

    def print_pages(
        self,
        pages: Sequence[Image.Image],
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        """All pages in one job (one job per page for JPEG), each once."""
        return self._print(pages, host, 1, progress)

    def _print(
        self,
        pages: Sequence[Image.Image],
        host: str,
        copies: int,
        progress: ProgressCallback | None,
    ) -> PrintResult | None:
        uri = printer_uri(host)
        attrs = _printer_attributes(uri)
        problem = state_error(attrs, _is_local_queue(uri))
        if problem:
            raise problem
        mime = document_format(attrs)  # NoFormat before anything is sent
        dpi = DPI
        if mime in RASTERS:
            raster_dpi = _raster_format(attrs, mime)[1]
            check_page_size(pages[0], raster_dpi)  # before rendering it
            pages = _at_resolution(pages, raster_dpi)
        elif mime == PDF:
            again = _at_resolution(pages, pdf_resolution(attrs))
            if again is not pages:  # else the pages stay at 300 dpi
                pages, dpi = again, pdf_resolution(attrs)
        turned, width, height = _page(pages[0], attrs.get("media-size-supported") or [])
        if turned:
            pages = _Turned(pages)
        return _Jobs(uri, attrs, _Pages(pages, width, height, dpi), copies, progress).run()


def check_page_size(page: Image.Image, dpi: int) -> None:
    """PageTooLarge if a page of this size at dpi would take more than
    MAX_PAGE_PIXELS: a printer that takes Apple Raster at 1200 dpi only
    would need an A4 page of 140 megapixels (over 400 MB) in memory."""
    width_mm, height_mm = page.info.get("size_mm") or (
        page.width / DPI * 25.4,
        page.height / DPI * 25.4,
    )
    check_size(width_mm, height_mm, dpi)


def fitting_dpi(width_mm: float, height_mm: float, max_pixels: float | None = None) -> int:
    """The finest whole resolution at which a page of this size takes no
    more than max_pixels (by default MAX_PAGE_PIXELS: check_size()
    accepts it)."""
    budget = MAX_PAGE_PIXELS if max_pixels is None else max_pixels
    square_inches = max(width_mm / 25.4 * height_mm / 25.4, 1e-6)
    return max(1, math.floor(math.sqrt(budget / square_inches)))


def check_size(width_mm: float, height_mm: float, dpi: float) -> None:
    """PageTooLarge for a page of this size at dpi (see check_page_size)."""
    pixels = (width_mm / 25.4 * dpi) * (height_mm / 25.4 * dpi)
    if pixels > MAX_PAGE_PIXELS:
        raise PageTooLarge(
            "%.0f x %.0f mm at %d dpi: %.0f megapixels" % (width_mm, height_mm, dpi, pixels / 1e6)
        )


def _at_resolution(pages: Sequence[Image.Image], dpi: int) -> Sequence[Image.Image]:
    """The pages rendered again at dpi where they can be (a label, or the
    pages of a label sheet): sharper than resized, and without grey edges
    that were not there. Pages that cannot are resized when encoded."""
    if dpi == DPI:
        return pages
    at_dpi = getattr(pages, "at_dpi", None)  # the pages of a label sheet
    if at_dpi is not None:
        return at_dpi(dpi) or pages
    if len(pages) == 1 and callable(render_at := pages[0].info.get("render_at")):
        return [render_at(dpi)]
    return pages


class _Turned(Sequence[Image.Image]):
    """The pages, each turned to portrait when it is asked for."""

    def __init__(self, pages: Sequence[Image.Image]):
        self.pages = pages

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index):
        page = self.pages[index]
        turned = page.rotate(90, expand=True)
        width, height = _size_mm(page)
        turned.info["size_mm"] = (height, width)
        return turned


def _size_mm(image: Image.Image) -> tuple[float, float]:
    return image.info.get("size_mm") or (image.width / DPI * 25.4, image.height / DPI * 25.4)


def _page(image: Image.Image, sizes: list[Any]) -> tuple[bool, int, int]:
    """Whether the page has to be turned to portrait (the printer only
    takes it that way) and its size in hundredths of mm; UnsupportedSize if
    the printer does not take it at all."""
    width_mm, height_mm = _size_mm(image)
    width, height = _hundredths(width_mm), _hundredths(height_mm)
    size = match_size(sizes, width, height)
    if size:
        return False, size[0], size[1]
    size = match_size(sizes, height, width)
    if size:
        return True, size[0], size[1]
    raise UnsupportedSize("%g × %g mm not in media-size-supported" % (width_mm, height_mm))


def _printer_attributes(uri: str, keywords: list[str] = PRINTER_ATTRIBUTES) -> dict:
    requested = ipp.Attribute(ipp.KEYWORD, "requested-attributes", keywords)
    return _request(uri, ipp.GET_PRINTER_ATTRIBUTES, [requested]).group(ipp.PRINTER_GROUP)


def _cups_queues() -> list[FoundPrinter]:
    """The printers and classes CUPS on this computer has; [] without CUPS."""
    keywords = ["printer-name", "printer-info", "printer-uri-supported"]
    try:
        answer = _request(
            CUPS_URI,
            ipp.CUPS_GET_PRINTERS,
            [ipp.Attribute(ipp.KEYWORD, "requested-attributes", keywords)],
        )
    except PrinterError:
        return []
    found = []
    for attrs in answer.all(ipp.PRINTER_GROUP):
        name = _text(attrs, "printer-name")
        if not name:
            continue
        # /printers/<name> or /classes/<name>, as CUPS says
        path = urlsplit(_text(attrs, "printer-uri-supported")).path.lstrip("/")
        uri = CUPS_URI + (path or "printers/" + quote(name))
        found.append(FoundPrinter(_text(attrs, "printer-info") or name, uri, "CUPS"))
    return found


def _job_attributes(uri: str, job_id: int) -> dict:
    keywords = [
        "job-state",
        "job-state-reasons",
        "job-impressions-completed",
        "job-media-sheets-completed",
    ]
    return _request(
        uri,
        ipp.GET_JOB_ATTRIBUTES,
        [
            ipp.Attribute(ipp.INTEGER, "job-id", [job_id]),
            _user(),
            ipp.Attribute(ipp.KEYWORD, "requested-attributes", keywords),
        ],
    ).group(ipp.JOB_GROUP)


def _completed(attrs: dict) -> int | None:
    """Pages (impressions, or sheets) the printer has finished of a job;
    None if it counts neither."""
    counts = [_int(attrs, "job-impressions-completed"), _int(attrs, "job-media-sheets-completed")]
    known = [count for count in counts if count is not None]
    return max(known) if known else None


def takes_copies(attrs: dict, copies: int) -> bool:
    """Whether the printer makes this many copies itself (copies-supported,
    a range 1..n); a printer that does not say is trusted to, unless it gets
    Apple Raster and urf-supported has no CP."""
    supported = _value(attrs, "copies-supported", (tuple, int), None)
    if isinstance(supported, tuple) and len(supported) == 2:
        return supported[1] >= copies
    if isinstance(supported, int):
        return supported >= copies
    return document_format(attrs) != URF or _urf_copies(attrs)


# --- sending and following jobs -------------------------------------------------------


@dataclass
class _Pages:
    images: Sequence[Image.Image]
    width: int  # hundredths of mm, as the printer lists the size
    height: int
    dpi: float = DPI  # of the images


# PrinterError params that count labels, or sheets with "pages"
_COUNTED = frozenset(("printed", "queue_canceled"))


class _Jobs:
    """Sends the copies: in one job if the printer makes copies itself,
    else the page repeated in jobs of up to PAGES_PER_JOB pages (PWG
    Raster, Apple Raster: compressed once) or one job per copy. Several
    pages (label sheets, each once) go in jobs of up to PAGES_PER_JOB pages,
    as JPEG one job per page: a document is made in memory, so a long job
    is split; raster pages also once they reach MAX_JOB_BYTES. Each job is
    followed until it is done; once one waits in the queue, the others are
    sent behind it without waiting. If a step fails, the jobs sent and not
    over are canceled, so that they do not print later on their own.
    Progress counts copies, or pages."""

    def __init__(
        self, uri: str, attrs: dict, pages: _Pages, copies: int, progress: ProgressCallback | None
    ):
        self.uri, self.attrs, self.pages = uri, attrs, pages
        self.copies, self.progress = copies, progress
        self.several = len(pages.images) > 1  # pages, each once
        self.total = len(pages.images) if self.several else copies
        self.queued = False
        self.sent: list[int] = []  # job ids
        self.over: set[int] = set()  # nothing left to cancel: done, ended or canceled
        self.printed = 0  # labels (or pages) the printer said it finished
        # (first page, pages, repeats), data, format
        self._document: tuple[tuple[int, int, int], bytes, str] | None = None
        self._single: tuple[bytes, str] | None = None  # the one page, compressed once

    def run(self) -> PrintResult | None:
        try:
            if self.several:
                self._all_pages()
            else:
                self._send_all()
        except Exception as e:
            canceled = self._cancel_unfinished()
            if isinstance(e, PrinterError):
                self._explain(e, canceled)
            raise
        return PrintResult(queued=True) if self.queued else None

    def _explain(self, error: PrinterError, canceled: bool) -> None:
        """What the message adds: the labels (or sheets) printed already,
        and whether jobs waiting in the queue were canceled."""
        if self.printed:
            error.params.update(printed=self.printed, copies=self.total)
        if canceled and self.queued:
            error.params["queue_canceled"] = True
        if self.several and error.params.keys() & _COUNTED:
            error.params["pages"] = True  # the counts are of sheets

    def _cancel_unfinished(self) -> bool:
        """Cancel-Job for the jobs sent and not over; whether there were
        any and the printer could be reached. Once it cannot, trying the
        others is no use: each would wait TIMEOUT."""
        unfinished = [job_id for job_id in self.sent if job_id not in self.over]
        for job_id in unfinished:
            if not _cancel(self.uri, job_id):
                return False
        return bool(unfinished)

    def _send_all(self) -> None:
        together = self.copies == 1 or takes_copies(self.attrs, self.copies)
        ignored = self._validate(self.copies if together else 0)
        done = 0
        if together and "copies" not in ignored:
            ignored = self._job(0, self.copies, copies=self.copies)
            # a printer that ignored copies printed one
            done = 1 if "copies" in ignored else self.copies
        if done < self.copies:
            self._one_by_one(done)

    def _all_pages(self) -> None:
        """The pages in order, each once."""
        self._validate(0)
        mime = document_format(self.attrs)
        if mime in RASTERS:
            for first, parts in self._raster_batches():
                document = (join_documents(parts, mime), mime)
                self._job(first, len(parts), first=first, document=document)
            return
        if mime == PDF:
            for first, document, count in self._pdf_batches():
                self._job(first, count, first=first, document=document)
            return
        for first in range(self.total):  # JPEG: a job each
            self._job(first, 1, first=first)

    def _pdf_batches(self) -> Iterator[tuple[int, tuple[bytes, str], int]]:
        """The pages as PDF documents for one job each: up to PAGES_PER_JOB
        pages, and no further page once another as large as the last would
        take the document over MAX_JOB_BYTES (one page at least)."""
        images = self.pages.images
        first = 0
        while first < self.total:
            handed = 0

            def given(start: int = first) -> Iterator[Image.Image]:
                nonlocal handed
                for n in range(start, min(start + PAGES_PER_JOB, self.total)):
                    handed += 1
                    yield images[n]

            data = pdf.encode(given(), self.pages.dpi, MAX_JOB_BYTES)
            yield first, (data, PDF), handed
            first += handed

    def _raster_batches(self) -> Iterator[tuple[int, list[bytes]]]:
        """The pages, each a document of its own, in groups for one job:
        up to PAGES_PER_JOB pages and MAX_JOB_BYTES (one page at least)."""
        images = self.pages.images
        first, parts, size = 0, list[bytes](), 0
        for n in range(self.total):
            part = document_of([images[n]], 1, self.attrs)[0]
            if parts and (len(parts) == PAGES_PER_JOB or size + len(part) > MAX_JOB_BYTES):
                yield first, parts
                first, parts, size = n, [], 0
            parts.append(part)
            size += len(part)
        yield first, parts

    def _one_by_one(self, done: int) -> None:
        if document_format(self.attrs) in RASTERS:
            single = len(self.data(0, 1, 1)[0])
            step = max(1, min(PAGES_PER_JOB, MAX_JOB_BYTES // single))
            for first in range(done, self.copies, step):
                pages = min(step, self.copies - first)
                self._job(first, pages, pages=pages)
            return
        for n in range(done, self.copies):
            self._job(n, 1)

    def data(self, first: int, count: int, repeat: int) -> tuple[bytes, str]:
        """The document of count pages from the first one (one page:
        repeated, compressed once), made once for all jobs alike; the last
        one is let go before the next is made."""
        if not self.several:
            if self._single is None:
                self._single = document_of(
                    [self.pages.images[0]], 1, self.attrs, dpi=self.pages.dpi
                )
            if repeat == 1:
                return self._single
        key = (first, count, repeat)
        if self._document is None or self._document[0] != key:
            self._document = None
            if self._single is not None:  # copies of the one page, as pages
                data, mime = self._single
                self._document = (key, join_documents([data] * repeat, mime), mime)
            else:
                images = self.pages.images
                pages = (images[n] for n in range(first, first + count))
                document = document_of(pages, count, self.attrs, repeat, dpi=self.pages.dpi)
                self._document = (key, *document)
        return self._document[1], self._document[2]

    def _operation(self, mime: str) -> list[ipp.Attribute]:
        return [
            _user(),
            ipp.Attribute(ipp.NAME, "job-name", ["Etiketto"]),
            ipp.Attribute(ipp.MIME_TYPE, "document-format", [mime]),
        ]

    def _template(self, copies: int) -> list[ipp.Attribute]:
        job = [_media(self.pages.width, self.pages.height)]
        if copies:
            job.insert(0, ipp.Attribute(ipp.INTEGER, "copies", [copies]))
        if "none" in _texts(self.attrs, "print-scaling-supported"):
            job.append(ipp.Attribute(ipp.KEYWORD, "print-scaling", ["none"]))
        return job

    def _validate(self, copies: int) -> set[str]:
        """Validate-Job, if the printer has it: the attributes it would
        ignore, before anything prints. A printer that cannot validate the
        job says so after Print-Job instead."""
        operations = self.attrs.get("operations-supported")
        if not isinstance(operations, list) or ipp.VALIDATE_JOB not in operations:
            return set()
        answer = _send(
            self.uri,
            ipp.VALIDATE_JOB,
            self._operation(document_format(self.attrs)),
            job_attributes=self._template(copies),
        )
        # 0x040b: client-error-attributes-or-values-not-supported
        if answer.status not in (0x0000, 0x0001, 0x040B):
            return set()
        ignored = set(answer.group(ipp.UNSUPPORTED_GROUP))
        if "media-col" in ignored:  # it would print on its default paper
            raise UnsupportedSize(
                "media-col ignored (Validate-Job, IPP status 0x%04x)" % answer.status
            )
        return ignored

    def _job(
        self,
        done: int,
        count: int,
        copies: int = 0,
        pages: int = 1,
        first: int = 0,
        document: tuple[bytes, str] | None = None,
    ) -> set[str]:
        """Print-Job for count labels after done ones: copies of the page,
        the page repeated pages times, or count pages from the first one
        (or the document made for them); the names of the attributes the
        printer ignored."""
        report_progress(self.progress, done + 1, self.total)
        if document is not None:
            data, mime = document
        elif self.several:
            data, mime = self.data(first, count, 1)
        else:
            data, mime = self.data(0, 1, pages)
        answer = self._print_job(data, mime, copies)
        job_id = _int(answer.group(ipp.JOB_GROUP), "job-id")
        if job_id is not None:
            self.sent.append(job_id)
        # status 0x0001 (successful-ok-ignored-or-substituted-attributes)
        ignored = set(answer.group(ipp.UNSUPPORTED_GROUP))
        if "media-col" in ignored:  # canceled by run()
            raise UnsupportedSize("media-col ignored (IPP status 0x%04x)" % answer.status)
        if job_id is not None and not self.queued:
            self._wait(job_id, done, 1 if "copies" in ignored else count)
        return ignored

    def _print_job(self, data: bytes, mime: str, copies: int) -> ipp.Response:
        """Print-Job; a printer that is busy (with the last job, or another
        computer's) makes no job and is asked again for up to BUSY_LIMIT."""
        start = _clock()
        while True:
            answer = _send(
                self.uri,
                ipp.PRINT_JOB,
                self._operation(mime),
                job_attributes=self._template(copies),
                data=data,
            )
            if answer.status != BUSY:
                return _checked(answer)
            if _int(answer.group(ipp.JOB_GROUP), "job-id"):
                return answer  # busy, yet a job was made: never send it twice
            if _clock() - start >= BUSY_LIMIT:
                raise PrinterBusy("IPP status 0x%04x for %g s" % (BUSY, BUSY_LIMIT))
            _sleep(BUSY_PAUSE)

    def _wait(self, job_id: int, done: int, count: int) -> None:
        def report(completed: int, stage: str) -> None:
            self.printed = max(self.printed, done + min(count, completed))
            report_progress(self.progress, done + min(count, completed + 1), self.total, stage)

        watch = _Watch(self.uri, job_id, count)
        try:
            self.queued = _wait_done(watch, report)
        finally:
            if watch.over:
                self.over.add(job_id)
        if not self.queued:
            self.over.add(job_id)
            self.printed = done + count
            report_progress(self.progress, done + count, self.total)


WAITING, DONE, QUEUED = "waiting", "done", "queued"


class _Watch:
    """Follows one job, judged by its own state. A problem the user fixes at
    the printer (out of paper, cover open, jam) is shown while Etiketto
    waits for it to be fixed. A job that stops for another reason, is held,
    waits in a stopped queue, meets another known printer problem or makes
    no progress is canceled, so that it does not print later on its own; a
    job waiting behind others is left to print when its turn comes."""

    MISSED = object()  # no answer this time

    def __init__(self, uri: str, job_id: int, labels: int):
        self.uri, self.job_id, self.labels = uri, job_id, labels
        self.local = _is_local_queue(uri)
        self.pending: float | None = None  # since when the job waits in the queue
        self.progressed: float | None = None  # when it started, or did its last page
        self.completed = 0  # pages done, as the printer counts them
        self.counting = False  # the printer's page counter moved
        self.failures = 0  # answers missed in a row
        self.problems = 0  # answers in a row that show a problem
        self.problem: PrinterError | None = None  # the printer's, at its last answer
        # the kind of problem to fix and since when; the time spent waiting
        # for fixes: both since the last page
        self.recovery: tuple[type[PrinterError], float] | None = None
        self.fixing: float | None = None  # first problem to fix since the last page
        self.over = False  # ended, or canceled here: nothing more to cancel
        self.stage = ""  # for the interface: what the user has to do

    def attributes(self) -> Any:
        """The job's attributes; None if the printer forgot the job (some
        keep no history: it is done), MISSED if it did not answer. Only the
        printer saying "not found" counts as forgotten: a network error, or
        an answer without a job state, is a missed answer."""
        try:
            attrs = _job_attributes(self.uri, self.job_id)
        except IppNotFound:
            return None
        except PrinterError as e:
            return self._missed(e)
        if _int(attrs, "job-state", 0) not in JOB_STATES:
            return self._missed(PrinterError("job %s: no job-state" % self.job_id))
        self.failures = 0
        return attrs

    def _missed(self, error: PrinterError) -> Any:
        self.failures += 1
        if self.failures > POLL_FAILURES:
            raise error
        return self.MISSED

    def judge(self, attrs: dict) -> str:
        """DONE, QUEUED (left to print later) or WAITING; raises on problems."""
        state = _int(attrs, "job-state", 0) or 0
        reasons = _texts(attrs, "job-state-reasons")
        if state == JOB_COMPLETED:
            return DONE
        if state in (JOB_CANCELED, JOB_ABORTED):
            self.over = True
            raise _ended(self.job_id, state, reasons)
        if state != JOB_PENDING:
            self.pending = None
        now = _clock()
        self._follow(attrs, now)
        problem = self._problem(state, reasons)
        if problem is not None and type(problem) in RECOVERABLE:
            self._recover(problem, now)
            return WAITING
        self._suspect(problem)
        if state == JOB_PROCESSING:
            self._check_progress(now)
        return self._queued(now) if state == JOB_PENDING else WAITING

    def _follow(self, attrs: dict, now: float) -> None:
        """Notes the page counter. A page
        that comes out starts the time for fixes again: a problem the
        printer reports while it prints (paper out in another tray) does
        not hold the job up."""
        done = _completed(attrs)
        if done is not None and done > self.completed:
            self.completed, self.counting, self.progressed = done, True, now
            self.fixing = None
            if self.recovery is not None:
                self.recovery = (self.recovery[0], now)

    def _problem(self, state: int, job_reasons: list[str]) -> PrinterError | None:
        """What holds the job up. Without the printer's answer nothing
        changes: the problem it reported last stays. A stopped job without
        a reason is one too."""
        if state == JOB_HELD:
            return JobHeld("job-state: held; job-state-reasons: " + ", ".join(job_reasons))
        try:
            printer = _printer_attributes(self.uri, STATE_ATTRIBUTES)
        except PrinterError:
            problem = self.problem
        else:
            problem = self.problem = self._printer_problem(state, job_reasons, printer)
        if problem is None and state == JOB_STOPPED:
            return PrinterError("job %s stopped" % self.job_id)
        return problem

    def _printer_problem(
        self, state: int, job_reasons: list[str], printer: dict
    ) -> PrinterError | None:
        """The printer's reason for a stopped job; a stopped printer (or
        queue) holds up every job; while the job prints, a problem Etiketto
        knows (out of paper, jam ...) too."""
        reasons = _texts(printer, "printer-state-reasons")
        if state == JOB_STOPPED:
            return reasons_error(reasons, self.local)
        if _int(printer, "printer-state", IDLE) == STOPPED or "printer-stopped" in job_reasons:
            return reasons_error(reasons, self.local) or _paused(self.local, "printer stopped")
        return known_error(reasons, self.local) if state == JOB_PROCESSING else None

    def _recover(self, problem: PrinterError, now: float) -> None:
        """Wait for the user to fix the problem: RECOVERY_LIMIT for each kind
        of problem, RECOVERY_TOTAL for all together, both since the last
        page; then the job is canceled."""
        kind = type(problem)
        if self.recovery is None or self.recovery[0] is not kind:
            self.recovery = (kind, now)
        if self.fixing is None:
            self.fixing = now
        total = now - self.fixing > RECOVERY_TOTAL
        if now - self.recovery[1] > RECOVERY_LIMIT or total:
            self._cancel()
            limit = RECOVERY_TOTAL if total else RECOVERY_LIMIT
            raise kind("%s; not fixed within %g s" % (problem, limit))
        # the clock for progress starts again once the printer goes on
        self.stage, self.progressed, self.problems = RECOVERABLE[kind], None, 0

    def _suspect(self, problem: PrinterError | None) -> None:
        """A problem counts once it lasts PROBLEM_POLLS answers: then the job
        is canceled. An answer without one ends the wait for a fix."""
        self.stage = ""
        if problem is None:
            self.recovery, self.problems = None, 0
            return
        self.problems += 1
        if self.problems >= PROBLEM_POLLS:
            self._cancel()
            raise problem

    def _check_progress(self, now: float) -> None:
        """Once the page counter moves, PRINT_TIMEOUT without a page is too
        long; a printer that counts nothing (or keeps 0) gets PRINT_TIMEOUT
        plus TIME_PER_LABEL per label for the whole job."""
        if self.progressed is None:
            self.progressed = now
        limit = PRINT_TIMEOUT if self.counting else PRINT_TIMEOUT + TIME_PER_LABEL * self.labels
        if now - self.progressed > limit:
            self._cancel()
            raise PrinterTimeout("job %s made no progress for %g s" % (self.job_id, limit))

    def _queued(self, now: float) -> str:
        """QUEUED once the job waited PENDING_LIMIT behind others, counted
        from when it last became pending."""
        if self.pending is None:
            self.pending = now
        return QUEUED if now - self.pending > PENDING_LIMIT else WAITING

    def _cancel(self) -> None:
        # a cancel that did not get through is tried again by the caller
        self.over = _cancel(self.uri, self.job_id)


def _ended(job_id: int, state: int, reasons: list[str]) -> PrinterError:
    """The error for a job that ended without printing: canceled by someone
    at the printer or in the queue, or aborted by the printer."""
    word = "canceled" if state == JOB_CANCELED else "aborted"
    details = "job %s %s; job-state-reasons: %s" % (job_id, word, ", ".join(reasons))
    if state == JOB_CANCELED or any(r.startswith("job-canceled-") for r in reasons):
        return JobCanceled(details)
    return PrinterError(details)


def _wait_done(watch: _Watch, report: Callable[[int, str], None]) -> bool:
    """Until the job is done (False) or left in the queue (True), see
    _Watch; report gets the number of pages finished so far and the stage
    (a problem the user can fix, or "")."""
    while True:
        attrs = watch.attributes()
        if attrs is None:
            return False
        if attrs is not watch.MISSED:
            verdict = watch.judge(attrs)
            if verdict != WAITING:
                return verdict == QUEUED
            report(_completed(attrs) or 0, watch.stage)
        _sleep(POLL_INTERVAL)


def _cancel(uri: str, job_id: int) -> bool:
    """Cancel-Job; False if the printer cannot be reached. A printer that
    refuses, or no longer knows the job, has nothing more to cancel."""
    try:
        _request(uri, ipp.CANCEL_JOB, [ipp.Attribute(ipp.INTEGER, "job-id", [job_id]), _user()])
    except (PrinterTimeout, PrinterNotFound) as e:  # a network error, or IppNotFound
        return isinstance(e, IppNotFound)
    except PrinterError:
        pass
    return True
