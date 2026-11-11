"""Label sheets: pages with a grid of labels, from the gLabels template
database (templates.json, see tools/glabels_templates.py; MIT license in
LICENSE.glabels, Copyright (C) 2001-2026 Jaye Evins).

Everything is in millimetres, x to the right and y down the page, the
origin in the page's top left corner. Positions are numbered from 0 in the
order a sheet is filled: row by row, left to right.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path

DATA = Path(__file__).parent / "templates.json"
SHAPES = ("rectangle", "round", "ellipse", "cd")
# a size typed in a search ("70x37", "70 × 37", "63.5x38,1") matches labels
# this close, either way round
SIZE_TOLERANCE_MM = 1.0
_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)")
# papers the search can filter by; the rest is "other"
COMMON_PAPERS = ("A4", "US Letter")

Layout = tuple[int, int, float, float, float, float]  # nx, ny, x0, y0, dx, dy


@dataclass(frozen=True)
class Sheet:
    brand: str
    part: str
    description: str
    paper: tuple[float, float]  # page width, height in mm
    shape: str  # rectangle, round, ellipse, cd
    width: float  # one label, mm
    height: float
    layouts: tuple[Layout, ...]  # one or more grids of labels
    paper_name: str = ""  # "A4", "US Letter"; empty for other sizes
    corner: float = 0.0  # corner radius of rectangles
    hole: float = 0.0  # diameter of a CD's hole
    diameter: float = 0.0  # a CD cut off at the sides: the disc's diameter
    categories: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """What a label file stores: "Herma 4283"."""
        return "%s %s" % (self.brand, self.part)

    @cached_property
    def _positions(self) -> tuple[tuple[float, float], ...]:
        spots = {
            (round(x0 + col * dx, 3), round(y0 + row * dy, 3))
            for nx, ny, x0, y0, dx, dy in self.layouts
            for row in range(ny)
            for col in range(nx)
        }
        # rows first; labels of two grids less than 0.1 mm apart share a row
        return tuple(sorted(spots, key=lambda xy: (round(xy[1], 1), xy[0])))

    def positions(self) -> list[tuple[float, float]]:
        """Top left corner of every label on the page in mm, row by row
        (the order in which a sheet is filled); a label two layouts place
        on the same spot counts once."""
        return list(self._positions)

    @property
    def count(self) -> int:
        return len(self._positions)

    def rect(self, index: int) -> tuple[float, float, float, float]:
        """Label index as (left, top, right, bottom) in mm."""
        x, y = self._positions[index]
        return x, y, x + self.width, y + self.height

    def position_at(self, x: float, y: float) -> int | None:
        """The label at the point (x, y) on the page, or None."""
        for index in range(self.count):
            left, top, right, bottom = self.rect(index)
            if left <= x <= right and top <= y <= bottom:
                return index
        return None

    @property
    def disc(self) -> float:
        """A CD's diameter (0 for other shapes)."""
        if self.shape != "cd":
            return 0.0
        return self.diameter or max(self.width, self.height)


@cache
def sheets() -> tuple[Sheet, ...]:
    """All sheets, by brand and part number."""
    entries = json.loads(DATA.read_text(encoding="utf-8"))
    return tuple(_sheet(entry) for entry in entries)


def _sheet(entry: dict) -> Sheet:
    return Sheet(
        brand=entry["brand"],
        part=entry["part"],
        description=entry.get("description", ""),
        paper=(entry["paper"][0], entry["paper"][1]),
        paper_name=entry.get("paper_name", ""),
        shape=entry["shape"],
        width=entry["width"],
        height=entry["height"],
        layouts=tuple(tuple(layout) for layout in entry["layouts"]),
        corner=entry.get("corner", 0.0),
        hole=entry.get("hole", 0.0),
        diameter=entry.get("diameter", 0.0),
        categories=tuple(entry.get("categories", ())),
    )


def find(sheet_id: str) -> Sheet | None:
    return _by_id().get(sheet_id)


@cache
def _by_id() -> dict[str, Sheet]:
    return {sheet.id: sheet for sheet in sheets()}


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _sizes(text: str) -> tuple[str, list[tuple[float, float]]]:
    """The sizes typed in the search ("70x37") and the rest of the text."""
    sizes = [(_number(w), _number(h)) for w, h in _SIZE.findall(text)]
    return _SIZE.sub(" ", text), sizes


def _has_size(sheet: Sheet, width: float, height: float) -> bool:
    def close(a: float, b: float) -> bool:
        return abs(a - b) <= SIZE_TOLERANCE_MM

    return (close(sheet.width, width) and close(sheet.height, height)) or (
        close(sheet.width, height) and close(sheet.height, width)
    )


def paper_matches(sheet: Sheet, paper: str) -> bool:
    """paper: "" (any), a name from COMMON_PAPERS, or "other"."""
    if not paper:
        return True
    if paper == "other":
        return sheet.paper_name not in COMMON_PAPERS
    return sheet.paper_name == paper


def search(text: str, paper: str = "") -> list[Sheet]:
    """Sheets whose brand, part number or description contain every word of
    text (any case), and whose labels have every size in it ("70x37", within
    SIZE_TOLERANCE_MM, also turned); optionally only for one paper (see
    paper_matches)."""
    rest, sizes = _sizes(text.lower())
    words = rest.split()
    found = []
    for sheet in sheets():
        if not paper_matches(sheet, paper):
            continue
        if not all(_has_size(sheet, w, h) for w, h in sizes):
            continue
        haystack = " ".join((sheet.brand, sheet.part, sheet.description)).lower()
        # "L7160" also as "L 7160", "Zweckform 3475" as "zweckform3475"
        squeezed = haystack.replace(" ", "").replace("-", "")
        if all(word in haystack or word.replace("-", "") in squeezed for word in words):
            found.append(sheet)
    return found
