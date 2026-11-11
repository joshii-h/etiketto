"""Render a Label to a Pillow image at printer resolution.

Label space: x runs along the tape (the label length), y across the tape.
The preview shows this image as is; for printing it is rotated so that the
image width matches the tape width, which is what the printer expects.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping
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


def _text_block(element: TextElement, size_px: float) -> Image.Image:
    """Lay out text with Pango (font fallback, emoji, kerning) into RGBA."""
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
    width = max(1, logical.width)
    height = max(1, logical.height)

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


def render_text(element: TextElement, height: int, dpi: float) -> Image.Image:
    if element.size_pt > 0:
        return _text_block(element, element.size_pt / 72 * dpi)
    # Fit to the tape: the block height scales about linearly with font size
    probe = 100.0
    block = _text_block(element, probe)
    size = probe * height / max(1, block.height)
    block = _text_block(element, size)
    while block.height > height and size > 1:
        size *= 0.97
        block = _text_block(element, size)
    return block


# Decoding a large photo (base64 + EXIF + resample) takes far longer than the
# rest of a render; cache it so typing in a text field stays smooth.
Image.MAX_IMAGE_PIXELS = 80_000_000  # refuse decompression bombs early


@lru_cache(maxsize=8)
def _decoded(data: str) -> Image.Image:
    import base64

    with Image.open(io.BytesIO(base64.b64decode(data))) as src:
        return ImageOps.exif_transpose(src).convert("RGBA")


@lru_cache(maxsize=16)
def _scaled(data: str, height: int) -> Image.Image:
    img = _decoded(data)
    width = max(1, round(img.width * height / img.height))
    return img.resize((width, height), Image.Resampling.LANCZOS)


def render_image(element: ImageElement, height: int) -> Image.Image:
    return _scaled(element.data, height).copy()


def _ean13_ok(data: str) -> bool:
    digits = [int(c) for c in data[:12]]
    check = (10 - sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits)) % 10) % 10
    return int(data[12]) == check


# what Code 128 (sets A and B) can encode: printable ASCII
CODE128_CHARS = frozenset(map(chr, range(32, 127)))


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


def render_code(element: CodeElement, height: int) -> Image.Image:
    check_code(element)
    if element.symbology == "qr":
        import segno

        qr = segno.make(element.data, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=1, border=0, dark=element.color, light=None)
        buf.seek(0)
        with Image.open(buf) as src:
            img = src.convert("RGBA")
        return img.resize((height, height), Image.Resampling.NEAREST)

    import barcode
    from barcode.writer import ImageWriter

    cls = barcode.get_barcode_class(element.symbology)
    writer = ImageWriter()
    code = cls(element.data, writer=writer)
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
        img = src.convert("RGBA")
    # white background -> transparent so the label background shows
    img.putalpha(img.convert("L").point(lambda v: 0 if v == 255 else 255))
    width = max(1, round(img.width * height / img.height))
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
    if isinstance(error, CodeError):
        return RenderWarning(error.key, {"n": index})
    if isinstance(element, ImageElement):
        return RenderWarning("image_unreadable", {"n": index})
    return RenderWarning("element_failed", {"n": index})


def _parts(label: Label, f: _Frame, dpi: float, warnings) -> list[Image.Image]:
    parts = []
    for index, element in enumerate(label.elements, 1):
        try:
            part = render_element(element, f.inner, dpi)
        except Exception as e:  # noqa: BLE001 - show any element error, keep going
            warnings.append(_element_warning(element, index, e))
            part = _placeholder(f.inner)
        if part.height > f.inner:  # fixed-size text taller than the tape
            part = part.resize(
                (max(1, round(part.width * f.inner / part.height)), f.inner),
                Image.Resampling.LANCZOS,
            )
        parts.append(part)
    return parts


def _content_width(parts: list[Image.Image], gap: int) -> int:
    return sum(p.width for p in parts) + gap * max(0, len(parts) - 1)


def _fit_length(label: Label, parts, f: _Frame, dpi: float, warnings, min_length_mm: float):
    """Label length, the (possibly scaled) parts and the gap between them."""
    min_len = max(1, math.ceil(min_length_mm / 25.4 * dpi))
    content_w = _content_width(parts, f.margin)
    if label.auto_length:
        return max(min_len, content_w + 2 * (f.margin + f.frame)), parts, f.margin, False
    length = max(min_len, mm_to_px(label.length_mm, dpi))
    room = length - 2 * (f.margin + f.frame)
    if parts and room <= 0:
        warnings.append(RenderWarning("fixed_too_short"))
    elif parts and content_w > room:
        scale = room / content_w
        parts = [
            p.resize(
                (max(1, round(p.width * scale)), max(1, round(p.height * scale))),
                Image.Resampling.LANCZOS,
            )
            for p in parts
        ]
        return length, parts, round(f.margin * scale), True
    return length, parts, f.margin, False


def _background(label: Label, size: tuple[int, int], warnings) -> Image.Image:
    img = Image.new("RGB", size, _color(label.background, "#ffffff"))
    if label.background_image and label.background_image.data:
        try:
            bg = _decoded(label.background_image.data).convert("RGB")
            img.paste(_cover(bg, img.size), (0, 0))
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
    warnings: list[RenderWarning] = []
    f = _frame(label, dpi, safe_mm, tape_px, bleed_px, warnings)
    parts = _parts(label, f, dpi, warnings)
    length, parts, gap, overflow = _fit_length(label, parts, f, dpi, warnings, min_length_mm)
    img = _background(label, (length, f.tape + 2 * f.bleed), warnings)
    _place(img, label, parts, gap, f)
    _draw_border(img, label, f)
    return Rendered(img, px_to_mm(length, dpi), warnings, overflow)
