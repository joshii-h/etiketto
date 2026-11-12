"""Find the shipping label on a page.

Carriers send labels as an A4 or Letter page with the label in one half or
quarter, next to receipts and instructions. The detection only proposes an
area; the user always checks and adjusts it.

- Frames: rectangles of long horizontal and vertical lines (most labels
  have a printed border).
- Blocks: the content is cut at wide blank rows and at wide blank columns,
  again and again (an XY cut); runs of neighbouring parts that stand closer
  together than to the rest are blocks too, so a label with blank space
  inside is found whole.
- Frames and blocks are scored alike: large, but not more of the sheet
  than a label takes (a framed one up to an A5 label on A4), about 2:3
  like the common label sizes, with a barcode; a frame counts a little
  more. Frames and blocks inside a label's frame are parts of it. If the next area that is not a
  part of the best one scores about as much, the user is asked to check.
- If nothing on the page scores near all of its content (or a frame around
  all of it), the page is one label: everything printed is proposed.
- A page of label size (by its size in mm: a PDF, or an image that states
  its resolution) is the label itself.

Every area keeps a margin of blank paper, so no bar and no quiet zone of a
barcode is cut off. The block search and the barcode test follow ThermalPrint
(https://github.com/arios4k/thermalprint, MIT License, Copyright (c) 2026
Alexis Rios-Negron), see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

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
# a printed frame is better evidence: a frame of an A5 label takes half of
# an A4 sheet; a border around the whole page is no label
MAX_FRAME_AREA = 0.6
BARCODE_RUNS = 12  # a row through a linear barcode crosses at least this many bars
BARCODE_HEIGHT = 0.025  # ... over at least this part of the page's height
DOMINANT = 1.5  # the best area must score this much more than the next one


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


def _cuts(mask: Image.Image, box: Box, gaps: tuple[int, int]) -> list[tuple[list[Box], bool]]:
    """The box split at blank rows and split at blank columns (True: the
    parts stand side by side); a direction without a gap is left out."""
    region = mask.crop((box.x0, box.y0, box.x1, box.y1))
    found = []
    rows = _segments(_rows(region), gaps[1])
    if len(rows) > 1:
        found.append(([Box(box.x0, box.y0 + a, box.x1, box.y0 + b) for a, b in rows], False))
    columns = _segments(_columns(region), gaps[0])
    if len(columns) > 1:
        found.append(([Box(box.x0 + a, box.y0, box.x0 + b, box.y1) for a, b in columns], True))
    return found


@dataclass(frozen=True)
class Block:
    box: Box
    # it holds large parts side by side: more likely a label next to a
    # receipt than one label
    beside: bool = False


def _union(boxes: list[Box]) -> Box:
    return Box(
        min(b.x0 for b in boxes),
        min(b.y0 for b in boxes),
        max(b.x1 for b in boxes),
        max(b.y1 for b in boxes),
    )


def blocks(mask: Image.Image, box: Box, depth: int = 5) -> list[Block]:
    """The content cut at wide blank rows and at wide blank columns, again
    and again (an XY cut): every block on the way, the box itself first,
    and every run of neighbouring parts of a cut. A label with wide blank
    space inside is cut into parts too; one of the runs is the whole label."""
    own = tight(mask, box)
    if not own:
        return []
    found: dict[Block, None] = {}
    _blocks(mask, own, depth, found, set())
    return list(found)


def _groups(parts: list[Box], beside: bool) -> list[tuple[int, int]]:
    """Runs of neighbouring parts (first, last + 1) that stand closer to each
    other than to the parts around them: the sections of one label, not a
    label and half of the instructions below it."""
    if beside:
        gaps = [b.x0 - a.x1 for a, b in pairwise(parts)]
    else:
        gaps = [b.y0 - a.y1 for a, b in pairwise(parts)]
    found = []
    for first in range(len(parts)):
        for last in range(first + 2, len(parts) + 1):
            if last - first == len(parts):
                continue  # all of them: the box that was cut
            inside = max(gaps[first : last - 1])
            around = gaps[first - 1 : first] + gaps[last - 1 : last]
            if inside < min(around):
                found.append((first, last))
    return found


def _blocks(mask: Image.Image, own: Box, depth: int, found: dict, seen: set) -> None:
    if own in seen:
        return
    seen.add(own)
    gaps = (max(3, round(mask.width * GAP)), max(3, round(mask.height * GAP)))
    large_area = MIN_AREA * mask.width * mask.height
    cuts = []
    for cut, beside in _cuts(mask, own, gaps) if depth else []:
        cuts.append(([p for part in cut if (p := tight(mask, part))], beside))
    side_by_side = any(b and sum(p.area >= large_area for p in parts) > 1 for parts, b in cuts)
    found[Block(own, side_by_side)] = None
    for parts, beside in cuts:
        for first, last in _groups(parts, beside):
            run = parts[first:last]
            large = sum(p.area >= large_area for p in run)
            found.setdefault(Block(_union(run), beside and large > 1))
        for part in parts:
            _blocks(mask, part, depth - 1, found, seen)


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
    return max(0.1, 1 - abs(box.ratio() - 1.5) / 0.75)


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
    lines = frames(mask)
    ranked = _ranked(mask, content, lines)
    one = _one_label(mask, content, lines)
    # an image printed all over is likely the label itself (an A6 label has
    # the shape of A4); a PDF page of sheet size rarely is
    share = ONE_IMAGE if width_mm is None and _fills(content, size) else ONE_LABEL
    if one and (not ranked or ranked[0].score < share * one.score):
        # all of the page is one label: in a frame around everything, or
        # nothing on it looks like a label of its own
        crop = one.crop(pad, size) if one.how == "frame" else whole
        others = [c.crop(pad, size) for c in ranked[:3]]
        return Detection(crop, one.how, [crop, *others], one.how == "frame")
    if not ranked:
        return Detection(whole, "content", [whole])
    candidates = [c.crop(pad, size) for c in ranked[:4]]
    return Detection(candidates[0], ranked[0].how, [*candidates, whole], _certain(ranked))


FILLS = 0.8  # content over this part of the page's width and height fills it
SAME = 0.9  # areas overlapping this much are the same area
FRAME_BONUS = 1.2  # a printed frame around an area is evidence of a label
# the page is one label if the best area on it scores less than this part
# of the page's content (or a frame around all of it), scored alike
ONE_LABEL, ONE_IMAGE = 0.15, 0.4


@dataclass(frozen=True)
class Candidate:
    box: Box
    how: str  # "frame" or "block"
    score: float

    def crop(self, pad: int, size: tuple[int, int]) -> tuple[float, float, float, float]:
        # a frame: a little more than the frame, the whole line stays on the label
        grown = self.box.grown(max(1, pad // 2) if self.how == "frame" else pad, size)
        return grown.fractions(size)


def _fills(content: Box, size: tuple[int, int]) -> bool:
    return content.width >= FILLS * size[0] and content.height >= FILLS * size[1]


def _common(a: Box, b: Box) -> int:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return max(0, w) * max(0, h)


def _overlap_share(a: Box, b: Box) -> float:
    """Common area by the area of both together."""
    common = _common(a, b)
    return common / max(1, a.area + b.area - common)


def _inside_share(a: Box, b: Box) -> float:
    """How much of a lies in b."""
    return _common(a, b) / max(1, a.area)


def _inside(a: Box, b: Box) -> bool:
    """a lies in b, and b is larger (an area is not inside itself)."""
    return b.area > a.area and _inside_share(a, b) >= SAME


def _section(a: Box, b: Box, tol: int) -> bool:
    """a is a part of frame b cut off by divider lines: inside it and on
    at least two of its sides."""
    if not _inside(a, b):
        return False
    sides = (a.x0 - b.x0, b.x1 - a.x1, a.y0 - b.y0, b.y1 - a.y1)
    return sum(abs(d) <= tol for d in sides) >= 2


def _certain(ranked: list[Candidate]) -> bool:
    """The best area scores clearly more than the next one that is not a
    part of it (a part of the label is no other label)."""
    best = ranked[0]
    others = [c for c in ranked[1:] if _inside_share(c.box, best.box) < SAME]
    return not others or best.score >= DOMINANT * others[0].score


def _one_label(mask: Image.Image, content: Box, lines: list[Box]) -> Candidate | None:
    """All of the content as one label, if it is more of the page than a
    label on a sheet takes: the frame around all of it, else the content."""
    if _label_like(content, mask.size):
        return None  # a part of the page, among the candidates already
    tol = max(2, round(min(mask.size) * PAD))
    around = [f for f in lines if _inside_share(content, f.grown(tol, mask.size)) >= SAME]
    if around:
        return Candidate(around[0], "frame", score(mask, Block(around[0])) * FRAME_BONUS)
    if content.area < MIN_AREA * mask.width * mask.height:
        return None
    return Candidate(content, "content", score(mask, Block(content)))


def _label_like(box: Box, size: tuple[int, int], most: float = MAX_AREA) -> bool:
    """Large enough, and not more of the sheet than a label takes."""
    page = size[0] * size[1]
    return MIN_AREA * page <= box.area <= most * page


def _ranked(mask: Image.Image, content: Box, lines: list[Box]) -> list[Candidate]:
    """Frames and blocks that could be the label, scored alike, best first;
    of areas that are about the same only the best is kept."""
    size = mask.size
    framed = [b for b in lines if b.ratio() <= 2.6 and _label_like(b, size, MAX_FRAME_AREA)]
    # a frame inside a label's frame is a part of it (a section between
    # divider lines, a box), and so is a block inside it; a section shares
    # sides with any frame it is in, also one too large to be a candidate
    tol = max(2, round(min(size) * 0.006))
    outer = [
        b
        for b in framed
        if not any(_inside(b, other) for other in framed)
        and not any(_section(b, other, tol) for other in lines)
    ]
    found = [Candidate(b, "frame", score(mask, Block(b)) * FRAME_BONUS) for b in outer]
    found += [
        Candidate(b.box, "block", score(mask, b))
        for b in set(blocks(mask, content))
        if _label_like(b.box, size) and not any(_inside(b.box, f) for f in outer)
    ]
    found.sort(key=lambda c: (c.score, c.how == "frame"), reverse=True)
    kept: list[Candidate] = []
    for candidate in found:
        if all(_overlap_share(candidate.box, k.box) < SAME for k in kept):
            kept.append(candidate)
    return kept
