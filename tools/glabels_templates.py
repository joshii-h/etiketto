#!/usr/bin/env python3
"""Turn the gLabels template database into etiketto/sheets/templates.json.

    python3 tools/glabels_templates.py <glabels-qt checkout>/templates

The database (MIT license, Copyright (C) 2001-2026 Jaye Evins) describes
label sheets: paper size, label shape and size, and where the labels sit
(one or more grids). Etiketto keeps what it needs for sheets, in mm, with
aliases ("equiv") resolved, and the license next to it. Rolls and labels
with a free-form outline are left out: Etiketto prints those differently.
So are the few templates whose labels lie off the page (typing errors in
the database: a label more than OFF_PAGE_MM beyond the paper's edge) and
labels smaller than a label file may be (MIN_SIZE_MM).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - our own copy of a known source
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "etiketto" / "sheets"
UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "": 25.4 / 72}
OFF_PAGE_MM = 1.0
MIN_SIZE_MM = 5.0  # etiketto.model.MIN_SIZE_MM
SHAPES = {
    "Label-rectangle": "rectangle",
    "Label-round": "round",
    "Label-ellipse": "ellipse",
    "Label-cd": "cd",
}


def mm(length: str | None) -> float:
    """A gLabels length ("2.5in", "210mm", "72pt", "72" = points) in mm."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([a-z]*)\s*", length or "0")
    if not match or match.group(2) not in UNITS:
        raise ValueError("unknown length %r" % length)
    return round(float(match.group(1)) * UNITS[match.group(2)], 3)


def paper_sizes(folder: Path) -> dict[str, tuple[str, float, float]]:
    """Paper id: (name, width, height)."""
    root = ET.parse(folder / "paper-sizes.xml").getroot()  # noqa: S314
    return {
        p.get("id", ""): (p.get("name", ""), mm(p.get("width")), mm(p.get("height"))) for p in root
    }


def label(element: ET.Element) -> dict | None:
    shape = SHAPES.get(element.tag)
    if not shape:
        return None
    if shape in ("round", "cd"):
        diameter = 2 * mm(element.get("radius"))
        # business-card CDs: the disc cut off at the sides (or top and bottom)
        width = mm(element.get("width")) if element.get("width") else diameter
        height = mm(element.get("height")) if element.get("height") else diameter
    else:
        width, height = mm(element.get("width")), mm(element.get("height"))
    layouts = [
        [
            int(lay.get("nx", 1)),
            int(lay.get("ny", 1)),
            mm(lay.get("x0")),
            mm(lay.get("y0")),
            mm(lay.get("dx")),
            mm(lay.get("dy")),
        ]
        for lay in element.findall("Layout")
    ]
    entry = {"shape": shape, "width": width, "height": height, "layouts": layouts}
    if shape == "rectangle" and element.get("round"):
        # one template says round="in": no radius given
        with contextlib.suppress(ValueError):
            entry["corner"] = mm(element.get("round"))
    if shape == "cd":
        entry["hole"] = 2 * mm(element.get("hole"))
        if diameter > max(width, height) + 0.01:
            entry["diameter"] = diameter
    return entry


def usable(entry: dict, paper: tuple[float, float]) -> bool:
    """Whether every label lies on the paper (within OFF_PAGE_MM) and is
    large enough."""
    for nx, ny, x0, y0, dx, dy in entry["layouts"]:
        right = x0 + (nx - 1) * dx + entry["width"]
        bottom = y0 + (ny - 1) * dy + entry["height"]
        if min(x0, y0) < -OFF_PAGE_MM or right > paper[0] + OFF_PAGE_MM:
            return False
        if bottom > paper[1] + OFF_PAGE_MM:
            return False
    return min(entry["width"], entry["height"]) >= MIN_SIZE_MM


def templates(folder: Path) -> list[dict]:
    papers = paper_sizes(folder)
    found: dict[tuple[str, str], dict] = {}
    aliases: list[tuple[str, str, str]] = []
    for path in sorted(folder.glob("*-templates.xml")):
        for t in ET.parse(path).getroot().findall("Template"):  # noqa: S314
            brand, part = t.get("brand", ""), t.get("part", "")
            if t.get("equiv"):
                aliases.append((brand, part, t.get("equiv", "")))
                continue
            size = t.get("size", "")
            if size in papers:
                name, *paper = papers[size]
            elif size == "other":
                name, paper = "", [mm(t.get("width")), mm(t.get("height"))]
            else:
                continue  # rolls
            shapes = [label(child) for child in t]
            shapes = [s for s in shapes if s and s["layouts"]]
            if len(shapes) != 1 or not usable(shapes[0], (paper[0], paper[1])):
                continue  # free-form outlines, several kinds of label, off the page
            found[(brand, part)] = {
                "brand": brand,
                "part": part,
                "description": t.get("_description") or t.get("description") or "",
                "paper": paper,
                "paper_name": name,
                "categories": [m.get("category") for m in t.findall("Meta") if m.get("category")],
                **shapes[0],
            }
    for brand, part, equiv in aliases:
        original = found.get((brand, equiv))
        if original and (brand, part) not in found:
            found[(brand, part)] = dict(original, part=part)
    return sorted(found.values(), key=lambda t: (t["brand"].lower(), _natural(t["part"])))


def _natural(text: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def main(folder: Path) -> None:
    entries = templates(folder)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "templates.json").write_text(
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    shutil.copy(folder / "LICENSE", OUT / "LICENSE.glabels")
    print("%d sheets written to %s" % (len(entries), OUT / "templates.json"))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
