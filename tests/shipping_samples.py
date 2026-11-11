"""Synthetic carrier documents: A4 pages with a shipping label somewhere,
receipts and instructions next to it, drawn with cairo as PDF or Pillow as
image. No real carrier layout, only what the detection looks at."""

from __future__ import annotations

import io
import random

import cairo
from PIL import Image, ImageDraw

A4 = (595.28, 841.89)  # points
MM = 72 / 25.4
LABEL = (101.6 * MM, 152.4 * MM)  # 4 × 6 in


def _bars(ctx, x: float, y: float, width: float, height: float, seed: int = 1) -> None:
    """A Code 128 lookalike: bars of 1 to 4 modules."""
    rng = random.Random(seed)
    module = 0.33 * MM
    pos = x
    while pos < x + width - 4 * module:
        bar = rng.randint(1, 4) * module
        ctx.rectangle(pos, y, bar, height)
        pos += bar + rng.randint(1, 3) * module
    ctx.fill()


def _text(ctx, x: float, y: float, lines: int, size: float = 9, width: int = 40) -> None:
    ctx.select_font_face("Sans")
    ctx.set_font_size(size)
    for i in range(lines):
        ctx.move_to(x, y + i * size * 1.3)
        ctx.show_text(("Musterstrasse 12 8000 Zuerich " * 6)[: max(1, width - i % 7)])


def draw_label(ctx, x: float, y: float, w: float, h: float, frame: bool = True) -> None:
    """A label at (x, y) of w × h points: sender, receiver, barcode, footer;
    with a frame and divider lines, or without any lines."""
    ctx.set_source_rgb(0, 0, 0)
    if frame:
        ctx.set_line_width(1.5)
        ctx.rectangle(x, y, w, h)
        ctx.stroke()
        for part in (0.2, 0.5):
            ctx.move_to(x, y + h * part)
            ctx.line_to(x + w, y + h * part)
            ctx.stroke()
    columns = max(10, int(w / 6.5))  # characters that fit across at 10 pt
    _text(ctx, x + 8, y + 16, 4, 9, columns)
    _text(ctx, x + 8, y + h * 0.24, 7, 12, int(columns * 0.8))
    _bars(ctx, x + 12, y + h * 0.54, w - 24, h * 0.16)
    _text(ctx, x + 8, y + h * 0.76, 6, 9, columns)
    # a 2D code in the lower corner
    ctx.rectangle(x + w - 0.3 * min(w, h) - 8, y + h * 0.24, 0.3 * min(w, h), 0.3 * min(w, h))
    ctx.fill()


def a4_pdf(layout: str = "top-left", frame: bool = True, pages: int = 1) -> bytes:
    """layout: "top-left" (label in the upper left quarter, receipt on the
    right, instructions below), "top" (landscape label in the upper half),
    "alone" (only the label on the page)."""
    buf = io.BytesIO()
    surface = cairo.PDFSurface(buf, *A4)
    ctx = cairo.Context(surface)
    for _ in range(pages):
        _draw_page(ctx, layout, frame)
        ctx.show_page()
    surface.finish()
    return buf.getvalue()


def _draw_page(ctx, layout: str, frame: bool) -> None:
    w, h = LABEL
    if layout == "top":  # sideways label: long side across the page
        draw_label(ctx, 40, 50, h, w, frame)
    else:
        draw_label(ctx, 30, 30, w, h, frame)
    if layout == "alone":
        return
    if layout == "top-left":
        _text(ctx, 360, 40, 30, 8, 30)  # receipt next to the label
    # instructions below a dashed cut line
    ctx.set_dash([4, 4])
    ctx.set_line_width(0.5)
    ctx.move_to(20, 500)
    ctx.line_to(575, 500)
    ctx.stroke()
    ctx.set_dash([])
    _text(ctx, 40, 560, 12, 9, 80)


def page_image(
    layout: str = "top-left", dpi: int = 150, noise: bool = False, frame: bool = True
) -> Image.Image:
    """The page as a scan would be: grey paper and dust if noise."""
    scale = dpi / 72
    size = (round(A4[0] * scale), round(A4[1] * scale))
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, *size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    _draw_page(ctx, layout, frame)
    surface.flush()
    image = Image.frombuffer(
        "RGB", size, bytes(surface.get_data()), "raw", "BGRX", surface.get_stride(), 1
    ).copy()
    return _scanned(image) if noise else image


def _scanned(image: Image.Image) -> Image.Image:
    rng = random.Random(7)
    grey = Image.new("RGB", image.size, (226, 224, 220))
    image = Image.composite(image, grey, image.convert("L").point(lambda v: 255 if v < 128 else 0))
    draw = ImageDraw.Draw(image)
    for _ in range(image.width * image.height // 1500):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        size = rng.choice((0, 0, 1))
        draw.rectangle((x, y, x + size, y + size), fill=(40, 40, 40))
    return image


def encode(image: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()
