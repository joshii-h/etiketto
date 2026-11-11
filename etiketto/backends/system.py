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
import ssl
import time
from contextlib import suppress
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlsplit

from PIL import Image

from etiketto.backends import ipp, mdns, pwg
from etiketto.backends.base import (
    Backend,
    Capabilities,
    CoverOpen,
    FoundPrinter,
    LabelSize,
    MediaJam,
    NoFormat,
    OutOfPaper,
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
    error_text,
    network_error,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

CUPS_URI = "ipp://localhost/"
DPI = 300.0
TIMEOUT = 10.0  # one IPP request
PRINT_TIMEOUT = 300.0  # printing one job, once the printer has started it
PENDING_LIMIT = 30.0  # waiting behind other jobs: then it prints on its own
POLL_INTERVAL = 1.0
POLL_FAILURES = 3  # a busy printer may miss an answer now and then
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
]


# --- addresses ---------------------------------------------------------------------


def printer_uri(host: str) -> str:
    """The IPP address for what the user entered:
    - a printer URI (ipp, ipps; http and https as the CUPS web pages show them)
    - a CUPS queue name on this computer ("HP_LaserJet")
    - a network printer's host name or address, with or without port and
      path ("hp.local", "192.0.2.5:631", "2001:db8::5", "hp.local/ipp/print")
    """
    host = host.strip()
    scheme, sep, rest = host.partition("://")
    if sep:
        known = {"ipp": "ipp", "ipps": "ipps", "http": "ipp", "https": "ipps"}
        return known.get(scheme.lower(), scheme) + "://" + rest
    if _is_ipv6(host):
        return "ipp://[%s]/ipp/print" % host
    name, slash, path = host.partition("/")
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


def _is_problem(reason: Any) -> bool:
    """Warnings, reports and the routine states CUPS reports are none."""
    return (
        isinstance(reason, str)
        and reason not in ("", "none")
        and not reason.endswith(("-warning", "-report"))
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


def _request(uri: str, operation: int, attributes: list[ipp.Attribute], **kwargs) -> ipp.Response:
    try:
        body = ipp.encode(
            operation, 1, [ipp.Attribute(ipp.URI, "printer-uri", [uri]), *attributes], **kwargs
        )
        answer = ipp.send(uri, body, TIMEOUT)
    except Exception as e:  # noqa: BLE001 - network and protocol errors alike
        raise map_error(e) from e
    if not answer.ok:
        message = _text(answer.group(ipp.OPERATION_GROUP), "status-message")
        if answer.status == 0x0406:  # client-error-not-found
            raise PrinterNotFound("IPP status 0x%04x %s" % (answer.status, message))
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


# --- page size -----------------------------------------------------------------------


def _hundredths(mm: float) -> int:
    return round(mm * 100)


def _covers(value: Any, wanted: int) -> bool:
    if isinstance(value, tuple) and len(value) == 2:  # rangeOfInteger
        return value[0] - SIZE_TOLERANCE <= wanted <= value[1] + SIZE_TOLERANCE
    return isinstance(value, int) and abs(value - wanted) <= SIZE_TOLERANCE


def fits(sizes: list[Any], width: int, height: int) -> bool:
    """Whether media-size-supported has this size (hundredths of mm); an
    unknown list means the printer will cope."""
    collections = [s for s in sizes if isinstance(s, dict)]
    if not collections:
        return True
    return any(
        any(_covers(x, width) for x in size.get("x-dimension", []))
        and any(_covers(y, height) for y in size.get("y-dimension", []))
        for size in collections
    )


def _media(width: int, height: int) -> ipp.Attribute:
    """media-col with the page size in hundredths of millimetres."""
    size = {
        "x-dimension": ipp.Attribute(ipp.INTEGER, "x-dimension", [width]),
        "y-dimension": ipp.Attribute(ipp.INTEGER, "y-dimension", [height]),
    }
    member = ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size", [size])
    return ipp.Attribute(ipp.BEGIN_COLLECTION, "media-col", [{"media-size": member}])


# --- documents ---------------------------------------------------------------------


def document(image: Image.Image, attrs: dict) -> tuple[bytes, str]:
    """The page in a format the printer takes: PDF, PWG Raster or JPEG."""
    formats = _texts(attrs, "document-format-supported")
    buf = io.BytesIO()
    if not formats or "application/pdf" in formats:
        # Pillow embeds RGB as JPEG: at its best quality, without chroma
        # subsampling, so that small text stays sharp
        image.convert("RGB").save(buf, format="PDF", resolution=DPI, quality=95, subsampling=0)
        return buf.getvalue(), "application/pdf"
    if "image/pwg-raster" in formats:
        return _pwg(image, attrs), "image/pwg-raster"
    if "image/jpeg" in formats:
        image.convert("RGB").save(buf, format="JPEG", quality=95, subsampling=0, dpi=(DPI, DPI))
        return buf.getvalue(), "image/jpeg"
    raise NoFormat("document-format-supported: " + ", ".join(formats))


def _pwg(image: Image.Image, attrs: dict) -> bytes:
    """PWG Raster in a colour space and resolution the printer lists."""
    types = _texts(attrs, "pwg-raster-document-type-supported")
    if not types or "srgb_8" in types:
        mode = "RGB"
    elif "sgray_8" in types:
        mode = "L"
    else:
        raise NoFormat("pwg-raster-document-type-supported: " + ", ".join(types))
    dpi = _pwg_resolution(attrs.get("pwg-raster-document-resolution-supported") or [])
    if dpi != DPI:
        size = (round(image.width * dpi / DPI), round(image.height * dpi / DPI))
        image = image.resize(size, Image.Resampling.LANCZOS)
    return pwg.encode(image.convert(mode), dpi)


def _pwg_resolution(resolutions: list[Any]) -> int:
    """300 dpi if the printer takes it, else the lowest it has above (or
    its highest): never less sharp than the render."""
    dpis = sorted(
        r[0]
        for r in resolutions
        if isinstance(r, tuple) and len(r) == 3 and r[0] == r[1] and r[2] == 3
    )
    if not dpis or int(DPI) in dpis:
        return int(DPI)
    return next((d for d in dpis if d > DPI), dpis[-1])


# --- the backend -----------------------------------------------------------------


class SystemPrinter(Backend):
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
        attrs = _printer_attributes(uri)
        state = _int(attrs, "printer-state", IDLE)
        error = reasons_error(_texts(attrs, "printer-state-reasons"), _is_local_queue(uri))
        if error is None and state == STOPPED:
            stopped = QueuePaused if _is_local_queue(uri) else PrinterPaused
            error = stopped("printer-state: stopped")
        return PrinterInfo(
            model=_text(attrs, "printer-make-and-model") or _text(attrs, "printer-info"),
            tape_mm=None,
            remaining_mm=None,
            total_mm=None,
            state="ERROR" if error else ("BUSY" if state == PROCESSING else "IDLE"),
            error=error.code if error else "",
            firmware=_text(attrs, "printer-firmware-string-version"),
        )

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """One page: x across the paper, y down the page. Its size in mm
        travels with it (info), exact for paper sizes like A4."""
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info["size_mm"] = label.media_size()
        return image

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        uri = printer_uri(host)
        copies = max(1, options.copies)
        attrs = _printer_attributes(uri)
        problem = reasons_error(_texts(attrs, "printer-state-reasons"), _is_local_queue(uri))
        if problem:
            raise problem
        image, width, height = _page(image, attrs.get("media-size-supported") or [])
        data, mime = document(image, attrs)
        job = [ipp.Attribute(ipp.INTEGER, "copies", [copies]), _media(width, height)]
        if "none" in _texts(attrs, "print-scaling-supported"):
            job.append(ipp.Attribute(ipp.KEYWORD, "print-scaling", ["none"]))
        report_progress(progress, 1, copies)
        answer = _request(
            uri,
            ipp.PRINT_JOB,
            [
                _user(),
                ipp.Attribute(ipp.NAME, "job-name", ["Etiketto"]),
                ipp.Attribute(ipp.MIME_TYPE, "document-format", [mime]),
            ],
            job_attributes=job,
            data=data,
        )
        job_id = _int(answer.group(ipp.JOB_GROUP), "job-id")
        if job_id is not None:
            _wait_done(uri, job_id, progress, copies)
        return None


def _page(image: Image.Image, sizes: list[Any]) -> tuple[Image.Image, int, int]:
    """The page and its size in hundredths of mm, turned to portrait if the
    printer only takes it that way; UnsupportedSize if not at all."""
    width_mm, height_mm = image.info.get("size_mm") or (
        image.width / DPI * 25.4,
        image.height / DPI * 25.4,
    )
    width, height = _hundredths(width_mm), _hundredths(height_mm)
    if fits(sizes, width, height):
        return image, width, height
    if fits(sizes, height, width):
        return image.rotate(90, expand=True), height, width
    raise UnsupportedSize("%g × %g mm not in media-size-supported" % (width_mm, height_mm))


def _printer_attributes(uri: str) -> dict:
    keywords = ipp.Attribute(ipp.KEYWORD, "requested-attributes", PRINTER_ATTRIBUTES)
    return _request(uri, ipp.GET_PRINTER_ATTRIBUTES, [keywords]).group(ipp.PRINTER_GROUP)


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
    keywords = ["job-state", "job-state-reasons", "job-impressions-completed"]
    return _request(
        uri,
        ipp.GET_JOB_ATTRIBUTES,
        [
            ipp.Attribute(ipp.INTEGER, "job-id", [job_id]),
            _user(),
            ipp.Attribute(ipp.KEYWORD, "requested-attributes", keywords),
        ],
    ).group(ipp.JOB_GROUP)


class _Watch:
    """Follows one job. Judged by the job's own state: the printer's
    reasons only matter once the job stopped. A job that stops, or prints
    for ever, is canceled, so that it does not print later on its own; a
    job waiting behind others is left to print when its turn comes."""

    MISSED = object()  # no answer this time

    def __init__(self, uri: str, job_id: int):
        self.uri, self.job_id = uri, job_id
        self.asked = _clock()
        self.started: float | None = None  # when the printer began with it
        self.failures = 0

    def attributes(self) -> Any:
        """The job's attributes; None if the printer forgot the job (some
        keep no history: it is done), MISSED if it did not answer."""
        try:
            attrs = _job_attributes(self.uri, self.job_id)
        except PrinterNotFound:
            return None
        except PrinterError:
            self.failures += 1
            if self.failures > POLL_FAILURES:
                raise
            return self.MISSED
        self.failures = 0
        return attrs

    def finished(self, state: int) -> bool:
        """True once there is nothing more to wait for; raises on problems."""
        if state == JOB_COMPLETED:
            return True
        if state in (JOB_CANCELED, JOB_ABORTED):
            word = "canceled" if state == JOB_CANCELED else "aborted"
            raise PrinterError("job %s %s" % (self.job_id, word))
        if state == JOB_STOPPED:
            reasons = _texts(_printer_attributes(self.uri), "printer-state-reasons")
            _cancel(self.uri, self.job_id)
            raise reasons_error(reasons, _is_local_queue(self.uri)) or PrinterError(
                "job %s stopped" % self.job_id
            )
        if state != JOB_PROCESSING:  # pending or held: behind other jobs
            return _clock() - self.asked > PENDING_LIMIT
        if self.started is None:
            self.started = _clock()
        if _clock() - self.started > PRINT_TIMEOUT:
            _cancel(self.uri, self.job_id)
            raise PrinterTimeout("job %s still printing after %g s" % (self.job_id, PRINT_TIMEOUT))
        return False


def _wait_done(uri: str, job_id: int, progress: ProgressCallback | None, copies: int) -> None:
    """Until the job is done, see _Watch."""
    watch = _Watch(uri, job_id)
    while True:
        attrs = watch.attributes()
        if attrs is None:
            return
        if attrs is not watch.MISSED:
            state = _int(attrs, "job-state", JOB_COMPLETED) or JOB_COMPLETED
            if watch.finished(state):
                if state == JOB_COMPLETED:
                    report_progress(progress, copies, copies)
                return
            done = _int(attrs, "job-impressions-completed", 0) or 0
            report_progress(progress, min(copies, done + 1), copies)
        _sleep(POLL_INTERVAL)


def _cancel(uri: str, job_id: int) -> None:
    # gone already, or the printer refuses: nothing more to do
    with suppress(PrinterError):
        _request(uri, ipp.CANCEL_JOB, [ipp.Attribute(ipp.INTEGER, "job-id", [job_id]), _user()])
