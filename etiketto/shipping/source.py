"""PDF pages and images as the source of a shipping label.

PDF pages are drawn by Poppler (through GObject introspection) straight at
the size they are printed: lines and barcodes stay sharp at any printer
resolution and nothing is scaled after drawing. Vector content is drawn
without anti-aliasing, so bars come out as whole black dots instead of grey
edges a thermal printer would have to dither. Images are scaled with whole
pixels when enlarged (nearest neighbour), never smoothed.
"""

from __future__ import annotations

import base64
import io
import math
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageOps

POINTS_PER_MM = 72 / 25.4
# a drawn area may not exceed this; a label of 1 m × 1 m at 600 dpi would
MAX_PIXELS = 80_000_000
# Poppler documents are not safe to use from two threads (preview and print)
_lock = threading.RLock()


class SourceError(ValueError):
    """A file that cannot be used. code: "no_pdf" (PDF support is missing),
    "unreadable" (damaged, encrypted or not a PDF or image) or "too_large";
    the message is English detail for bug reports."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class PageInfo:
    width: float  # points for PDF pages, pixels for images
    height: float
    pages: int  # pages in the file
    pdf: bool

    def width_mm(self) -> float | None:
        """The page width in mm; None for images, which have no reliable size."""
        return self.width / POINTS_PER_MM if self.pdf else None


def is_pdf(raw: bytes) -> bool:
    # the header may follow other bytes within the first kilobyte
    return b"%PDF-" in raw[:1024]


def _poppler() -> Any:
    try:
        import gi

        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
    except (ImportError, ValueError) as e:
        raise SourceError("no_pdf", "Poppler (poppler-glib) is not installed: %s" % e) from e
    return Poppler


@lru_cache(maxsize=4)
def _document(data: str) -> Any:
    """A Poppler document or a decoded image, by the file's content."""
    try:
        raw = base64.b64decode(data)
    except ValueError as e:
        raise SourceError("unreadable", str(e)) from e
    if is_pdf(raw):
        poppler = _poppler()
        from gi.repository import GLib

        try:
            with _lock:
                return poppler.Document.new_from_bytes(GLib.Bytes.new(raw), None)
        except Exception as e:  # noqa: BLE001 - GLib.Error: damaged or encrypted
            raise SourceError("unreadable", str(e)) from e
    try:
        with Image.open(io.BytesIO(raw)) as src:
            image = ImageOps.exif_transpose(src)
            if image.mode in ("RGBA", "LA", "P"):  # transparency becomes white paper
                image = image.convert("RGBA")
                paper = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(paper, image)
            return image.convert("RGB")
    except Exception as e:  # noqa: BLE001 - PIL raises many kinds
        raise SourceError("unreadable", str(e)) from e


def _pdf_page(doc, index: int):
    with _lock:
        count = doc.get_n_pages()
        if count < 1:
            raise SourceError("unreadable", "the PDF has no pages")
        return doc.get_page(min(max(0, index), count - 1)), count


def page_info(data: str, page: int = 0) -> PageInfo:
    """Size of a page and the number of pages; SourceError if unusable."""
    doc = _document(data)
    if isinstance(doc, Image.Image):
        return PageInfo(doc.width, doc.height, 1, False)
    pdf_page, count = _pdf_page(doc, page)
    with _lock:
        width, height = pdf_page.get_size()
    if width <= 0 or height <= 0:
        raise SourceError("unreadable", "page without a size")
    return PageInfo(width, height, count, True)


# --- geometry -------------------------------------------------------------------


def area(crop, width: float, height: float) -> tuple[float, float, float, float]:
    """The crop fractions in page units: x, y, width, height."""
    x, y, w, h = crop
    return x * width, y * height, max(w * width, 1e-6), max(h * height, 1e-6)


def turned(width: float, height: float, rotation: int) -> tuple[float, float]:
    return (height, width) if rotation % 180 else (width, height)


def fitted(
    content: tuple[float, float], box: tuple[int | None, int], fit: str = "fit"
) -> tuple[int, int, float]:
    """Pixel size of the drawn area and its scale (pixels per page unit).
    box is (width or None for "as wide as needed", height); "fit" keeps all
    of the area inside the box, "fill" covers the box (edges are cut off)."""
    cw, ch = content
    box_w, box_h = box
    if box_w is None:
        scale = box_h / ch
    elif fit == "fill":
        scale = max(box_w / cw, box_h / ch)
        return box_w, box_h, scale
    else:
        scale = min(box_w / cw, box_h / ch)
    return max(1, round(cw * scale)), max(1, round(ch * scale)), scale


# --- drawing --------------------------------------------------------------------


def _surface_image(surface) -> Image.Image:
    surface.flush()
    return Image.frombuffer(
        "RGB",
        (surface.get_width(), surface.get_height()),
        bytes(surface.get_data()),
        "raw",
        "BGRX",
        surface.get_stride(),
        1,
    ).copy()


def _draw_pdf(pdf_page, region, rotation: int, size, scale: float, sharp: bool) -> Image.Image:
    """Draw region (x, y, w, h in points) turned by rotation into size pixels."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, *size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    if sharp:
        ctx.set_antialias(cairo.ANTIALIAS_NONE)
        options = cairo.FontOptions()
        options.set_antialias(cairo.ANTIALIAS_NONE)
        ctx.set_font_options(options)
    x, y, w, h = region
    ctx.translate(size[0] / 2, size[1] / 2)
    ctx.rotate(math.radians(rotation))  # y points down: clockwise
    ctx.scale(scale, scale)
    ctx.translate(-(x + w / 2), -(y + h / 2))
    ctx.rectangle(x, y, w, h)
    ctx.clip()
    with _lock:
        pdf_page.render_for_printing(ctx)
    return _surface_image(surface)


TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,  # Pillow turns counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _draw_image(image: Image.Image, region, rotation: int, size, scale: float) -> Image.Image:
    x, y, w, h = region
    box = (math.floor(x), math.floor(y), math.ceil(x + w), math.ceil(y + h))
    part = image.crop(box)
    if rotation:
        part = part.transpose(TRANSPOSE[rotation])
    # at least the size: the whole pixels of the crop may be a little smaller
    # than the area, and cropping beyond the image would add black edges
    scaled = (max(size[0], round(part.width * scale)), max(size[1], round(part.height * scale)))
    # enlarging: whole pixels, bars stay sharp; reducing: the average of the
    # pixels that fall together (no ringing around bars as with Lanczos)
    resample = Image.Resampling.NEAREST if scale >= 1 else Image.Resampling.BOX
    part = part.resize(scaled, resample)
    left, top = (part.width - size[0]) // 2, (part.height - size[1]) // 2
    return part.crop((left, top, left + size[0], top + size[1]))


def _check_size(size: tuple[int, int]) -> None:
    if size[0] * size[1] > MAX_PIXELS:
        raise SourceError("too_large", "%d × %d pixels" % size)


@lru_cache(maxsize=8)
def draw(
    data: str,
    page: int,
    crop: tuple[float, float, float, float],
    rotation: int,
    box: tuple[int | None, int],
    fit: str = "fit",
    sharp: bool = True,
) -> Image.Image:
    """The area of the page, turned and drawn into box (see fitted()), RGB.
    Cached: the preview draws the label again on every change."""
    doc = _document(data)
    info = page_info(data, page)
    region = area(crop, info.width, info.height)
    width, height, scale = fitted(turned(region[2], region[3], rotation), box, fit)
    _check_size((width, height))
    if isinstance(doc, Image.Image):
        return _draw_image(doc, region, rotation, (width, height), scale)
    pdf_page, _count = _pdf_page(doc, page)
    return _draw_pdf(pdf_page, region, rotation, (width, height), scale, sharp)


def preview(data: str, page: int, longest_px: int) -> Image.Image:
    """The whole page, smooth, its longer side longest_px: for the crop
    view and the detection."""
    info = page_info(data, page)
    scale = longest_px / max(info.width, info.height)
    box = (max(1, round(info.width * scale)), max(1, round(info.height * scale)))
    return draw(data, page, (0.0, 0.0, 1.0, 1.0), 0, box, "fit", sharp=False)
