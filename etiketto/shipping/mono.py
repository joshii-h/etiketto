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
  of its own black and white; a tile with grey of its own (a grey logo,
  grey text, a shaded band) keeps its grey, which the printer dithers.
- Rows crossing many bars (a barcode) are always thresholded, even when
  thin bars of a blurred scan never reach full black.
"""

from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageFilter

DARK_SHARE = 0.1  # black: the level the darkest tenth of the dark pixels reach
MIN_RANGE = 64  # less difference between black and white: nothing to tell apart
EDGE_SHARE = 0.35  # within this part of the range from black (white): near it
OWN_GREY = 0.02  # a tile with more grey pixels of its own than this keeps them
TILE_SHARE = 1 / 32  # tile side: this part of the area's shorter side ...
MIN_TILE = 24  # ... at least this many pixels
BARCODE_RUNS = 6  # a row (over five tiles) crossing this many dark runs: a barcode
MAX_NEAR = 15  # the largest neighbourhood (pixels)


def levels(grey: Image.Image) -> tuple[int, int]:
    """The image's black and white: of the pixels below Otsu's threshold
    the darker ones (DARK_SHARE: grey areas larger than the black ones do
    not count as black), of those above it the median (the paper)."""
    histogram = grey.histogram()
    split = otsu(histogram)
    return _share(histogram, 0, split, DARK_SHARE), _share(histogram, split, 256, 0.5)


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


def black_and_white(grey: Image.Image, scale: float = 1.0) -> Image.Image:
    """The grey area (L) for a black and white printer: thresholded where
    it is black and white but for edges, grey left where it has grey of
    its own. scale: printer dots per source pixel (enlargement)."""
    dark, light = levels(grey)
    if light - dark < MIN_RANGE:
        return grey.point(lambda v: 255 if v >= 128 else 0)
    span = (light - dark) * EDGE_SHARE
    low, high = dark + span, light - span
    near = neighbourhood(scale)
    near_dark = grey.filter(ImageFilter.MinFilter(near)).point(lambda v: 255 if v <= low else 0)
    near_light = grey.filter(ImageFilter.MaxFilter(near)).point(lambda v: 255 if v >= high else 0)
    between = grey.point(lambda v: 255 if low < v < high else 0)
    own = ImageChops.subtract(between, ImageChops.darker(near_dark, near_light))
    tile = max(MIN_TILE, round(min(grey.size) * TILE_SHARE))
    result = grey.copy()
    middle = (dark + light) / 2
    for top in range(0, grey.height, tile):
        for left in range(0, grey.width, tile):
            box = (left, top, min(grey.width, left + tile), min(grey.height, top + tile))
            _tile(grey, own, result, box, tile, middle, light - dark)
    return result


def _tile(grey, own, result, box, tile: int, middle: float, span: int) -> None:
    """Threshold the tile, or the rows of it that cross a barcode, unless
    it has grey of its own."""
    left, top, right, bottom = box
    part = grey.crop(box)
    cut = _local_middle(part, middle, span)
    pixels = (right - left) * (bottom - top)
    if own.crop(box).histogram()[255] > OWN_GREY * pixels:
        # the rows of a barcode are looked at over five tiles: one tile
        # may hold only a few of its bars
        wide = grey.crop((max(0, left - 2 * tile), top, min(grey.width, right + 2 * tile), bottom))
        rows = barcode_rows(wide, middle)
        if len(rows) < part.height:
            for row in rows:
                line = part.crop((0, row, part.width, row + 1))
                result.paste(line.point(lambda v, c=cut: 255 if v >= c else 0), (left, top + row))
            return
    result.paste(part.point(lambda v, c=cut: 255 if v >= c else 0), (left, top))


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


def _local_middle(part: Image.Image, middle: float, span: int) -> float:
    """The middle of the tile's own black and white where the tile has
    both (uneven light on a scan), else the image's."""
    low, high = (float(v) for v in part.getextrema())  # type: ignore[arg-type]  # one band
    if high - low < span / 2:
        return middle
    tile_dark, tile_light = levels(part)
    return (tile_dark + tile_light) / 2
