"""Render a Label to a Pillow image at printer resolution.

Label space: x runs along the tape (the label length), y across the tape.
The preview shows this image as is; for printing it is rotated so that the
image width matches the tape width, which is what the printer expects.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageColor, ImageDraw, ImageOps

from zinklabel.model import CodeElement, Element, ImageElement, Label, TextElement

# Resolution of the vivid print mode (labelprinter: 317 lpi)
PRINT_DPI = 317

# The printer cannot cut labels shorter than this
MIN_LENGTH_MM = 15.0

# Default safety margin. The VC-500W loses about 0.5 mm on every side
# (measured); devices and rolls vary, so the app makes this a per-printer
# setting. Content and border keep this distance from the edges, the
# background is always printed edge to edge.
DEFAULT_SAFE_MM = 1.0


@dataclass
class Rendered:
    image: Image.Image  # label space, RGB
    length_mm: float
    warnings: list[str] = field(default_factory=list)
    overflow: bool = False  # content was scaled down to fit a fixed length


def mm_to_px(mm: float, dpi: int = PRINT_DPI) -> int:
    return max(1, round(mm / 25.4 * dpi))


def px_to_mm(px: int, dpi: int = PRINT_DPI) -> float:
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


def render_text(element: TextElement, height: int, dpi: int) -> Image.Image:
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


def render_code(element: CodeElement, height: int) -> Image.Image:
    if not element.data:
        raise ValueError("empty code")
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


def render_element(element: Element, height: int, dpi: int) -> Image.Image:
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


def render(label: Label, dpi: int = PRINT_DPI, safe_mm: float = DEFAULT_SAFE_MM) -> Rendered:
    tape = mm_to_px(label.tape_mm, dpi)
    edge = mm_to_px(label.edge_mm, dpi) if label.edge_mm > 0 else 0
    margin = mm_to_px(label.margin_mm, dpi) if label.margin_mm > 0 else 0
    border = mm_to_px(label.border_mm, dpi) if label.border_mm > 0 else 0
    safe = mm_to_px(safe_mm, dpi) if safe_mm > 0 else 0
    frame = safe + border  # kept free of content on every side
    inner_h = max(1, tape - 2 * (edge + frame))
    warnings: list[str] = []

    parts: list[Image.Image] = []
    for index, element in enumerate(label.elements, 1):
        try:
            part = render_element(element, inner_h, dpi)
        except Exception as e:  # noqa: BLE001 - show any element error, keep going
            warnings.append("element %d: %s" % (index, e))
            part = _placeholder(inner_h)
        if part.height > inner_h:  # fixed-size text taller than the tape
            part = part.resize(
                (max(1, round(part.width * inner_h / part.height)), inner_h),
                Image.Resampling.LANCZOS,
            )
        parts.append(part)

    gaps = margin * max(0, len(parts) - 1)
    content_w = sum(p.width for p in parts) + gaps
    min_len = math.ceil(MIN_LENGTH_MM / 25.4 * dpi)
    overflow = False
    margin_scaled = margin

    if label.auto_length:
        length = max(min_len, content_w + 2 * (margin + frame))
    else:
        length = max(min_len, mm_to_px(label.length_mm, dpi))
        room = length - 2 * (margin + frame)
        if parts and content_w > room > 0:
            overflow = True
            scale = room / content_w
            parts = [
                p.resize(
                    (max(1, round(p.width * scale)), max(1, round(p.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                for p in parts
            ]
            margin_scaled = round(margin * scale)
            content_w = sum(p.width for p in parts) + margin_scaled * (len(parts) - 1)

    img = Image.new("RGB", (length, tape), _color(label.background, "#ffffff"))
    if label.background_image and label.background_image.data:
        try:
            bg = _decoded(label.background_image.data).convert("RGB")
            img.paste(_cover(bg, img.size), (0, 0))
        except Exception as e:  # noqa: BLE001
            warnings.append("background image: %s" % e)

    free = length - 2 * frame - content_w
    if label.auto_length or label.align == "center":
        x = frame + free // 2
    elif label.align == "end":
        x = length - frame - margin - content_w
    else:
        x = frame + margin
    for part in parts:
        y = (tape - part.height) // 2
        img.paste(part, (x, y), part)
        x += part.width + margin_scaled

    if border:  # inside the safety margin so it is never cut off
        draw = ImageDraw.Draw(img)
        draw.rectangle(
            (safe, safe, length - 1 - safe, tape - 1 - safe),
            outline=_color(label.border_color),
            width=border,
        )

    return Rendered(img, px_to_mm(length, dpi), warnings, overflow)


def print_image(rendered: Rendered) -> Image.Image:
    """Rotate to printer orientation: image width = tape width."""
    return rendered.image.rotate(-90, expand=True)
