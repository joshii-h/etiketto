"""Which printed frames on a page may be the label, and which are parts of
one or hold several.

Frames come nested: a label's divider lines make frames inside its frame,
a label may sit in a larger frame (a box to cut along, a form), and labels
side by side or stacked may share their lines, which makes a frame around
all of them. So, for every frame:

- A frame holding two or more alike frames side by side (labels of a
  sheet, labels sharing lines, a 2 × 2 grid) is a container: its cells are
  the candidates, the container is only offered after them.
- A frame inside another is a part of it (a section between divider
  lines, a box of a table) if one of its inner edges lies on a line running
  across the whole outer frame. A part is offered after the others if it
  could be a label itself (the shape of one, or a barcode), else not at all.
- Otherwise it lies in the outer frame like a label in a box: with a
  barcode and much smaller than the box (a label in a corner of a form, in
  a frame with a receipt), it is the candidate and the box is offered
  after it; filling most of the box (a box to cut along), the box is the
  candidate, the label the next suggestion, and the choice is uncertain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_RATIO = 2.6  # longer by shorter side of a frame that may be a label
ALIKE_AREA = 0.75  # siblings: the smaller has at least this part of the larger's area
ALIKE_RATIO = 0.35  # ... and their shapes differ by at most this much
APART = 0.1  # siblings overlap by at most this part of the smaller
FILLED = 0.8  # a label filling this much of its box: the box is the candidate
LABEL_SHAPED = 0.5  # a part this alike to 2:3 (aspect weight) could be a label


@dataclass
class Frames:
    main: list = field(default_factory=list)  # may be the label
    later: list = field(default_factory=list)  # offered after the others
    doubtful: dict = field(default_factory=dict)  # a box: the label filling it


@dataclass
class Lines:
    """The long lines of the page, to tell a divider from a frame of its own."""

    horizontal: list
    vertical: list
    tol: int

    def across(self, y: int, box) -> bool:
        """A horizontal line at y from the left to the right side of box."""
        return any(
            line.pos - self.tol <= y <= line.last + self.tol
            and line.start <= box.x0 + self.tol
            and line.end >= box.x1 - self.tol
            for line in self.horizontal
        )

    def down(self, x: int, box) -> bool:
        """A vertical line at x from the top to the bottom of box."""
        return any(
            line.pos - self.tol <= x <= line.last + self.tol
            and line.start <= box.y0 + self.tol
            and line.end >= box.y1 - self.tol
            for line in self.vertical
        )


def _common(a, b) -> int:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return max(0, w) * max(0, h)


def inside(a, b, share: float = 0.9) -> bool:
    """a lies in b, and b is larger."""
    return b.area > a.area and _common(a, b) >= share * max(1, a.area)


def alike(a, b) -> bool:
    small, large = sorted((a.area, b.area))
    if small < ALIKE_AREA * large:
        return False
    return abs(a.ratio() - b.ratio()) <= ALIKE_RATIO and _common(a, b) <= APART * small


def siblings(inner: list) -> list:
    """The largest set of alike frames among inner, side by side."""
    best: list = []
    for first in inner:
        group = [first]
        for other in inner:
            if other is not first and all(alike(other, g) for g in group):
                group.append(other)
        if len(group) > len(best):
            best = group
    return best if len(best) >= 2 else []


def split_halves(box, frames: list, lines: Lines, barcode) -> list:
    """box cut by one line across it into two alike frames, each with a
    barcode: two labels sharing that line (stacked, side by side, a row of
    a grid). [] if there is no such line."""
    inner = [f for f in frames if inside(f, box)]
    for first in inner:
        for second in inner:
            if first is second or not alike(first, second):
                continue
            if _halves(box, first, second, lines.tol) and barcode(first) and barcode(second):
                return [first, second]
    return []


def _halves(box, a, b, tol: int) -> bool:
    """a and b together are box, one beside or above the other."""

    def near(*pairs) -> bool:
        return all(abs(u - v) <= tol for u, v in pairs)

    rows = near((a.y0, box.y0), (b.y0, box.y0), (a.y1, box.y1), (b.y1, box.y1))
    columns = near((a.x0, box.x0), (b.x0, box.x0), (a.x1, box.x1), (b.x1, box.x1))
    across = near((a.x0, box.x0), (a.x1, b.x0), (b.x1, box.x1))
    down = near((a.y0, box.y0), (a.y1, b.y0), (b.y1, box.y1))
    return (rows and across) or (columns and down)


def distinct(boxes: list, tol: int) -> list:
    """Frames whose sides are all about the same (a line drawn twice, a
    label's line next to its box's) count once: the smaller."""
    kept: list = []
    for box in sorted(boxes, key=lambda b: b.area):
        if not any(_same(box, k, 3 * tol) for k in kept):
            kept.append(box)
    return kept


def _same(a, b, tol: int) -> bool:
    return all(
        abs(u - v) <= tol for u, v in ((a.x0, b.x0), (a.y0, b.y0), (a.x1, b.x1), (a.y1, b.y1))
    )


def crossing(a, b) -> bool:
    """a and b overlap, neither lies in the other."""
    common = _common(a, b)
    return common > APART * min(a.area, b.area) and not inside(a, b) and not inside(b, a)


def part_of(a, b, lines: Lines) -> bool:
    """a is cut out of frame b by a line across all of b (a divider)."""
    tol = lines.tol
    return (
        (a.y0 > b.y0 + tol and lines.across(a.y0, b))
        or (a.y1 < b.y1 - tol and lines.across(a.y1 - 1, b))
        or (a.x0 > b.x0 + tol and lines.down(a.x0, b))
        or (a.x1 < b.x1 - tol and lines.down(a.x1 - 1, b))
    )


def _groups(usable: list, lines: Lines, barcode) -> dict:
    """Containers and their cells, the largest container first: a frame
    across a cell of a container found before (from the top of one label
    to a divider of the next) is no frame of its own, nor a container."""
    groups: dict = {}
    left = list(usable)
    for box in sorted(usable, key=lambda b: b.area, reverse=True):
        if box not in left:
            continue
        group = siblings(_children(box, left)) or split_halves(box, left, lines, barcode)
        if group:
            groups[box] = group
            left = [f for f in left if f in group or not any(crossing(f, m) for m in group)]
    return groups


def _crosses(box, groups: dict) -> bool:
    return any(crossing(box, m) for group in groups.values() for m in group)


def _children(box, frames: list) -> list:
    """The frames in box that lie in no other frame in box."""
    inner = [i for i in frames if inside(i, box)]
    return [i for i in inner if not any(inside(i, j) for j in inner)]


def _nested(box, outer, lines: Lines, found: Frames, sets, tests: Tests) -> None:
    """Sort box, which lies in outer, into later or demoted (or move outer
    to later)."""
    later, demoted = sets
    if part_of(box, outer, lines):
        if tests.barcode(box) or tests.shaped(box) >= LABEL_SHAPED:
            later.add(box)
        else:
            demoted.add(box)
    elif not tests.candidate(outer):
        return  # a label in a page border or in a large form: the label counts
    elif outer.area * FILLED <= box.area:
        later.add(box)  # a box to cut along: the label is the next suggestion
        found.doubtful[outer] = box
    elif tests.barcode(box):
        later.add(outer)  # a label in a larger frame
    else:
        later.add(box)  # a box in a label (an address field)


@dataclass(frozen=True)
class Tests:
    candidate: Callable[[Any], bool]  # of a size a label may have
    barcode: Callable[[Any], bool]  # holds a barcode
    shaped: Callable[[Any], float]  # how alike to 2:3 (0 to 1)


def sort_frames(boxes: list, lines: Lines, tests: Tests) -> Frames:
    """The frames that may be the label, those to offer later, and boxes
    filled by a label."""
    usable = distinct([b for b in boxes if b.ratio() <= MAX_RATIO], lines.tol)
    groups = _groups(usable, lines, tests.barcode)
    cells = {member for group in groups.values() for member in group}
    usable = _not_across(usable, groups, cells)
    found = Frames()
    later, demoted = _sort_nested(usable, set(groups), cells, lines, tests, found)
    _offer(found, [b for b in usable if tests.candidate(b)], later | set(groups), demoted)
    return found


def _offer(found: Frames, candidates: list, later: set, left_out: set) -> None:
    offered = [b for b in candidates if b not in left_out]
    found.later = [b for b in offered if b in later]
    found.main = [b for b in offered if b not in later]


def _not_across(usable: list, groups: dict, cells: set) -> list:
    """Without the frames across a cell of a container."""
    kept = set(groups) | cells
    return [b for b in usable if b in kept or not _crosses(b, groups)]


def _sort_nested(usable, containers, in_group, lines: Lines, tests: Tests, found: Frames):
    """The frames to offer later and those not at all, by the frames they
    lie in directly (a section of a label in a box is a part of the label,
    not of the box); the cells of a container stay."""
    later: set = set()
    demoted: set = set()
    for box in usable:
        if box in in_group:
            continue
        around = [o for o in usable if inside(box, o)]
        for outer in around:
            direct = not any(inside(o, outer) for o in around)
            if direct and outer not in containers:
                _nested(box, outer, lines, found, (later, demoted), tests)
    return later, demoted
