"""The interface every printer backend implements.

A backend knows one printer family: what it can do (Capabilities), how to
find it on the network, how to ask for its state and how to print a rendered
label. The user interface only talks to this interface, so supporting a new
printer means adding one module (see CONTRIBUTING.md).
"""

from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import KW_ONLY, dataclass, field
from datetime import date
from typing import Literal

from PIL import Image

from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, PRINT_DPI, render

ColorModel = Literal["color", "mono", "two-color"]
# rounding in unit conversions must not make a label "too long" or "too wide"
LIMIT_SLACK_MM = 0.01
CUT_MODES = ("full", "half", "none")
# settings a backend may offer beyond copies, cut and mode (Capabilities.extras):
# "chain": no feed after the last label, so the next job starts without a
# blank piece; "high_res": twice the resolution along the tape; "feed": the
# blank tape before and after each label (PrintOptions.feed_mm)
EXTRAS = ("chain", "high_res", "feed")


# --- what a printer can do --------------------------------------------------


@dataclass(frozen=True)
class PrintMode:
    id: str  # protocol name, e.g. "vivid"; the interface translates known ids
    lpi: int  # lines per inch along the tape
    speed: int = 0


@dataclass(frozen=True)
class LabelSize:
    """A die-cut label as the material is sold: width across the print head,
    height along the feed direction, in mm."""

    width_mm: float
    height_mm: float


@dataclass(frozen=True)
class Capabilities:
    tape_widths_mm: tuple[float, ...]  # endless tape; empty for die-cut printers
    dpi: float  # resolution across the tape the preview is rendered at
    modes: tuple[PrintMode, ...]  # fallback when the printer does not report its modes
    color_model: ColorModel = "color"
    cut_modes: tuple[str, ...] = CUT_MODES  # empty: the printer does not cut
    reports_remaining: bool = False  # remaining tape in the status
    supports_color_adjust: bool = False  # brightness / colour balance per job
    # endless tape: the label grows with its content. False: die-cut labels
    # of a fixed size, chosen from label_sizes or entered by hand.
    continuous: bool = True
    # die-cut only: the labels are pages (a page printer), not on a roll, so
    # there is no gap and no direction of the roll
    pages: bool = False
    label_sizes: tuple[LabelSize, ...] = ()  # die-cut: common sizes, the first is the default
    max_width_mm: float | None = None  # die-cut: widest printable label (print head width)
    data_formats: tuple[str, ...] = ("jpeg",)  # the first one is the default
    min_length_mm: float = 0.0  # shorter labels are extended to this length
    max_length_mm: float | None = None  # longer labels are refused (LabelTooLong)
    extras: tuple[str, ...] = ()  # of EXTRAS: what the interface offers on top
    # "feed": (smallest, largest, default) in mm
    feed_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def too_long(self, length_mm: float) -> bool:
        return self.max_length_mm is not None and length_mm > self.max_length_mm + LIMIT_SLACK_MM

    def too_wide(self, width_mm: float) -> bool:
        return self.max_width_mm is not None and width_mm > self.max_width_mm + LIMIT_SLACK_MM

    def nearest_tape(self, width_mm: float) -> float:
        return min(self.tape_widths_mm, key=lambda w: abs(w - width_mm))


@dataclass(frozen=True)
class Raster:
    """How to render a label for printing: resolution and extra background.

    tape_px replaces the tape height computed from the nominal width; bleed_px
    extends the background beyond both tape edges; width_px is the exact
    height (across the tape) of the finished image."""

    dpi: float
    tape_px: int | None = None
    bleed_px: int = 0
    width_px: int | None = None


# --- printer state ----------------------------------------------------------


@dataclass
class FoundPrinter:
    name: str  # service name, e.g. "Brother VC-500W (Kitchen)"
    host: str  # mDNS host name, e.g. "VC-500W6382.local"
    address: str  # IPv4 address


@dataclass
class CassetteInfo:
    lot: str = ""  # lot number as printed on the box
    serial: str = ""
    made: date | None = None  # production date, if the serial number reveals it
    printable_mm: float | None = None
    media: str = ""  # kind of tape, e.g. "laminated", "heat_shrink_2"; "" = not reported


@dataclass
class PrinterInfo:
    model: str
    tape_mm: float | None  # nominal cassette width, None = no cassette
    remaining_mm: int | None
    total_mm: int | None
    state: str
    _: KW_ONLY  # the optional fields by name only: safe to add or remove some
    error: str = ""  # PrinterError code of a problem the printer reports; "" = none
    modes: tuple[PrintMode, ...] = ()  # reported by the printer; empty = unknown
    firmware: str = ""
    cassette: CassetteInfo | None = None
    pitch_mm: float | None = None  # die-cut: measured distance from label to label


# --- a print job ------------------------------------------------------------


@dataclass(frozen=True)
class ColorAdjust:
    """Brightness and colour balance in steps from -STEPS to +STEPS."""

    brightness: int = 0
    red: int = 0
    green: int = 0
    blue: int = 0

    STEPS = 3
    FIELDS = ("brightness", "red", "green", "blue")

    @classmethod
    def from_dict(cls, data) -> ColorAdjust:
        """From settings.json: junk is ignored, numbers are clamped."""
        if not isinstance(data, dict):
            return cls()
        values = {}
        for name in cls.FIELDS:
            value = data.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = 0
            values[name] = int(max(-cls.STEPS, min(cls.STEPS, round(value))))
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.FIELDS}

    def is_neutral(self) -> bool:
        return not any(self.to_dict().values())


@dataclass(frozen=True)
class PrintOptions:
    copies: int = 1
    cut: str = "full"
    mode: str = ""  # PrintMode.id; empty = the printer's first mode
    color: ColorAdjust = field(default_factory=ColorAdjust)
    data_format: str = "rawrgb"  # backends fall back to their own default
    gap_mm: float = 3.0  # die-cut labels: the gap to the next label (Label.gap_mm)
    # Capabilities.extras: only backends that offer them look at these
    chain: bool = False
    high_res: bool = False
    feed_mm: float | None = None  # None: the backend's default


@dataclass(frozen=True)
class PrintResult:
    """What print_label() did differently from the request, if anything."""

    mode: str = ""  # the mode actually used, when the requested one was not available
    # the job waits in the printer's queue: it prints when its turn comes,
    # nothing went wrong (yet)
    queued: bool = False


@dataclass(frozen=True)
class Progress:
    copy: int  # 1-based
    copies: int
    stage: str = ""  # printer stage, e.g. "PREHEATING"; empty = printing


ProgressCallback = Callable[[Progress], None]


# --- errors -----------------------------------------------------------------


class PrinterError(Exception):
    """A problem with the printer. `code` selects the (translated) message in
    the interface, `details` is the original text for bug reports."""

    code = "generic"

    def __init__(self, details: str = "", **params):
        super().__init__(details)
        self.details = details
        self.params = params


class PrinterNotFound(PrinterError):
    code = "not_found"


class PrinterTimeout(PrinterError):
    code = "timeout"


class PrinterStalled(PrinterTimeout):
    """The printer stopped taking the data of a job (paper out, buffer
    full, a problem it shows on its own)."""

    code = "stalled"


class PortClosed(PrinterError):
    code = "port_closed"


class NoCassette(PrinterError):
    code = "no_cassette"


class CoverOpen(PrinterError):
    code = "cover_open"


class PaperJam(PrinterError):
    code = "jam"


class BadCassette(PrinterError):
    code = "bad_cassette"


class PrinterBusy(PrinterError):
    code = "busy"


class LabelTooLong(PrinterError):
    code = "too_long"  # params: max_mm


class LabelTooWide(PrinterError):
    code = "too_wide"  # params: max_mm


class OutOfLabels(PrinterError):
    code = "out_of_labels"


class HeadOpen(PrinterError):
    code = "head_open"


class LabelNotFound(PrinterError):
    code = "label_not_found"  # the sensor finds no gap: wrong sensor or material


class RibbonError(PrinterError):
    code = "ribbon"


class PrinterPaused(PrinterError):
    code = "paused"


class OutOfPaper(PrinterError):
    code = "out_of_paper"


class MediaJam(PrinterError):
    code = "media_jam"


class SuppliesEmpty(PrinterError):
    code = "supplies"


class UnsupportedSize(PrinterError):
    """The printer takes no paper of the label's size."""

    code = "paper_size"


class NoFormat(PrinterError):
    """The printer takes none of the document formats the backend can make."""

    code = "format"


class QueuePaused(PrinterError):
    """A print queue on this computer is paused (not the printer itself)."""

    code = "queue_paused"


class JobHeld(PrinterError):
    """The queue holds the job (until released, a set time or a password)."""

    code = "job_held"


class JobCanceled(PrinterError):
    """Someone canceled the job at the printer or in the queue."""

    code = "job_canceled"


class UsbNotFound(PrinterNotFound):
    """No such printer on USB, or it went away (unplugged, switched off,
    asleep): the advice differs from a printer in the network."""

    code = "usb_not_found"


class UsbPermission(PrinterError):
    """The printer's USB device file exists, but this user may not open it."""

    code = "usb_permission"


class LowBattery(PrinterError):
    code = "battery"


class Overheated(PrinterError):
    code = "overheat"


class UnsupportedModel(PrinterError):
    """A model of the printer family that Etiketto cannot print on."""

    code = "unsupported_model"


class EditorLite(PrinterError):
    """A P-touch in "Editor Lite" mode: on USB it is a drive, not a printer."""

    code = "editor_lite"


def error_text(error: Exception) -> str:
    return str(error) or type(error).__name__


def network_error(error: Exception) -> PrinterError | None:
    """The PrinterError for a printer error or a plain network problem; None
    for anything else, which each backend maps from its own messages."""
    if isinstance(error, PrinterError):
        return error
    if isinstance(error, socket.gaierror):
        return PrinterNotFound(error_text(error))
    if isinstance(error, ConnectionRefusedError):
        return PortClosed(error_text(error))
    if isinstance(error, TimeoutError):  # socket.timeout too
        return PrinterTimeout(error_text(error))
    return None


@contextmanager
def open_session(connect, wrap, map_error) -> Iterator:
    """Connect, hand out wrap(connection), close it again; every exception on
    the way, also from the caller's block, becomes a PrinterError."""
    try:
        connection = connect()
    except Exception as e:  # noqa: BLE001 - libraries raise all kinds
        raise map_error(e) from e
    try:
        yield wrap(connection)
    except PrinterError:
        raise
    except Exception as e:  # noqa: BLE001
        raise map_error(e) from e
    finally:
        connection.close()


def report_progress(
    progress: ProgressCallback | None, n: int, copies: int, stage: str = ""
) -> None:
    """Tell the caller which copy is printing, if it asked to know."""
    if progress:
        progress(Progress(n, copies, stage))


def check_length(length_mm: float, capabilities: Capabilities) -> None:
    """Raise LabelTooLong if the label exceeds capabilities.max_length_mm."""
    limit = capabilities.max_length_mm
    if limit is not None and capabilities.too_long(length_mm):
        raise LabelTooLong("%.1f mm is longer than %.1f mm" % (length_mm, limit), max_mm=limit)


def check_width(width_mm: float, capabilities: Capabilities) -> None:
    """Raise LabelTooWide if a die-cut label exceeds capabilities.max_width_mm."""
    limit = capabilities.max_width_mm
    if limit is not None and capabilities.too_wide(width_mm):
        raise LabelTooWide("%.1f mm is wider than %.1f mm" % (width_mm, limit), max_mm=limit)


# --- the backend ------------------------------------------------------------


def apply_color_model(image: Image.Image, model: ColorModel) -> Image.Image:
    """What the printer can reproduce: mono printers get Floyd-Steinberg
    dithered black and white, so the preview shows the real result."""
    if model == "mono":
        return image.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
    return image


class Backend(ABC):
    id: str = ""
    name: str = ""
    capabilities: Capabilities

    def configured_host(self) -> str | None:
        """A host configured outside Etiketto (e.g. by a CLI tool), if any."""
        return None

    def display_name(self, info: PrinterInfo | None) -> str:
        """The connected printer's name for the interface; the model family by default."""
        return self.name

    def modes(self, info: PrinterInfo | None) -> tuple[PrintMode, ...]:
        """Print modes of the connected printer, else the documented ones."""
        return (info.modes if info else ()) or self.capabilities.modes

    def cut_modes(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """Cut modes of the connected printer, else the documented ones."""
        return self.capabilities.cut_modes

    def extras(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """Capabilities.extras the connected printer has, else all of them."""
        return self.capabilities.extras

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(self.capabilities.dpi)

    def preview(self, image: Image.Image, mode: str = "") -> Image.Image:
        """What the printer makes of the image in the given print mode."""
        return apply_color_model(image, self.capabilities.color_model)

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """Render the label for printing (label space: x along the tape)."""
        raster = self.raster(label.tape_mm, options)
        image = render(
            label,
            dpi=raster.dpi or PRINT_DPI,
            safe_mm=safe_mm,
            tape_px=raster.tape_px,
            bleed_px=raster.bleed_px,
            min_length_mm=self.capabilities.min_length_mm,
        ).image
        if raster.width_px and image.height > raster.width_px:
            image = image.crop((0, 0, image.width, raster.width_px))
        return self.preview(image, options.mode)

    @abstractmethod
    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """Printers announced on the local network; [] if none or no mDNS."""

    @abstractmethod
    def query(self, host: str) -> PrinterInfo:
        """Current state; raises PrinterError."""

    @abstractmethod
    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        """Print an image made by prepare(); raises PrinterError. Returns a
        PrintResult if something differed from the request (or None)."""
