"""Find the shipping label on a page.

Carriers send labels as an A4 or Letter page with the label in one half or
quarter, next to receipts and instructions. The detection only proposes an
area; the user always checks and adjusts it. In this order:

1. A frame: most labels have a printed border. The largest rectangle made
   of long horizontal and vertical lines is taken.
2. Blocks: the content is cut at wide blank rows and columns (an XY cut);
   the block that looks most like a label wins: large, about 2:3 like the
   common label sizes, with a barcode.
3. Otherwise everything printed on the page.

Every area keeps a margin of blank paper, so no bar and no quiet zone of a
barcode is cut off. The block search and the barcode test follow ThermalPrint
(https://github.com/arios4k/thermalprint, MIT License, Copyright (c) 2026
Alexis Rios-Negron), see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageFilter

INK_LEVEL = 200  # darker than this is ink: grey scanner paper is not
# a pixel stays ink only with this many ink pixels in its 5 × 5
# neighbourhood: a 1 pixel line keeps 5, scanner dust (up to 2 × 2) has fewer
NEIGHBOURS = 5
LINE_RUN = 0.12  # a frame line is at least this part of the page's side
MIN_AREA = 0.025  # areas smaller than this part of the page are not labels
GAP = 0.02  # blank rows (columns) this part of the page's side separate blocks
PAD = 0.01  # blank paper kept around a block, as part of the page's shorter side
# a row or column with fewer ink pixels counts as blank
MIN_LINE_INK, LINE_INK_SHARE = 3, 0.005
MAX_AREA = 0.35  # a label takes at most about a quarter of a sheet, 100 × 200 mm a third
BARCODE_RUNS = 12  # a row through a linear barcode crosses at least this many bars
BARCODE_HEIGHT = 0.025  # ... over at least this part of the page's height
DOMINANT = 1.5  # the best block must score this much more than the next one


@dataclass(frozen=True)
class Box:
    """A rectangle in pixels, right and bottom exclusive."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def area(self) -> int:
        return self.width * self.height

    def ratio(self) -> float:
        """Longer side by shorter side: 1.5 for 4 × 6 in, 100 × 150 mm."""
        return max(self.width, self.height) / max(1, min(self.width, self.height))

    def grown(self, pad: int, size: tuple[int, int]) -> Box:
        return Box(
            max(0, self.x0 - pad),
            max(0, self.y0 - pad),
            min(size[0], self.x1 + pad),
            min(size[1], self.y1 + pad),
        )

    def fractions(self, size: tuple[int, int]) -> tuple[float, float, float, float]:
        w, h = size
        return (self.x0 / w, self.y0 / h, self.width / w, self.height / h)


@dataclass
class Detection:
    """The proposed area (fractions x, y, width, height of the page), how it
    was found ("frame", "block", "content" or "none" for a blank page), and
    other areas that could be the label."""

    crop: tuple[float, float, float, float]
    how: str
    candidates: list[tuple[float, float, float, float]] = field(default_factory=list)
    certain: bool = True  # False: several areas look alike, the user should check


FULL = (0.0, 0.0, 1.0, 1.0)


# --- ink -----------------------------------------------------------------------------


def ink_mask(page: Image.Image) -> Image.Image:
    """255 where something is printed, 0 for paper; scanner dust removed."""
    grey = page.convert("L")
    mask = grey.point(lambda v: 255 if v < INK_LEVEL else 0)
    # count the ink around each pixel: 10 per ink pixel, 25 pixels fit in 255
    weights = mask.point(lambda v: 10 if v else 0)
    counted = weights.filter(ImageFilter.Kernel((5, 5), [1] * 25, scale=1))
    keep = counted.point(lambda v: 255 if v >= 10 * NEIGHBOURS else 0)
    return ImageChops.darker(mask, keep)


def _rows(mask: Image.Image) -> list[bytes]:
    data = mask.tobytes()
    width = mask.width
    return [data[i : i + width] for i in range(0, len(data), width)]


def _columns(mask: Image.Image) -> list[bytes]:
    return _rows(mask.transpose(Image.Transpose.TRANSPOSE))


# --- frames --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    pos: int  # y of a horizontal line, x of a vertical one
    start: int
    end: int  # exclusive
    last: int = -1  # the line's last row (column): thick lines have several

    def __post_init__(self):
        if self.last < self.pos:
            object.__setattr__(self, "last", self.pos)


def long_lines(lines: list[bytes], min_run: int) -> list[Line]:
    """Runs of ink at least min_run long; neighbouring rows of one thick
    line become one line."""
    pattern = re.compile(rb"\xff{%d,}" % max(1, min_run))
    found = [
        Line(pos, match.start(), match.end())
        for pos, line in enumerate(lines)
        for match in pattern.finditer(line)
    ]
    return _merge_lines(found)


def _merge_lines(lines: list[Line]) -> list[Line]:
    """Lines in order of pos; one in the next rows with about the same span
    is the same (thick) line."""
    merged: list[Line] = []
    for line in lines:
        index = _same_line(merged, line)
        if index is None:
            merged.append(line)
        else:
            last = merged[index]
            start, end = min(last.start, line.start), max(last.end, line.end)
            merged[index] = Line(last.pos, start, end, line.pos)
    return merged


def _same_line(merged: list[Line], line: Line) -> int | None:
    for index in range(len(merged) - 1, -1, -1):
        other = merged[index]
        if line.pos - other.last > 2:
            return None
        if _overlap(other, line) > 0.8:
            return index
    return None


def _overlap(a: Line, b: Line) -> float:
    common = min(a.end, b.end) - max(a.start, b.start)
    return common / max(1, min(a.end - a.start, b.end - b.start))


def _near(a: int, b: int, tolerance: int) -> bool:
    return abs(a - b) <= tolerance


def _has_side(verticals: list[Line], x: int, top: int, bottom: int, tol: int) -> bool:
    """A vertical line at x that runs (almost) from top to bottom."""
    return any(
        v.pos - tol <= x <= v.last + tol and v.start <= top + tol and v.end >= bottom - tol
        for v in verticals
    )


def frames(mask: Image.Image) -> list[Box]:
    """Rectangles drawn with lines: two horizontal lines of the same span
    joined by vertical lines at both ends. Largest first."""
    w, h = mask.size
    horizontal = long_lines(_rows(mask), round(w * LINE_RUN))[:200]
    vertical = long_lines(_columns(mask), round(h * LINE_RUN))[:200]
    tol = max(2, round(min(w, h) * 0.006))
    found = []
    for i, top in enumerate(horizontal):
        for bottom in horizontal[i + 1 :]:
            box = _frame_between(top, bottom, vertical, tol)
            if box and box.area >= MIN_AREA * w * h:
                found.append(box)
    return sorted(set(found), key=lambda b: b.area, reverse=True)


def _frame_between(top: Line, bottom: Line, vertical: list[Line], tol: int) -> Box | None:
    if not (_near(top.start, bottom.start, tol) and _near(top.end, bottom.end, tol)):
        return None
    left, right = min(top.start, bottom.start), max(top.end, bottom.end)
    sides = _has_side(vertical, left, top.pos, bottom.last, tol) and _has_side(
        vertical, right - 1, top.pos, bottom.last, tol
    )
    return Box(left, top.pos, right, bottom.last + 1) if sides else None


# --- blocks --------------------------------------------------------------------------


def _inked(line: bytes) -> bool:
    """A row (column) with print on it; the odd speck of dust left over by
    ink_mask() is not enough."""
    return line.count(b"\xff") >= max(MIN_LINE_INK, len(line) * LINE_INK_SHARE)


def _segments(lines: list[bytes], min_gap: int) -> list[tuple[int, int]]:
    """Runs of rows with ink, separated by at least min_gap blank rows."""
    segments: list[tuple[int, int]] = []
    start = end = None
    for i, line in enumerate(lines):
        if not _inked(line):
            continue
        if start is None:
            start = i
        elif end is not None and i - end >= min_gap:
            segments.append((start, end))
            start = i
        end = i + 1
    if start is not None and end is not None:
        segments.append((start, end))
    return segments


def _span(lines: list[bytes]) -> tuple[int, int] | None:
    inked = [i for i, line in enumerate(lines) if _inked(line)]
    return (inked[0], inked[-1] + 1) if inked else None


def tight(mask: Image.Image, box: Box) -> Box | None:
    """The part of the box with print on it."""
    region = mask.crop((box.x0, box.y0, box.x1, box.y1))
    rows, columns = _span(_rows(region)), _span(_columns(region))
    if not rows or not columns:
        return None
    return Box(box.x0 + columns[0], box.y0 + rows[0], box.x0 + columns[1], box.y0 + rows[1])


def _cut(mask: Image.Image, box: Box, gaps: tuple[int, int]) -> tuple[list[Box], bool]:
    """Split the box at blank rows; if there are none, at blank columns
    (then True: the parts stand side by side)."""
    region = mask.crop((box.x0, box.y0, box.x1, box.y1))
    parts = _segments(_rows(region), gaps[1])
    if len(parts) > 1:
        return [Box(box.x0, box.y0 + a, box.x1, box.y0 + b) for a, b in parts], False
    parts = _segments(_columns(region), gaps[0])
    return [Box(box.x0 + a, box.y0, box.x0 + b, box.y1) for a, b in parts], True


@dataclass(frozen=True)
class Block:
    box: Box
    # it holds large parts side by side: more likely a label next to a
    # receipt than one label
    beside: bool = False


def blocks(mask: Image.Image, box: Box, depth: int = 4) -> list[Block]:
    """The content cut at wide blank rows and columns, again and again
    (an XY cut): every block on the way, the box itself first."""
    own = tight(mask, box)
    if not own:
        return []
    gaps = (max(3, round(mask.width * GAP)), max(3, round(mask.height * GAP)))
    parts, beside = _cut(mask, own, gaps) if depth else ([], False)
    large = sum(1 for part in parts if part.area >= MIN_AREA * mask.width * mask.height)
    found = [Block(own, beside and large > 1)]
    if len(parts) > 1:
        for part in parts:
            found.extend(blocks(mask, part, depth - 1))
    return found


def has_barcode(mask: Image.Image, box: Box) -> bool:
    """Rows crossing many bars, over a height a linear barcode has."""
    region = mask.crop((box.x0, box.y0, box.x1, box.y1))
    needed = max(3, round(mask.height * BARCODE_HEIGHT))
    run = 0
    for line in _rows(region):
        run = run + 1 if len(re.findall(rb"\xff+", line)) >= BARCODE_RUNS else 0
        if run >= needed:
            return True
    return False


def _aspect_weight(box: Box) -> float:
    """Shipping labels are 2:3 (4 × 6 in, 100 × 150 mm); the further a
    block is from that, the less it looks like one."""
    return max(0.1, 1 - abs(box.ratio() - 1.5) / 0.5)


def score(mask: Image.Image, block: Block) -> float:
    box = block.box
    weight = _aspect_weight(box) * (0.5 if block.beside else 1.0)
    return box.area * weight * (1.5 if has_barcode(mask, box) else 1.0)


# --- the proposal --------------------------------------------------------------------

# sheets a label comes on, by their shape (longer by shorter side): A4, Letter
SHEET_RATIOS = (2**0.5, 11 / 8.5)
SHEET_MM = 160  # a page with a shorter side up to this is a label, not a sheet


def is_sheet(size: tuple[float, float], width_mm: float | None = None) -> bool:
    """Whether the page is a sheet with the label somewhere on it, not the
    label itself: by its size if known (PDF), else by its shape (images)."""
    if width_mm is not None:
        return min(width_mm, width_mm * size[1] / size[0]) > SHEET_MM
    ratio = max(size) / min(size)
    return any(abs(ratio - sheet) < 0.02 for sheet in SHEET_RATIOS)


def detect(page: Image.Image, width_mm: float | None = None) -> Detection:
    """Propose the area of the label on a page (any resolution; about 100
    to 150 dpi is fast and enough). width_mm: the page width if known."""
    mask = ink_mask(page)
    size = mask.size
    content = tight(mask, Box(0, 0, *size))
    if not content:
        return Detection(FULL, "none", certain=False)
    pad = max(2, round(min(size) * PAD))
    whole = content.grown(pad, size).fractions(size)
    if not is_sheet(size, width_mm):  # the page is the label
        return Detection(whole, "content", [whole])
    framed = [b for b in frames(mask) if b.ratio() <= 2.6]
    if framed:
        # a little more than the frame: the whole line stays on the label
        crop = framed[0].grown(max(1, pad // 2), size).fractions(size)
        others = [b.grown(pad, size).fractions(size) for b in framed[1:4]]
        return Detection(crop, "frame", [crop, *others, whole])
    return _from_blocks(mask, content, pad, whole)


def _label_like(box: Box, size: tuple[int, int]) -> bool:
    """Large enough, and not more of the sheet than a label takes."""
    page = size[0] * size[1]
    return MIN_AREA * page <= box.area <= MAX_AREA * page


def _from_blocks(mask: Image.Image, content: Box, pad: int, whole) -> Detection:
    size = mask.size
    found = [b for b in blocks(mask, content) if _label_like(b.box, size)]
    if not found:
        return Detection(whole, "content", [whole])
    ranked = sorted(set(found), key=lambda b: score(mask, b), reverse=True)
    scores = [score(mask, b) for b in ranked[:2]]
    certain = len(scores) < 2 or scores[0] >= DOMINANT * scores[1]
    candidates = [b.box.grown(pad, size).fractions(size) for b in ranked[:4]]
    return Detection(candidates[0], "block", [*candidates, whole], certain)
