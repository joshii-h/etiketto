"""The pages of a label sheet job: which label goes to which position, and
the pages themselves at the printer's resolution.

Each position gets its own label image from a source (label_at(n) for the
n-th label of the job), so that a mail merge can fill every label with
another row; printing copies of one label is the source that always
returns the same image. Pages are composed one at a time, when the printer
backend asks for them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import overload

from PIL import Image, ImageDraw

from etiketto.sheets import Sheet

MAX_LABELS = 999  # one job; also keeps the number of pages sane
OUTLINE_MM = 0.2  # line width of the outlines for a test print
OUTLINE_GREY = (128, 128, 128)  # the pages are RGB: grey in every channel
MARGIN_SLACK_MM = 0.05  # rounding in the printer's hundredths of mm

# label_at(n): the n-th label of the job (0-based), rendered at the page's
# resolution in the label's size (turned on its side when the job is
# rotated: the image is then as wide as the label is high)
LabelSource = Callable[[int], Image.Image]


class Copies:
    """A label source: the same label at every position. Can render it
    again at another resolution if the image says how
    (info["render_at"](dpi), as the system printer's prepare() makes it)."""

    def __init__(self, image: Image.Image):
        self.image = image

    def __call__(self, _n: int) -> Image.Image:
        return self.image

    def at_dpi(self, dpi: float) -> Copies | None:
        render_at = self.image.info.get("render_at")
        return Copies(render_at(dpi)) if callable(render_at) else None


def px(mm: float, dpi: float) -> int:
    return round(mm / 25.4 * dpi)


def first_positions(sheet: Sheet, positions: Iterable[int]) -> list[int]:
    """The usable positions of a choice, sorted; all of them if none is left."""
    chosen = sorted({p for p in positions if 0 <= p < sheet.count})
    return chosen or list(range(sheet.count))


def page_slots(sheet: Sheet, count: int, positions: Iterable[int]) -> list[list[int]]:
    """The positions to fill, page by page, for count labels: the chosen
    positions on the first sheet (a sheet that is partly used already),
    every position on the sheets after it."""
    every = list(range(sheet.count))
    slots, pages, left = first_positions(sheet, positions), [], max(0, min(count, MAX_LABELS))
    while left > 0:
        page = slots[:left]
        pages.append(page)
        left -= len(page)
        slots = every
    return pages


@dataclass(frozen=True)
class SheetJob:
    sheet: Sheet
    count: int  # labels to print
    positions: tuple[int, ...] = ()  # on the first sheet; empty: all of them
    rotated: bool = False  # the label is designed turned: placed turned back
    outlines: bool = False  # draw the label outlines (a test on plain paper)

    def slots(self) -> list[list[int]]:
        return page_slots(self.sheet, self.count, self.positions)


class Pages(Sequence[Image.Image]):
    """The pages of a job, each composed when it is asked for, so that only
    one page at a time is in memory (the last one is kept: the printer
    backend looks at the first page before it sends them all)."""

    def __init__(self, job: SheetJob, label_at: LabelSource, dpi: float):
        self.job, self.label_at, self.dpi = job, label_at, dpi
        self._slots = job.slots()
        self._first = [sum(len(s) for s in self._slots[:i]) for i in range(len(self._slots))]
        self._last: tuple[int, Image.Image] | None = None

    def __len__(self) -> int:
        return len(self._slots)

    def at_dpi(self, dpi: float) -> Pages | None:
        """The same pages composed at another resolution, the labels
        rendered at it too; None if the label source cannot render again."""
        at_dpi = getattr(self.label_at, "at_dpi", None)
        source = at_dpi(dpi) if at_dpi is not None else None
        return Pages(self.job, source, dpi) if source is not None else None

    @overload
    def __getitem__(self, index: int) -> Image.Image: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Image.Image]: ...

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        slots = self._slots[index]  # IndexError ends iteration
        index %= len(self)
        if self._last and self._last[0] == index:
            return self._last[1]
        n = self._first[index]
        placed = [(slot, self.label_at(n + i)) for i, slot in enumerate(slots)]
        job = self.job
        page = compose_page(job.sheet, placed, self.dpi, job.outlines, job.rotated)
        self._last = (index, page)
        return page


def compose_page(
    sheet: Sheet,
    placed: Sequence[tuple[int, Image.Image]],
    dpi: float,
    outlines: bool = False,
    rotated: bool = False,
) -> Image.Image:
    """One page: each (position, label image) at its place, exact to the
    pixel at dpi; round labels and CDs clipped to their shape. The page's
    size in mm travels with it (info["size_mm"])."""
    page = Image.new("RGB", (px(sheet.paper[0], dpi), px(sheet.paper[1], dpi)), "white")
    masks: dict[tuple[int, int], Image.Image | None] = {}
    for position, label in placed:
        image = label.rotate(90, expand=True) if rotated else label
        size = (px(sheet.width, dpi), px(sheet.height, dpi))
        if abs(image.width - size[0]) > 2 or abs(image.height - size[1]) > 2:
            image = image.resize(size, Image.Resampling.LANCZOS)  # another resolution
        if image.size not in masks:
            masks[image.size] = shape_mask(sheet, image.size)
        left, top, _right, _bottom = sheet.rect(position)
        page.paste(image.convert("RGB"), (px(left, dpi), px(top, dpi)), masks[image.size])
    if outlines:
        draw_outlines(page, sheet, dpi)
    page.info["size_mm"] = sheet.paper
    return page


def shape_mask(sheet: Sheet, size: tuple[int, int]) -> Image.Image | None:
    """Where a label of this sheet is, in a box of size pixels: None for
    rectangles (printed to the edge, a little beyond the corners does no
    harm), the ellipse for round labels, the disc without its hole for CDs."""
    if sheet.shape == "rectangle":
        return None
    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    if sheet.shape != "cd":
        draw.ellipse((0, 0, width - 1, height - 1), fill=255)
        return mask
    scale = width / sheet.width
    _circle(draw, width / 2, height / 2, sheet.disc * scale / 2, 255)
    if sheet.hole:
        _circle(draw, width / 2, height / 2, sheet.hole * scale / 2, 0)
    return mask


def _circle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill: int) -> None:
    draw.ellipse((cx - r, cy - r, cx + r - 1, cy + r - 1), fill=fill)


def draw_outlines(page: Image.Image, sheet: Sheet, dpi: float) -> None:
    """The outline of every label, as thin grey lines inside the label."""
    draw = ImageDraw.Draw(page)
    line = max(1, px(OUTLINE_MM, dpi))
    for index in range(sheet.count):
        left, top, right, bottom = (px(v, dpi) for v in sheet.rect(index))
        box = (left, top, right - 1, bottom - 1)
        if sheet.shape == "rectangle":
            radius = px(sheet.corner, dpi)
            draw.rounded_rectangle(box, radius, outline=OUTLINE_GREY, width=line)
        elif sheet.shape != "cd":
            draw.ellipse(box, outline=OUTLINE_GREY, width=line)
        else:
            _cd_outline(draw, sheet, box, dpi, line)


def _cd_outline(draw, sheet: Sheet, box, dpi: float, line: int) -> None:
    left, top, right, bottom = box
    cx, cy = (left + right + 1) / 2, (top + bottom + 1) / 2
    for diameter in (sheet.disc, sheet.hole):
        if diameter:
            r = px(diameter, dpi) / 2
            draw.ellipse(
                (cx - r, cy - r, cx + r - 1, cy + r - 1), outline=OUTLINE_GREY, width=line
            )
    if sheet.diameter:  # a business-card CD: its straight edges too
        draw.rectangle(box, outline=OUTLINE_GREY, width=line)


def clip_preview(image: Image.Image, sheet: Sheet, rotated: bool = False) -> Image.Image:
    """The label as it looks on the sheet: outside a round label's (or a
    CD's) shape white. image is the label as designed (turned if rotated)."""
    size = (image.height, image.width) if rotated else image.size
    mask = shape_mask(sheet, size)
    if mask is None:
        return image
    if rotated:
        mask = mask.rotate(-90, expand=True)
    blank = Image.new(image.mode, image.size, "white")
    return Image.composite(image, blank, mask)


def in_margins(
    sheet: Sheet,
    positions: Iterable[int],
    margins: tuple[float, float, float, float],
    inset_mm: float = 0.0,
) -> list[int]:
    """The positions whose content (the label inset by the safety margin)
    reaches into the paper's unprintable margins (top, right, bottom,
    left in mm): there it would be cut off."""
    top, right, bottom, left = (m - MARGIN_SLACK_MM for m in margins)
    width, height = sheet.paper
    found = []
    for index in sorted(set(positions)):
        if not 0 <= index < sheet.count:
            continue
        x0, y0, x1, y1 = sheet.rect(index)
        if (
            x0 + inset_mm < left
            or y0 + inset_mm < top
            or x1 - inset_mm > width - right
            or y1 - inset_mm > height - bottom
        ):
            found.append(index)
    return found
