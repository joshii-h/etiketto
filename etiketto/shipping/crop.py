"""Moving and resizing the selected area with the pointer.

Everything is in fractions of the page (0 to 1), so the same code works for
any page size and zoom. A grab is one of the handles: "move" (inside),
an edge ("n", "s", "e", "w"), a corner ("nw", "ne", "sw", "se"), or "new"
(outside: a new area is drawn from the starting point).
"""

from __future__ import annotations

Crop = tuple[float, float, float, float]  # x, y, width, height
MIN_SIDE = 0.02  # the smallest area: 2 % of the page in each direction


def handle_at(crop: Crop, x: float, y: float, tol_x: float, tol_y: float) -> str:
    """Which handle of the area lies at (x, y); tolerances in fractions."""
    left, top, width, height = crop
    right, bottom = left + width, top + height
    inside_x = left - tol_x <= x <= right + tol_x
    inside_y = top - tol_y <= y <= bottom + tol_y
    if not (inside_x and inside_y):
        return "new"
    vertical = "n" if abs(y - top) <= tol_y else "s" if abs(y - bottom) <= tol_y else ""
    horizontal = "w" if abs(x - left) <= tol_x else "e" if abs(x - right) <= tol_x else ""
    return (vertical + horizontal) or "move"


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _moved(crop: Crop, dx: float, dy: float) -> Crop:
    x, y, w, h = crop
    return (_clamp(x + dx, 0, 1 - w), _clamp(y + dy, 0, 1 - h), w, h)


def _sides(crop: Crop, handle: str, dx: float, dy: float) -> Crop:
    """Move the edges named in the handle; the opposite edges stay."""
    left, top, width, height = crop
    right, bottom = left + width, top + height
    if "w" in handle:
        left = _clamp(left + dx, 0, right - MIN_SIDE)
    if "e" in handle:
        right = _clamp(right + dx, left + MIN_SIDE, 1)
    if "n" in handle:
        top = _clamp(top + dy, 0, bottom - MIN_SIDE)
    if "s" in handle:
        bottom = _clamp(bottom + dy, top + MIN_SIDE, 1)
    return (left, top, right - left, bottom - top)


def _drawn(start: tuple[float, float], dx: float, dy: float) -> Crop:
    x0, y0 = start
    x1, y1 = _clamp(x0 + dx, 0, 1), _clamp(y0 + dy, 0, 1)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    width, height = max(MIN_SIDE, right - left), max(MIN_SIDE, bottom - top)
    return (min(left, 1 - width), min(top, 1 - height), width, height)


def dragged(crop: Crop, handle: str, start: tuple[float, float], dx: float, dy: float) -> Crop:
    """The area after dragging handle from start by (dx, dy)."""
    if handle == "move":
        return _moved(crop, dx, dy)
    if handle == "new":
        return _drawn(start, dx, dy)
    return _sides(crop, handle, dx, dy)
