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
import threading
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import NamedTuple

from PIL import Image, ImageColor, ImageDraw, ImageOps

from etiketto.model import CodeElement, Element, ImageElement, Label, TextElement

# Resolution of the vivid print mode (labelprinter: 317 lpi)
PRINT_DPI = 317

# Default safety margin: content and border keep this distance from the
# edges, the background is always printed edge to edge. Measured on a
# VC-500W: ~0.5 mm lost per side plus up to ~1 mm feed skew on long labels.
# Devices and rolls vary, so the app makes it a per-printer setting.
DEFAULT_SAFE_MM = 1.5


class RenderWarning(NamedTuple):
    """A problem with the label; the interface turns key and params into a
    translated message (keys: code_empty, ean13_digits, ean13_check,
    code128_charset, code128_long, barcode_narrow, image_unreadable, element_failed,
    no_room, fixed_too_short, background_unreadable; and from variables.py
    unknown_variable, unclosed_variable, date_out_of_range)."""

    key: str
    # read-only: a NamedTuple default is one object shared by all warnings
    params: Mapping[str, object] = MappingProxyType({})


class RenderTooLarge(ValueError):
    """An image would exceed the limits set with limited()."""


@dataclass(frozen=True)
class Limits:
    pixels: int  # of any one image made while rendering
    max_length_mm: float | None = None  # of the label, if the printer has a limit
    # what every render within these limits holds while it runs (the
    # server's render slots, so print jobs share them with previews)
    slot: Callable[[], contextlib.AbstractContextManager] | None = None

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


def _check_size(width: int, height: int) -> None:
    limits = _LIMITS.get()
    if limits is not None and width * height > limits.pixels:
        raise RenderTooLarge("%d x %d pixels" % (width, height))


class CodeError(ValueError):
    """The content cannot be encoded in the chosen symbology."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


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


def _color(value: str, fallback: str = "#000000") -> tuple[int, int, int]:
    try:
        return ImageColor.getrgb(value)[:3]
    except ValueError:
        return ImageColor.getrgb(fallback)[:3]


# --- elements ---------------------------------------------------------------


def _layout(element: TextElement, size_px: float):
    """The Pango layout of a text at a size, and its logical extents."""
    import cairo
    import gi

    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    desc = Pango.FontDescription.from_string(element.font or "Sans")
    desc.set_weight(Pango.Weight.BOLD if element.bold else Pango.Weight.NORMAL)
    desc.set_style(Pango.Style.ITALIC if element.italic else Pango.Style.NORMAL)
    desc.set_absolute_size(max(1.0, size_px) * Pango.SCALE)

    measure = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout = PangoCairo.create_layout(cairo.Context(measure))
    layout.set_font_description(desc)
    layout.set_alignment(
        {
            "center": Pango.Alignment.CENTER,
            "right": Pango.Alignment.RIGHT,
        }.get(element.align, Pango.Alignment.LEFT)
    )
    # a trailing newline (easy to type) would add an empty line and halve the text
    layout.set_text(element.text.rstrip("\n") or " ", -1)
    _ink, logical = layout.get_pixel_extents()
    return layout, logical


def _text_size(element: TextElement, size_px: float) -> tuple[int, int]:
    """Width and height of the text block at a size, without drawing it."""
    _layout_, logical = _layout(element, size_px)
    return max(1, logical.width), max(1, logical.height)


def _text_block(element: TextElement, size_px: float) -> Image.Image:
    """Lay out text with Pango (font fallback, emoji, kerning) into RGBA."""
    import cairo
    from gi.repository import PangoCairo

    layout, logical = _layout(element, size_px)
    width = max(1, logical.width)
    height = max(1, logical.height)
    _check_size(width, height)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    r, g, b = _color(element.color)
    ctx.set_source_rgb(r / 255, g / 255, b / 255)
    ctx.move_to(-logical.x, -logical.y)
    PangoCairo.update_layout(ctx, layout)
    PangoCairo.show_layout(ctx, layout)
    surface.flush()
    return Image.frombuffer(
        "RGBA",
        (width, height),
        bytes(surface.get_data()),
        "raw",
        "BGRa",
        surface.get_stride(),
        1,
    ).copy()


def text_size_px(element: TextElement, height: int, dpi: float) -> float:
    """The font size of a text: its own, or the largest that fits the
    height; found by measuring, nothing is drawn."""
    if element.size_pt > 0:
        return element.size_pt / 72 * dpi
    # Fit to the tape: the block height scales about linearly with font size
    probe = 100.0
    size = probe * height / _text_size(element, probe)[1]
    while _text_size(element, size)[1] > height and size > 1:
        size *= 0.97
    return size


def render_text(element: TextElement, height: int, dpi: float) -> Image.Image:
    return _text_block(element, text_size_px(element, height, dpi))


# Decoding a large photo (base64 + EXIF + resample) takes far longer than the
# rest of a render; cache it so typing in a text field stays smooth.
# refuse decompression bombs early: checked by _opened() itself, so Pillow's
# own check (a warning from 80 MP, an error from twice that) is turned off;
# a warning filter to keep it quiet would not be safe in threads
IMAGE_FILE_PIXELS = 80_000_000
Image.MAX_IMAGE_PIXELS = None
# image files a label may embed: what Pillow decodes itself, quickly. Not
# EPS (Ghostscript would run it), not ICO/ICNS/CUR (decoded when opened,
# before their size can be checked), not the dozens of rarer formats.
IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "BMP", "WEBP", "TIFF")
_ROTATED = (5, 6, 7, 8)  # EXIF orientations that swap width and height
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


_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _orientation(src: Image.Image) -> int:
    """The EXIF orientation of an opened file, from what was read when it
    was opened: JPEG and WebP keep EXIF in the header, PNG an eXIf chunk
    before the image data (one after it is not looked for: that would
    decode the image). TIFF is already turned when it is opened."""
    raw = src.info.get("exif")
    if src.format == "TIFF" or not raw:
        return 1
    exif = Image.Exif()
    try:
        exif.load(raw)
    except Exception:  # noqa: BLE001 - broken EXIF: the image as it is
        return 1
    value = exif.get(0x0112, 1)
    return value if value in _TRANSPOSE else 1


def _header_size(data: str) -> tuple[int, int]:
    """Width and height of an image as shown (EXIF orientation applied),
    read from its header: nothing is decoded."""
    with _opened(data) as src:
        width, height = src.size
        orientation = _orientation(src)
    return (height, width) if orientation in _ROTATED else (width, height)


def _decode(data: str) -> Image.Image:
    with _opened(data) as src:
        _check_size(*src.size)  # before anything is unpacked
        # read before loading, as _header_size does: a PNG's eXIf chunk after
        # the image data only shows after loading, and would turn the image
        # the measuring did not turn
        method = _TRANSPOSE.get(_orientation(src))
        src.load()
        _check_size(*src.size)  # a format may know its size only now
        # one copy at a time: converted only when it is no RGBA yet, turned after
        image = src if src.mode == "RGBA" else src.convert("RGBA")
    return image.transpose(method) if method is not None else image


def _scale(data: str, height: int) -> Image.Image:
    img = _decoded(data)
    width = max(1, round(img.width * height / img.height))
    _check_size(width, height)
    return img.resize((width, height), Image.Resampling.LANCZOS)


def _fit_image(data: str, width: int, height: int) -> Image.Image:
    """An image at a size of its own (a label scaled down to its length),
    resampled once from the decoded file. reducing_gap makes it fast for a
    large photo (Pillow first reduces by a whole factor): typing in such a
    label changes the size with every key. No full-height copy is made (a
    label of many narrow images made one of 16 megapixels for each)."""
    _check_size(width, height)
    image = _decoded(data)
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)


class _SizeCache:
    """Header sizes by the image's hash and length: a few small entries,
    never the image data (a label's images can be megabytes each). The
    hash of a str is computed once and kept by Python, so a lookup does not
    read the megabytes again on every render."""

    def __init__(self, size: int = 64):
        self._size = size
        self._entries: dict[tuple[int, int], tuple[int, int]] = {}
        self._lock = threading.Lock()

    def get(self, data: str) -> tuple[int, int]:
        key = (hash(data), len(data))
        with self._lock:
            if key in self._entries:
                self._entries[key] = self._entries.pop(key)  # the newest
                return self._entries[key]
        size = _header_size(data)
        with self._lock:
            self._entries[key] = size
            while len(self._entries) > self._size:
                self._entries.pop(next(iter(self._entries)))
        return size


_sizes = _SizeCache()
_decoded_cached = lru_cache(maxsize=8)(_decode)
_scaled_cached = lru_cache(maxsize=16)(_scale)


def _per_render(kind: str, key, make):
    """The result of `make` once per render (within limits), else None."""
    cache = _RENDER_CACHE.get()
    if cache is None:
        return make()
    if (kind, key) not in cache:
        cache[(kind, key)] = make()
    return cache[(kind, key)]


def _image_size(data: str) -> tuple[int, int]:
    if _LIMITS.get():
        return _per_render("size", data, lambda: _header_size(data))
    return _sizes.get(data)


def _decoded(data: str) -> Image.Image:
    if _LIMITS.get():
        return _per_render("decoded", data, lambda: _decode(data))
    return _decoded_cached(data)


def _scaled(data: str, height: int) -> Image.Image:
    if _LIMITS.get():
        return _per_render("scaled", (data, height), lambda: _scale(data, height))
    return _scaled_cached(data, height)


def _fitted(data: str, width: int, height: int) -> Image.Image:
    if _LIMITS.get():
        key = (data, width, height)
        return _per_render("fitted", key, lambda: _fit_image(data, width, height))
    return _fit_image(data, width, height)


def render_image(element: ImageElement, height: int) -> Image.Image:
    return _scaled(element.data, height).copy()


def _ean13_ok(data: str) -> bool:
    digits = [int(c) for c in data[:12]]
    check = (10 - sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits)) % 10) % 10
    return int(data[12]) == check


# what Code 128 (sets A and B) can encode: printable ASCII
CODE128_CHARS = frozenset(map(chr, range(32, 127)))
# longer codes are wider than any label and no scanner reads them; they
# also took hundreds of megabytes to draw
CODE128_MAX = 200


def check_code(element: CodeElement) -> None:
    """Raise CodeError if the content cannot be encoded (python-barcode would
    silently drop characters or replace a wrong check digit)."""
    data = element.data
    if not data:
        raise CodeError("code_empty")
    if element.symbology == "ean13":
        _check_ean13(data)
    elif element.symbology == "code128":
        _check_code128(data)


def _check_ean13(data: str) -> None:
    if not data.isascii() or not data.isdigit() or len(data) not in (12, 13):
        raise CodeError("ean13_digits")
    if len(data) == 13 and not _ean13_ok(data):
        raise CodeError("ean13_check")


def _check_code128(data: str) -> None:
    if not CODE128_CHARS.issuperset(data):
        raise CodeError("code128_charset")
    if len(data) > CODE128_MAX:
        raise CodeError("code128_long")


def _qr(element: CodeElement) -> Image.Image:
    """The QR code, one pixel per module."""
    import segno

    qr = segno.make(element.data, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=1, border=0, dark=element.color, light=None)
    buf.seek(0)
    with Image.open(buf) as src:
        return src.convert("RGBA")


# a barcode's geometry: modules of 0.3 mm, bars 10 mm high, a line of
# digits below; the quiet zones the standards ask for, in modules
BAR_MM = 0.3
BAR_HEIGHT_MM = 10.0
DIGITS_MM = 4.5
QUIET = {"code128": (10, 10), "ean13": (11, 7)}
# how the barcodes of Etiketto before 1.1 were laid out (python-barcode's
# image): a label keeps its length with the new drawing
OLD_QUIET_MM = 1.0
OLD_HEIGHT_MM = 10.0 + 2 * 1.0 + 8 * 25.4 / 72 / 2 + 3.0  # bars, margins, half the digits, gap
# a module at least this wide and this many dots: thinner bars do not scan
# reliably (most scanners want 0.17 mm or more)
MIN_MODULE_MM = 0.17
MIN_MODULE_DOTS = 2


@dataclass(frozen=True)
class _Bars:
    """A barcode as its modules ("1" a bar, quiet zones included) and the
    digits printed below: a few hundred characters, no image."""

    modules: str
    text: str
    core: int  # modules without the quiet zones

    def width_for(self, height: int) -> int:
        """The width the barcode takes at a height: the proportions it had
        when python-barcode drew it (1 mm quiet zones, 0.3 mm modules, 10 mm
        bars and the digits below), so labels keep their length. The bars
        are then drawn whole pixels per module within that width; only
        where that would be less than one pixel per module is the barcode
        made wider, one pixel per module (it would not scan otherwise), so
        a tape at a low resolution can get a little longer."""
        old_width_mm = 2 * OLD_QUIET_MM + self.core * BAR_MM
        width = round(height * old_width_mm / OLD_HEIGHT_MM)
        return max(1, width, len(self.modules))


def _make_bars(symbology: str, data: str) -> _Bars:
    import barcode

    check_code(CodeElement(data=data, symbology=symbology))  # type: ignore[arg-type]
    code = barcode.get(symbology, data)
    left, right = QUIET[symbology]
    text = code.get_fullcode() if symbology == "ean13" else data
    core = "".join(code.build())
    return _Bars("0" * left + core + "0" * right, text, len(core))


def _make_qr(data: str, color: str) -> Image.Image:
    element = CodeElement(data=data, symbology="qr", color=color)
    check_code(element)
    return _qr(element)


# small: a barcode is a string, a QR code at most 177 x 177 pixels
_bars_cached = lru_cache(maxsize=256)(_make_bars)
_qr_cached = lru_cache(maxsize=64)(_make_qr)


def _bars(element: CodeElement) -> _Bars:
    key = (element.symbology, element.data)
    if _LIMITS.get():
        return _per_render("bars", key, lambda: _make_bars(*key))
    return _bars_cached(*key)


def _code_image(element: CodeElement) -> Image.Image:
    """A QR code, one pixel per module: drawn larger only at the end. The
    same code is made once per render (within limits), else cached."""
    key = (element.data, element.color)
    if _LIMITS.get():
        return _per_render("qr", key, lambda: _make_qr(*key))
    return _qr_cached(*key)


def _module_px(bars: _Bars, width: int) -> int:
    """Whole pixels per module at a width."""
    return width // len(bars.modules)


def _barcode_at(element: CodeElement, width: int, height: int) -> Image.Image:
    """The bars drawn whole modules wide (every module the same number of
    pixels, centred), the digits below drawn as text at their final size.
    Narrower than one pixel per module the bars are squeezed (and warned
    about, see _bar_warning)."""
    _check_size(width, height)
    bars = _bars(element)
    count = len(bars.modules)
    bars_height = max(1, round(height * BAR_HEIGHT_MM / (BAR_HEIGHT_MM + DIGITS_MM)))
    row = Image.frombytes("L", (count, 1), bytes(255 if m == "1" else 0 for m in bars.modules))
    per = _module_px(bars, width)
    mask = row.resize((count * per if per else width, bars_height), Image.Resampling.NEAREST)
    ink = Image.new("RGBA", mask.size, _color(element.color) + (255,))
    ink.putalpha(mask)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.paste(ink, ((width - mask.width) // 2, 0))
    text_height = height - bars_height
    if text_height >= 4:
        digits = _digits(element, bars.text, width, round(text_height * 0.8))
        x = (width - digits.width) // 2
        image.alpha_composite(
            digits, (max(0, x), bars_height + (text_height - digits.height) // 2)
        )
    return image


def _bar_warning(element: CodeElement, width: int, dpi: float) -> str | None:
    """Modules narrower than MIN_MODULE_MM or MIN_MODULE_DOTS do not scan well."""
    per = _module_px(_bars(element), width)
    if per < MIN_MODULE_DOTS or per * 25.4 / dpi < MIN_MODULE_MM:
        return "barcode_narrow"
    return None


def warm_up() -> None:
    """Draw a small text once. PyGObject sets up Pango's types on first use,
    and that is not safe in several threads at once (texts then failed
    until a restart): the server calls this in its main thread before any
    request or print job renders. (The window renders in the main thread,
    where GTK has set up Pango already.)"""
    _text_block(TextElement(text="x"), 10)


def render_code(element: CodeElement, height: int) -> Image.Image:
    if element.symbology == "qr":
        return _nearest(_code_image(element), height, height)
    return _barcode_at(element, _bars(element).width_for(height), height)


def render_element(element: Element, height: int, dpi: float) -> Image.Image:
    if isinstance(element, TextElement):
        return render_text(element, height, dpi)
    if isinstance(element, ImageElement):
        return render_image(element, height)
    return render_code(element, height)


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
    frame: int  # kept free of content on every side (safety margin + border)
    inner: int  # content height


def _frame(label: Label, dpi: float, safe_mm: float, tape_px, bleed_px, warnings) -> _Frame:
    tape = tape_px or mm_to_px(label.tape_mm, dpi)
    edge = mm_to_px(label.edge_mm, dpi) if label.edge_mm > 0 else 0
    margin = mm_to_px(label.margin_mm, dpi) if label.margin_mm > 0 else 0
    border = mm_to_px(label.border_mm, dpi) if label.border_mm > 0 else 0
    safe = mm_to_px(safe_mm, dpi) if safe_mm > 0 else 0
    frame = safe + border
    inner = tape - 2 * (edge + frame)
    if inner < mm_to_px(2, dpi):
        warnings.append(RenderWarning("no_room"))
        frame = min(frame, max(0, tape // 2 - edge - 1))
        inner = max(1, tape - 2 * (edge + frame))
    return _Frame(tape, max(0, bleed_px), margin, safe, border, frame, inner)


def _element_warning(element: Element, index: int, error: Exception) -> RenderWarning:
    if isinstance(error, CodeError) and error.key == "code128_long":
        return RenderWarning(error.key, {"n": index, "max": CODE128_MAX})
    if isinstance(error, CodeError):
        return RenderWarning(error.key, {"n": index})
    if isinstance(element, ImageElement):
        return RenderWarning("image_unreadable", {"n": index})
    return RenderWarning("element_failed", {"n": index})


@dataclass
class _Part:
    """An element measured: its size in the label, and how to draw it at
    another size (texts are drawn at the size they end up with, never
    drawn large and scaled down: a large page would need gigapixels)."""

    width: int
    height: int
    draw: Callable[[int, int], Image.Image]
    element: Element | None = None  # for the warning if drawing fails
    index: int = 0
    # a warning the part has at the size it is drawn at (a barcode too narrow)
    warning: Callable[[int, int], str | None] | None = None


def _resized(image: Image.Image, width: int, height: int) -> Image.Image:
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _text_part(element: TextElement, inner: int, dpi: float) -> _Part:
    size = text_size_px(element, inner, dpi)
    width, height = _text_size(element, size)
    if height > inner:  # fixed-size text taller than the tape
        width, height = max(1, round(width * inner / height)), inner
    natural_height = _text_size(element, size)[1]

    def draw(w: int, h: int) -> Image.Image:
        if (w, h) == _text_size(element, size):
            return _text_block(element, size)
        return _resized(_text_block(element, size * h / natural_height), w, h)

    return _Part(width, height, draw)


def _picture_part(element: ImageElement, inner: int) -> _Part:
    """An image file: measured from its header; drawn at the final size
    straight from the file (no full-height copy of a very wide image)."""
    source_w, source_h = _image_size(element.data)
    width = max(1, round(source_w * inner / source_h))

    def draw(w: int, h: int) -> Image.Image:
        if (w, h) == (width, inner):
            return render_image(element, inner)
        return _fitted(element.data, w, h).copy()

    return _Part(width, inner, draw)


def _code_part(element: CodeElement, inner: int, dpi: float) -> _Part:
    """A code: measured from its modules, drawn at the final size (a QR
    code on a page is not drawn page-sized while measuring)."""
    if element.symbology == "qr":
        small = _code_image(element)
        return _Part(inner, inner, lambda w, h: _nearest(small, w, h))
    width = _bars(element).width_for(inner)
    return _Part(
        width,
        inner,
        lambda w, h: _barcode_at(element, w, h),
        warning=lambda w, h: _bar_warning(element, w, dpi),
    )


def _nearest(image: Image.Image, width: int, height: int) -> Image.Image:
    _check_size(width, height)
    return image.resize((width, height), Image.Resampling.NEAREST)


def _digits(element: CodeElement, digits: str, width: int, height: int) -> Image.Image:
    """The human-readable line of a barcode, fitted into width and height."""
    text = TextElement(text=digits, font="Sans", bold=False, color=element.color)
    size = text_size_px(text, max(1, height), 96)
    text_width = _text_size(text, size)[0]
    if text_width > width:
        size *= width / text_width
    return _text_block(text, size)


def _image_part(image: Image.Image, inner: int) -> _Part:
    if image.height > inner:
        image = _resized(image, max(1, round(image.width * inner / image.height)), inner)
    return _Part(image.width, image.height, lambda w, h: _resized(image, w, h))


def _source_budget(label: Label) -> None:
    """Within limits: the image files of a label together (each counted
    once) may not unpack to more than the pixel budget; checked from their
    headers before any is decoded."""
    limits = _LIMITS.get()
    if limits is None:
        return
    files = {e.data for e in label.elements if isinstance(e, ImageElement) and e.data}
    if label.background_image and label.background_image.data:
        files.add(label.background_image.data)
    total = 0
    for data in files:
        try:
            width, height = _image_size(data)
        except Exception:  # noqa: BLE001 - an unreadable file becomes its warning later
            continue
        total += width * height
        if total > limits.image_pixels:
            raise RenderTooLarge("%d pixels of image files" % total)


def _part(element: Element, inner: int, dpi: float) -> _Part:
    if isinstance(element, TextElement):
        return _text_part(element, inner, dpi)
    if isinstance(element, ImageElement):
        return _picture_part(element, inner)
    return _code_part(element, inner, dpi)


def _code_budget(label: Label) -> None:
    """Within limits: the small images of the QR codes (each different one
    once) are held until the label is drawn; together within the budget.
    Barcodes are kept as strings of modules."""
    limits = _LIMITS.get()
    if limits is None:
        return
    total = 0
    codes = {
        (e.data, e.color)
        for e in label.elements
        if isinstance(e, CodeElement) and e.symbology == "qr"
    }
    for data, color in codes:
        try:
            small = _code_image(CodeElement(data=data, color=color))
        except RenderTooLarge:
            raise
        except Exception:  # noqa: BLE001 - becomes its warning when measured
            continue
        total += small.width * small.height
        if total > limits.image_pixels:
            raise RenderTooLarge("%d pixels of codes" % total)


def _measure(label: Label, f: _Frame, dpi: float, warnings) -> list[_Part]:
    _source_budget(label)
    _code_budget(label)
    parts = []
    for index, element in enumerate(label.elements, 1):
        try:
            part = _part(element, f.inner, dpi)
        except RenderTooLarge:
            raise  # over the limits of an untrusted label: refused, not drawn
        except Exception as e:  # noqa: BLE001 - show any element error, keep going
            warnings.append(_element_warning(element, index, e))
            part = _image_part(_placeholder(f.inner), f.inner)
        part.element, part.index = element, index
        parts.append(part)
    return parts


def _content_width(parts, gap: int) -> int:
    return sum(p.width for p in parts) + gap * max(0, len(parts) - 1)


def _fit_length(label: Label, parts, f: _Frame, dpi: float, warnings, min_length_mm: float):
    """Label length, the scale of the parts (None: as they are), the gap
    between them and whether the content was scaled down."""
    min_len = max(1, math.ceil(min_length_mm / 25.4 * dpi))
    content_w = _content_width(parts, f.margin)
    if label.auto_length:
        return max(min_len, content_w + 2 * (f.margin + f.frame)), None, f.margin, False
    length = max(min_len, mm_to_px(label.length_mm, dpi))
    room = length - 2 * (f.margin + f.frame)
    if parts and room <= 0:
        warnings.append(RenderWarning("fixed_too_short"))
    elif parts and content_w > room:
        scale = room / content_w
        return length, scale, round(f.margin * scale), True
    return length, None, f.margin, False


def _draw(part: _Part, width: int, height: int, warnings) -> Image.Image:
    """One part at its size; a file that fails only now (its header was
    fine) becomes the crossed-out box and its warning."""
    try:
        image = part.draw(width, height)
    except RenderTooLarge:
        raise
    except Exception as e:  # noqa: BLE001 - as while measuring
        if part.element is None:
            raise
        warnings.append(_element_warning(part.element, part.index, e))
        return _placeholder(height).resize((width, height))
    if part.warning is not None and (key := part.warning(width, height)):
        warnings.append(RenderWarning(key, {"n": part.index}))
    return image


def _draw_parts(parts: list[_Part], scale: float | None, warnings) -> list[Image.Image]:
    """The parts at their final size; within limits, all of them together
    stay within the pixel budget (many small parts add up)."""
    limits = _LIMITS.get()
    used = 0
    images = []
    for part in parts:
        width, height = part.width, part.height
        if scale is not None:
            width, height = max(1, round(width * scale)), max(1, round(height * scale))
        used += width * height
        if limits is not None and used > limits.pixels:
            raise RenderTooLarge("%d pixels of elements" % used)
        images.append(_draw(part, width, height, warnings))
    return images


def _background(label: Label, size: tuple[int, int], warnings) -> Image.Image:
    img = Image.new("RGB", size, _color(label.background, "#ffffff"))
    if label.background_image and label.background_image.data:
        try:
            bg = _decoded(label.background_image.data).convert("RGB")
            img.paste(_cover(bg, img.size), (0, 0))
        except RenderTooLarge:
            raise
        except Exception:  # noqa: BLE001
            warnings.append(RenderWarning("background_unreadable"))
    return img


def _place(img: Image.Image, label: Label, parts, gap: int, f: _Frame) -> None:
    length = img.width
    content_w = _content_width(parts, gap)
    if label.auto_length or label.align == "center":
        x = f.frame + (length - 2 * f.frame - content_w) // 2
    elif label.align == "end":
        x = length - f.frame - f.margin - content_w
    else:
        x = f.frame + f.margin
    for part in parts:
        y = f.bleed + (f.tape - part.height) // 2
        img.paste(part, (x, y), part)
        x += part.width + gap


def _draw_border(img: Image.Image, label: Label, f: _Frame) -> None:
    if not f.border:
        return
    # inside the safety margin so it is never cut off
    top, bottom = f.bleed + f.safe, f.bleed + f.tape - 1 - f.safe
    ImageDraw.Draw(img).rectangle(
        (f.safe, top, img.width - 1 - f.safe, bottom),
        outline=_color(label.border_color),
        width=f.border,
    )


def _check_label(length: int, height: int, dpi: float) -> None:
    """Within the limits, before the label's image is made."""
    limits = _LIMITS.get()
    if limits is None:
        return
    if limits.max_length_mm is not None and px_to_mm(length, dpi) > limits.max_length_mm + 1:
        raise RenderTooLarge("%.0f mm" % px_to_mm(length, dpi))
    _check_size(length, height)


def render(
    label: Label,
    dpi: float = PRINT_DPI,
    safe_mm: float = DEFAULT_SAFE_MM,
    tape_px: int | None = None,
    bleed_px: int = 0,
    min_length_mm: float = 0.0,
) -> Rendered:
    """tape_px overrides the tape height from the nominal width; bleed_px adds
    background beyond both tape edges (the content keeps its place);
    min_length_mm is the printer's shortest label (Capabilities.min_length_mm)."""
    limits = _LIMITS.get()
    token = _RENDER_CACHE.set({}) if limits else None
    slot = limits.slot() if limits and limits.slot else contextlib.nullcontext()
    try:
        with slot:
            return _render(label, dpi, safe_mm, tape_px, bleed_px, min_length_mm)
    finally:
        if token is not None:
            _RENDER_CACHE.reset(token)


def _render(label: Label, dpi: float, safe_mm: float, tape_px, bleed_px, min_length_mm):
    warnings: list[RenderWarning] = []
    f = _frame(label, dpi, safe_mm, tape_px, bleed_px, warnings)
    measured = _measure(label, f, dpi, warnings)
    length, scale, gap, overflow = _fit_length(label, measured, f, dpi, warnings, min_length_mm)
    _check_label(length, f.tape + 2 * f.bleed, dpi)  # before anything large is drawn
    parts = _draw_parts(measured, scale, warnings)
    img = _background(label, (length, f.tape + 2 * f.bleed), warnings)
    _place(img, label, parts, gap, f)
    _draw_border(img, label, f)
    return Rendered(img, px_to_mm(length, dpi), warnings, overflow)
