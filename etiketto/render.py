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
import warnings
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
    code128_charset, image_unreadable, element_failed, no_room,
    fixed_too_short, background_unreadable)."""

    key: str
    # read-only: a NamedTuple default is one object shared by all warnings
    params: Mapping[str, object] = MappingProxyType({})


class RenderTooLarge(ValueError):
    """An image would exceed the limits set with limited()."""


@dataclass(frozen=True)
class Limits:
    pixels: int  # of any one image made while rendering
    max_length_mm: float | None = None  # of the label, if the printer has a limit


# Untrusted labels (the print server) are rendered within limits: a text of
# thousands of lines or a tiny PNG that unpacks to gigapixels must fail,
# not fill the memory. The window and the command line render without.
_LIMITS: ContextVar[Limits | None] = ContextVar("render_limits", default=None)


@contextlib.contextmanager
def limited(pixels: int, max_length_mm: float | None = None) -> Iterator[None]:
    """Render within limits in this thread while the block runs; images
    are then decoded without the caches (they would keep large ones)."""
    token = _LIMITS.set(Limits(pixels, max_length_mm))
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
Image.MAX_IMAGE_PIXELS = 80_000_000  # refuse decompression bombs early
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
    """An image file opened, a possible decompression bomb an error."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(base64.b64decode(data)), formats=IMAGE_FORMATS) as src:
            yield src


def _header_size(data: str) -> tuple[int, int]:
    """Width and height of an image as shown (EXIF orientation applied),
    read from its header: nothing is decoded."""
    with _opened(data) as src:
        width, height = src.size
        orientation = src.getexif().get(0x0112, 1)
    return (height, width) if orientation in _ROTATED else (width, height)


def _decode(data: str) -> Image.Image:
    with _opened(data) as src:
        _check_size(*src.size)  # before anything is unpacked
        src.load()
        _check_size(*src.size)  # a format may know its size only now
        return ImageOps.exif_transpose(src).convert("RGBA")


def _scale(data: str, height: int) -> Image.Image:
    img = _decoded(data)
    width = max(1, round(img.width * height / img.height))
    _check_size(width, height)
    return img.resize((width, height), Image.Resampling.LANCZOS)


_header_size_cached = lru_cache(maxsize=256)(_header_size)
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
    return _header_size_cached(data)


def _decoded(data: str) -> Image.Image:
    if _LIMITS.get():
        return _per_render("decoded", data, lambda: _decode(data))
    return _decoded_cached(data)


def _scaled(data: str, height: int) -> Image.Image:
    if _LIMITS.get():
        return _per_render("scaled", (data, height), lambda: _scale(data, height))
    return _scaled_cached(data, height)


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
        if not data.isascii() or not data.isdigit() or len(data) not in (12, 13):
            raise CodeError("ean13_digits")
        if len(data) == 13 and not _ean13_ok(data):
            raise CodeError("ean13_check")
    elif element.symbology == "code128" and not CODE128_CHARS.issuperset(data):
        raise CodeError("code128_charset")
    elif element.symbology == "code128" and len(data) > CODE128_MAX:
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


def _barcode(element: CodeElement) -> Image.Image:
    """The barcode at its own size (0.3 mm modules at 300 dpi), white made
    transparent so the label background shows."""
    import barcode
    from barcode.writer import ImageWriter

    cls = barcode.get_barcode_class(element.symbology)
    code = cls(element.data, writer=ImageWriter())
    buf = io.BytesIO()
    code.write(
        buf,
        options={
            "module_width": 0.3,
            "module_height": 10.0,
            "quiet_zone": 1.0,
            "font_size": 8,
            "text_distance": 3.0,
            "dpi": 300,
            "foreground": element.color,
            "background": "white",
        },
    )
    buf.seek(0)
    with Image.open(buf) as src:
        _check_size(*src.size)
        img = src.convert("RGBA")
    img.putalpha(img.convert("L").point(lambda v: 0 if v == 255 else 255))
    return img


def _code_image(element: CodeElement) -> Image.Image:
    """A code at its own small size: drawn larger only at the end."""
    check_code(element)
    return _qr(element) if element.symbology == "qr" else _barcode(element)


def render_code(element: CodeElement, height: int) -> Image.Image:
    img = _code_image(element)
    width = height if element.symbology == "qr" else max(1, round(img.width * height / img.height))
    _check_size(width, height)
    return img.resize((width, height), Image.Resampling.NEAREST)


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
        _check_size(w, h)
        return _resized(_decoded(element.data), w, h)

    return _Part(width, inner, draw)


def _code_part(element: CodeElement, inner: int) -> _Part:
    """A code: measured from its small image, drawn at the final size
    (a QR code on a page is not drawn page-sized while measuring)."""
    small = _code_image(element)
    square = element.symbology == "qr"
    width = inner if square else max(1, round(small.width * inner / small.height))

    def draw(w: int, h: int) -> Image.Image:
        _check_size(w, h)
        return small.resize((w, h), Image.Resampling.NEAREST)

    return _Part(width, inner, draw)


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
        if total > limits.pixels:
            raise RenderTooLarge("%d pixels of image files" % total)


def _part(element: Element, inner: int, dpi: float) -> _Part:
    if isinstance(element, TextElement):
        return _text_part(element, inner, dpi)
    if isinstance(element, ImageElement):
        return _picture_part(element, inner)
    return _code_part(element, inner)


def _measure(label: Label, f: _Frame, dpi: float, warnings) -> list[_Part]:
    _source_budget(label)
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
        return part.draw(width, height)
    except RenderTooLarge:
        raise
    except Exception as e:  # noqa: BLE001 - as while measuring
        if part.element is None:
            raise
        warnings.append(_element_warning(part.element, part.index, e))
        return _placeholder(height).resize((width, height))


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
    token = _RENDER_CACHE.set({}) if _LIMITS.get() else None
    try:
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
