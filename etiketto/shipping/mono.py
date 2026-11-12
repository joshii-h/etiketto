"""A page area in black and white for a printer that has no grey.

A scan or a small image of a label is grey at the edges of bars and
letters, often with grey paper and grey ink. Dithering that grey turns the
edges of bars into a ragged dot pattern that scanners misread; a fixed
threshold loses grey text and turns a shaded band into solid black. So the
area is judged tile by tile:

- The image's own black and white are estimated (the darker dark pixels
  and the median light one, split by Otsu's method), not assumed to be 0
  and 255: a scan's ink may be 60, its paper 230.
- A grey pixel is an edge if black and white are both near it; how near
  grows with the enlargement, since an enlarged edge is wider.
- A tile with edges only (bars, black text) is thresholded at the middle
  of its own black and white (the image's, if the tile's darkest is no
  black: a tile of mostly light grey would else turn black).
- A tile with grey of its own (a grey logo, grey text down to light grey,
  a shaded band) keeps that grey, which the printer dithers; its paper
  stays white, its ink black, and grey right beside black becomes paper,
  so black text on a dark band stays readable.
- Rows crossing many bars over at least a tile's height (a barcode, not a
  line of text) are always thresholded, even when thin bars of a blurred
  scan never reach full black.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageChops

DARK_SHARE = 0.1  # black: the level the darkest tenth of the dark pixels reach,
DARKEST_SHARE = 0.005  # or the darkest half percent of all, if darker
MIN_RANGE = 64  # less difference between black and white: nothing to tell apart
EDGE_SHARE = 0.35  # within this part of the range from black (white): near it
OWN_GREY = 0.02  # a tile with more grey pixels of its own than this keeps them
TILE_SHARE = 1 / 32  # tile side: this part of the area's shorter side ...
MIN_TILE = 24  # ... at least this many pixels
BARCODE_RUNS = 6  # a row (over five tiles) crossing this many dark runs: a barcode
MAX_NEAR = 15  # the largest neighbourhood (pixels)
PAPER_SHARE = 0.12  # lighter than this part of the range below the paper: paper
LOCAL_DARK = 0.25  # a tile's black this part of the range above the image's: grey
HALO = 5  # grey within two pixels of black becomes paper


def levels(grey: Image.Image) -> tuple[int, int]:
    """The image's black and white: of the pixels below Otsu's threshold
    the darker ones (DARK_SHARE: grey areas larger than the black ones do
    not count as black; the darkest of all if darker still), of those above
    it the median (the paper)."""
    histogram = grey.histogram()
    split = otsu(histogram)
    # black text beside large dark grey areas: the darkest few pixels count
    darkest = _share(histogram, 0, 256, DARKEST_SHARE)
    dark = min(darkest, _share(histogram, 0, split, DARK_SHARE))
    return dark, _share(histogram, split, 256, 0.5)


def otsu(histogram: list[int]) -> int:
    """The level that separates dark from light best (Otsu's method)."""
    total = sum(histogram)
    weighted = sum(i * n for i, n in enumerate(histogram))
    best, split, count, below = -1.0, 128, 0, 0
    for level in range(256):
        count += histogram[level]
        below += level * histogram[level]
        if count == 0 or count == total:
            continue
        mean_low, mean_high = below / count, (weighted - below) / (total - count)
        spread = count * (total - count) * (mean_low - mean_high) ** 2
        if spread > best:
            best, split = spread, level + 1
    return split


def _share(histogram: list[int], start: int, end: int, share: float) -> int:
    """The level below which share of the pixels from start to end lie."""
    part = histogram[start:end]
    wanted, seen = sum(part) * share, 0
    for offset, n in enumerate(part):
        seen += n
        if n and seen >= wanted:
            return start + offset
    return (start + end) // 2


def neighbourhood(scale: float) -> int:
    """The side of the square around a pixel that its edge may span: one
    source pixel and a half on each side, enlarged (odd, 3 to MAX_NEAR)."""
    radius = math.ceil(1.5 * max(1.0, scale))
    return min(MAX_NEAR, 2 * radius + 1)


@dataclass(frozen=True)
class _Image:
    """The grey area and what is known about it."""

    grey: Image.Image
    dark: int
    light: int
    tile: int

    @property
    def middle(self) -> float:
        return (self.dark + self.light) / 2

    @property
    def span(self) -> int:
        return self.light - self.dark


def _shifted(image: Image.Image, dx: int, dy: int, fill: int) -> Image.Image:
    """The image moved by (dx, dy), fill where nothing came."""
    moved = Image.new("L", image.size, fill)
    moved.paste(image, (dx, dy))
    return moved


def spread(image: Image.Image, darkest: bool, size: int) -> Image.Image:
    """The darkest (or lightest) pixel in the size × size square around
    each pixel, like a min (max) filter, in a few whole-image steps: per
    direction the windows double (1, 2, 4 …), then two overlapping ones
    cover the square. Outside the image counts as neither."""
    if size < 2:
        return image
    pick = ImageChops.darker if darkest else ImageChops.lighter
    fill = 255 if darkest else 0
    radius = (size - 1) // 2
    for axis in (0, 1):
        windows, span = image, 1  # windows: over span pixels from each one on
        while span * 2 <= size:
            step = (-span, 0) if axis == 0 else (0, -span)
            windows = pick(windows, _shifted(windows, *step, fill))
            span *= 2
        first = (radius, 0) if axis == 0 else (0, radius)
        rest = radius - (size - span)
        second = (rest, 0) if axis == 0 else (0, rest)
        image = pick(_shifted(windows, *first, fill), _shifted(windows, *second, fill))
    return image


def black_and_white(grey: Image.Image, scale: float = 1.0) -> Image.Image:
    """The grey area (L) for a black and white printer: thresholded where
    it is black and white but for edges, grey left where it has grey of
    its own. scale: printer dots per source pixel (enlargement)."""
    dark, light = levels(grey)
    if light - dark < MIN_RANGE:
        return grey.point(lambda v: 255 if v >= 128 else 0)
    edge = (light - dark) * EDGE_SHARE
    low, high = dark + edge, light - edge
    near = neighbourhood(scale)
    near_dark = spread(grey, True, near).point(lambda v: 255 if v <= low else 0)
    near_light = spread(grey, False, near).point(lambda v: 255 if v >= high else 0)
    # grey of its own: from just above black to just below the paper, so
    # that light grey text is kept, with no black and white around it
    lightest = light - (light - dark) * PAPER_SHARE
    between = grey.point(lambda v: 255 if low < v < lightest else 0)
    own = ImageChops.subtract(between, ImageChops.darker(near_dark, near_light))
    # grey right beside black (a band behind black text) becomes paper, so
    # that the dots of the grey do not run into the letters
    beside_black = spread(grey, True, HALO).point(lambda v: 255 if v <= low else 0)
    keep = ImageChops.subtract(own, beside_black)
    halo = ImageChops.darker(between, beside_black)
    area = _Image(grey, dark, light, max(MIN_TILE, round(min(grey.size) * TILE_SHARE)))
    bars = _BarcodeRows(area)
    result = Image.new("L", grey.size)
    for top in range(0, grey.height, area.tile):
        for left in range(0, grey.width, area.tile):
            box = (left, top, min(grey.width, left + area.tile), min(grey.height, top + area.tile))
            _tile(area, (keep, halo), result, box, bars)
    return result


class _BarcodeRows:
    """The rows through a barcode, per column of tiles: rows crossing many
    dark runs (looked at over five tiles: one tile may hold only a few
    bars), in runs of at least a tile's height (a line of text is lower)."""

    def __init__(self, area: _Image):
        self.area = area
        self._columns: dict[int, set[int]] = {}

    def at(self, left: int) -> set[int]:
        if left not in self._columns:
            area, tile = self.area, self.area.tile
            strip = area.grey.crop(
                (
                    max(0, left - 2 * tile),
                    0,
                    min(area.grey.width, left + 3 * tile),
                    area.grey.height,
                )
            )
            self._columns[left] = _tall(barcode_rows(strip, area.middle), tile)
        return self._columns[left]


def _tall(rows: list[int], height: int) -> set[int]:
    """The rows in runs of at least height consecutive rows."""
    found: set[int] = set()
    run: list[int] = []
    for row in [*rows, -2]:
        if run and row != run[-1] + 1:
            if len(run) >= height:
                found.update(run)
            run = []
        run.append(row)
    return found


def _tile(area: _Image, masks, result, box, bars: _BarcodeRows) -> None:
    """Threshold the tile; a tile with grey of its own keeps that grey
    (only that: paper stays white, ink and edges black), but not in the
    rows that cross a barcode."""
    left, top, right, bottom = box
    part = area.grey.crop(box)
    cut = _local_middle(part, area)
    result.paste(part.point(lambda v, c=cut: 255 if v >= c else 0), (left, top))
    keep, halo = (mask.crop(box) for mask in masks)
    if keep.histogram()[255] <= OWN_GREY * (right - left) * (bottom - top):
        return
    for row in bars.at(left):
        if top <= row < bottom:
            for mask in (keep, halo):
                mask.paste(0, (0, row - top, mask.width, row - top + 1))
    result.paste(part, (left, top), keep)
    result.paste(255, (left, top), halo)


def barcode_rows(part: Image.Image, middle: float) -> list[int]:
    """The rows that cross many dark runs: through a barcode."""
    binary = part.point(lambda v: 0 if v < middle else 255).tobytes()
    width = part.width
    return [
        row
        for row in range(part.height)
        if len([r for r in binary[row * width : (row + 1) * width].split(b"\xff") if r])
        >= BARCODE_RUNS
    ]


def _local_middle(part: Image.Image, area: _Image) -> float:
    """The middle of the tile's own black and white where the tile has
    black as dark as the image's and white (uneven light on a scan), else
    the image's: a tile of mostly grey would split grey from white and
    turn the grey black."""
    low, high = (float(v) for v in part.getextrema())  # type: ignore[arg-type]  # one band
    if high - low < area.span / 2:
        return area.middle
    tile_dark, tile_light = levels(part)
    if tile_dark - area.dark > LOCAL_DARK * area.span:
        return area.middle
    return (tile_dark + tile_light) / 2
