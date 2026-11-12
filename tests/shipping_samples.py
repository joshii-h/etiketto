"""Synthetic carrier documents: A4 pages with a shipping label somewhere,
receipts and instructions next to it, drawn with cairo as PDF or Pillow as
image. No real carrier layout, only what the detection looks at."""

from __future__ import annotations

import io
import random

import cairo
from PIL import Image, ImageChops, ImageDraw, ImageFilter

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


def gappy_label(ctx, x: float, y: float) -> None:
    """A label of LABEL size without a frame and with wide blank space
    between its sections: a black header band, sender, receiver, two
    barcodes and a footer."""
    w = LABEL[0]
    ctx.set_source_rgb(0, 0, 0)
    ctx.rectangle(x, y, w, 28)
    ctx.fill()
    _text(ctx, x + 8, y + 60, 3, 9, 40)
    _text(ctx, x + 8, y + 130, 5, 12, 30)
    _bars(ctx, x + 12, y + 240, w - 24, 60)
    _bars(ctx, x + 12, y + 340, w - 24, 70, seed=3)
    _text(ctx, x + 8, y + 425, 1, 8, 40)


def instructions(ctx, y: float = 560) -> None:
    """Lines of text across the lower half of an A4 page."""
    _text(ctx, 40, y, 12, 9, 80)


def drawn_page(draw, size=A4, dpi: float = 120) -> Image.Image:
    """A page drawn by draw(ctx) in points, as an image at dpi."""
    scale = dpi / 72
    pixels = (round(size[0] * scale), round(size[1] * scale))
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, *pixels)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    ctx.set_source_rgb(0, 0, 0)
    draw(ctx)
    surface.flush()
    return Image.frombuffer(
        "RGB", pixels, bytes(surface.get_data()), "raw", "BGRX", surface.get_stride(), 1
    ).copy()


def page_pdf(draw, size=A4, pages: int = 1) -> bytes:
    """A PDF of pages drawn by draw(ctx, page index) in points."""
    buf = io.BytesIO()
    surface = cairo.PDFSurface(buf, *size)
    ctx = cairo.Context(surface)
    for index in range(pages):
        ctx.set_source_rgb(0, 0, 0)
        draw(ctx, index)
        ctx.show_page()
    surface.finish()
    return buf.getvalue()


def raw_pdf(pages: int = 2, media=(0, 0, 595.32, 841.92), rotate: int = 0, crop=None) -> bytes:
    """A PDF written by hand, as other programs write them: a page size in
    other rounding or with an offset, turned (/Rotate) or cut (/CropBox);
    on each page bars, a frame and a grey band."""
    x0, y0 = media[0], media[1]
    content = b"0 g %.2f %.2f 300 200 re S\n" % (x0 + 30, y0 + 500)
    for i in range(40):
        content += b"%.3f %.2f %.3f 80 re f\n" % (x0 + 40 + i * 6.3, y0 + 560, 1 + i % 3)
    content += b"0.5 g %.2f %.2f 200 30 re f\n" % (x0 + 40, y0 + 420)
    page_keys = b"/MediaBox [%s]" % b" ".join(b"%g" % v for v in media)
    if rotate:
        page_keys += b" /Rotate %d" % rotate
    if crop:
        page_keys += b" /CropBox [%s]" % b" ".join(b"%g" % v for v in crop)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(pages))
    objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages))
    for i in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R %s /Contents %d 0 R >>" % (page_keys, 4 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


MODULE = 0.33 * MM  # narrowest bar of the sample code


def code_bars(ctx, x: float, y: float, height: float, n: int = 60, seed: int = 4) -> None:
    """n bars of 1 to 4 modules with spaces of 1 to 4 (beyond a label's
    edge if they do not fit)."""
    rng = random.Random(seed)
    pos = x
    for _ in range(n):
        width = rng.randint(1, 4) * MODULE
        ctx.rectangle(pos, y, width, height)
        pos += width + rng.randint(1, 4) * MODULE
    ctx.fill()


CODE_Y, CODE_H = 260, 70  # where the bars of code_label() are, in points
CODE_RUNS = 115  # bars and spaces of code_bars() left of the label's right edge


def code_label(ctx, logo: float = 0, grey_text: bool = False) -> None:
    """A 4 × 6 in label: an address, bars, a grey logo of logo points if
    asked, grey small print if asked."""
    ctx.set_source_rgb(0, 0, 0)
    ctx.select_font_face("Sans")
    ctx.set_font_size(11)
    for i in range(6):
        ctx.move_to(12, 150 + i * 14)
        ctx.show_text("Erika Mustermann Heidestrasse %d" % i)
    code_bars(ctx, 14, CODE_Y, CODE_H)
    if logo:
        ctx.set_source_rgb(0.6, 0.6, 0.6)
        ctx.arc(30 + logo / 2, 20 + logo / 2, logo / 2, 0, 6.3)
        ctx.fill()
    if grey_text:
        ctx.set_source_rgb(0.5, 0.5, 0.5)
        ctx.set_font_size(7)
        for i in range(3):
            ctx.move_to(12, 400 + i * 9)
            ctx.show_text("Terms and conditions apply, see the carrier's website")


def scanned_label(dpi: float, kind: str = "png", logo: float = 0) -> bytes:
    """code_label() as an image file at dpi: "png" (clean), "jpeg" (quality
    75), "soft" (blurred, ink 25, paper 245) or "real" (blurred, ink 55,
    paper 235, noise)."""
    image = drawn_page(lambda c: code_label(c, logo), LABEL, dpi)
    if kind in ("soft", "real"):
        ink, paper = (25, 245) if kind == "soft" else (55, 235)
        grey = image.convert("L").filter(ImageFilter.GaussianBlur(0.6))
        image = grey.point(lambda v: ink + v * (paper - ink) / 255)
        if kind == "real":
            noise = Image.effect_noise(image.size, 4)  # around 128
            image = ImageChops.add(image, noise, 1, -128)
    buf = io.BytesIO()
    if kind == "jpeg":
        image.save(buf, format="JPEG", quality=75, dpi=(dpi, dpi))
    else:
        image.save(buf, format="PNG", dpi=(dpi, dpi))
    return buf.getvalue()
