"""Label document model and its JSON (.etiketto) format."""

from __future__ import annotations

import base64
import contextlib
import copy
import errno
import fcntl
import itertools
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import MISSING, asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Version 1: every label. Version 2: the file lists "features" it needs; an
# Etiketto that does not know one of them refuses the file ("newer") instead
# of printing it wrongly. A file is written with version 2 only if it uses a
# feature, so labels without one still open in every version. New features
# add a name to FEATURES, not a version.
FORMAT_VERSION = 2
# "barcodes": a code of a symbology added after version 1 (BARCODES); version
# 1 readers would call the file damaged; "symbols": a symbol element
# (SymbolElement; earlier readers also refuse the unknown element kind)
# "variables": {=date}, {=counter} … in texts and codes, and the counter
# fields; older versions would print "{=date}" as it is and drop the counter
# when they save
# "serial": a linked data file whose rows fill {Column} placeholders; older
# versions would print "{Name}" as it is
# "styles": a design option added later (STYLE_FIELDS: text decoration and
# boxes, image adjustments, the border's shape, an element's own alignment
# or spacing) set to anything but its default; older versions would ignore
# the field and print the label differently
# "shapes": a line or shape element (ShapeElement; earlier readers also
# refuse the unknown element kind)
FEATURES = frozenset({"barcodes", "symbols", "serial", "variables", "styles", "shapes"})

DEFAULT_PRINTER = "vc500w"
MIN_TAPE_LENGTH_MM = 15.0  # endless tape: the shortest fixed length a file may have
# die-cut labels: smallest and largest side a file may have
MIN_SIZE_MM, MAX_SIZE_MM = 5.0, 1000.0
MAX_GAP_MM = 50.0
# {=counter}: up to 15 digits stay exact in the interface's number fields
MAX_COUNTER = 10**15 - 1
MAX_COUNTER_STEP = 10**6
# a label file is read whole before it is parsed: refuse absurd sizes (the
# json documentation advises a limit for data from untrusted sources)
MAX_FILE_BYTES = 200 * 1024 * 1024
# what a printer model decides about a label: kept per model when switching
GEOMETRY_FIELDS = ("tape_mm", "auto_length", "length_mm", "orientation", "gap_mm", "sheet")
MAX_SHEET_POSITIONS = 10000  # far more than any sheet has


class LabelError(ValueError):
    """A file that cannot be used as a label. code: "not_label" (not an
    Etiketto file at all), "newer" (written by a newer Etiketto) or "damaged"
    (an Etiketto file with invalid content); the message is English detail
    for bug reports, the interface shows its own translated text."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _damaged(detail: str) -> LabelError:
    return LabelError("damaged", detail)


ROTATIONS = (0, 90, 180, 270)
VALIGNS = ("top", "center", "bottom")
MAX_GAP_AFTER_MM = 50.0


@dataclass(kw_only=True)  # after each kind's own fields in __init__
class Placed:
    """Where an element sits in its place along the tape: across the tape
    (valign) and the space after it (gap_mm; None: the label's spacing).
    Every element kind has these fields."""

    valign: Literal["top", "center", "bottom"] = "center"
    gap_mm: float | None = None

    def __post_init__(self) -> None:
        # who it is while the label is edited (not a field: not in the file,
        # not compared); a deep copy (undo) keeps it, a new element gets one
        self.__dict__["_id"] = next(_ELEMENT_IDS)


_ELEMENT_IDS = itertools.count(1)


def element_id(element) -> int:
    """An element's identity while the label is edited, the same in copies
    of the label (undo history); 0 for an element without one."""
    return element.__dict__.get("_id", 0)


def new_id(element) -> None:
    """Give a copied element an identity of its own (duplicating)."""
    element.__dict__["_id"] = next(_ELEMENT_IDS)


@dataclass
class TextElement(Placed):
    text: str = "Text"
    font: str = "Sans"  # fontconfig family
    bold: bool = True
    italic: bool = False
    size_pt: float = 0.0  # 0 = fit to tape height
    color: str = "#000000"
    align: Literal["left", "center", "right"] = "left"
    # for the command line: `etiketto print --set NAME=VALUE` replaces the text
    name: str = ""
    kind: Literal["text"] = "text"
    # --- the feature "styles" (STYLE_FIELDS) ---
    underline: bool = False
    strikethrough: bool = False
    letter_spacing: float = 0.0  # % of the font size, may be negative
    line_spacing: float = 1.0  # times the font's own line height
    # fitting: shrink until it fits the tape height and the room along a
    # fixed length (or max_width_mm), not below min_size_pt
    shrink: bool = False
    min_size_pt: float = 0.0
    max_width_mm: float = 0.0  # wrap lines at this width; 0 = only at line breaks
    rotation: int = 0  # clockwise; 90 and 270: vertical text
    # white on black: the text cut out of a box in its colour
    inverted: bool = False
    box_color: str = ""  # a box behind the text; "" = none
    padding_mm: float = 0.0  # between the text and its box or frame
    corner_mm: float = 0.0  # radius of the box's and frame's corners
    frame_mm: float = 0.0  # a frame in the text colour around it
    outline_mm: float = 0.0  # a line around each letter
    outline_color: str = "#ffffff"
    shadow_mm: float = 0.0  # offset of a shadow down and to the right
    shadow_color: str = "#808080"


IMAGE_MODES = ("auto", "threshold", "dither", "ordered")


@dataclass
class ImageElement(Placed):
    data: str = ""  # base64 encoded image file, keeps documents self-contained
    name: str = ""  # original file name, for display only
    kind: Literal["image"] = "image"
    # --- the feature "styles" (STYLE_FIELDS) ---
    # black-and-white printers: "auto" (threshold for drawings, dithering
    # for photos), "threshold", "dither" (Floyd-Steinberg) or "ordered" (Bayer)
    mode: Literal["auto", "threshold", "dither", "ordered"] = "auto"
    threshold: int = 128  # 1-254: lighter pixels become white
    brightness: float = 0.0  # -100 … 100
    contrast: float = 0.0  # -100 … 100
    gamma: float = 1.0  # 0.2 … 5
    invert: bool = False
    rotation: int = 0  # clockwise
    mirror: bool = False  # left and right swapped
    flip: bool = False  # upside down
    # the part of the image (before turning) as fractions: x, y, width, height
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    corner_mm: float = 0.0  # rounded corners
    border_mm: float = 0.0  # a border around the image
    border_color: str = "#000000"

    @classmethod
    def from_file(cls, path: str | Path) -> ImageElement:
        path = Path(path)
        return cls(data=base64.b64encode(path.read_bytes()).decode(), name=path.name)

    def raw(self) -> bytes:
        return base64.b64decode(self.data)


# the symbologies of the first versions, and those added since (the feature
# "barcodes"); how each is drawn: etiketto.codes
FIRST_SYMBOLOGIES = ("qr", "code128", "ean13")
BARCODES = (
    "code39",
    "code93",
    "ean8",
    "upca",
    "upce",
    "itf14",
    "gs1_128",
    "datamatrix",
    "gs1_datamatrix",
    "pdf417",
    "aztec",
)
SYMBOLOGIES = FIRST_SYMBOLOGIES + BARCODES


@dataclass
class CodeElement(Placed):
    data: str = "https://"
    symbology: str = "qr"  # one of SYMBOLOGIES
    color: str = "#000000"
    name: str = ""  # see TextElement.name
    kind: Literal["code"] = "code"


MAX_SYMBOL_PATH = 100_000  # etiketto.symbols.path.MAX_PATH_CHARS
MAX_SYMBOL_NAME = 100  # the longest icon name has 41 characters


@dataclass
class SymbolElement(Placed):
    """A symbol of the symbol library (etiketto.symbols), drawn as a vector
    path at the printer's resolution. The file keeps the name, for the
    chooser, and a copy of the path, which is what is drawn: the label looks
    the same with an icon set where the symbol was changed, renamed or
    removed (the same reason images are embedded)."""

    name: str = ""  # "printer-outline"
    path: str = ""  # SVG path data in the 24 × 24 square; empty: look up name
    size_mm: float = 0.0  # 0 = the content height
    rotation: int = 0  # clockwise, a quarter turn at a time
    color: str = "#000000"  # black on black-and-white printers
    kind: Literal["symbol"] = "symbol"


FITS = ("fit", "fill")


@dataclass
class PageElement(Placed):
    """A page of a PDF or an image file, cropped to one area and printed at
    the label's size: a shipping label. The file is kept (of a PDF with
    several pages only the chosen page where it can be copied unchanged,
    see shipping.source.only_page()), so the area is drawn at the printer's
    resolution, never scaled up blurrily. All of the page is kept, also
    outside the area."""

    data: str = ""  # base64 encoded PDF or image file
    name: str = ""  # original file name, for display only
    page: int = 0  # 0-based
    # area of the page (before rotation) as fractions: x, y, width, height
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    rotation: int = 0  # clockwise, in degrees: one of ROTATIONS
    fit: Literal["fit", "fill"] = "fit"  # keep all of the area, or fill the label
    kind: Literal["page"] = "page"

    @classmethod
    def from_file(cls, path: str | Path) -> PageElement:
        """OSError if unreadable, LabelError if absurdly large."""
        path = Path(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise _damaged("file larger than %d MB" % (MAX_FILE_BYTES // 2**20))
        return cls(data=base64.b64encode(path.read_bytes()).decode(), name=path.name)


SHAPES = ("line", "rectangle", "ellipse", "separator")
DIRECTIONS = ("horizontal", "vertical", "up", "down")  # up/down: diagonal, rising or falling
LINE_STYLES = ("solid", "dashed", "dotted")
ARROWS = ("none", "start", "end", "both")
FILLS = ("none", "solid", "hatched", "dotted", "grid")  # the patterns stay 1 bit
MAX_LINE_MM = 10.0


@dataclass
class ShapeElement(Placed):
    """A line, a rectangle (rounded with corner_mm), an ellipse or a
    separator (a line across the whole content height) in the row of
    elements. Lines are at least one printer dot wide."""

    shape: Literal["line", "rectangle", "ellipse", "separator"] = "line"
    direction: Literal["horizontal", "vertical", "up", "down"] = "horizontal"  # lines
    width_mm: float = 10.0  # along the tape
    height_mm: float = 0.0  # across; 0 = the content height
    line_mm: float = 0.5  # 0: no outline (a filled rectangle or ellipse)
    line_style: Literal["solid", "dashed", "dotted"] = "solid"
    arrows: Literal["none", "start", "end", "both"] = "none"  # lines
    corner_mm: float = 0.0  # rectangles
    fill: Literal["none", "solid", "hatched", "dotted", "grid"] = "none"
    color: str = "#000000"
    kind: Literal["shape"] = "shape"


Element = TextElement | ImageElement | CodeElement | SymbolElement | PageElement | ShapeElement
# an Etiketto that does not know a kind refuses the file as "newer"
ELEMENT_TYPES = {
    "text": TextElement,
    "image": ImageElement,
    "code": CodeElement,
    "symbol": SymbolElement,
    "page": PageElement,
    "shape": ShapeElement,
}
BORDER_STYLES = ("solid", "dashed", "dotted", "double")
_PLACED = ("valign", "gap_mm")
# the fields of the feature "styles": written only when they differ from
# their default (the default is how earlier versions print), so a label
# without them opens in earlier versions; a ShapeElement is new as a whole
STYLE_FIELDS: dict[type, tuple[str, ...]] = {
    TextElement: _PLACED
    + (
        "underline",
        "strikethrough",
        "letter_spacing",
        "line_spacing",
        "shrink",
        "min_size_pt",
        "max_width_mm",
        "rotation",
        "inverted",
        "box_color",
        "padding_mm",
        "corner_mm",
        "frame_mm",
        "outline_mm",
        "outline_color",
        "shadow_mm",
        "shadow_color",
    ),
    ImageElement: _PLACED
    + (
        "mode",
        "threshold",
        "brightness",
        "contrast",
        "gamma",
        "invert",
        "rotation",
        "mirror",
        "flip",
        "crop",
        "corner_mm",
        "border_mm",
        "border_color",
    ),
    CodeElement: _PLACED,
    SymbolElement: _PLACED,
    PageElement: _PLACED,
    ShapeElement: _PLACED,
}
LABEL_STYLE_FIELDS = ("border_radius_mm", "border_style", "border_inset_mm")


def default_of(cls: type, name: str):
    """The default value of a dataclass field."""
    spec = cls.__dataclass_fields__[name]  # type: ignore[attr-defined]
    if spec.default_factory is not MISSING:
        return spec.default_factory()
    return spec.default


def _boxed(text) -> bool:
    return bool(text.inverted or text.box_color or text.frame_mm > 0)


# fields that change nothing unless another one is set (by class name): an
# outline colour without an outline is still written, but is no feature
EFFECTIVE: dict[str, dict[str, Callable[[Any], bool]]] = {
    "TextElement": {
        "min_size_pt": lambda e: e.shrink,
        "padding_mm": _boxed,
        "corner_mm": _boxed,
        "outline_color": lambda e: e.outline_mm > 0,
        "shadow_color": lambda e: e.shadow_mm > 0,
    },
    "ImageElement": {
        "threshold": lambda e: e.mode == "threshold",
        "border_color": lambda e: e.border_mm > 0,
    },
    "Label": {
        "border_radius_mm": lambda label: label.border_mm > 0,
        "border_style": lambda label: label.border_mm > 0,
        "border_inset_mm": lambda label: label.border_mm > 0,
    },
}


def _matters(item, name: str) -> bool:
    """Whether the field differs from its default and does something."""
    if getattr(item, name) == default_of(type(item), name):
        return False
    condition = EFFECTIVE.get(type(item).__name__, {}).get(name)
    return condition is None or condition(item)


def styled(item, names: tuple[str, ...]) -> bool:
    """Whether any of the fields differs from its default (and has an effect)."""
    return any(_matters(item, name) for name in names)


def _compact(item, data: dict, names: tuple[str, ...]) -> dict:
    """data without the fields that have their default value. A field
    without effect (an outline colour without an outline) is still written,
    so it comes back when the file is opened, but it is no feature: earlier
    versions ignore fields they do not know."""
    for name in names:
        if getattr(item, name) == default_of(type(item), name):
            data.pop(name, None)
    return data


@dataclass
class SheetChoice:
    """Page printers: the label printed on a label sheet (see etiketto.sheets)."""

    id: str = ""  # Sheet.id: "Avery L7160"
    # positions to fill on the first sheet (0-based, row by row); the
    # sheets after it are filled completely
    positions: list[int] = field(default_factory=list)
    # the content turned by a quarter: designed on the label lying on its side
    rotated: bool = False

    @classmethod
    def from_dict(cls, data) -> SheetChoice | None:
        """None: no sheet (also files without one)."""
        if data is None:
            return None
        if not isinstance(data, dict):
            raise _damaged("sheet must be an object")
        choice = cls(**_known_fields(cls, data))
        choice.id = _str(choice.id, "sheet id")
        positions = choice.positions
        if not isinstance(positions, list) or not all(
            isinstance(p, int) and not isinstance(p, bool) for p in positions
        ):
            raise _damaged("sheet positions must be a list of numbers")
        choice.positions = sorted({p for p in positions if 0 <= p < MAX_SHEET_POSITIONS})
        if not isinstance(choice.rotated, bool):  # bool("false") would be True
            raise _damaged("sheet rotated must be true or false")
        return choice


@dataclass
class Label:
    # Label space: the length runs along x, the elements are placed one after
    # another along it; tape_mm is the height across. On endless tape tape_mm
    # is the tape width; on die-cut labels both together are the label size
    # (see media_size()).
    tape_mm: float = 50
    auto_length: bool = True  # endless tape only
    length_mm: float = 60.0  # used when auto_length is False
    margin_mm: float = 2.0  # space at both ends and between elements
    edge_mm: float = 0.0  # extra space along the tape edges (on top of the safety margin)
    align: Literal["start", "center", "end"] = "center"  # fixed length only
    background: str = "#ffffff"
    background_image: ImageElement | None = None  # covers the whole label
    border_mm: float = 0.0
    border_color: str = "#000000"
    # --- the feature "styles" (STYLE_FIELDS) ---
    border_radius_mm: float = 0.0
    border_style: Literal["solid", "dashed", "dotted", "double"] = "solid"
    border_inset_mm: float = 0.0  # further in than the safety margin
    elements: list[Element] = field(default_factory=list)
    printer: str = DEFAULT_PRINTER  # backend id; files without it are for the VC-500W
    # die-cut labels only: the gap to the next label, and whether the content
    # runs across the roll (along the print head) or along it like on tape
    gap_mm: float = 3.0
    orientation: Literal["across", "along"] = "along"
    # page printers only: printed on a label sheet instead of one page of
    # the label's size; None: a page of its own
    sheet: SheetChoice | None = None
    # {=counter} (see variables.py): the number of the next printed label and
    # how much it goes up per label; printing moves counter_next on
    counter_next: int = 1
    counter_step: int = 1
    # serial labels: the data file whose rows fill the {column} placeholders,
    # never its content. In memory an absolute path; in the file relative to
    # the label file if it lies beside it or below (see save() and load()).
    data_file: str = ""
    data_sheet: str = ""  # "" = the first sheet

    # --- die-cut labels ----------------------------------------------------

    def media_size(self) -> tuple[float, float]:
        """(width across the print head, height along the feed) in mm."""
        if self.orientation == "across":
            return self.length_mm, self.tape_mm
        return self.tape_mm, self.length_mm

    def set_media_size(self, width_mm: float, height_mm: float) -> None:
        if self.orientation == "across":
            self.length_mm, self.tape_mm = width_mm, height_mm
        else:
            self.tape_mm, self.length_mm = width_mm, height_mm

    def set_orientation(self, orientation: str) -> None:
        """Turn the content, the label size stays."""
        width, height = self.media_size()
        self.orientation = "across" if orientation == "across" else "along"
        self.set_media_size(width, height)

    def pitch_mm(self) -> float:
        """Distance from the start of one die-cut label to the next."""
        return self.media_size()[1] + self.gap_mm

    def use_default_size(self, caps) -> None:
        """A die-cut label of the printer's default size, content across the roll."""
        # a plugin without sizes of its own: 50 × 25 mm, entered by hand
        size = caps.label_sizes[0] if caps.label_sizes else None
        self.auto_length = False
        self.orientation = "across"
        self.set_media_size(*((size.width_mm, size.height_mm) if size else (50.0, 25.0)))

    # --- printer models ------------------------------------------------------

    @classmethod
    def new_for(cls, printer: str, inserted_tape: float | None = None) -> Label:
        """An empty label for a printer model: on tape the inserted (or widest
        common) tape, die-cut labels in the default size."""
        caps = _capabilities(printer)
        label = cls(printer=printer)
        if caps.continuous:
            widths = caps.tape_widths_mm
            fallback = caps.default_tape_mm or (50 if 50 in widths else widths[-1])
            label.tape_mm = (
                inserted_tape if inserted_tape in widths and inserted_tape else fallback
            )
        else:
            label.use_default_size(caps)
        return label

    def geometry(self) -> dict:
        return {name: copy.deepcopy(getattr(self, name)) for name in GEOMETRY_FIELDS}

    def sheet_size(self) -> tuple[float, float] | None:
        """The size the label sheet gives the label (turned if rotated), or
        None without a sheet this version knows."""
        from etiketto.sheets import find

        found = find(self.sheet.id) if self.sheet else None
        if not found or not self.sheet:
            return None
        if self.sheet.rotated:
            return found.height, found.width
        return found.width, found.height

    def use_sheet_size(self) -> None:
        """The label takes the size of its sheet's labels."""
        size = self.sheet_size()
        if size:
            self.set_media_size(*size)

    def switch_printer(self, printer: str, geometry: dict | None = None) -> None:
        """Move the label to another printer model. `geometry` is what it had
        on that model before (see geometry()); without it the label is made
        to fit: the nearest tape, or the default label size."""
        was_continuous = _capabilities(self.printer).continuous
        caps = _capabilities(printer)
        self.printer = printer
        if geometry:
            for name, value in geometry.items():
                setattr(self, name, copy.deepcopy(value))
        elif caps.continuous:
            if not was_continuous:  # the content runs along the tape again
                self.orientation = "along"
                self.auto_length = True
            self.tape_mm = caps.nearest_tape(self.tape_mm)  # 50, not 50.0
        elif was_continuous:
            self.use_default_size(caps)
        if not caps.pages:
            self.sheet = None  # sheets are pages
        self._page_upright(caps)

    def _page_upright(self, caps) -> None:
        """A page has no roll the content could run along, and the interface
        offers no direction: the content runs across, the size stays."""
        if caps.pages and self.orientation != "across":
            self.set_orientation("across")

    # --- persistence -------------------------------------------------------

    def features(self) -> list[str]:
        """What the label uses that older versions cannot print as meant
        (FEATURES)."""
        kinds = {type(e) for e in self.elements}
        uses = {
            "barcodes": any(
                isinstance(e, CodeElement) and e.symbology in BARCODES for e in self.elements
            ),
            "symbols": SymbolElement in kinds,
            "serial": bool(self.data_file),
            "variables": self._uses_variables(),
            "styles": self._styled(),
            "shapes": ShapeElement in kinds,
        }
        return [name for name, used in uses.items() if used]

    def _styled(self) -> bool:
        return styled(self, LABEL_STYLE_FIELDS) or any(
            styled(e, STYLE_FIELDS[type(e)]) for e in self.elements
        )

    def _uses_variables(self) -> bool:
        if (self.counter_next, self.counter_step) != (1, 1):
            return True
        return any(
            "{=" in (e.data if isinstance(e, CodeElement) else e.text)
            for e in self.elements
            if isinstance(e, (TextElement, CodeElement))
        )

    def to_dict(self) -> dict:
        data = _compact(self, asdict(self), LABEL_STYLE_FIELDS)
        data["elements"] = [
            _compact(e, item, STYLE_FIELDS[type(e)])
            for e, item in zip(self.elements, data["elements"], strict=True)
        ]
        if self.background_image:  # its image options are not used
            image = self.background_image
            data["background_image"] = _compact(image, asdict(image), STYLE_FIELDS[ImageElement])
        features = self.features()
        data["version"] = FORMAT_VERSION if features else 1
        if features:
            data["features"] = features
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Label:
        """Build a label from untrusted JSON: unknown fields are ignored,
        wrong types raise LabelError, numbers are clamped to sane ranges."""
        if not isinstance(data, dict):
            raise LabelError("not_label", "not an Etiketto file")
        data = dict(data)
        version = data.pop("version", FORMAT_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise _damaged("invalid format version %r" % (version,))
        if version > FORMAT_VERSION:
            raise LabelError("newer", "file was written by a newer Etiketto (format %s)" % version)
        features = data.pop("features", [])
        if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
            raise _damaged("features must be a list of names")
        if unknown := sorted(set(features) - FEATURES):
            raise LabelError("newer", "file needs features of a newer Etiketto: %s" % unknown)
        items = data.pop("elements", [])
        if not isinstance(items, list):
            raise _damaged("elements must be a list")
        elements = [_element_from_dict(item) for item in items]
        background_image = data.pop("background_image", None)
        sheet = data.pop("sheet", None)
        label = cls(**_known_fields(cls, data))
        label.elements = elements
        label.sheet = SheetChoice.from_dict(sheet)
        if background_image:
            if not isinstance(background_image, dict):
                raise _damaged("background_image must be an object")
            image = _element_from_dict(dict(background_image, kind="image"))
            assert isinstance(image, ImageElement)
            label.background_image = image
        label._validate()
        return label

    def _validate(self) -> None:
        self.printer = _str(self.printer, "printer")
        caps = _capabilities(self.printer)
        continuous = caps.continuous
        if continuous:
            widths = caps.tape_widths_mm
            if isinstance(self.tape_mm, bool) or self.tape_mm not in widths:
                raise _damaged("unsupported tape width %r mm" % (self.tape_mm,))
            self.tape_mm = widths[widths.index(self.tape_mm)]  # 25, not 25.0
        else:
            self.tape_mm = _clamp(self.tape_mm, MIN_SIZE_MM, MAX_SIZE_MM, "tape_mm")
        # a die-cut label has its size, it does not grow with the content
        self.auto_length = bool(self.auto_length) and continuous
        lower = MIN_TAPE_LENGTH_MM if continuous else MIN_SIZE_MM
        self.length_mm = _clamp(self.length_mm, lower, MAX_SIZE_MM, "length_mm")
        self.gap_mm = _clamp(self.gap_mm, 0, MAX_GAP_MM, "gap_mm")
        self.orientation = _choice(self.orientation, ("across", "along"), "orientation")
        if not continuous and caps.max_width_mm:
            # no wider than the print head: also keeps the render of an
            # untrusted file small
            width, height = self.media_size()
            self.set_media_size(min(width, caps.max_width_mm), height)
        self._page_upright(caps)  # a file saved that way by hand or an older version
        if not caps.pages:
            self.sheet = None
        self.use_sheet_size()  # an unknown sheet (newer templates): the size as saved
        self.margin_mm = _clamp(self.margin_mm, 0, 20, "margin_mm")
        self.edge_mm = _clamp(self.edge_mm, 0, 10, "edge_mm")
        self.border_mm = _clamp(self.border_mm, 0, 5, "border_mm")
        self.border_radius_mm = _clamp(self.border_radius_mm, 0, 50, "border_radius_mm")
        self.border_style = _choice(self.border_style, BORDER_STYLES, "border_style")
        self.border_inset_mm = _clamp(self.border_inset_mm, 0, 10, "border_inset_mm")
        self.align = _choice(self.align, ("start", "center", "end"), "align")
        self.background = _str(self.background, "background")
        self.border_color = _str(self.border_color, "border_color")
        self.counter_next = _int(self.counter_next, 0, MAX_COUNTER, "counter_next")
        self.counter_step = _int(self.counter_step, 1, MAX_COUNTER_STEP, "counter_step")
        self.data_file = _str(self.data_file, "data_file")
        if "\x00" in self.data_file:  # no path can contain it
            raise _damaged("invalid data_file")
        self.data_sheet = _str(self.data_sheet, "data_sheet")

    def save(self, path: str | Path) -> None:
        path = Path(path)
        data = self.to_dict()
        data["data_file"] = stored_data_path(self.data_file, path.parent)
        write_file(path, json.dumps(data, indent=1, ensure_ascii=False).encode())

    @staticmethod
    def store_counter(path: str | Path, counter_next: int) -> None:
        """Write a new counter_next into a saved label and change nothing else
        (the command line prints with overrides that must not be saved)."""
        data = json.loads(read_file(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise LabelError("not_label", "not an Etiketto file")
        data["counter_next"] = min(max(counter_next, 0), MAX_COUNTER)
        features = data.get("features")
        features = list(features) if isinstance(features, list) else []
        if "variables" not in features:  # see FEATURES
            data["features"] = features + ["variables"]
        version = data.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < FORMAT_VERSION:
            data["version"] = FORMAT_VERSION
        write_file(path, json.dumps(data, indent=1, ensure_ascii=False).encode())

    @staticmethod
    def saved_counter(path: str | Path) -> int | None:
        """counter_next as saved in the file; None if it cannot be read."""
        try:
            data = json.loads(read_file(path).decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        value = data.get("counter_next", 1) if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return min(max(value, 0), MAX_COUNTER)

    @classmethod
    def load(cls, path: str | Path) -> Label:
        """OSError if the file cannot be read, LabelError for its content."""
        raw = read_file(path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as e:  # no JSON, not UTF-8, absurdly nested
            raise LabelError("not_label", str(e) or type(e).__name__) from e
        label = cls.from_dict(data)
        label.data_file = resolved_data_path(label.data_file, Path(path).parent)
        return label


# --- the link to a data file --------------------------------------------------


def stored_data_path(data_file: str, directory: Path) -> str:
    """How a label file in `directory` refers to the data file: relative if
    the data file lies in that directory or below (both can be moved or
    shared together), else absolute."""
    if not data_file:
        return ""
    target = Path(data_file).absolute()
    if target.is_relative_to(directory.absolute()):
        return target.relative_to(directory.absolute()).as_posix()
    return str(target)


# what a data file is called; a label names one of these or is asked about
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".ods", ".xlsx")


def data_inside(data_file: str, label_file: Path | None) -> bool:
    """May the data file be read without asking when the label is opened?
    Only a table (by its suffix) in the label file's folder or below, also
    after following links, and not in a hidden folder: a label from
    elsewhere could name any file ("../../.ssh/id_rsa", "/etc/passwd", a
    link to them) and show its lines as rows."""
    if not data_file or label_file is None:
        return False
    folder = label_file.absolute().parent.resolve()
    target = Path(data_file).resolve()
    if not target.is_relative_to(folder) or target.suffix.lower() not in TABLE_SUFFIXES:
        return False
    return not any(part.startswith(".") for part in target.relative_to(folder).parts)


def resolved_data_path(data_file: str, directory: Path) -> str:
    """The absolute path of a data file named in a label file in `directory`."""
    if not data_file:
        return ""
    return os.path.normpath(directory.absolute() / data_file)


# --- files ------------------------------------------------------------------


def read_file(path: str | Path) -> bytes:
    """The content of a label file, read only up to MAX_FILE_BYTES. Only a
    regular file: a device like /dev/zero never ends, a FIFO would block
    (opened without blocking to find out)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:  # also a folder: closed here, open() would leave it open
        os.close(fd)
        raise LabelError("not_label", "not a regular file")
    with open(fd, "rb") as f:  # owns fd from here on
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise _damaged("file larger than %d MB" % (MAX_FILE_BYTES // 2**20))
    return raw


def write_file(path: str | Path, data: bytes) -> None:
    """Replace the file at `path` with `data` in one step: a crash leaves the
    old or the new file, never half of one.

    - A symbolic link stays a link: the file it points to is replaced.
    - The file keeps its permissions and, where allowed, its owner and group.
    - The temporary file next to it has a random name and is created
      exclusively (O_EXCL, O_NOFOLLOW): a link planted under a predictable
      name cannot redirect the write, and the window and the command line
      writing the same label at once do not share one temporary file.
    - File and directory are flushed to the disk before and after the rename.

    Flatpak: a file opened through the document portal lives on its FUSE
    file system (/run/user/…/doc/). The portal handles the rename of a file
    within its directory, but only that file is exported, not its folder;
    should a portal version refuse the temporary file, saving fails with an
    error and the label stays unchanged. Not testable outside the sandbox.
    """
    target = Path(os.path.realpath(path))
    try:
        old: os.stat_result | None = os.stat(target)
    except FileNotFoundError:
        old = None
    if old and not os.access(target, os.W_OK):  # replacing would get round it
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    fd, tmp = _create_temporary(target, 0o600 if old else 0o666)  # new: the umask applies
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if old:
                _keep_owner_and_mode(f.fileno(), old)
            with contextlib.suppress(OSError):  # EINVAL/ENOTSUP: no fsync there
                os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _sync_directory(target.parent)


def writable(path: str | Path) -> bool:
    """Whether write_file(path) can work: it refuses a file without write
    permission, and makes the new file in the folder, so both have to be
    writable. Owner, mode and fsync are best effort, not needed."""
    target = os.path.realpath(path)
    if os.path.exists(target) and not os.access(target, os.W_OK):
        return False
    return os.access(os.path.dirname(target), os.W_OK | os.X_OK)


def _create_temporary(target: Path, mode: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(100):
        tmp = str(target.with_name(".%s.%s.tmp" % (target.name, secrets.token_hex(6))))
        try:
            return os.open(tmp, flags, mode), tmp
        except FileExistsError:  # pragma: no cover - 48 random bits
            continue
    raise FileExistsError("no free temporary name next to %s" % target.name)  # pragma: no cover


def _keep_owner_and_mode(fd: int, old: os.stat_result) -> None:
    if (old.st_uid, old.st_gid) != (os.getuid(), os.getgid()):
        # only root may give a file away; a group of one's own works for all
        with contextlib.suppress(OSError):
            os.fchown(fd, old.st_uid, old.st_gid)
        with contextlib.suppress(OSError):
            os.fchown(fd, -1, old.st_gid)
    # best effort: on vfat/NTFS mounts (fixed owner and mode) changing them
    # fails, and the file gets the mount's mode anyway, like a new file
    with contextlib.suppress(OSError):
        os.fchmod(fd, stat.S_IMODE(old.st_mode))


def _sync_directory(directory: Path) -> None:
    """The rename itself on the disk; not every file system can do this."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def locked(
    path: str | Path, waiting: Callable[[], None] | None = None, timeout: float = 60.0
) -> Iterator[None]:
    """Hold an exclusive lock (flock) on the label file: two command line
    prints of the same label run one after the other, so no counter value
    is printed twice. write_file() replaces the file, a waiting process
    would then hold a lock on the old one: it checks that the locked file
    is still the one at `path`, else it locks the new one. Without a lock
    (no such file, a file system without flock) it goes on unlocked.

    waiting() is called once if another process holds the lock; after
    `timeout` seconds LockTimeout is raised. The file is opened for
    writing where allowed (flock over NFS needs that), else for reading.
    Anything but a regular file (a FIFO would block the open) is not
    opened here: the load that follows refuses it."""
    target = os.path.realpath(path)
    deadline = time.monotonic() + timeout
    while True:
        fd = _open_for_lock(target)
        if fd is None:
            yield  # the load that follows says what is wrong
            return
        try:
            _lock(fd, waiting, deadline)
            waiting = None
            if _same_inode(fd, target):
                break
        except LockTimeout:
            os.close(fd)
            raise
        except OSError:  # no flock on this file system
            os.close(fd)
            yield
            return
        os.close(fd)  # replaced meanwhile: lock the new file
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


class LockTimeout(OSError):
    """Another process held the label file longer than the timeout."""


def _open_for_lock(target: str) -> int | None:
    try:
        if not stat.S_ISREG(os.stat(target).st_mode):
            return None
    except OSError:
        return None
    for flags in (os.O_RDWR, os.O_RDONLY):
        try:
            return os.open(target, flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            if isinstance(e, PermissionError) or e.errno == errno.EROFS:
                continue  # not for writing: for reading then
            return None
    return None


def _lock(fd: int, waiting: Callable[[], None] | None, deadline: float) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        if waiting:
            waiting()
    while True:  # poll: flock itself has no timeout
        time.sleep(0.1)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout(errno.EWOULDBLOCK, "label file locked") from None


def _same_inode(fd: int, path: str) -> bool:
    try:
        now = os.stat(path)
    except OSError:
        return False
    mine = os.fstat(fd)
    return (mine.st_dev, mine.st_ino) == (now.st_dev, now.st_ino)


# --- validation helpers -----------------------------------------------------


def _capabilities(printer: str):
    """Capabilities of the printer's backend; a printer this version does not
    know means the file comes from a newer Etiketto."""
    from etiketto.backends import get_backend

    try:
        return get_backend(printer).capabilities
    except KeyError:
        raise LabelError("newer", "unknown printer %r" % printer) from None


def _known_fields(cls, data: dict) -> dict:
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}


def _clamp(value, lower: float, upper: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _damaged("%s must be a number" % name)
    return float(min(upper, max(lower, value)))


def _int(value, lower: int, upper: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _damaged("%s must be a whole number" % name)
    return min(upper, max(lower, value))


def _choice(value, allowed: tuple, name: str):
    if value not in allowed:
        raise _damaged("invalid %s %r" % (name, value))
    return value


def _str(value, name: str) -> str:
    if not isinstance(value, str):
        raise _damaged("%s must be text" % name)
    return value


def _flag(value, name: str) -> bool:
    if not isinstance(value, bool):  # bool("false") would be True
        raise _damaged("%s must be true or false" % name)
    return value


def _rotation(value) -> int:
    if isinstance(value, bool) or value not in ROTATIONS:
        raise _damaged("invalid rotation %r" % (value,))
    return ROTATIONS[ROTATIONS.index(value)]  # 90, not 90.0


def _crop(crop) -> list[float]:
    """An area as fractions x, y, width, height, at least 1 % on each side."""
    if not isinstance(crop, list) or len(crop) != 4:
        raise _damaged("crop must be a list of four numbers")
    x, y, w, h = (_clamp(v, 0, 1, "crop") for v in crop)
    x, y = min(x, 0.99), min(y, 0.99)
    return [x, y, max(0.01, min(w, 1 - x)), max(0.01, min(h, 1 - y))]


def _element_from_dict(item) -> Element:
    if not isinstance(item, dict):
        raise _damaged("element must be an object")
    kind = item.get("kind")
    if not isinstance(kind, str) or not kind:  # also no list or object: unhashable
        raise _damaged("element without a valid kind")
    if kind not in ELEMENT_TYPES:  # a kind of element added later
        raise LabelError("newer", "unknown element type %r" % kind)
    cls = ELEMENT_TYPES[kind]
    element = cls(**_known_fields(cls, item))
    _validate_placed(element)
    _VALIDATORS[cls](element)
    return element


def _validate_placed(element: Placed) -> None:
    element.valign = _choice(element.valign, VALIGNS, "valign")
    if element.gap_mm is not None:
        element.gap_mm = _clamp(element.gap_mm, 0, MAX_GAP_AFTER_MM, "gap_mm")


def _validate_text(element: TextElement) -> None:
    element.text = _str(element.text, "text")
    element.font = _str(element.font, "font")
    element.color = _str(element.color, "color")
    element.bold, element.italic = bool(element.bold), bool(element.italic)
    element.size_pt = _clamp(element.size_pt, 0, 300, "size_pt")
    element.align = _choice(element.align, ("left", "center", "right"), "align")
    element.name = _str(element.name, "name")
    for name in ("underline", "strikethrough", "shrink", "inverted"):
        setattr(element, name, _flag(getattr(element, name), name))
    for name in ("box_color", "outline_color", "shadow_color"):
        setattr(element, name, _str(getattr(element, name), name))
    element.rotation = _rotation(element.rotation)
    for name, lower, upper in (
        ("letter_spacing", -50, 200),
        ("line_spacing", 0.5, 3),
        ("min_size_pt", 0, 300),
        ("max_width_mm", 0, MAX_SIZE_MM),
        ("padding_mm", 0, 20),
        ("corner_mm", 0, 50),
        ("frame_mm", 0, 5),
        ("outline_mm", 0, 5),
        ("shadow_mm", 0, 5),
    ):
        setattr(element, name, _clamp(getattr(element, name), lower, upper, name))


def _validate_image(element: ImageElement) -> None:
    element.data = _str(element.data, "image data")
    element.name = _str(element.name, "image name")
    element.mode = _choice(element.mode, IMAGE_MODES, "mode")
    element.threshold = _int(element.threshold, 1, 254, "threshold")
    for name in ("invert", "mirror", "flip"):
        setattr(element, name, _flag(getattr(element, name), name))
    element.rotation = _rotation(element.rotation)
    element.crop = _crop(element.crop)
    element.border_color = _str(element.border_color, "border_color")
    for name, lower, upper in (
        ("brightness", -100, 100),
        ("contrast", -100, 100),
        ("gamma", 0.2, 5),
        ("corner_mm", 0, 50),
        ("border_mm", 0, 5),
    ):
        setattr(element, name, _clamp(getattr(element, name), lower, upper, name))


def _validate_code(element: CodeElement) -> None:
    element.data = _str(element.data, "code data")
    element.color = _str(element.color, "color")
    element.name = _str(element.name, "name")
    element.symbology = _str(element.symbology, "symbology")
    if element.symbology not in SYMBOLOGIES:  # a symbology added later
        raise LabelError("newer", "unknown symbology %r" % element.symbology)


def _validate_shape(element: ShapeElement) -> None:
    element.shape = _choice(element.shape, SHAPES, "shape")
    element.direction = _choice(element.direction, DIRECTIONS, "direction")
    element.line_style = _choice(element.line_style, LINE_STYLES, "line_style")
    element.arrows = _choice(element.arrows, ARROWS, "arrows")
    element.fill = _choice(element.fill, FILLS, "fill")
    element.color = _str(element.color, "color")
    element.width_mm = _clamp(element.width_mm, 0.1, MAX_SIZE_MM, "width_mm")
    element.height_mm = _clamp(element.height_mm, 0, MAX_SIZE_MM, "height_mm")
    element.line_mm = _clamp(element.line_mm, 0, MAX_LINE_MM, "line_mm")
    element.corner_mm = _clamp(element.corner_mm, 0, 50, "corner_mm")


def _validate_symbol(element: SymbolElement) -> None:
    element.name = _str(element.name, "symbol name")
    if len(element.name) > MAX_SYMBOL_NAME:
        raise _damaged("symbol name too long")
    element.path = _str(element.path, "symbol path")
    if len(element.path) > MAX_SYMBOL_PATH:
        raise _damaged("symbol path too long")
    element.color = _str(element.color, "color")
    element.size_mm = _clamp(element.size_mm, 0, MAX_SIZE_MM, "size_mm")
    element.rotation = _rotation(element.rotation)


def _validate_page(element: PageElement) -> None:
    element.data = _str(element.data, "page data")
    element.name = _str(element.name, "page name")
    if isinstance(element.page, bool) or not isinstance(element.page, int):
        raise _damaged("page must be a whole number")
    element.page = max(0, element.page)
    if isinstance(element.rotation, bool):
        raise _damaged("invalid rotation")
    element.rotation = int(_choice(element.rotation, ROTATIONS, "rotation"))  # 90, not 90.0
    element.fit = _choice(element.fit, FITS, "fit")
    element.crop = _crop(element.crop)


_VALIDATORS: dict[type, Callable[[Any], None]] = {
    TextElement: _validate_text,
    ImageElement: _validate_image,
    CodeElement: _validate_code,
    SymbolElement: _validate_symbol,
    PageElement: _validate_page,
    ShapeElement: _validate_shape,
}
