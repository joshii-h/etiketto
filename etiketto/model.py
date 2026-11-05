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
    tape_mm: int = 50
    auto_length: bool = True
    length_mm: float = 60.0  # used when auto_length is False
    margin_mm: float = 2.0  # space at both ends and between elements
    edge_mm: float = 0.0  # extra space along the tape edges (on top of the safety margin)
    align: Literal["start", "center", "end"] = "center"  # fixed length only
    background: str = "#ffffff"
    background_image: ImageElement | None = None  # covers the whole label
    border_mm: float = 0.0
    border_color: str = "#000000"
    elements: list[Element] = field(default_factory=list)

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = FORMAT_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Label":
        """Build a label from untrusted JSON: unknown fields are ignored,
        wrong types raise ValueError, numbers are clamped to sane ranges."""
        if not isinstance(data, dict):
            raise ValueError("not an Etiketto file")
        data = dict(data)
        version = data.pop("version", FORMAT_VERSION)
        if not isinstance(version, int) or version > FORMAT_VERSION:
            raise ValueError("file was written by a newer Etiketto (format %s)" % version)
        items = data.pop("elements", [])
        if not isinstance(items, list):
            raise ValueError("elements must be a list")
        elements = [_element_from_dict(item) for item in items]
        background_image = data.pop("background_image", None)
        label = cls(**_known_fields(cls, data, frozenset({"elements", "background_image"})))
        label.elements = elements
        if background_image:
            image = _element_from_dict(dict(background_image, kind="image"))
            assert isinstance(image, ImageElement)
            label.background_image = image
        label._validate()
        return label

    def _validate(self) -> None:
        if self.tape_mm not in TAPE_WIDTHS_MM:
            raise ValueError("unsupported tape width %r mm" % self.tape_mm)
        self.auto_length = bool(self.auto_length)
        self.length_mm = _clamp(self.length_mm, 15, 1000, "length_mm")
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
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --- validation helpers -----------------------------------------------------


def _known_fields(cls, data: dict, skip: frozenset[str] = frozenset()) -> dict:
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in skip}


def _clamp(value, lower: float, upper: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % name)
    return float(min(upper, max(lower, value)))


def _choice(value, allowed: tuple, name: str):
    if value not in allowed:
        raise ValueError("invalid %s %r" % (name, value))
    return value


def _str(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError("%s must be text" % name)
    return value


def _element_from_dict(item) -> Element:
    if not isinstance(item, dict):
        raise ValueError("element must be an object")
    kind = item.get("kind")
    if kind not in ELEMENT_TYPES:
        raise ValueError("unknown element type %r" % kind)
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
