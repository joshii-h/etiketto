"""The interface every printer backend implements.

A backend knows one printer family: what it can do (Capabilities), how to
find it on the network, how to ask for its state and how to print a rendered
label. The user interface only talks to this interface, so supporting a new
continuous-tape printer means adding one module (see CONTRIBUTING.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from PIL import Image

from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, PRINT_DPI, render

ColorModel = Literal["color", "mono", "two-color"]
CUT_MODES = ("full", "half", "none")


# --- what a printer can do --------------------------------------------------


@dataclass(frozen=True)
class PrintMode:
    id: str  # protocol name, e.g. "vivid"; the interface translates known ids
    lpi: int  # lines per inch along the tape
    speed: int = 0


@dataclass(frozen=True)
class Capabilities:
    tape_widths_mm: tuple[int, ...]
    dpi: int  # resolution across the tape the preview is rendered at
    modes: tuple[PrintMode, ...]  # fallback when the printer does not report its modes
    color_model: ColorModel = "color"
    cut_modes: tuple[str, ...] = CUT_MODES  # empty: the printer does not cut
    reports_remaining: bool = False  # remaining tape in the status
    supports_color_adjust: bool = False  # brightness / colour balance per job
    # endless tape; the interface offers length and alignment only for these.
    # False (die-cut labels of fixed size) is not supported by the interface yet.
    continuous: bool = True
    data_formats: tuple[str, ...] = ("jpeg",)  # the first one is the default
    min_length_mm: float = 0.0  # shorter labels are extended to this length
    max_length_mm: float | None = None  # longer labels are refused (LabelTooLong)


@dataclass(frozen=True)
class Raster:
    """How to render a label for printing: resolution and extra background.

    tape_px replaces the tape height computed from the nominal width; bleed_px
    extends the background beyond both tape edges; width_px is the exact
    height (across the tape) of the finished image."""

    dpi: int
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
    stage: str = ""
    error: str = ""
    modes: tuple[PrintMode, ...] = ()  # reported by the printer; empty = unknown
    firmware: str = ""
    cassette: CassetteInfo | None = None


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


def check_length(length_mm: float, capabilities: Capabilities) -> None:
    """Raise LabelTooLong if the label exceeds capabilities.max_length_mm."""
    limit = capabilities.max_length_mm
    if limit is not None and length_mm > limit + 0.01:
        raise LabelTooLong("%.1f mm is longer than %.1f mm" % (length_mm, limit), max_mm=limit)


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

    def modes(self, info: PrinterInfo | None) -> tuple[PrintMode, ...]:
        """Print modes of the connected printer, else the documented ones."""
        return (info.modes if info else ()) or self.capabilities.modes

    def raster(self, tape_mm: int, options: PrintOptions) -> Raster:
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
