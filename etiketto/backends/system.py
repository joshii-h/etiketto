"""Printers the system knows, through IPP: CUPS queues on this computer
(also USB printers) and IPP Everywhere printers on the network.

The label is one page: a PDF (or a JPEG, which every IPP Everywhere printer
takes) of exactly the label's size, printed unscaled. The printer's own
setup decides everything else, as with the other backends.
"""

from __future__ import annotations

import io
import time
from contextlib import suppress
from dataclasses import replace
from urllib.parse import quote

from PIL import Image

from etiketto.backends import ipp, mdns
from etiketto.backends.base import (
    Backend,
    Capabilities,
    CoverOpen,
    FoundPrinter,
    LabelSize,
    MediaJam,
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
    SuppliesEmpty,
    error_text,
    network_error,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

CUPS_URI = "ipp://localhost/"
DPI = 300.0
TIMEOUT = 10.0  # one IPP request
PRINT_TIMEOUT = 300.0  # the whole job, from sending to done
POLL_INTERVAL = 1.0

_sleep = time.sleep  # replaced in tests
_clock = time.monotonic

# paper sizes to start from; the printer's own list comes with query()
PAPER_SIZES = (
    LabelSize(210, 297),  # A4
    LabelSize(148, 210),  # A5
    LabelSize(105, 148),  # A6
    LabelSize(215.9, 279.4),  # US Letter
    LabelSize(101.6, 152.4),  # 4 × 6 in
    LabelSize(100, 150),
)

# printer-state
IDLE, PROCESSING, STOPPED = 3, 4, 5
# job-state
COMPLETED_STATES = {7: "canceled", 8: "aborted", 9: "completed"}

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
    "connecting-to-device": PrinterTimeout,
}
PRINTER_ATTRIBUTES = [
    "printer-state",
    "printer-state-reasons",
    "printer-make-and-model",
    "printer-info",
    "printer-firmware-string-version",
    "document-format-supported",
    "print-scaling-supported",
]


# --- addresses ---------------------------------------------------------------------


def printer_uri(host: str) -> str:
    """The IPP address for what the user entered: a printer URI as is, a
    CUPS queue name ("HP_LaserJet") on this computer, or a host name or IP
    address of a network printer (IPP Everywhere path /ipp/print)."""
    host = host.strip()
    if host.startswith(("ipp://", "ipps://")):
        return host
    if not any(c in host for c in ".:/"):
        return CUPS_URI + "printers/" + quote(host)
    return "ipp://%s/ipp/print" % ("[%s]" % host if ":" in host else host)


# --- errors ------------------------------------------------------------------------


def map_error(error: Exception) -> PrinterError:
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    if isinstance(error, OSError) or "resolve" in text.lower():
        return PrinterNotFound(text)
    return PrinterError(text)


def reasons_error(reasons: list[str]) -> PrinterError | None:
    """The first problem in printer-state-reasons; warnings and reports
    ("toner-low-warning") are no problem."""
    for reason in reasons:
        if not reason or reason == "none" or reason.endswith(("-warning", "-report")):
            continue
        keyword = reason.removesuffix("-error")
        return REASONS.get(keyword, PrinterError)("printer-state-reasons: " + reason)
    return None


def _request(uri: str, operation: int, attributes: list[ipp.Attribute], **kwargs) -> ipp.Response:
    body = ipp.encode(
        operation,
        1,
        [ipp.Attribute(ipp.URI, "printer-uri", [uri]), *attributes],
        **kwargs,
    )
    try:
        answer = ipp.send(uri, body, TIMEOUT)
    except Exception as e:  # noqa: BLE001 - network and protocol errors alike
        raise map_error(e) from e
    if not answer.ok:
        message = answer.group(ipp.OPERATION_GROUP).get("status-message", [""])[0]
        if answer.status == 0x0406:  # client-error-not-found
            raise PrinterNotFound("IPP status 0x%04x %s" % (answer.status, message))
        raise PrinterError("IPP status 0x%04x %s" % (answer.status, message))
    return answer


def _first(attrs: dict, name: str, default=None):
    values = attrs.get(name) or [default]
    return values[0]


def _user() -> ipp.Attribute:
    return ipp.Attribute(ipp.NAME, "requesting-user-name", ["etiketto"])


# --- documents ---------------------------------------------------------------------


def document(image: Image.Image, formats: list[str]) -> tuple[bytes, str]:
    """The label as one page in a format the printer takes: PDF, else JPEG
    (IPP Everywhere printers all take JPEG)."""
    buf = io.BytesIO()
    rgb = image.convert("RGB")
    if "application/pdf" in formats or not formats:
        rgb.save(buf, format="PDF", resolution=DPI)
        return buf.getvalue(), "application/pdf"
    rgb.save(buf, format="JPEG", quality=95, dpi=(DPI, DPI))
    return buf.getvalue(), "image/jpeg"


def _media(image: Image.Image) -> ipp.Attribute:
    """media-col with the page size in hundredths of millimetres."""

    def hundredths(px: int) -> int:
        return round(px / DPI * 2540)

    size = {
        "x-dimension": ipp.Attribute(ipp.INTEGER, "x-dimension", [hundredths(image.width)]),
        "y-dimension": ipp.Attribute(ipp.INTEGER, "y-dimension", [hundredths(image.height)]),
    }
    member = ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size", [size])
    return ipp.Attribute(ipp.BEGIN_COLLECTION, "media-col", [{"media-size": member}])


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
        label_sizes=PAPER_SIZES,
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
        attrs = _printer_attributes(printer_uri(host))
        state = _first(attrs, "printer-state", IDLE)
        error = reasons_error(attrs.get("printer-state-reasons", []))
        if error is None and state == STOPPED:
            error = PrinterPaused("printer-state: stopped")
        return PrinterInfo(
            model=_first(attrs, "printer-make-and-model") or _first(attrs, "printer-info", ""),
            tape_mm=None,
            remaining_mm=None,
            total_mm=None,
            state="ERROR" if error else ("BUSY" if state == PROCESSING else "IDLE"),
            error=error.code if error else "",
            firmware=_first(attrs, "printer-firmware-string-version", ""),
        )

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """One page: x across the paper, y down the page."""
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
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
        problem = reasons_error(attrs.get("printer-state-reasons", []))
        if problem:
            raise problem
        data, mime = document(image, attrs.get("document-format-supported", []))
        job = [ipp.Attribute(ipp.INTEGER, "copies", [copies]), _media(image)]
        if "none" in attrs.get("print-scaling-supported", []):
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
        job_id = _first(answer.group(ipp.JOB_GROUP), "job-id")
        if job_id is not None:
            _wait_done(uri, job_id, progress, copies)
        return None


def _printer_attributes(uri: str) -> dict:
    keywords = ipp.Attribute(ipp.KEYWORD, "requested-attributes", PRINTER_ATTRIBUTES)
    return _request(uri, ipp.GET_PRINTER_ATTRIBUTES, [keywords]).group(ipp.PRINTER_GROUP)


def _cups_queues() -> list[FoundPrinter]:
    """The printers CUPS on this computer has queues for; [] without CUPS."""
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
        name = _first(attrs, "printer-name", "")
        if name:
            uri = CUPS_URI + "printers/" + quote(name)
            found.append(FoundPrinter(_first(attrs, "printer-info", "") or name, uri, "CUPS"))
    return found


def _wait_done(uri: str, job_id: int, progress: ProgressCallback | None, copies: int) -> None:
    """Until the job is completed; a canceled or aborted job, or a printer
    that reports a problem, raises (and the job is canceled, so that it
    does not print later on its own)."""
    deadline = _clock() + PRINT_TIMEOUT
    keywords = ["job-state", "job-state-reasons", "job-impressions-completed"]
    while True:
        attrs = _request(
            uri,
            ipp.GET_JOB_ATTRIBUTES,
            [
                ipp.Attribute(ipp.INTEGER, "job-id", [job_id]),
                _user(),
                ipp.Attribute(ipp.KEYWORD, "requested-attributes", keywords),
            ],
        ).group(ipp.JOB_GROUP)
        state = _first(attrs, "job-state", 9)
        if state == 9:
            report_progress(progress, copies, copies)
            return
        if state in COMPLETED_STATES:
            raise PrinterError("job %s: %s" % (job_id, COMPLETED_STATES[state]))
        problem = reasons_error(_printer_attributes(uri).get("printer-state-reasons", []))
        if problem or _clock() > deadline:
            _cancel(uri, job_id)
            raise problem or PrinterTimeout("job %s still %s" % (job_id, state))
        done = _first(attrs, "job-impressions-completed", 0) or 0
        report_progress(progress, min(copies, done + 1), copies)
        _sleep(POLL_INTERVAL)


def _cancel(uri: str, job_id: int) -> None:
    # gone already, or the printer refuses: nothing more to do
    with suppress(PrinterError):
        _request(uri, ipp.CANCEL_JOB, [ipp.Attribute(ipp.INTEGER, "job-id", [job_id]), _user()])
