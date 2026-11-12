"""What a shipping label is printed on, and the label document made from it."""

from __future__ import annotations

from dataclasses import dataclass

from etiketto.model import MAX_SIZE_MM, MIN_SIZE_MM, Label, PageElement
from etiketto.shipping.source import area, page_info, turned


@dataclass(frozen=True)
class Target:
    """A label size: width across the printer, height along the feed; no
    height: endless material, as long as the area needs."""

    key: str
    width_mm: float
    height_mm: float | None = None


# the label sizes carriers design for, the sheet sizes office printers take,
# and a 62 mm roll (endless labels of label printers)
TARGETS = (
    Target("4x6", 101.6, 152.4),
    Target("100x150", 100, 150),
    Target("100x200", 100, 200),
    Target("a6", 105, 148),
    Target("a4", 210, 297),
    Target("roll62", 62),
)


def targets_for(caps, tape_mm: float) -> list[Target]:
    """The sizes a printer model can print: on endless tape only the tape
    of the label; die-cut and page printers every size not too wide."""
    if caps.continuous:
        return [Target("tape", tape_mm)]
    return [t for t in TARGETS if not caps.too_wide(t.width_mm)]


def matching(targets: list[Target], label: Label) -> Target:
    """The target of the label's current size, else the first."""
    width, height = label.media_size()
    for target in targets:
        same_width = abs(target.width_mm - width) < 0.05
        if same_width and target.height_mm and abs(target.height_mm - height) < 0.05:
            return target
    return targets[0]


def area_size(element: PageElement) -> tuple[float, float]:
    """Width and height of the area as printed (after rotation), in page units."""
    info = page_info(element.data, element.page)
    _x, _y, w, h = area(element.crop, info.width, info.height)
    return turned(w, h, element.rotation)


def upright(element: PageElement, target: Target) -> int:
    """The rotation that turns the area the way the target is: endless tape
    runs lengthwise (landscape), all other targets are portrait. Turned
    clockwise, as most labels printed sideways read from bottom to top."""
    info = page_info(element.data, element.page)
    _x, _y, w, h = area(element.crop, info.width, info.height)
    landscape = w > h
    wants_landscape = target.key == "tape"
    return 0 if landscape == wants_landscape else 90


def _length(target: Target, size: tuple[float, float], safe_mm: float, caps) -> float:
    """Height of an endless label: the area at the width, plus the margins."""
    if target.height_mm:
        return target.height_mm
    width, height = size
    printable = max(1.0, target.width_mm - 2 * safe_mm)
    length = printable * height / width + 2 * safe_mm
    upper = min(MAX_SIZE_MM, caps.max_length_mm or MAX_SIZE_MM)
    return round(min(upper, max(MIN_SIZE_MM, length)), 1)


def make_label(backend, element: PageElement, target: Target, safe_mm: float) -> Label:
    """A label with the area as its only content, at the target size."""
    caps = backend.capabilities
    if caps.continuous:
        label = Label.new_for(backend.id, int(target.width_mm))
        label.auto_length = True
    else:
        label = Label.new_for(backend.id)
        label.orientation = "across"  # the label is read as it leaves the printer
        label.auto_length = False
        height = _length(target, area_size(element), safe_mm, caps)
        label.set_media_size(target.width_mm, height)
    label.margin_mm = 0.0
    label.edge_mm = 0.0
    label.align = "center"
    label.elements = [element]
    return label
