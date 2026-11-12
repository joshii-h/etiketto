"""Render a Label to a Pillow image at printer resolution.

Label space: x runs along the tape (the label length), y across the tape.
The preview shows this image as is; for printing it is rotated so that the
image width matches the tape width, which is what the printer expects.
"""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache, partial
from types import MappingProxyType
from typing import NamedTuple

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from etiketto import codes, imagefx, shapes, typeset
from etiketto.codes import CodeError
from etiketto.model import (
    CodeElement,
    Element,
    ImageElement,
    Label,
    PageElement,
    ShapeElement,
    SymbolElement,
    TextElement,
)
from etiketto.symbols import VIEW_BOX, find, segments, trace

# Resolution of the vivid print mode (labelprinter: 317 lpi)
PRINT_DPI = 317

# Default safety margin: content and border keep this distance from the
# edges, the background is always printed edge to edge. Measured on a
# VC-500W: ~0.5 mm lost per side plus up to ~1 mm feed skew on long labels.
# Devices and rolls vary, so the app makes it a per-printer setting.
DEFAULT_SAFE_MM = 1.5


class RenderWarning(NamedTuple):
    """A problem with the label; the interface turns key and params into a
    translated message (keys: image_unreadable, page_unreadable,
    pdf_missing, symbol_unknown, element_failed, no_room, fixed_too_short,
    background_unreadable, no_contrast, shape_invisible; codes that cannot be encoded: see
    etiketto.codes.CodeError; codes that may not scan or do not fit:
    code_thin, code_quiet, code_too_big)."""

    key: str
    # read-only: a NamedTuple default is one object shared by all warnings
    params: Mapping[str, object] = MappingProxyType({})


class ElementError(ValueError):
    """An element that cannot be drawn; key is its RenderWarning (a code
    that cannot be encoded raises etiketto.codes.CodeError instead)."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass
class Rendered:
    image: Image.Image  # label space, RGB
    length_mm: float
    warnings: list[RenderWarning] = field(default_factory=list)
    overflow: bool = False  # content was scaled down to fit a fixed length


def mm_to_px(mm: float, dpi: float = PRINT_DPI) -> int:
    return max(1, round(mm / 25.4 * dpi))


def px_to_mm(px: int, dpi: float = PRINT_DPI) -> float:
    return px / dpi * 25.4


_color = imagefx.rgb


@dataclass(frozen=True)
class Inks:
    """How colours are printed: as they are, or (mono) black or white
    against the brightness of the label's background (imagefx.mono_ink),
    with red kept on black-and-red printers (red)."""

    mono: bool = False
    red: bool = False
    background: float = 255.0

    def color(self, value: str) -> str:
        if not self.mono:
            return value
        return "#%02x%02x%02x" % imagefx.mono_ink(_color(value), self.background, self.red)


# --- elements ---------------------------------------------------------------


def _text_block(
    element: TextElement,
    size_px: float,
    antialias: bool = True,
    dpi: float = PRINT_DPI,
    wrap_px: int | None = None,
) -> Image.Image:
    """Lay out text with Pango (font fallback, emoji, kerning) into RGBA;
    without anti-aliasing every pixel is either ink or not (typeset.block)."""
    return typeset.block(element, size_px, dpi, antialias, wrap_px)


TEXT_PROBE = 100.0  # pixels: the size the first layout of fitted text is measured at
MAX_TEXT_PT = 300.0  # the largest size a fitted text grows to


def render_text(
    element: TextElement,
    height: int,
    dpi: float,
    mono: bool = False,
    room: int | None = None,
    inks: Inks | None = None,
) -> Image.Image:
    """The text block: at its size, or (size 0) filling the content height.
    mono: no anti-aliasing, black and white (typeset.mono_text). room: the
    space along a label of fixed length, which shrink fits the text into."""
    inks = inks or Inks(mono)
    antialias = not inks.mono
    if inks.mono:
        element = typeset.mono_text(element, inks.background, inks.red)
    wrap = mm_to_px(element.max_width_mm, dpi) if element.max_width_mm > 0 else None
    draw = partial(_text_block, element, antialias=antialias, dpi=dpi, wrap_px=wrap)
    if element.size_pt > 0 and not element.shrink:
        size = element.size_pt / 72 * dpi
    elif not _needs_fitting(element, wrap):
        block = _fill_height(draw, height)
        return _redrawable(block, draw, block.info["size"], inks.mono)
    else:
        measure = partial(typeset.size_of, element, dpi=dpi, antialias=antialias, wrap_px=wrap)
        limits = (_width_limit(element, room, wrap), height)
        size = _fitted_size(element, measure, limits, dpi)
    return _redrawable(draw(size), draw, size, inks.mono)


def _needs_fitting(element: TextElement, wrap: int | None) -> bool:
    """Whether the size is found by measuring (shrinking, wrapping,
    vertical, or a box, frame, outline or shadow that does not grow with
    the letters) instead of scaling a probe (_fill_height)."""
    if element.shrink or wrap or element.rotation in (90, 270):
        return True
    return bool(
        element.inverted
        or element.box_color
        or element.frame_mm > 0
        or element.outline_mm > 0
        or element.shadow_mm > 0
    )


def _redrawable(block: Image.Image, draw, size: float, mono: bool) -> Image.Image:
    """Black and white: a text reduced to fit a fixed length is laid out
    again at the smaller size, not resampled (see _resize)."""
    if mono:
        block.info["redraw"] = lambda _w, h: draw(size * h / max(1, block.height))
    return block


def _fill_height(draw, height: int) -> Image.Image:
    """Fit to the tape: the block height scales about linearly with font size."""
    block = draw(TEXT_PROBE)
    size = TEXT_PROBE * height / max(1, block.height)
    block = draw(size)
    while block.height > height and size > 1:
        size *= 0.97
        block = draw(size)
    block.info["size"] = size
    return block


def _width_limit(element: TextElement, room: int | None, wrap: int | None) -> int | None:
    """How wide a shrinking text may be: the room and the wrap width."""
    if not element.shrink:
        return None
    limits = [limit for limit in (room, wrap) if limit]
    return min(limits) if limits else None


def _fitted_size(element: TextElement, measure, limits, dpi: float) -> float:
    """The largest size (pixels) at which the block fits width and height
    (None: no limit), at most its own size (or MAX_TEXT_PT for size 0),
    at least min_size_pt (where it may not fit)."""
    width, height = limits
    lowest = max(1.0, element.min_size_pt / 72 * dpi)
    if element.size_pt > 0:
        highest = element.size_pt / 72 * dpi
    else:
        highest = MAX_TEXT_PT / 72 * dpi
        if element.rotation in (0, 180) or not element.max_width_mm:
            highest = min(highest, _height_estimate(measure, height))

    def fits(size: float) -> bool:
        w, h = measure(size)
        return h <= height and (width is None or w <= width)

    return _largest(fits, lowest, max(lowest, highest))


def _height_estimate(measure, height: int) -> float:
    """The size at which the unwrapped block is about as high as height
    (a bit above: an upper bound for the bisection). The height grows
    linearly with the size plus what does not grow (padding, a frame):
    two measurements give both."""
    first = measure(TEXT_PROBE, wrap_px=None)[1]
    second = measure(2 * TEXT_PROBE, wrap_px=None)[1]
    growth = max(1e-6, (second - first) / TEXT_PROBE)
    fixed = first - growth * TEXT_PROBE
    return max(1.0, (height - fixed) / growth * 1.05)


def _largest(fits, low: float, high: float) -> float:
    """The largest size in [low, high] that fits (bisection to half a
    pixel), low if none."""
    if fits(high):
        return high
    if not fits(low):
        return low
    while high - low > 0.5:
        middle = (low + high) / 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


# Decoding a large photo (base64 + EXIF + resample) takes far longer than the
# rest of a render; cache it so typing in a text field stays smooth.
Image.MAX_IMAGE_PIXELS = 80_000_000  # refuse decompression bombs early


@lru_cache(maxsize=8)
def _decoded(data: str) -> Image.Image:
    with Image.open(io.BytesIO(base64.b64decode(data))) as src:
        return ImageOps.exif_transpose(src).convert("RGBA")


@lru_cache(maxsize=16)
def _scaled(data: str, height: int) -> Image.Image:
    img = _decoded(data)
    width = max(1, round(img.width * height / img.height))
    return img.resize((width, height), Image.Resampling.LANCZOS)


# a large photo: cropped and turned once (cheap), then scaled to the label,
# then adjusted at that size, not at its own
@lru_cache(maxsize=2)
def _cut(data: str, crop: tuple, rotation: int, mirror: bool, flip: bool) -> Image.Image:
    return imagefx.turned(imagefx.cropped(_decoded(data), crop), rotation, mirror, flip)


@lru_cache(maxsize=2)
def _scaled_cut(data: str, cut: tuple, adjust: tuple, height: int) -> Image.Image:
    img = _cut(data, *cut)
    width = max(1, round(img.width * height / img.height))
    return imagefx.adjusted(img.resize((width, height), Image.Resampling.LANCZOS), *adjust)


def render_image(
    element: ImageElement, height: int, dpi: float = PRINT_DPI, mono: bool = False
) -> Image.Image:
    """The image at the content height, with its options. Black and white:
    render() converts it by its mode afterwards, at its final size, and
    only then draws corners and border (_mono_part)."""
    cut = (tuple(element.crop), element.rotation, element.mirror, element.flip)
    adjust = (element.brightness, element.contrast, element.gamma, element.invert)
    if cut == ((0.0, 0.0, 1.0, 1.0), 0, False, False) and adjust == (0.0, 0.0, 1.0, False):
        img = _scaled(element.data, height).copy()
    else:
        img = _scaled_cut(element.data, cut, adjust, height).copy()
    if mono:
        return img
    return imagefx.framed(
        img,
        shapes.px(element.corner_mm, dpi),
        shapes.px(element.border_mm, dpi),
        _color(element.border_color),
    )


def check_code(element: CodeElement) -> None:
    """Raise CodeError if the content cannot be encoded (the libraries would
    silently drop characters or replace a wrong check digit)."""
    codes.check(element)


# A barcode's design, in mm at its designed size; the element's height
# scales all of it (and so gives its width). Each module is then drawn a
# whole number of device pixels wide (see _module): a printer gets every bar
# and space exactly, no resampling ever touches a code. The quiet zone the
# symbology asks for (codes.SPECS) is part of the element.
BAR_MODULE_MM = 0.3  # narrowest bar or space
BAR_TOP_MM = 1.0  # above the bars
BAR_HEIGHT_MM = 10.0
BAR_TEXT_GAP_MM = 0.3  # between the bars and the human-readable text (its ink)
BAR_TEXT_PT = 8.0
BAR_TOTAL_MM = 16.3  # the whole element: the text below the bars, then space
EAN_GUARD_MODULES = 5  # EAN/UPC guard bars reach this much below the others
# quiet zones in modules (left, right; 2D codes on every side)
QUIET = {symbology: spec.quiet for symbology, spec in codes.SPECS.items()}
# EAN/UPC (GS1 General Specifications): the guard bars (modules, longer than
# the others; UPC-A: with the first and last digit's bars) and where the
# digits go: under the modules (first, after last), or "left"/"right" of the
# bars, in the quiet zone
EAN_LAYOUTS = {
    "ean13": (
        ((0, 3), (45, 50), (92, 95)),
        ((0, 1, "left"), (1, 7, (3, 45)), (7, 13, (50, 92))),
    ),
    "ean8": (((0, 3), (31, 36), (64, 67)), ((0, 4, (3, 31)), (4, 8, (36, 64)))),
    "upca": (
        ((0, 10), (45, 50), (85, 95)),
        ((0, 1, "left"), (1, 6, (10, 45)), (6, 11, (50, 85)), (11, 12, "right")),
    ),
    "upce": (((0, 3), (45, 51)), ((0, 1, "left"), (1, 7, (3, 45)), (7, 8, "right"))),
}
EAN_GUARDS = EAN_LAYOUTS["ean13"][0]
THIN_MODULE = 2  # pixels: a code with narrower modules may not scan (warning code_thin)
PROBLEMS = ("code_too_big", "code_quiet", "code_thin")  # in this order, each once


@dataclass(frozen=True)
class _Fit:
    """How a code fits in its box: the module (pixels), where its first
    module starts (pixels from the box's start, after the quiet zone) and
    what is wrong, as warning keys."""

    module: int
    start: int
    problems: tuple[str, ...]


def _module(designed: float, modules: int, quiet: tuple[int, int], box: int) -> _Fit:
    """How modules of a code plus their quiet zone (modules left and right)
    fit in box pixels. The module: the designed width (pixels) rounded to
    the nearest if the code and its quiet zone fit in the box with it, else
    the widest whole module with which they fit (box // all modules), at
    least 1. Where it starts and what is wrong: see _fit_at()."""
    total = modules + sum(quiet)
    module = max(1, round(designed))
    if module * total > box:
        module = max(1, box // total)
    return _fit_at(module, modules, quiet, box)


def _fit_at(
    module: int, modules: int, quiet: tuple[int, int], box: int, thin: int = THIN_MODULE
) -> _Fit:
    """The start (the first module, in pixels): the code and its quiet zone
    centred in the box; if the quiet zone does not fit, the code alone
    centred; if not even that fits, 0 (the rest is cut off).
    Problems: code_too_big (not even the modules fit), code_quiet (they
    fit, their quiet zone does not), code_thin (1-pixel modules)."""
    total = modules + sum(quiet)
    problems = []
    if module * modules > box:
        problems.append("code_too_big")
    elif module * total > box:
        problems.append("code_quiet")
    if module < thin:
        problems.append("code_thin")
    if module * total <= box:
        start = (box - module * total) // 2 + quiet[0] * module
    else:
        start = max(0, (box - module * modules) // 2)
    return _Fit(module, start, tuple(problems))


def _problems(*fits: _Fit) -> tuple[str, ...]:
    found = {problem for fit in fits for problem in fit.problems}
    if "code_too_big" in found:
        found.discard("code_quiet")  # it does not fit at all: that says it
    return tuple(problem for problem in PROBLEMS if problem in found)


def code_problems(part: Image.Image) -> tuple[str, ...]:
    """What is wrong with a code drawn by render_code (warning keys)."""
    return getattr(part, "code_problems", ())


def _dark(element: CodeElement, mask: Image.Image, fit: _Fit) -> Image.Image:
    """The code's colour where mask is 255, transparent elsewhere: 1 bit.
    Its fit goes along as attributes of the image, not in its info (which
    an image file brings along too)."""
    img = Image.new("RGBA", mask.size, (*_color(element.color), 0))
    img.putalpha(mask.point(lambda v: 255 if v >= 128 else 0))
    img.code_module = fit.module  # type: ignore[attr-defined]
    img.code_problems = fit.problems  # type: ignore[attr-defined]
    return img


def _matrix(
    element: CodeElement,
    symbol: codes.Symbol,
    spec: codes.Spec,
    height: int,
    width: int | None,
    step: int = 1,
) -> Image.Image:
    """A 2D code: square modules of a whole number of pixels with their
    quiet zone on every side, centred. A square code (QR, Data Matrix,
    Aztec) in height x height pixels (or width x width, if less); PDF417
    height pixels high, as wide as its modules (or width pixels). `step`:
    the image is reduced across (in height) by this factor later: modules
    and the first row are multiples of it (code_row0), so they stay whole."""
    quiet = (spec.quiet[0], spec.quiet[0])
    tall, wide = len(symbol.rows) * spec.row_modules, symbol.width
    if spec.layout == "matrix":
        height = width = height if width is None else min(height, width)
    module = _module(height / (tall + sum(quiet)), tall, quiet, height).module
    if width is None:
        width = (wide + sum(quiet)) * module
    else:
        module = min(module, _module(width / (wide + sum(quiet)), wide, quiet, width).module)
    module = max(step, module // step * step)
    thin = THIN_MODULE * step
    across = _fit_at(module, wide, quiet, width, thin)
    down = _fit_at(module, tall, quiet, height, thin)
    mask = Image.new(
        "L",
        (max(width, across.start + wide * module), max(height, down.start + tall * module)),
        0,
    )
    draw = ImageDraw.Draw(mask)
    row_px = spec.row_modules * module
    for index, row in enumerate(symbol.rows):
        top = down.start + index * row_px
        for start, end in _runs(row):
            x0, x1 = across.start + start * module, across.start + end * module
            draw.rectangle((x0, top, x1 - 1, top + row_px - 1), 255)
    img = _dark(element, mask, _Fit(module, across.start, _problems(across, down)))
    img.code_row0 = down.start  # type: ignore[attr-defined]
    return img


def _ink(text: str, size_px: float) -> Image.Image | None:
    """A line of the human-readable text without anti-aliasing (1 bit),
    cropped to its ink; None if it cannot be laid out."""
    try:
        block = _text_block(TextElement(text=text, font="Monospace"), size_px, antialias=False)
    except Exception:  # noqa: BLE001 - the bars alone still scan
        return None
    alpha = block.getchannel("A")
    box = alpha.getbbox()
    return alpha.crop(box) if box else None


def _text_in(text: str, size_px: float, width: int) -> Image.Image | None:
    """The text at size_px, smaller if it would be wider than width."""
    ink = _ink(text, size_px)
    if ink is not None and ink.width > width > 0:
        ink = _ink(text, size_px * width / ink.width)
    return ink


def _linear_pattern(symbol: codes.Symbol, spec: codes.Spec) -> tuple[str, tuple[int, int]]:
    """The modules to draw in a row and the quiet zone outside them. ITF-14:
    its quiet zones lie inside the bearer bars, so bearers and quiet zones
    are part of the drawn modules."""
    pattern = symbol.rows[0]
    if spec.layout != "itf14":
        return pattern, spec.quiet
    bearer, (left, right) = "1" * codes.ITF14_BEARER, spec.quiet
    return bearer + "0" * left + pattern + "0" * right + bearer, (0, 0)


def _barcode(
    element: CodeElement, symbol: codes.Symbol, spec: codes.Spec, height: int, width: int | None
) -> Image.Image:
    """Bars of whole-pixel modules with their quiet zones, centred in the
    element, the text below them."""
    pattern, quiet = _linear_pattern(symbol, spec)
    designed_modules = sum(quiet) + len(pattern)
    scale = height / BAR_TOTAL_MM  # pixels per designed mm
    if width is None:  # as wide as designed, and as the code at 1-pixel modules
        width = max(designed_modules, round(designed_modules * BAR_MODULE_MM * scale))
    fit = _module(width / designed_modules, len(pattern), quiet, width)
    module, left = fit.module, fit.start
    top = round(BAR_TOP_MM * scale)
    bars_h = max(1, round(BAR_HEIGHT_MM * scale))
    mask = Image.new("L", (max(width, left + module * len(pattern)), height), 0)
    draw = ImageDraw.Draw(mask)
    guards = EAN_LAYOUTS[spec.layout][0] if spec.layout in EAN_LAYOUTS else ()
    for start, end in _runs(pattern):
        long = any(a <= start < b for a, b in guards)
        bottom = top + bars_h + (EAN_GUARD_MODULES * module if long else 0)
        draw.rectangle((left + start * module, top, left + end * module - 1, bottom - 1), 255)
    if spec.layout == "itf14":  # the bearer bars above and below
        thick = codes.ITF14_BEARER * module
        right = left + len(pattern) * module - 1
        draw.rectangle((left, top, right, top + thick - 1), 255)
        draw.rectangle((left, top + bars_h - thick, right, top + bars_h - 1), 255)
    y = top + bars_h + round(BAR_TEXT_GAP_MM * scale)
    size = BAR_TEXT_PT / 72 * 25.4 * scale
    if spec.layout in EAN_LAYOUTS:
        _ean_text(mask, symbol.text, spec, size, (left, module, len(pattern)), y)
    else:
        text = _text_in(symbol.text, size, mask.width)
        if text is not None:
            _paste_text(mask, text, left + (module * len(pattern) - text.width) // 2, y)
    return _dark(element, mask, fit)


def _ean_text(mask: Image.Image, digits: str, spec: codes.Spec, size: float, bars, y: int):
    """EAN/UPC digits as the GS1 specification places them: between the
    guard bars, the first (and UPC's last) in the quiet zone beside them.
    bars: (the first module's pixel, the module, the number of modules)."""
    left, module, count = bars
    places: list[tuple[str, str | tuple[int, int], int]] = []
    for first, end, place in EAN_LAYOUTS[spec.layout][1]:
        if isinstance(place, str):  # beside the bars, in the quiet zone
            quiet = spec.quiet[0 if place == "left" else 1]
            places.append((digits[first:end], place, (quiet - 1) * module))
        else:
            places.append((digits[first:end], place, (place[1] - place[0] - 2) * module))
    # one size for all: as designed, or smaller if one would not fit
    for text_, _place, room in places:
        ink = _ink(text_, size)
        if ink is not None and ink.width > room:
            size *= max(1, room) / ink.width
    for text_, place, _room in places:
        ink = _ink(text_, size)
        if ink is None:
            continue
        if place == "left":
            x = left - module - ink.width
        elif isinstance(place, str):
            x = left + (count + 1) * module
        else:
            x = left + (place[0] + place[1]) * module // 2 - ink.width // 2
        _paste_text(mask, ink, x, y)


def _paste_text(mask: Image.Image, text: Image.Image, x: int, y: int) -> None:
    """The text's ink into the mask, cut off at the element's bottom."""
    if y < mask.height:
        ink = text.crop((0, 0, text.width, mask.height - y))
        mask.paste(255, (max(0, x), y, max(0, x) + ink.width, y + ink.height), ink)


def _runs(pattern: str) -> list[tuple[int, int]]:
    """The bars: (first module, module after the last) of each run of 1s."""
    runs, start = [], None
    for index, bit in enumerate(pattern + "0"):
        if bit == "1" and start is None:
            start = index
        elif bit != "1" and start is not None:
            runs.append((start, index))
            start = None
    return runs


def render_code(
    element: CodeElement, height: int, width: int | None = None, step: int = 1
) -> Image.Image:
    """The code in its colour on transparent, 1 bit, each module a whole
    number of pixels, the quiet zone inside: a square 2D code height pixels
    square (or as wide as width, if less), a barcode or PDF417 height pixels
    high and as wide as its design (or width pixels). What is wrong with
    it: code_problems(). `step`: see _matrix()."""
    symbol = codes.encode(element)
    spec = codes.SPECS[element.symbology]
    if spec.layout in ("matrix", "stacked"):
        img = _matrix(element, symbol, spec, height, width, step)
    else:
        img = _barcode(element, symbol, spec, height, width)
    img.code_problems += codes.notes(element)  # type: ignore[attr-defined]
    return img


def _draw_symbol(path: str, rotation: int, color: str, mono: bool, size: int) -> Image.Image:
    """The path filled in a square of size pixels, turned clockwise by
    rotation degrees. mono: no grey edges (a threshold instead of the
    dithering a mono printer gets), so the outline stays crisp."""
    import cairo

    path_segments = segments(path)  # PathError: a damaged file
    surface = cairo.ImageSurface(cairo.FORMAT_A8, size, size)
    ctx = cairo.Context(surface)
    ctx.translate(size / 2, size / 2)
    ctx.rotate(math.radians(rotation))
    ctx.scale(size / VIEW_BOX, size / VIEW_BOX)
    ctx.translate(-VIEW_BOX / 2, -VIEW_BOX / 2)
    trace(ctx, path_segments)
    ctx.fill()
    surface.flush()
    alpha = Image.frombuffer(
        "L", (size, size), bytes(surface.get_data()), "raw", "L", surface.get_stride(), 1
    )
    if mono:
        alpha = alpha.point(lambda v: 255 if v >= 128 else 0)
    img = Image.new("RGBA", (size, size), (*_color(color), 0))
    img.putalpha(alpha)
    return img


def render_symbol(
    element: SymbolElement, height: int, dpi: float, mono: bool = False, inks: Inks | None = None
) -> Image.Image:
    """The symbol as a square: its size, at most the height. The path saved
    with the label is drawn; without one the name is looked up."""
    path = element.path
    if not path:
        found = find(element.name)
        if found is None:
            raise ElementError("symbol_unknown")
        path = found.path
    size = height if element.size_mm <= 0 else min(height, mm_to_px(element.size_mm, dpi))
    inks = inks or Inks(mono)
    draw = partial(_draw_symbol, path, element.rotation, inks.color(element.color), inks.mono)
    img = draw(size)
    # scaled down to fit a fixed length: drawn again, as sharp as before
    img.info["redraw"] = lambda _w, h: draw(h)
    return img


def render_page(
    element: PageElement, width: int | None, height: int, mono: bool = False
) -> Image.Image:
    """The area of the page drawn right at its size on the label: no scaling
    afterwards, so barcodes keep whole printer dots. width None: as wide as
    the area needs at the height (endless tape). mono: for a black and
    white printer (see shipping.mono): edges thresholded, grey of its own
    kept for the printer's dithering."""
    from etiketto.shipping import source

    crop = (element.crop[0], element.crop[1], element.crop[2], element.crop[3])
    box = (width, height)
    args = (element.data, element.page, crop, element.rotation, box, element.fit)
    if not mono:
        return source.draw(*args).convert("RGBA")
    # kept: the preview renders again on every change of the label
    return source.cached(("mono", *args), lambda: _mono_page(element, args)).convert("RGBA")


def _mono_page(element: PageElement, args) -> Image.Image:
    from etiketto.shipping import source
    from etiketto.shipping.mono import black_and_white

    info = source.page_info(element.data, element.page)
    # an image is enlarged smoothly here: the threshold then finds its
    # edges between the pixels instead of on the steps of whole pixels
    data, page, crop, rotation, box, fit = args
    image = source.draw(data, page, crop, rotation, box, fit, sharp=info.pdf)
    scale = 1.0
    if not info.pdf:
        _x, _y, w, h = source.area(crop, info.width, info.height)
        turned_w, turned_h = source.turned(w, h, element.rotation)
        scale = max(image.width / turned_w, image.height / turned_h)
    return black_and_white(image.convert("L"), scale)


def render_shape(
    element: ShapeElement, height: int, dpi: float, mono: bool = False, inks: Inks | None = None
) -> Image.Image:
    inks = inks or Inks(mono)
    return shapes.render_shape(element, height, dpi, _color(inks.color(element.color)), inks.mono)


def render_element(
    element: Element,
    height: int,
    dpi: float,
    width: int | None = None,
    mono: bool = False,
    step: int = 1,
    inks: Inks | None = None,
) -> Image.Image:
    """width: the room along the label (fixed length), None if it grows;
    mono: the printer prints only black and white (see render_page()),
    inks: and against which background (Inks); step: the image is reduced
    across by this factor later (render())."""
    inks = inks or Inks(mono)
    if isinstance(element, TextElement):
        return render_text(element, height, dpi, room=width, inks=inks)
    if isinstance(element, ImageElement):
        return render_image(element, height, dpi, inks.mono)
    if isinstance(element, SymbolElement):
        return render_symbol(element, height, dpi, inks=inks)
    if isinstance(element, PageElement):
        return render_page(element, width, height, inks.mono)
    if isinstance(element, ShapeElement):
        return render_shape(element, height, dpi, inks=inks)
    if inks.mono:
        element = replace(element, color=inks.color(element.color))
    return render_code(element, height, step=step)


def _mono_part(element: Element, part: Image.Image, inks: Inks, dpi: float) -> Image.Image:
    """A part at its final size for a black-and-white printer: an image by
    its method, then its corners and border; a page area as it is
    (shipping.mono); everything else hard black and white (its colours
    are black and white already), so the printer's own conversion changes
    nothing."""
    if isinstance(element, PageElement):
        return part
    if isinstance(element, ImageElement):
        hard = imagefx.black_and_white(part, element.mode, element.threshold, inks.red)
        border = _color(inks.color(element.border_color))
        radius, width = shapes.px(element.corner_mm, dpi), shapes.px(element.border_mm, dpi)
        hard = imagefx.framed(hard, radius, width, border, antialias=False)
    else:
        hard = imagefx.hard(part, inks.red)
    for name in ("code_row0", "code_module", "code_problems"):  # what _place needs
        if hasattr(part, name):
            setattr(hard, name, getattr(part, name))
    return hard


def _visible(part: Image.Image, background: float) -> bool:
    """Whether a black-and-white part shows on a plain background of that
    brightness: some ink of the other colour (or red)."""
    alpha = part.getchannel("A")
    if not alpha.getbbox():
        return False
    if 5 < background < 250:  # a dithered grey: either ink shows
        return True
    other = 0 if background >= 250 else 255
    grey = part.convert("RGB").convert("L")
    reds = ImageChops.multiply(imagefx.red_mask(part), alpha)
    return grey.histogram(alpha)[other] > 0 or bool(reds.getbbox())


def _resize(part: Image.Image, width: int, height: int) -> Image.Image:
    """A part in another size: drawn again if it can be (symbols, shapes,
    black-and-white text), else resampled."""
    redraw = part.info.get("redraw")
    if redraw is not None:
        return redraw(max(1, width), max(1, height))
    return part.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)


def _placeholder(height: int) -> Image.Image:
    img = Image.new("RGBA", (height, height), (255, 230, 230, 255))
    draw = ImageDraw.Draw(img)
    draw.line((0, 0, height, height), fill=(200, 0, 0), width=max(1, height // 20))
    draw.line((0, height, height, 0), fill=(200, 0, 0), width=max(1, height // 20))
    return img


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img, size, Image.Resampling.LANCZOS)


# --- label ------------------------------------------------------------------


@dataclass
class _Frame:
    """Pixel geometry across the tape."""

    tape: int  # printable tape height
    bleed: int  # background beyond each tape edge
    margin: int  # spacing at the ends and between elements
    safe: int
    border: int
    frame: int  # kept free of content on every side (safety margin + inset + border)
    inner: int  # content height
    inset: int = 0  # the border this far inside the safety margin


def _frame(label: Label, dpi: float, safe_mm: float, tape_px, bleed_px, warnings) -> _Frame:
    tape = tape_px or mm_to_px(label.tape_mm, dpi)
    edge = mm_to_px(label.edge_mm, dpi) if label.edge_mm > 0 else 0
    margin = mm_to_px(label.margin_mm, dpi) if label.margin_mm > 0 else 0
    border = mm_to_px(label.border_mm, dpi) if label.border_mm > 0 else 0
    safe = mm_to_px(safe_mm, dpi) if safe_mm > 0 else 0
    inset = mm_to_px(label.border_inset_mm, dpi) if border and label.border_inset_mm > 0 else 0
    frame = safe + inset + border
    inner = tape - 2 * (edge + frame)
    if inner < mm_to_px(2, dpi):
        warnings.append(RenderWarning("no_room"))
        inset = 0  # as without an inset: the border on the safety margin
        frame = min(safe + border, max(0, tape // 2 - edge - 1))
        inner = max(1, tape - 2 * (edge + frame))
    return _Frame(tape, max(0, bleed_px), margin, safe, border, frame, inner, inset)


def _element_warning(element: Element, index: int, error: Exception) -> RenderWarning:
    if isinstance(error, (CodeError, ElementError)):
        return RenderWarning(error.key, {"n": index, **error.params})
    if isinstance(element, ImageElement):
        return RenderWarning("image_unreadable", {"n": index})
    if isinstance(element, PageElement):
        missing = getattr(error, "code", "") == "no_pdf"
        return RenderWarning("pdf_missing" if missing else "page_unreadable", {"n": index})
    return RenderWarning("element_failed", {"n": index})


def _room(label: Label, f: _Frame, dpi: float, min_length_mm: float) -> int | None:
    """The room for content along a label of fixed length; None if it grows."""
    if label.auto_length:
        return None
    length = max(math.ceil(min_length_mm / 25.4 * dpi), mm_to_px(label.length_mm, dpi))
    return max(1, length - 2 * (f.margin + f.frame))


def _parts(
    label: Label, f: _Frame, dpi: float, warnings, room: int | None, inks: Inks, step: int = 1
) -> list[Image.Image]:
    parts = []
    for index, element in enumerate(label.elements, 1):
        try:
            part = render_element(element, f.inner, dpi, room, step=step, inks=inks)
        except Exception as e:  # noqa: BLE001 - show any element error, keep going
            warnings.append(_element_warning(element, index, e))
            part = _placeholder(f.inner)
        if part.height > f.inner and not isinstance(element, CodeElement):
            # fixed-size text taller than the tape (a code too large for it
            # stays as it is: it cannot be printed, see code_too_big)
            part = _resize(part, round(part.width * f.inner / part.height), f.inner)
        parts.append(part)
    return parts


def _content_width(parts: list[Image.Image], gaps: list[int]) -> int:
    return sum(p.width for p in parts) + sum(gaps)


def _gaps(label: Label, f: _Frame, dpi: float) -> list[int]:
    """The space after each element but the last: its own, or the label's."""
    return [
        f.margin if e.gap_mm is None else (mm_to_px(e.gap_mm, dpi) if e.gap_mm > 0 else 0)
        for e in label.elements[:-1]
    ]


def _smaller(
    element: Element, part: Image.Image, scale: float, mono: bool, step: int = 1
) -> Image.Image:
    """A part reduced to fit a label of fixed length. A code, a page area or
    a symbol is drawn again at the smaller size (not reduced: that would
    blur bars, or a code's modules would no longer be whole pixels)."""
    size = (max(1, round(part.width * scale)), max(1, round(part.height * scale)))
    if isinstance(element, CodeElement):
        try:
            return render_code(element, size[1], size[0], step)
        except Exception:  # noqa: BLE001 - the placeholder of a code that failed
            pass
    if isinstance(element, PageElement):
        try:
            drawn = render_page(replace(element, fit="fill"), size[0], size[1], mono)
            if drawn.size == size:
                return drawn
        except Exception:  # noqa: BLE001 - it was drawn before: reduce that instead
            pass
    return _resize(part, *size)


def _fit_length(
    label: Label,
    parts,
    f: _Frame,
    dpi: float,
    warnings,
    min_length_mm: float,
    mono: bool,
    step: int = 1,
):
    """Label length, the (possibly scaled) parts and the gaps between them."""
    min_len = max(1, math.ceil(min_length_mm / 25.4 * dpi))
    gaps = _gaps(label, f, dpi)
    content_w = _content_width(parts, gaps)
    if label.auto_length:
        return max(min_len, content_w + 2 * (f.margin + f.frame)), parts, gaps, False
    length = max(min_len, mm_to_px(label.length_mm, dpi))
    room = length - 2 * (f.margin + f.frame)
    if parts and room <= 0:
        warnings.append(RenderWarning("fixed_too_short"))
    elif parts and content_w > room:
        scale = room / content_w
        parts = [
            _smaller(element, p, scale, mono, step)
            for element, p in zip(label.elements, parts, strict=True)
        ]
        return length, parts, [round(gap * scale) for gap in gaps], True
    return length, parts, gaps, False


def _background(label: Label, size: tuple[int, int], warnings, inks: Inks) -> Image.Image:
    img = Image.new("RGB", size, _color(label.background, "#ffffff"))
    if label.background_image and label.background_image.data:
        try:
            bg = _decoded(label.background_image.data).convert("RGB")
            img.paste(_cover(bg, img.size), (0, 0))
        except Exception:  # noqa: BLE001
            warnings.append(RenderWarning("background_unreadable"))
    if inks.mono:  # dithered on its own: no error spreads into the crisp elements
        img = imagefx.black_and_white(img, "dither", red=inks.red).convert("RGB")
    return img


def background_brightness(label: Label) -> float:
    """How bright the label's background is (0-255): its colour, or the
    average of its background image. Black-and-white inks are chosen
    against it (Inks)."""
    if label.background_image and label.background_image.data:
        try:
            small = _decoded(label.background_image.data).convert("RGB").resize((16, 16))
            return imagefx.luma(ImageStat.Stat(small).mean)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - render() warns about it
            pass
    return imagefx.luma(_color(label.background, "#ffffff"))


def _top(element: Element, part: Image.Image, f: _Frame) -> int:
    """Where a part starts across the tape: in the middle, or at the top or
    bottom of the content height."""
    inner_top = f.bleed + (f.tape - f.inner) // 2
    if element.valign == "top":
        return inner_top
    if element.valign == "bottom":
        return inner_top + f.inner - part.height
    return f.bleed + (f.tape - part.height) // 2


def _place(
    img: Image.Image, label: Label, parts, gaps: list[int], f: _Frame, step: int = 1
) -> None:
    length = img.width
    content_w = _content_width(parts, gaps)
    if label.auto_length or label.align == "center":
        x = f.frame + (length - 2 * f.frame - content_w) // 2
    elif label.align == "end":
        x = length - f.frame - f.margin - content_w
    else:
        x = f.frame + f.margin
    for element, part, gap in zip(label.elements, parts, [*gaps, 0][: len(parts)], strict=True):
        y = _top(element, part, f)
        row0 = getattr(part, "code_row0", None)
        if row0 is not None:  # a 2D code's first row on a multiple of step
            y -= (y + row0) % step
        img.paste(part, (x, y), part)
        x += part.width + gap


def _draw_border(img: Image.Image, label: Label, f: _Frame, dpi: float, inks: Inks) -> None:
    if not f.border:
        return
    color = _color(inks.color(label.border_color))
    # inside the safety margin (and the inset) so it is never cut off; the
    # content keeps clear of it (_frame)
    inset = f.safe + f.inset
    top, bottom = f.bleed + inset, f.bleed + f.tape - 1 - inset
    box = (inset, top, img.width - 1 - inset, bottom)
    if (label.border_style, label.border_radius_mm) == ("solid", 0):
        ImageDraw.Draw(img).rectangle(box, outline=color, width=f.border)
        return
    radius = shapes.px(label.border_radius_mm, dpi)
    mask = shapes.border_mask(img.size, box, f.border, radius, label.border_style, not inks.mono)
    img.paste(Image.new("RGB", img.size, color), (0, 0), mask)


def render(
    label: Label,
    dpi: float = PRINT_DPI,
    safe_mm: float = DEFAULT_SAFE_MM,
    tape_px: int | None = None,
    bleed_px: int = 0,
    min_length_mm: float = 0.0,
    mono: bool = False,
    across_step: int = 1,
    red: bool = False,
) -> Rendered:
    """tape_px overrides the tape height from the nominal width; bleed_px adds
    background beyond both tape edges (the content keeps its place);
    min_length_mm is the printer's shortest label (Capabilities.min_length_mm);
    mono: the printer prints only black and white (Capabilities.color_model):
    every part converted on its own at its final size (text without
    anti-aliasing, codes, symbols and shapes hard black and white, images
    by their mode, page areas see shipping.mono), the background dithered,
    colours black or white against the background (Inks); red: with mono,
    a black-and-red printer: red stays red;
    across_step: the caller reduces the image across the tape by this whole
    factor (a resolution finer along the tape than across it, like the
    P-touch's high resolution): 2D codes then get modules and rows on
    multiples of it, so their modules stay whole pixels after the reduction."""
    warnings: list[RenderWarning] = []
    step = across_step
    inks = Inks(mono, mono and red, background_brightness(label) if mono else 255.0)
    f = _frame(label, dpi, safe_mm, tape_px, bleed_px, warnings)
    parts = _parts(label, f, dpi, warnings, _room(label, f, dpi, min_length_mm), inks, step)
    length, parts, gaps, overflow = _fit_length(
        label, parts, f, dpi, warnings, min_length_mm, mono, step
    )
    warnings += _code_warnings(label, parts) + _shape_warnings(label)
    if mono:  # each part on its own, at its final size
        parts = [_mono_part(e, p, inks, dpi) for e, p in zip(label.elements, parts, strict=True)]
        warnings += _ink_warnings(label, parts, inks)
    img = _background(label, (length, f.tape + 2 * f.bleed), warnings, inks)
    _place(img, label, parts, gaps, f, step)
    _draw_border(img, label, f, dpi, inks)
    # nothing left for apply_color_model to do (a page area keeps its greys)
    img.info["black_and_white"] = mono and not any(
        isinstance(e, PageElement) for e in label.elements
    )
    return Rendered(img, px_to_mm(length, dpi), warnings, overflow)


def _code_warnings(label: Label, parts) -> list[RenderWarning]:
    return [
        RenderWarning(key, {"n": index})
        for index, (element, part) in enumerate(zip(label.elements, parts, strict=True), 1)
        if isinstance(element, CodeElement)
        for key in code_problems(part)
    ]


def _shape_warnings(label: Label) -> list[RenderWarning]:
    """A rectangle or ellipse without outline and fill draws nothing."""
    return [
        RenderWarning("shape_invisible", {"n": index})
        for index, e in enumerate(label.elements, 1)
        if _invisible_shape(e)
    ]


def _invisible_shape(element: Element) -> bool:
    return (
        isinstance(element, ShapeElement)
        and element.shape in ("rectangle", "ellipse")
        and element.line_mm <= 0
        and element.fill == "none"
    )


def _ink_warnings(label: Label, parts, inks: Inks) -> list[RenderWarning]:
    """Black and white: an element that ends up with no ink against the
    background (a white code on white paper) is not printed at all."""
    return [
        RenderWarning("no_contrast", {"n": index})
        for index, (e, part) in enumerate(zip(label.elements, parts, strict=True), 1)
        if not isinstance(e, PageElement)
        and not _invisible_shape(e)
        and not _visible(part, inks.background)
    ]
