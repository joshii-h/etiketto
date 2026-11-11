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
    tape_widths_mm: tuple[int, ...]  # endless tape; empty for die-cut printers
    dpi: float  # resolution across the tape the preview is rendered at
    modes: tuple[PrintMode, ...]  # fallback when the printer does not report its modes
    color_model: ColorModel = "color"
    cut_modes: tuple[str, ...] = CUT_MODES  # empty: the printer does not cut
    reports_remaining: bool = False  # remaining tape in the status
    supports_color_adjust: bool = False  # brightness / colour balance per job
    # endless tape: the label grows with its content. False: die-cut labels
    # of a fixed size, chosen from label_sizes or entered by hand.
    continuous: bool = True
    label_sizes: tuple[LabelSize, ...] = ()  # die-cut: common sizes, the first is the default
    max_width_mm: float | None = None  # die-cut: widest printable label (print head width)
    data_formats: tuple[str, ...] = ("jpeg",)  # the first one is the default
    min_length_mm: float = 0.0  # shorter labels are extended to this length
    max_length_mm: float | None = None  # longer labels are refused (LabelTooLong)

    def too_long(self, length_mm: float) -> bool:
        return self.max_length_mm is not None and length_mm > self.max_length_mm + LIMIT_SLACK_MM

    def too_wide(self, width_mm: float) -> bool:
        return self.max_width_mm is not None and width_mm > self.max_width_mm + LIMIT_SLACK_MM

    def nearest_tape(self, width_mm: float) -> int:
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


@dataclass
class PrinterInfo:
    model: str
    tape_mm: int | None  # nominal cassette width, None = no cassette
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


@dataclass(frozen=True)
class PrintResult:
    """What print_label() did differently from the request, if anything."""

    mode: str = ""  # the mode actually used, when the requested one was not available


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
