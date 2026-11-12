"""The interface every printer backend implements.

A backend knows one printer family: what it can do (Capabilities), how to
find it on the network, how to ask for its state and how to print a rendered
label. The user interface only talks to this interface, so supporting a new
printer means adding one module (see CONTRIBUTING.md).
"""

from __future__ import annotations

import math
import socket
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import KW_ONLY, dataclass, field, replace
from datetime import date
from typing import Literal

from PIL import Image

from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, PRINT_DPI, RenderWarning, render

ColorModel = Literal["color", "mono", "two-color"]
# rounding in unit conversions must not make a label "too long" or "too wide"
LIMIT_SLACK_MM = 0.01
# every cut mode a backend may offer; "end": one cut after the last label of
# a job, the copies stay together
CUT_MODES = ("full", "half", "end", "none")
DEFAULT_CUT_MODES = ("full", "half", "none")
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
class Choice:
    """A printer setting chosen from a list of protocol values, e.g. the
    label sensor ("gap", "mark", "continuous"); the first is the default.
    The interface names the key and the values (ui/texts.py)."""

    key: str  # in settings.json (printers.<id>) and PrintOptions.settings
    choices: tuple[str, ...]

    def value(self, raw: object) -> str:
        """A stored value; anything else (missing, junk) is the default."""
        return raw if isinstance(raw, str) and raw in self.choices else self.choices[0]


@dataclass(frozen=True)
class Level:
    """A printer setting in steps, e.g. darkness in per cent. None, the
    default, keeps what is set on the printer."""

    key: str
    lower: int
    upper: int
    step: int = 1

    def value(self, raw: object) -> int | None:
        """A stored value, clamped to the range; junk is None."""
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:  # an int too large for a float, inf, nan
            number = float(raw)
        except OverflowError:
            return None
        if not math.isfinite(number):
            return None
        return int(max(self.lower, min(self.upper, round(number))))

    def steps(self) -> range:
        return range(self.lower, self.upper + 1, self.step)


@dataclass(frozen=True)
class Toggle:
    """A printer setting that is on or off; off by default."""

    key: str

    def value(self, raw: object) -> bool:
        return raw is True


Setting = Choice | Level | Toggle


@dataclass(frozen=True)
class Capabilities:
    tape_widths_mm: tuple[float, ...]  # endless tape; empty for die-cut printers
    dpi: float  # resolution across the tape the preview is rendered at
    modes: tuple[PrintMode, ...]  # fallback when the printer does not report its modes
    color_model: ColorModel = "color"
    cut_modes: tuple[str, ...] = DEFAULT_CUT_MODES  # empty: the printer does not cut
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
    # printer settings the interface offers in the printer group
    settings: tuple[Setting, ...] = ()
    # print qualities ("standard", "high"); empty or one: the interface offers no choice
    qualities: tuple[str, ...] = ()
    # continuous only, what the tape comes as: a "cassette" (VC-500W, P-touch)
    # or a "roll" (Brother QL) the printer recognises, or "paper" whose width
    # is the user's choice, nothing "inserted" (receipt printers); the
    # interface words and checks follow it
    media: Literal["cassette", "roll", "paper"] = "cassette"
    extras: tuple[str, ...] = ()  # of EXTRAS: what the interface offers on top
    # endless tape: the width of a new label when no tape is known; None:
    # 50 mm if the printer takes it, else the widest
    default_tape_mm: float | None = None
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


@dataclass(frozen=True)
class Margins:
    """What a page printer cannot print on at the paper's edges, in mm; for
    one paper size (width, height in mm), or for any if size_mm is None."""

    top: float
    right: float
    bottom: float
    left: float
    size_mm: tuple[float, float] | None = None

    def sides(self) -> tuple[float, float, float, float]:
        return self.top, self.right, self.bottom, self.left


def margins_for(
    margins: Sequence[Margins], paper_mm: tuple[float, float], tolerance_mm: float = 1.0
) -> Margins | None:
    """The margins for this paper: the first entry of its size, else the
    first for any size, else None (unknown)."""
    for entry in margins:
        size = entry.size_mm
        if size and all(abs(a - b) <= tolerance_mm for a, b in zip(size, paper_mm, strict=True)):
            return entry
    return next((entry for entry in margins if entry.size_mm is None), None)


@dataclass
class PrinterInfo:
    model: str
    tape_mm: float | None  # nominal cassette width, None = no cassette
    remaining_mm: int | None
    total_mm: int | None
    state: str  # IDLE, BUSY, PAUSED, ERROR; UNKNOWN: reachable, but no status answer
    _: KW_ONLY  # the optional fields by name only: safe to add or remove some
    error: str = ""  # PrinterError code of a problem the printer reports; "" = none
    modes: tuple[PrintMode, ...] = ()  # reported by the printer; empty = unknown
    firmware: str = ""
    cassette: CassetteInfo | None = None
    pitch_mm: float | None = None  # die-cut: measured distance from label to label
    # page printers: unprintable margins, the paper loaded first; empty = unknown
    margins: tuple[Margins, ...] = ()
    # the resolution the label is printed at, if it is not Capabilities.dpi
    dpi: float | None = None
    qualities: tuple[str, ...] = ()  # print qualities of the connected model; empty = unknown
    # die-cut: the size of the loaded labels (width, height), if the printer knows it
    label_mm: tuple[float, float] | None = None
    # the id of another backend that fits the loaded media (e.g. die-cut
    # labels loaded while the label is for continuous rolls); "" = none
    media_backend: str = ""


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
    # values of Capabilities.settings by key; read them with Backend.setting()
    settings: Mapping[str, object] = field(default_factory=dict)
    # the printer the job is for, so that prepare() can render for what this
    # printer reported (its resolution); "" = not known
    host: str = ""
    quality: str = ""  # one of Capabilities.qualities; empty = the first
    # Capabilities.extras: only backends that offer them look at these
    chain: bool = False
    high_res: bool = False
    feed_mm: float | None = None  # None: the backend's default
    # the printer as last asked, for backends whose models render differently;
    # not part of the request itself
    info: PrinterInfo | None = field(default=None, compare=False)


@dataclass(frozen=True)
class PrintResult:
    """What print_label() did differently from the request, if anything."""

    mode: str = ""  # the mode actually used, when the requested one was not available
    # the job waits in the printer's queue: it prints when its turn comes,
    # nothing went wrong (yet)
    queued: bool = False
    # the job was sent, but the printer did not confirm that it printed it
    # (it may still have): no "Printed"
    unconfirmed: bool = False
    # why the mode differs: "roll" = the loaded roll needs it; "" = the
    # printer does not have the requested mode
    reason: str = ""


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


class ResolutionUnknown(PrinterError):
    """The resolution is to be taken from the printer, which does not tell it."""

    code = "resolution"


class RenderAgain(PrinterError):
    """The printer reported another resolution than the image was made
    for, by a ratio that scaling would spoil: prepare() the label again
    (it now renders for the reported resolution) and print once more."""


class WrongMedia(PrinterError):
    """The loaded roll is of another kind or size than the label (a
    continuous roll for a die-cut label or the other way round)."""

    code = "wrong_media"


class UsbNotFound(PrinterNotFound):
    """No such printer on USB, or it went away (unplugged, switched off,
    asleep): the advice differs from a printer in the network."""

    code = "usb_not_found"


class UsbPermission(PrinterError):
    """The printer's USB device file exists, but this user may not open it."""

    code = "usb_permission"


class PageTooLarge(PrinterError):
    """A page too large to make at the only resolutions the printer takes
    (more pixels than fit in memory)."""

    code = "page_too_large"


class CodeTooLarge(PrinterError):
    """A code does not fit in its space at the printer's resolution, not
    even at one dot per module: it would be cut off."""

    code = "code_too_large"  # params: n, the element


def check_codes(warnings: list[RenderWarning]) -> None:
    """CodeTooLarge for a code of the render that does not fit (warning
    code_too_big): printed, it would be cut off or squeezed."""
    for warning in warnings:
        if warning.key == "code_too_big":
            raise CodeTooLarge("element %s" % warning.params.get("n"), **warning.params)


class LowBattery(PrinterError):
    """A printer that runs on batteries has too little charge to print."""

    code = "battery"


class Overheated(PrinterError):
    code = "overheat"


class UnsupportedModel(PrinterError):
    """A model of the printer family that cannot print what Etiketto sends."""

    code = "unsupported_model"


class UnknownModel(PrinterError):
    """A printer of the family whose model Etiketto does not know."""

    code = "unknown_model"


class EditorLite(PrinterError):
    """A P-touch in "Editor Lite" mode: on USB it is a drive, not a printer."""

    code = "editor_lite"


class SerialPermission(PrinterError):
    """The printer's serial port (/dev/ttyACM0) exists, but this user may
    not open it: the group differs from the printer class driver's."""

    code = "serial_permission"


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
    dithered black and white, so the preview shows the real result (page
    areas are black and white already, see render(mono=True))."""
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

    def setting(self, options: PrintOptions, key: str) -> str | int | bool | None:
        """The value of one of the printer's settings for a job (settings()
        of the printer in options.info): what the options carry if valid,
        else the default. KeyError for other keys."""
        for setting in self.settings(options.info):
            if setting.key == key:
                return setting.value(options.settings.get(key))
        raise KeyError(key)

    def modes(self, info: PrinterInfo | None) -> tuple[PrintMode, ...]:
        """Print modes of the connected printer, else the documented ones."""
        return (info.modes if info else ()) or self.capabilities.modes

    def render_dpi(self, options: PrintOptions) -> float:
        """The resolution a job with these options is rendered at, for the
        preview: capabilities.dpi, unless a setting chooses another one."""
        return self.capabilities.dpi

    @property
    def settings_id(self) -> str:
        """The key of this backend's settings; backends for the same printer
        (one per kind of media) share one."""
        return self.id

    def cut_modes(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """Cut modes of the connected printer, else the documented ones."""
        return self.capabilities.cut_modes

    def qualities(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """Print qualities of the connected printer, else the documented ones."""
        return (info.qualities if info else ()) or self.capabilities.qualities

    def extras(self, info: PrinterInfo | None) -> tuple[str, ...]:
        """Capabilities.extras the connected printer has, else all of them."""
        return self.capabilities.extras

    def feed_mm(self, info: PrinterInfo | None) -> tuple[float, float, float]:
        """The "feed" extra of the connected printer: smallest, largest, default."""
        return self.capabilities.feed_mm

    def settings(self, info: PrinterInfo | None) -> tuple[Setting, ...]:
        """The printer settings of the connected printer (a model may take
        fewer label types or another density range), else the documented
        ones (Capabilities.settings)."""
        return self.capabilities.settings

    def migrate_settings(self, entry: dict) -> None:
        """Bring settings.json's entry of this printer up to date in place
        (keys or values an earlier version stored); nothing by default."""
        del entry  # noqa: B027 - most backends have nothing to migrate

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(self.capabilities.dpi)

    def renders_mono(self, mode: str = "") -> bool:
        """Whether a label for the given print mode is rendered in black and
        white (render(mono=True)): a mono printer, unless a mode prints in
        colour (the Brother QL's red and black)."""
        return self.capabilities.color_model == "mono"

    def preview(self, image: Image.Image, mode: str = "") -> Image.Image:
        """What the printer makes of the image in the given print mode."""
        return apply_color_model(image, self.capabilities.color_model)

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """Render the label for printing (label space: x along the tape)."""
        raster = self.raster(label.tape_mm, options)
        rendered = render(
            label,
            dpi=raster.dpi or PRINT_DPI,
            safe_mm=safe_mm,
            tape_px=raster.tape_px,
            bleed_px=raster.bleed_px,
            min_length_mm=self.capabilities.min_length_mm,
            mono=self.renders_mono(options.mode),
        )
        check_codes(rendered.warnings)  # before anything is sent
        image = rendered.image
        if raster.width_px and image.height > raster.width_px:
            image = image.crop((0, 0, image.width, raster.width_px))
        return self.preview(image, options.mode)

    # the render depends on what query() reports (the model's print head,
    # the loaded tape, the resolution): the command line asks first
    query_before_render: bool = False

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

    def print_pages(
        self,
        pages: Sequence[Image.Image],
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        """Page printers (Capabilities.pages): several pages, such as label
        sheets, each page an image of the paper's size (info["size_mm"]).
        Here one print_label() per page; a backend that can send them in
        one job overrides it."""
        result = None
        for n, page in enumerate(pages, 1):
            report_progress(progress, n, len(pages))
            result = self.print_label(page, host, replace(options, copies=1)) or result
        return result
