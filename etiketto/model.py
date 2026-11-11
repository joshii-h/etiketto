"""Label document model and its JSON (.etiketto) format."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Union

FORMAT_VERSION = 1

# Cassette widths of the VC-500W (CZ-1001 ... CZ-1005)
TAPE_WIDTHS_MM = (9, 12, 19, 25, 50)
DEFAULT_PRINTER = "vc500w"
# die-cut labels: smallest and largest side a file may have
MIN_SIZE_MM, MAX_SIZE_MM = 5.0, 1000.0
MAX_GAP_MM = 50.0
# what a printer model decides about a label: kept per model when switching
GEOMETRY_FIELDS = ("tape_mm", "auto_length", "length_mm", "orientation", "gap_mm")


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


@dataclass
class TextElement:
    text: str = "Text"
    font: str = "Sans"  # fontconfig family
    bold: bool = True
    italic: bool = False
    size_pt: float = 0.0  # 0 = fit to tape height
    color: str = "#000000"
    align: Literal["left", "center", "right"] = "left"
    kind: Literal["text"] = "text"


@dataclass
class ImageElement:
    data: str = ""  # base64 encoded image file, keeps documents self-contained
    name: str = ""  # original file name, for display only
    kind: Literal["image"] = "image"

    @classmethod
    def from_file(cls, path: str | Path) -> "ImageElement":
        path = Path(path)
        return cls(data=base64.b64encode(path.read_bytes()).decode(), name=path.name)

    def raw(self) -> bytes:
        return base64.b64decode(self.data)


@dataclass
class CodeElement:
    data: str = "https://"
    symbology: Literal["qr", "code128", "ean13"] = "qr"
    color: str = "#000000"
    kind: Literal["code"] = "code"


Element = Union[TextElement, ImageElement, CodeElement]
ELEMENT_TYPES = {"text": TextElement, "image": ImageElement, "code": CodeElement}


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
    elements: list[Element] = field(default_factory=list)
    printer: str = DEFAULT_PRINTER  # backend id; files without it are for the VC-500W
    # die-cut labels only: the gap to the next label, and whether the content
    # runs across the roll (along the print head) or along it like on tape
    gap_mm: float = 3.0
    orientation: Literal["across", "along"] = "along"

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
        if orientation != self.orientation:
            width, height = self.media_size()
            self.orientation = "across" if orientation == "across" else "along"
            self.set_media_size(width, height)

    def pitch_mm(self) -> float:
        """Distance from the start of one die-cut label to the next."""
        return self.media_size()[1] + self.gap_mm

    def use_default_size(self, caps) -> None:
        """A die-cut label of the printer's default size, content across the roll."""
        size = caps.label_sizes[0] if caps.label_sizes else None
        self.auto_length = False
        self.orientation = "across"
        self.set_media_size(*((size.width_mm, size.height_mm) if size else (50.0, 25.0)))

    # --- printer models ------------------------------------------------------

    @classmethod
    def new_for(cls, printer: str, caps, inserted_tape: float | None = None) -> "Label":
        """An empty label for a printer model: on tape the inserted (or widest
        common) tape, die-cut labels in the default size."""
        label = cls(printer=printer)
        if caps.continuous:
            widths = caps.tape_widths_mm
            fallback = 50 if 50 in widths else widths[-1]
            label.tape_mm = inserted_tape if inserted_tape in widths and inserted_tape else fallback
        else:
            label.use_default_size(caps)
        return label

    def geometry(self) -> dict:
        return {name: getattr(self, name) for name in GEOMETRY_FIELDS}

    def switch_printer(self, printer: str, caps, geometry: dict | None = None) -> None:
        """Move the label to another printer model. `geometry` is what it had
        on that model before (see geometry()); without it the label is made
        to fit: the nearest tape, or the default label size."""
        was_continuous = _capabilities(self.printer).continuous
        self.printer = printer
        if geometry:
            for name, value in geometry.items():
                setattr(self, name, value)
        elif caps.continuous:
            if not was_continuous:  # the content runs along the tape again
                self.orientation = "along"
                self.auto_length = True
            widths = caps.tape_widths_mm  # the tape's own number: 50, not 50.0
            self.tape_mm = min(widths, key=lambda w: abs(w - self.tape_mm))
        elif was_continuous:
            self.use_default_size(caps)

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = FORMAT_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Label":
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
        items = data.pop("elements", [])
        if not isinstance(items, list):
            raise _damaged("elements must be a list")
        elements = [_element_from_dict(item) for item in items]
        background_image = data.pop("background_image", None)
        label = cls(**_known_fields(cls, data, frozenset({"elements", "background_image"})))
        label.elements = elements
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
        lower = 15 if continuous else MIN_SIZE_MM
        self.length_mm = _clamp(self.length_mm, lower, MAX_SIZE_MM, "length_mm")
        self.gap_mm = _clamp(self.gap_mm, 0, MAX_GAP_MM, "gap_mm")
        self.orientation = _choice(self.orientation, ("across", "along"), "orientation")
        if not continuous and caps.max_width_mm:
            # no wider than the print head: also keeps the render of an
            # untrusted file small
            width, height = self.media_size()
            self.set_media_size(min(width, caps.max_width_mm), height)
        self.margin_mm = _clamp(self.margin_mm, 0, 20, "margin_mm")
        self.edge_mm = _clamp(self.edge_mm, 0, 10, "edge_mm")
        self.border_mm = _clamp(self.border_mm, 0, 5, "border_mm")
        self.align = _choice(self.align, ("start", "center", "end"), "align")
        self.background = _str(self.background, "background")
        self.border_color = _str(self.border_color, "border_color")

    def save(self, path: str | Path) -> None:
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> "Label":
        """OSError if the file cannot be read, LabelError for its content."""
        raw = Path(path).read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as e:  # JSON syntax or not UTF-8 at all
            raise LabelError("not_label", str(e)) from e
        return cls.from_dict(data)


# --- validation helpers -----------------------------------------------------


def _capabilities(printer: str):
    """Capabilities of the printer's backend; a printer this version does not
    know means the file comes from a newer Etiketto."""
    from etiketto.backends import get_backend

    try:
        return get_backend(printer).capabilities
    except KeyError:
        raise LabelError("newer", "unknown printer %r" % printer) from None


def _known_fields(cls, data: dict, skip: frozenset[str] = frozenset()) -> dict:
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in skip}


def _clamp(value, lower: float, upper: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _damaged("%s must be a number" % name)
    return float(min(upper, max(lower, value)))


def _choice(value, allowed: tuple, name: str):
    if value not in allowed:
        raise _damaged("invalid %s %r" % (name, value))
    return value


def _str(value, name: str) -> str:
    if not isinstance(value, str):
        raise _damaged("%s must be text" % name)
    return value


def _element_from_dict(item) -> Element:
    if not isinstance(item, dict):
        raise _damaged("element must be an object")
    kind = item.get("kind")
    if kind not in ELEMENT_TYPES:
        if isinstance(kind, str) and kind:  # a kind of element added later
            raise LabelError("newer", "unknown element type %r" % kind)
        raise _damaged("element without a valid kind")
    cls = ELEMENT_TYPES[kind]
    element = cls(**_known_fields(cls, item))
    if isinstance(element, TextElement):
        element.text = _str(element.text, "text")
        element.font = _str(element.font, "font")
        element.color = _str(element.color, "color")
        element.bold, element.italic = bool(element.bold), bool(element.italic)
        element.size_pt = _clamp(element.size_pt, 0, 300, "size_pt")
        element.align = _choice(element.align, ("left", "center", "right"), "align")
    elif isinstance(element, ImageElement):
        element.data = _str(element.data, "image data")
        element.name = _str(element.name, "image name")
    else:
        element.data = _str(element.data, "code data")
        element.color = _str(element.color, "color")
        element.symbology = _choice(element.symbology, ("qr", "code128", "ean13"), "symbology")
    return element
