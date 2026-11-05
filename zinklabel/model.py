"""Label document model and its JSON (.zlabel) format."""

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
    edge_mm: float = 1.0  # space along the tape edges
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
        data = dict(data)
        version = data.pop("version", FORMAT_VERSION)
        if version > FORMAT_VERSION:
            raise ValueError("file was written by a newer zinklabel (format %d)" % version)
        elements = []
        for item in data.pop("elements", []):
            item = dict(item)
            kind = item.get("kind")
            if kind not in ELEMENT_TYPES:
                raise ValueError("unknown element type %r" % kind)
            elements.append(ELEMENT_TYPES[kind](**item))
        background_image = data.pop("background_image", None)
        known = set(cls.__dataclass_fields__) - {"elements", "background_image"}
        label = cls(**{k: v for k, v in data.items() if k in known})
        label.elements = elements
        if background_image:
            label.background_image = ImageElement(**background_image)
        if label.tape_mm not in TAPE_WIDTHS_MM:
            raise ValueError("unsupported tape width %r mm" % label.tape_mm)
        return label

    def save(self, path: str | Path) -> None:
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False))
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> "Label":
        return cls.from_dict(json.loads(Path(path).read_text()))
