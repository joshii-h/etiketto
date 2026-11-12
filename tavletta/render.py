"""Render a Label to a Pillow image at printer resolution.

Label space: x runs along the tape (the label length), y across the tape.
The preview shows this image as is; for printing it is rotated so that the
image width matches the tape width, which is what the printer expects.
"""

from __future__ import annotations

import base64
import contextlib
import io
import math
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, replace
from functools import lru_cache, partial
from types import MappingProxyType
from typing import NamedTuple

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from tavletta import codes, imageformats, imagefx, shapes, typeset
from tavletta.codes import CodeError
from tavletta.model import (
    CodeElement,
    Element,
    ImageElement,
    Label,
    PageElement,
    ShapeElement,
    SymbolElement,
    TextElement,
)
from tavletta.symbols import VIEW_BOX, find, segments, trace

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
    outside_label (the free layout), background_unreadable, no_contrast, shape_invisible, padding_too_large; codes that cannot be encoded: see
    tavletta.codes.CodeError; codes that may not scan or do not fit:
    code_thin, code_quiet, code_too_big, barcode_narrow; code128_long: a Code 128 longer
    than CODE128_MAX characters)."""

    key: str
    # read-only: a NamedTuple default is one object shared by all warnings
    params: Mapping[str, object] = MappingProxyType({})


class RenderTooLarge(ValueError):
    """An image would exceed the limits set with limited()."""


TEXT_CHARS = 5000  # characters of a text or code within limits (as the server's MAX_TEXT)
TEXT_LINES = 100  # lines of a text within limits (MAX_LINES)


@dataclass(frozen=True)
class Limits:
    pixels: int  # of any one image made while rendering
    max_length_mm: float | None = None  # of the label, if the printer has a limit
    # what every render within these limits holds while it runs (the
    # server's render slots, so print jobs share them with previews)
    slot: Callable[[], contextlib.AbstractContextManager] | None = None
    # of any one text or code as it is drawn, its variables and columns
    # filled in (the server checks what is sent before; filled in, a short
    # text can grow a thousandfold)
    text_chars: int = TEXT_CHARS
    text_lines: int = TEXT_LINES

    @property
    def image_pixels(self) -> int:
        """Image files decoded at most, together: a decoded RGBA image is
        held while it is drawn, so less than the drawing budget."""
        return self.pixels // 2


# Untrusted labels (the print server) are rendered within limits: a text of
# thousands of lines or a tiny PNG that unpacks to gigapixels must fail,
# not fill the memory. The window and the command line render without.
_LIMITS: ContextVar[Limits | None] = ContextVar("render_limits", default=None)


@contextlib.contextmanager
def limited(
    pixels: int,
    max_length_mm: float | None = None,
    slot: Callable[[], contextlib.AbstractContextManager] | None = None,
) -> Iterator[None]:
    """Render within limits in this thread while the block runs; images
    are then decoded without the caches (they would keep large ones).
    `slot()` is held by every render meanwhile."""
    token = _LIMITS.set(Limits(pixels, max_length_mm, slot))
    try:
        yield
    finally:
        _LIMITS.reset(token)


def check_size(width: int, height: int) -> None:
    """Within limits: an image of this size may be made (RenderTooLarge)."""
    limits = _LIMITS.get()
    if limits is not None and width * height > limits.pixels:
        raise RenderTooLarge("%d x %d pixels" % (width, height))


def active_limits() -> Limits | None:
    """The limits renders in this thread are within (limited()), if any."""
    return _LIMITS.get()


def check_text(text: str, lines: bool = True) -> None:
    """Within limits: a text (lines: and its lines) or a code's content
    may be drawn as it is filled in (RenderTooLarge). Checked before
    Pango lays it out: a text of thousands of lines takes gigabytes."""
    limits = _LIMITS.get()
    if limits is None:
        return
    if len(text) > limits.text_chars:
        raise RenderTooLarge("%d characters" % len(text))
    if lines and text.count("\n") >= limits.text_lines:
        raise RenderTooLarge("%d lines" % (text.count("\n") + 1))


def warm_up() -> None:
    """Draw a small text once. PyGObject sets up Pango's types on first use,
    and that is not safe in several threads at once (texts then failed
    until a restart): the server calls this in its main thread before any
    request or print job renders. (The window renders in the main thread,
    where GTK has set up Pango already.)"""
    _text_block(TextElement(text="x"), 10)


class ElementError(ValueError):
    """An element that cannot be drawn; key is its RenderWarning (a code
    that cannot be encoded raises tavletta.codes.CodeError instead)."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass
class Rendered:
    image: Image.Image  # label space, RGB
    length_mm: float  # a fixed length: the label's, not the rounded pixels
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
    behind_red: bool = False  # red under the element (a black-and-red printer)

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
    without anti-aliasing every pixel is either ink or not (typeset.block;
    within limits a text of thousands of lines fails there)."""
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
    measure = partial(_size_of, element, dpi=dpi, antialias=antialias, wrap_px=wrap)
    if element.size_pt > 0 and not element.shrink:
        size = element.size_pt / 72 * dpi
    elif not _needs_fitting(element, wrap):
        size = _fill_height(measure, height)
    else:
        limits = (_width_limit(element, room, wrap), height)
        size = _fitted_size(element, measure, limits, dpi)
    block = draw(size)
    block.info["size_px"] = size  # the size it got (freelayout.adopt)
    if inks.mono:  # reduced to a fixed length: laid out again (see _resize)
        if wrap:  # with the lines it has now: smaller, it would wrap otherwise
            lines = typeset.wrapped_lines(element, size, False, wrap)
            element = replace(element, text="\n".join(lines), max_width_mm=0.0)
        block.info["redraw"] = _TextRedraw(element, size, dpi, block.height)
    return block


_TEXT_FIELDS = tuple(f.name for f in fields(TextElement))


def _size_of(
    element: TextElement, size_px: float, dpi: float, antialias: bool, wrap_px: int | None
) -> tuple[int, int]:
    """typeset.size_of, kept: fitting a text asks for the same sizes on
    every render while the label is edited (another element changes)."""
    values = tuple(getattr(element, name) for name in _TEXT_FIELDS)
    return _measured(values, size_px, dpi, antialias, wrap_px)


@lru_cache(maxsize=512)
def _measured(
    values: tuple, size_px: float, dpi: float, antialias: bool, wrap_px: int | None
) -> tuple[int, int]:
    element = TextElement(**dict(zip(_TEXT_FIELDS, values, strict=True)))
    return typeset.size_of(element, size_px, dpi=dpi, antialias=antialias, wrap_px=wrap_px)


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


# the lengths of a text element that scale with it when it is laid out
# again at a smaller size
TEXT_LENGTHS = ("padding_mm", "corner_mm", "frame_mm", "outline_mm", "shadow_mm", "max_width_mm")
REDRAW_TRIES = 20


class _TextRedraw:
    """A black-and-white text laid out again at a smaller size (a fixed
    length too short): the font and every length of the element (padding,
    frame, outline, shadow) scaled, a wrapped text with the line breaks it
    had (render_text), then checked against the size it must fit, smaller
    in steps until it does; None if no layout fits (the caller resamples).
    Keeps the parameters, not the image."""

    def __init__(self, element: TextElement, size: float, dpi: float, height: int):
        self.element, self.size, self.dpi, self.height = element, size, dpi, max(1, height)

    def __call__(self, width: int, height: int) -> Image.Image | None:
        def fits(scale: float) -> bool:
            element, size, wrap = self._at(scale)
            w, h = typeset.size_of(element, size, dpi=self.dpi, antialias=False, wrap_px=wrap)
            return w <= width and h <= height

        high = height / self.height
        low = high
        for _try in range(REDRAW_TRIES):  # smaller in steps until it fits …
            if fits(low):
                break
            high, low = low, low * 0.9
        else:
            return None
        if low < high:  # … then as large as fits (bisection, to 0.2 %)
            while high - low > 0.002 * high:
                middle = (low + high) / 2
                low, high = (middle, high) if fits(middle) else (low, middle)
        element, size, wrap = self._at(low)
        img = _text_block(element, size, antialias=False, dpi=self.dpi, wrap_px=wrap)
        return img if img.width <= width and img.height <= height else None

    def _at(self, scale: float) -> tuple[TextElement, float, int | None]:
        """The element with every length scaled, its font size, its wrap width."""
        changes = {name: getattr(self.element, name) * scale for name in TEXT_LENGTHS}
        element = replace(self.element, **changes)
        wrap = mm_to_px(element.max_width_mm, self.dpi) if element.max_width_mm > 0 else None
        return element, self.size * scale, wrap


def _fill_height(measure, height: int) -> float:
    """Fit to the tape: the block height scales about linearly with font
    size. Measured, not drawn: only the final size is drawn."""
    size = TEXT_PROBE * height / max(1, measure(TEXT_PROBE)[1])
    while measure(size)[1] > height and size > 1:
        size *= 0.97
    return size


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
    lowest = max(1.0, element.min_size_pt / 72 * dpi) if element.shrink else 1.0
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
# Refuse decompression bombs early: checked by _opened() itself, so Pillow's
# own check (a warning from 80 MP, an error from twice that) is turned off;
# a warning filter to keep it quiet would not be safe in threads.
IMAGE_FILE_PIXELS = 80_000_000
Image.MAX_IMAGE_PIXELS = None
# image files a label may embed: the one list (tavletta.imageformats)
IMAGE_FORMATS = imageformats.FORMATS
# within limits: decoded images of the render running in this thread,
# each image once however often the label uses it; never kept afterwards
_RENDER_CACHE: ContextVar[dict | None] = ContextVar("render_cache", default=None)


@contextlib.contextmanager
def _opened(data: str) -> Iterator[Image.Image]:
    """An image file opened: only the common formats, and refused when it
    would unpack to more than IMAGE_FILE_PIXELS."""
    with Image.open(io.BytesIO(base64.b64decode(data)), formats=IMAGE_FORMATS) as src:
        if src.width * src.height > IMAGE_FILE_PIXELS:
            raise Image.DecompressionBombError("%d x %d pixels" % src.size)
        yield src


def image_size(data: str) -> tuple[int, int]:
    """Width and height of an image file, from its header (EXIF
    orientation not applied): nothing is decoded."""
    with _opened(data) as src:
        return src.size


def _decode(data: str) -> Image.Image:
    with _opened(data) as src:
        check_size(*src.size)  # before anything is unpacked
        image = ImageOps.exif_transpose(src)
        check_size(*image.size)  # a format may know its size only now
        return image.convert("RGBA")


def _scale(data: str, height: int) -> Image.Image:
    img = _decoded(data)
    width = max(1, round(img.width * height / img.height))
    check_size(width, height)
    return img.resize((width, height), Image.Resampling.LANCZOS)


# a large photo: cropped and turned once (cheap), then scaled to the label,
# then adjusted at that size, not at its own
def _cut_image(data: str, crop: tuple, rotation: int, mirror: bool, flip: bool) -> Image.Image:
    return imagefx.turned(imagefx.cropped(_decoded(data), crop), rotation, mirror, flip)


def _scale_cut(data: str, cut: tuple, height: int) -> Image.Image:
    img = _cut(data, *cut)
    width = max(1, round(img.width * height / img.height))
    check_size(width, height)
    return img.resize((width, height), Image.Resampling.LANCZOS)


_decoded_cached = lru_cache(maxsize=8)(_decode)
_scaled_cached = lru_cache(maxsize=16)(_scale)
_cut_cached = lru_cache(maxsize=2)(_cut_image)
_scaled_cut_cached = lru_cache(maxsize=2)(_scale_cut)


def _per_render(kind: str, key, make):
    """The result of `make` once per render (within limits), else None."""
    cache = _RENDER_CACHE.get()
    if cache is None:
        return make()
    if (kind, key) not in cache:
        cache[(kind, key)] = make()
    return cache[(kind, key)]


def _decoded(data: str) -> Image.Image:
    if _LIMITS.get():
        return _per_render("decoded", data, lambda: _decode(data))
    return _decoded_cached(data)


def _scaled(data: str, height: int) -> Image.Image:
    if _LIMITS.get():
        return _per_render("scaled", (data, height), lambda: _scale(data, height))
    return _scaled_cached(data, height)


def _cut(data: str, crop: tuple, rotation: int, mirror: bool, flip: bool) -> Image.Image:
    key = (data, crop, rotation, mirror, flip)
    if _LIMITS.get():
        return _per_render("cut", key, lambda: _cut_image(*key))
    return _cut_cached(*key)


def _scaled_cut(data: str, cut: tuple, height: int) -> Image.Image:
    if _LIMITS.get():
        return _per_render(
            "scaled_cut", (data, cut, height), lambda: _scale_cut(data, cut, height)
        )
    return _scaled_cut_cached(data, cut, height)


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
        # adjusted at the label's size: a brightness step on a large photo is quick
        img = imagefx.adjusted(_scaled_cut(element.data, cut, height), *adjust).copy()
    if mono:
        return img
    return imagefx.framed(
        img,
        shapes.px(element.corner_mm, dpi),
        shapes.px(element.border_mm, dpi),
        _color(element.border_color),
    )


CODE128_MAX = codes.CODE128_MAX


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
    steps: tuple[int, int] = (1, 1),
) -> Image.Image:
    """A 2D code: square modules of a whole number of pixels with their
    quiet zone on every side, centred. A square code (QR, Data Matrix,
    Aztec) in height x height pixels (or width x width, if less); PDF417
    height pixels high, as wide as its modules (or width pixels). `steps`
    (x, y): the image is reduced by these factors later: modules, the first
    column (code_col0) and row (code_row0) are multiples, so they stay whole."""
    quiet = (spec.quiet[0], spec.quiet[0])
    tall, wide = len(symbol.rows) * spec.row_modules, symbol.width
    if spec.layout == "matrix":
        height = width = height if width is None else min(height, width)
    module = _module(height / (tall + sum(quiet)), tall, quiet, height).module
    if width is None:
        width = (wide + sum(quiet)) * module
    else:
        module = min(module, _module(width / (wide + sum(quiet)), wide, quiet, width).module)
    unit = math.lcm(*steps)
    module = max(unit, module // unit * unit)
    thin = THIN_MODULE * max(steps)
    across = _fit_at(module, wide, quiet, width, thin)
    down = _fit_at(module, tall, quiet, height, thin)
    size = (max(width, across.start + wide * module), max(height, down.start + tall * module))
    check_size(*size)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    row_px = spec.row_modules * module
    for index, row in enumerate(symbol.rows):
        top = down.start + index * row_px
        for start, end in _runs(row):
            x0, x1 = across.start + start * module, across.start + end * module
            draw.rectangle((x0, top, x1 - 1, top + row_px - 1), 255)
    img = _dark(element, mask, _Fit(module, across.start, _problems(across, down)))
    img.code_col0 = across.start  # type: ignore[attr-defined]
    img.code_row0 = down.start  # type: ignore[attr-defined]
    return img


def _ink(text: str, size_px: float) -> Image.Image | None:
    """A line of the human-readable text without anti-aliasing (1 bit),
    cropped to its ink; None if it cannot be laid out."""
    try:
        block = _text_block(TextElement(text=text, font="Monospace"), size_px, antialias=False)
    except RenderTooLarge:
        raise
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
    element: CodeElement,
    symbol: codes.Symbol,
    spec: codes.Spec,
    height: int,
    width: int | None,
    step: int = 1,
) -> Image.Image:
    """Bars of whole-pixel modules with their quiet zones, centred in the
    element, the text below them. `step`: the image is reduced along x by
    this factor later: modules and the first one (code_col0) multiples."""
    pattern, quiet = _linear_pattern(symbol, spec)
    designed_modules = sum(quiet) + len(pattern)
    scale = height / BAR_TOTAL_MM  # pixels per designed mm
    if width is None:  # as wide as designed, and as the code at 1-pixel modules
        width = max(designed_modules, round(designed_modules * BAR_MODULE_MM * scale))
    fit = _module(width / designed_modules, len(pattern), quiet, width)
    if step > 1:
        module = max(step, fit.module // step * step)
        fit = _fit_at(module, len(pattern), quiet, width, THIN_MODULE * step)
    module, left = fit.module, fit.start
    top = round(BAR_TOP_MM * scale)
    bars_h = max(1, round(BAR_HEIGHT_MM * scale))
    check_size(max(width, left + module * len(pattern)), height)
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
    img = _dark(element, mask, fit)
    img.code_col0 = left  # type: ignore[attr-defined]
    return img


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
    element: CodeElement,
    height: int,
    width: int | None = None,
    steps: tuple[int, int] = (1, 1),
) -> Image.Image:
    """The code in its colour on transparent, 1 bit, each module a whole
    number of pixels, the quiet zone inside: a square 2D code height pixels
    square (or as wide as width, if less), a barcode or PDF417 height pixels
    high and as wide as its design (or width pixels). What is wrong with
    it: code_problems(). `steps`: see _matrix()."""
    check_text(element.data, lines=False)  # within limits, as it is filled in
    symbol = codes.encode(element)
    spec = codes.SPECS[element.symbology]
    if spec.layout in ("matrix", "stacked"):
        img = _matrix(element, symbol, spec, height, width, steps)
    else:
        img = _barcode(element, symbol, spec, height, width, steps[0])
    img.code_problems += codes.notes(element)  # type: ignore[attr-defined]
    return img


def _draw_symbol(path: str, rotation: int, color: str, mono: bool, size: int) -> Image.Image:
    """The path filled in a square of size pixels, turned clockwise by
    rotation degrees. mono: no grey edges (a threshold instead of the
    dithering a mono printer gets), so the outline stays crisp."""
    import cairo

    path_segments = segments(path)  # PathError: a damaged file
    check_size(size, size)
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
    from tavletta.shipping import source

    if width is not None:
        check_size(width, height)
    crop = (element.crop[0], element.crop[1], element.crop[2], element.crop[3])
    box = (width, height)
    args = (element.data, element.page, crop, element.rotation, box, element.fit)
    if not mono:
        return source.draw(*args).convert("RGBA")
    if _LIMITS.get():  # nothing kept between renders (see source.draw)
        return _mono_page(element, args).convert("RGBA")
    # kept: the preview renders again on every change of the label
    return source.cached(("mono", *args), lambda: _mono_page(element, args)).convert("RGBA")


def _mono_page(element: PageElement, args) -> Image.Image:
    from tavletta.shipping import source
    from tavletta.shipping.mono import black_and_white

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
    steps: tuple[int, int] = (1, 1),
    inks: Inks | None = None,
) -> Image.Image:
    """width: the room along the label (fixed length), None if it grows;
    mono: the printer prints only black and white (see render_page()),
    inks: and against which background (Inks); steps: the image is reduced
    by these factors later (render())."""
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
    return render_code(element, height, steps=steps)


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
    elif isinstance(element, TextElement):
        hard = _mono_text_part(part, inks)
    else:
        hard = imagefx.hard(part, inks.red)
    for name in ("code_row0", "code_module", "code_problems"):  # what _place needs
        if hasattr(part, name):
            setattr(hard, name, getattr(part, name))
    return hard


# a background darker or lighter than this takes black or white ink as its own
DARK_BEHIND, LIGHT_BEHIND = 40, 215


def _mono_text_part(part: Image.Image, inks: Inks) -> Image.Image:
    """A text drawn without anti-aliasing in black and white is ready as it
    is; one with colour pixels (a colour emoji, whatever its code point) is
    converted like a drawing or photo. Decided from the pixels."""
    tones = imagefx.tones(part, inks.red)
    if tones == "hard":
        return part
    if tones == "colour":  # its shapes outlined, so light parts stay visible
        converted = imagefx.black_and_white(part, "auto", red=inks.red)
        ink = (0, 0, 0) if inks.background >= 128 else (255, 255, 255)
        return imagefx.outlined_colours(part, converted, ink)
    return imagefx.hard(part, inks.red)


def _visible(part: Image.Image, inks: Inks) -> bool:
    """Whether a black-and-white part shows against what lies under it:
    black ink on anything not dark, white ink on anything not light, red
    (black-and-red printers) on anything not red."""
    alpha = part.getchannel("A")  # hard edged already (_mono_part)
    if not alpha.getbbox():
        return False
    # black and white (and red, kept apart below): the red channel says it
    histogram = (part.convert("L") if inks.red else part.getchannel("R")).histogram(alpha)
    black, white = sum(histogram[:128]), sum(histogram[128:])
    if inks.red:
        reds = sum(imagefx.red_mask(part).histogram(alpha)[128:])
        black -= reds  # red counts as dark in grey
        if reds and not inks.behind_red:
            return True
    return (black > 0 and inks.background > DARK_BEHIND) or (
        white > 0 and inks.background < LIGHT_BEHIND
    )


def _resize(part: Image.Image, width: int, height: int) -> Image.Image:
    """A part in another size: drawn again if it can be (symbols, shapes,
    black-and-white text), else resampled."""
    redraw = part.info.get("redraw")
    drawn = redraw(max(1, width), max(1, height)) if redraw is not None else None
    if drawn is not None:
        return drawn
    check_size(max(1, width), max(1, height))
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
    inset = _inset(label, dpi, tape - 2 * (edge + safe + border), warnings) if border else 0
    frame = safe + inset + border
    inner = tape - 2 * (edge + frame)
    if inner < mm_to_px(2, dpi):
        if RenderWarning("no_room") not in warnings:
            warnings.append(RenderWarning("no_room"))
        inset = 0  # as without an inset: the border on the safety margin
        frame = min(safe + border, max(0, tape // 2 - edge - 1))
        inner = max(1, tape - 2 * (edge + frame))
    return _Frame(tape, max(0, bleed_px), margin, safe, border, frame, inner, inset)


def _inset(label: Label, dpi: float, room: int, warnings) -> int:
    """The border's inset in pixels; one too large for the tape is reduced
    (as along a short label), so the content keeps its smallest height."""
    inset = mm_to_px(label.border_inset_mm, dpi) if label.border_inset_mm > 0 else 0
    most = max(0, (room - mm_to_px(2, dpi)) // 2)
    if inset > most:
        warnings.append(RenderWarning("no_room"))
        return most
    return inset


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
    label: Label,
    f: _Frame,
    dpi: float,
    warnings,
    room: int | None,
    inks: Inks,
    steps: tuple[int, int] = (1, 1),
) -> list:
    _source_budget(label)
    parts: list = []  # images, and texts drawn later (_LaterText)
    used = 0
    for index, element in enumerate(label.elements, 1):
        part: Image.Image | _LaterText
        try:
            if _later(element, room, inks):
                assert isinstance(element, TextElement)
                part = _LaterText(element, f.inner, dpi)
            else:
                part = render_element(element, f.inner, dpi, room, steps=steps, inks=inks)
        except RenderTooLarge:
            raise
        except Exception as e:  # noqa: BLE001 - show any element error, keep going
            warnings.append(_element_warning(element, index, e))
            part = _placeholder(f.inner)
        if part.height > f.inner and not isinstance(element, CodeElement):
            # fixed-size text taller than the tape (a code too large for it
            # stays as it is: it cannot be printed, see code_too_big)
            width = round(part.width * f.inner / part.height)
            if isinstance(part, _LaterText):
                part = part.drawn(max(1, width), f.inner)
            else:
                part = _resize(part, width, f.inner)
        if not isinstance(part, _LaterText):  # counted once drawn (_render)
            used += part.width * part.height
            _check_budget(used, "elements")
        parts.append(part)
    return parts


class _LaterText:
    """A text on a label of fixed length, measured but not drawn yet: if
    the content is scaled down to fit, it is drawn at the size it ends up
    with (never drawn large and scaled down: on a page that would take
    gigapixels), else at its own (_drawn())."""

    def __init__(self, element: TextElement, height: int, dpi: float):
        wrap = None
        self.size_px = _fill_or_own(element, height, dpi)
        self.width, self.height = _size_of(element, self.size_px, dpi, True, wrap)
        self.element, self.dpi = element, dpi
        self.info = {"redraw": self.drawn}

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def drawn(self, width: int, height: int) -> Image.Image:
        size = self.size_px * min(width / self.width, height / self.height)
        block = _text_block(self.element, size, dpi=self.dpi)
        block.info["size_px"] = size
        if block.size != (width, height):  # a pixel off: the size it is placed at
            block = block.resize((width, height), Image.Resampling.LANCZOS)
            block.info["size_px"] = size
        return block


def _later(element: Element, room: int | None, inks: Inks) -> bool:
    """A text drawn once its size is known (_LaterText): in colour, on a
    label of fixed length, a plain text whose block scales with its size."""
    return (
        room is not None
        and not inks.mono
        and isinstance(element, TextElement)
        and element.max_width_mm <= 0
        and not _needs_fitting(element, None)
    )


def _fill_or_own(element: TextElement, height: int, dpi: float) -> float:
    if element.size_pt > 0:
        return element.size_pt / 72 * dpi
    measure = partial(_size_of, element, dpi=dpi, antialias=True, wrap_px=None)
    return _fill_height(measure, height)


def _drawn(part) -> Image.Image:
    """A part as an image: a _LaterText at the size it got."""
    return part.drawn(part.width, part.height) if isinstance(part, _LaterText) else part


def _check_budget(used: int, what: str) -> None:
    """Within limits: the parts of a label together (many small parts add
    up) stay within the pixel budget."""
    limits = _LIMITS.get()
    if limits is not None and used > limits.pixels:
        raise RenderTooLarge("%d pixels of %s" % (used, what))


def _source_budget(label: Label) -> None:
    """Within limits: the image files of a label together (each counted
    once, and once more if it is also a page: decoded apart) may not unpack
    to more than the image budget; checked from their headers before any is
    decoded. A PDF page is drawn in a process of its own (shipping.source)."""
    limits = _LIMITS.get()
    if limits is None:
        return
    total = 0
    for data in _image_files(label):
        try:
            width, height = _per_render("size", data, lambda data=data: image_size(data))
        except Exception:  # noqa: BLE001 - an unreadable file becomes its warning later
            continue
        total += width * height
        if total > limits.image_pixels:
            raise RenderTooLarge("%d pixels of image files" % total)


def _image_files(label: Label) -> list[str]:
    """The image files of a label decoded within limits: those of images
    and the background, each once, then those of image pages (not PDFs)."""
    from tavletta.shipping import source

    files = {e.data for e in label.elements if isinstance(e, ImageElement) and e.data}
    if label.background_image and label.background_image.data:
        files.add(label.background_image.data)
    pages = {
        e.data
        for e in label.elements
        if isinstance(e, PageElement) and e.data and not source.starts_as_pdf(e.data)
    }
    return [*files, *pages]


def check_label(length: int, height: int, dpi: float) -> None:
    """Within the limits, before the label's image is made: its size, and
    its length if the printer has a limit."""
    limits = _LIMITS.get()
    if limits is None:
        return
    if limits.max_length_mm is not None and px_to_mm(length, dpi) > limits.max_length_mm + 1:
        raise RenderTooLarge("%.0f mm" % px_to_mm(length, dpi))
    check_size(length, height)


def _content_width(parts: list[Image.Image], gaps: list[int]) -> int:
    return sum(p.width for p in parts) + sum(gaps)


def _gaps(label: Label, f: _Frame, dpi: float) -> list[int]:
    """The space after each element but the last: its own, or the label's."""
    return [
        f.margin if e.gap_mm is None else (mm_to_px(e.gap_mm, dpi) if e.gap_mm > 0 else 0)
        for e in label.elements[:-1]
    ]


def _smaller(
    element: Element,
    part: Image.Image,
    scale: float,
    inks: Inks,
    steps: tuple[int, int] = (1, 1),
) -> Image.Image:
    """A part reduced to fit a label of fixed length (see _at_size)."""
    size = (max(1, round(part.width * scale)), max(1, round(part.height * scale)))
    return _at_size(element, part, size, inks, steps)


def _at_size(
    element: Element,
    part: Image.Image,
    size: tuple[int, int],
    inks: Inks,
    steps: tuple[int, int] = (1, 1),
) -> Image.Image:
    """A part in another size. A code, a page area, a symbol, a shape or a
    black-and-white text is drawn again at that size (not resampled: that
    would blur bars, or a code's modules would no longer be whole pixels);
    a code in the inks it is printed with."""
    if part.size == size:
        return part
    if isinstance(part, _LaterText):
        return part.drawn(*size)
    if isinstance(element, CodeElement):
        try:
            if inks.mono:
                element = replace(element, color=inks.color(element.color))
            return render_code(element, size[1], size[0], steps)
        except RenderTooLarge:
            raise
        except Exception:  # noqa: BLE001 - the placeholder of a code that failed
            pass
    if isinstance(element, PageElement):
        try:
            drawn = render_page(replace(element, fit="fill"), size[0], size[1], inks.mono)
            if drawn.size == size:
                return drawn
        except RenderTooLarge:
            raise
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
    inks: Inks,
    steps: tuple[int, int] = (1, 1),
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
            _smaller(element, p, scale, inks, steps)
            for element, p in zip(label.elements, parts, strict=True)
        ]
        return length, parts, [round(gap * scale) for gap in gaps], True
    return length, parts, gaps, False


def _canvas(label: Label, size: tuple[int, int], warnings) -> Image.Image:
    """The background in colour: its colour, its image covering it (where
    the image is transparent, the colour shows)."""
    check_size(*size)
    img = Image.new("RGB", size, _color(label.background, "#ffffff"))
    if label.background_image and label.background_image.data:
        try:
            bg = _decoded(label.background_image.data)
            if bg.getchannel("A").getextrema()[0] == 255:  # opaque: as before
                img.paste(_cover(bg.convert("RGB"), img.size), (0, 0))
            else:
                cover = _cover(bg, img.size)
                img.paste(cover.convert("RGB"), (0, 0), cover)
        except RenderTooLarge:
            raise
        except Exception:  # noqa: BLE001
            warnings.append(RenderWarning("background_unreadable"))
    return img


def _dithered(label: Label, img: Image.Image, inks: Inks) -> Image.Image:
    """The background for a black-and-white printer, dithered on its own (no
    error spreads into the crisp elements); a plain colour as an even
    pattern (ordered dithering of one tile, repeated), white or black as
    it is."""
    if inks.red:
        return imagefx.black_and_white(img, "dither", red=True).convert("RGB")
    if label.background_image and label.background_image.data:  # opaque: no compositing
        return imagefx.floyd_steinberg(img.convert("L")).convert("RGB")
    level = round(imagefx.luma(_color(label.background, "#ffffff")))
    if level >= 255 or level <= 0:  # white or black already
        return (
            img
            if img.getpixel((0, 0)) in ((0, 0, 0), (255, 255, 255))
            else (Image.new("RGB", img.size, (255, 255, 255) if level >= 255 else (0, 0, 0)))
        )
    tile = imagefx.ordered(Image.new("L", (8, 8), level))
    return imagefx.tiled(tile, img.size).convert("RGB")


def background_brightness(label: Label) -> float:
    """How bright the label's background is (0-255) for a first choice of
    black-and-white inks: its colour, or the average of its background
    image on that colour. render() then decides each element against what
    lies under it (_local_inks)."""
    colour = _color(label.background, "#ffffff")
    if label.background_image and label.background_image.data:
        # within limits not kept: the cache would hold the client's files
        measure = _image_brightness if _LIMITS.get() else _image_brightness_cached
        try:
            return measure(label.background_image.data, colour)
        except RenderTooLarge:
            raise
        except Exception:  # noqa: BLE001 - render() warns about it
            pass
    return imagefx.luma(colour)


def _image_brightness(data: str, colour: tuple[int, int, int]) -> float:
    image = _decoded(data)
    small = image.reduce(16) if min(image.size) >= 64 else image
    flat = Image.new("RGBA", small.size, (*colour, 255))
    flat.alpha_composite(small)
    return imagefx.luma(ImageStat.Stat(flat.convert("RGB")).mean)  # type: ignore[arg-type]


_image_brightness_cached = lru_cache(maxsize=4)(_image_brightness)


def _local_inks(area: Image.Image, mask: Image.Image | None, inks: Inks) -> Inks:
    """The inks for an element against what lies under it on the background
    (area: the background under its rectangle; mask: where it has ink, so
    an element straddling a dark and a light part takes the ink that shows
    on most of its letters; red on a black and red printer)."""
    if not area.width or not area.height:
        return inks
    if mask is not None and not mask.getbbox():
        mask = None
    brightness = ImageStat.Stat(area.convert("L"), mask).mean[0]
    behind_red = inks.red and ImageStat.Stat(imagefx.red_mask(area)).mean[0] >= 128
    return replace(inks, background=brightness, behind_red=behind_red)


# ink and what lies behind it at least this far apart in brightness (0-255):
# text, symbols and lines; codes need more for a scanner
CONTRAST = 80
CODE_CONTRAST = 160
LOST_SHARE = 0.25  # more of an element's ink than this without contrast: a warning
# a code: a linear code has no error correction (one lost bar and it does
# not scan) and a 2D code's correction is for damage, not for a part laid
# out on the wrong background; only stray edge dots are let pass
CODE_LOST_SHARE = 0.02


def _lost(area: Image.Image, mask: Image.Image, ink: str, minimum: int, red: bool = False):
    """The share of an element's ink pixels (mask) whose background lies
    too near the ink's brightness (a part of a straddling element lost).
    Black-and-red printers: red ink is lost on red, black and white ink
    show on red (other inks), as in _ink_visible."""
    total = mask.histogram()[255]
    if not total or ink == "":
        return 0.0
    reds = imagefx.red_mask(area) if red else None
    if reds is not None and imagefx.mono_ink(_color(ink), 255, True) == imagefx.RED:
        return reds.histogram(mask)[255] / total
    level = 255 if _color(ink) == (255, 255, 255) else 0
    near = ImageChops.difference(area.convert("L"), Image.new("L", area.size, level))
    close = near.point([255] * minimum + [0] * (256 - minimum))
    if reds is not None:
        close = ImageChops.subtract(close, reds)
    return close.histogram(mask)[255] / total


def _top(element: Element, part: Image.Image, f: _Frame) -> int:
    """Where a part starts across the tape: in the middle, or at the top or
    bottom of the content height."""
    inner_top = f.bleed + (f.tape - f.inner) // 2
    if element.valign == "top":
        return inner_top
    if element.valign == "bottom":
        return inner_top + f.inner - part.height
    return f.bleed + (f.tape - part.height) // 2


def _positions(
    length: int, label: Label, parts, gaps: list[int], f: _Frame, steps: tuple[int, int] = (1, 1)
):
    """Where each part goes: (x, y) of its top left corner."""
    content_w = _content_width(parts, gaps)
    if label.auto_length or label.align == "center":
        x = f.frame + (length - 2 * f.frame - content_w) // 2
    elif label.align == "end":
        x = length - f.frame - f.margin - content_w
    else:
        x = f.frame + f.margin
    places = []
    for element, part, gap in zip(label.elements, parts, [*gaps, 0][: len(parts)], strict=True):
        y = _top(element, part, f)
        row0 = getattr(part, "code_row0", None)
        if row0 is not None:  # a 2D code's first row on a multiple of the step
            y -= (y + row0) % steps[1]
        col0 = getattr(part, "code_col0", None)
        if col0 is not None:  # a code's first module on a multiple of the step
            x += -(x + col0) % steps[0]
        places.append((x, y))
        x += part.width + gap
    return places


def _draw_border(img: Image.Image, label: Label, f: _Frame, dpi: float, inks: Inks, warnings):
    if not f.border:
        return
    color = _color(inks.color(label.border_color))
    # inside the safety margin (and the inset) so it is never cut off; the
    # content keeps clear of it (_frame). An inset too large for a short
    # label is reduced; without room for the border at all it is left out.
    inset = min(f.safe + f.inset, (img.width - 2 * f.border) // 2)
    if inset < f.safe:
        inset = f.safe
    top, bottom = f.bleed + inset, f.bleed + f.tape - 1 - inset
    box = (inset, top, img.width - 1 - inset, bottom)
    room = min(box[2] - box[0], box[3] - box[1]) + 1
    tight = inset != f.safe + f.inset or room < 2 * f.border
    if tight and RenderWarning("no_room") not in warnings:
        warnings.append(RenderWarning("no_room"))
    if room < 2 * f.border:  # all border: a block, or nothing if not even that fits
        if room > 0:
            ImageDraw.Draw(img).rectangle(box, fill=color)
        return
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
    along_step: int = 1,
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
    multiples of it, so their modules stay whole pixels after the reduction;
    along_step: the same along the label (x; a die-cut label whose length
    lies across a print head that is coarser, the QL's in high quality):
    codes get modules and first modules on multiples of it.
    A label of the free layout: tavletta.freelayout.

    Within limits (limited(), the print server) every image made is
    checked and the label refused (RenderTooLarge) instead of drawn."""
    limits = _LIMITS.get()
    token = _RENDER_CACHE.set({}) if limits else None
    slot = limits.slot() if limits and limits.slot else contextlib.nullcontext()
    try:
        with slot:
            return _render(
                label,
                dpi,
                safe_mm,
                (tape_px, bleed_px),
                min_length_mm,
                (mono, red),
                (along_step, across_step),
            )
    finally:
        if token is not None:
            _RENDER_CACHE.reset(token)


def _render(label: Label, dpi: float, safe_mm: float, raster, min_length_mm, colours, steps):
    tape_px, bleed_px = raster
    mono, red = colours
    across_step = steps[1]
    if tape_px is None and across_step > 1:  # whole pixel pairs: a multiple of the step
        tape_px = mm_to_px(label.tape_mm, dpi)
        tape_px += -tape_px % across_step
    if label.layout == "free":
        from tavletta import freelayout

        return freelayout.render(
            label, dpi, safe_mm, (tape_px, bleed_px), min_length_mm, (mono, red), steps
        )
    warnings: list[RenderWarning] = []
    inks = Inks(mono, mono and red, background_brightness(label) if mono else 255.0)
    if inks.red:
        red_bg = imagefx.red_mask(Image.new("RGB", (1, 1), _color(label.background, "#ffffff")))
        inks = replace(inks, behind_red=bool(red_bg.getpixel((0, 0))))
    f = _frame(label, dpi, safe_mm, tape_px, bleed_px, warnings)
    parts = _parts(label, f, dpi, warnings, _room(label, f, dpi, min_length_mm), inks, steps)
    length, parts, gaps, overflow = _fit_length(
        label, parts, f, dpi, warnings, min_length_mm, inks, steps
    )
    parts = [_drawn(part) for part in parts]
    _check_budget(sum(part.width * part.height for part in parts), "elements")
    # a fixed length is the user's, though rounded to whole pixels (pairs):
    # 20.1 mm stays 20.1 mm in the status line and the print dialog; one
    # shorter than the printer's shortest label says the length printed
    user_length = not label.auto_length and label.length_mm >= min_length_mm
    length += -length % steps[0]  # whole pixel pairs, also once the label is turned
    check_label(length, f.tape + 2 * f.bleed, dpi)
    warnings += _code_warnings(label, parts, dpi) + _layout_warnings(label, f, dpi)
    img = _canvas(label, (length, f.tape + 2 * f.bleed), warnings)
    places = _positions(length, label, parts, gaps, f, steps)
    if mono:  # each part on its own, at its final size, against what lies under it
        parts = _mono_parts(label, parts, places, img, inks, (f, dpi, steps), warnings)
        img = _dithered(label, img, inks)
    for part, place in zip(parts, places, strict=True):
        img.paste(part, place, part)
    _draw_border(img, label, f, dpi, inks, warnings)
    # nothing left for apply_color_model to do (a page area keeps its greys)
    img.info["black_and_white"] = mono and not any(
        isinstance(e, PageElement) for e in label.elements
    )
    length_mm = label.length_mm if user_length else px_to_mm(length, dpi)
    return Rendered(img, length_mm, warnings, overflow)


def whole_tape(image: Image.Image, tape: int) -> Image.Image:
    """Endless tape: the render covers only what the head prints (a
    P-touch's or QL's printable width); preview and export show the whole
    tape (`tape` pixels; 0: not endless tape), the strip in its middle and
    the tape the head does not reach white, as it comes out. The border
    and the background are where the printer puts them: on the strip."""
    if image.height >= tape:
        return image
    whole = Image.new("RGB", (image.width, tape), "white")
    whole.paste(image.convert("RGB"), (0, (tape - image.height) // 2))
    whole.info.update(image.info)  # black and white already (render(mono=True))
    return whole


def _mono_parts(label: Label, parts, places, canvas, inks: Inks, geometry, warnings):
    """Black and white: each element's inks against the background under it
    (drawn again where they differ from the first choice), converted on its
    own; a warning for an element that does not show."""
    f, dpi, steps = geometry
    plain = not (label.background_image and label.background_image.data)
    done = []
    for index, (element, part, (x, y)) in enumerate(
        zip(label.elements, parts, places, strict=True), 1
    ):
        local, lost = inks, 0.0
        if not plain and not isinstance(element, PageElement):
            area = canvas.crop((x, y, x + part.width, y + part.height))
            mask = part.getchannel("A").point(imagefx.HALF)
            local = _local_inks(area, mask, inks)
            if _ink_choice(element, local) != _ink_choice(element, inks):
                part = _redrawn(element, part, f, dpi, local, steps)
            lost = _lost(area, mask, _main_ink(element, local), _contrast(element), local.red)
        part = _mono_part(element, part, local, dpi)
        too_much = CODE_LOST_SHARE if isinstance(element, CodeElement) else LOST_SHARE
        if _shows_ink(element) and (lost > too_much or not _ink_shows(element, part, local)):
            warnings.append(RenderWarning("no_contrast", {"n": index}))
        done.append(part)
    return done


def _ink_choice(element: Element, inks: Inks) -> tuple:
    """The black-and-white inks an element gets with these inks: drawn again
    only where another background changes them."""
    if isinstance(element, TextElement):
        mono = typeset.mono_text(element, inks.background, inks.red)
        return (
            mono.color,
            mono.box_color,
            mono.outline_color if element.outline_mm > 0 else "",
            mono.shadow_color if element.shadow_mm > 0 else "",
        )
    if isinstance(element, ImageElement):
        return (inks.color(element.border_color),)
    return (inks.color(getattr(element, "color", "#000000")),)


def _contrast(element: Element) -> int:
    return CODE_CONTRAST if isinstance(element, CodeElement) else CONTRAST


def _main_ink(element: Element, inks: Inks) -> str:
    """The ink that has to stand out against the background ("" for a text
    in a box: the box decides, see _ink_shows)."""
    choice = _ink_choice(element, inks)
    if isinstance(element, TextElement) and (choice[1] or (choice[2] and choice[2] != choice[0])):
        return ""  # a box, or an outline in the other ink: they make it show
    return choice[0] if not isinstance(element, ImageElement) else ""


def _ink_shows(element: Element, part: Image.Image, inks: Inks) -> bool:
    """Whether an element shows against what lies under it, by its inks
    (quick; an image by its pixels): ink and background far enough apart in
    brightness (CONTRAST, codes CODE_CONTRAST); letters on a box in another
    ink than the box; letters cut out of a box: the box against the label."""
    if isinstance(element, ImageElement):
        return _visible(part, inks)
    choice = _ink_choice(element, inks)
    if isinstance(element, TextElement):
        text, box, outline, _shadow = choice
        if element.inverted:  # the box is in the text's colour (mono_text)
            return _ink_visible(text, inks)
        if box:
            return text != box or (bool(outline) and outline != box)
        return any(_ink_visible(ink, inks) for ink in (text, outline) if ink)
    return _ink_visible(choice[0], inks, _contrast(element))


def _ink_visible(ink: str, inks: Inks, minimum: int = CONTRAST) -> bool:
    if inks.red and imagefx.mono_ink(_color(ink), 255, True) == imagefx.RED:
        return not inks.behind_red
    if inks.behind_red:  # black or white on red ink: other inks
        return True
    level = 0 if _color(ink) == (0, 0, 0) else 255
    return abs(level - inks.background) >= minimum


def _shows_ink(element: Element) -> bool:
    """Whether an element is expected to show (a page area keeps its greys,
    an empty shape warns of its own)."""
    return not isinstance(element, PageElement) and not _invisible_shape(element)


def _redrawn(
    element: Element, part: Image.Image, f: _Frame, dpi: float, inks: Inks, steps: tuple[int, int]
):
    """The element drawn again in other inks, at the size it was placed at."""
    try:
        drawn = render_element(element, f.inner, dpi, None, steps=steps, inks=inks)
    except RenderTooLarge:
        raise
    except Exception:  # noqa: BLE001 - it was drawn before: keep that
        return part
    return _at_size(element, drawn, part.size, inks, steps)


# a barcode's narrowest bar at least this wide: thinner bars do not scan
# reliably (most scanners want 0.17 mm or more); one printer dot is code_thin
MIN_MODULE_MM = 0.17


def _code_warnings(label: Label, parts, dpi: float) -> list[RenderWarning]:
    found = []
    for index, (element, part) in enumerate(zip(label.elements, parts, strict=True), 1):
        if not isinstance(element, CodeElement):
            continue
        problems = code_problems(part)
        found += [RenderWarning(key, {"n": index}) for key in problems]
        if "code_thin" not in problems and _narrow(element, part, dpi):
            found.append(RenderWarning("barcode_narrow", {"n": index}))
    return found


def _narrow(element: CodeElement, part: Image.Image, dpi: float) -> bool:
    """A linear barcode whose bars are whole dots but under MIN_MODULE_MM."""
    module = getattr(part, "code_module", None)
    linear = codes.SPECS[element.symbology].layout not in ("matrix", "stacked")
    return linear and module is not None and module * 25.4 / dpi < MIN_MODULE_MM


def _invisible_shape(element: Element) -> bool:
    return (
        isinstance(element, ShapeElement)
        and element.shape in ("rectangle", "ellipse")
        and element.line_mm <= 0
        and element.fill == "none"
    )


def _layout_warnings(label: Label, f: _Frame, dpi: float) -> list[RenderWarning]:
    """A shape that draws nothing; a text whose padding leaves no room."""
    found = []
    for index, e in enumerate(label.elements, 1):
        if _invisible_shape(e):
            found.append(RenderWarning("shape_invisible", {"n": index}))
        elif isinstance(e, TextElement) and _padding_too_large(e, f, dpi):
            found.append(RenderWarning("padding_too_large", {"n": index}))
    return found


def _padding_too_large(element: TextElement, f: _Frame, dpi: float) -> bool:
    extra = typeset.extras(element, dpi)
    return extra.any() and 2 * extra.pad + extra.shadow >= f.inner
