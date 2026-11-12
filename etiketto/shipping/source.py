"""PDF pages and images as the source of a shipping label.

PDF pages are drawn by Poppler (through GObject introspection) straight at
the size they are printed: lines and barcodes stay sharp at any printer
resolution and nothing is scaled after drawing. Vector content is drawn
without anti-aliasing, so bars come out as whole black dots instead of grey
edges a thermal printer would have to dither; lines thinner than a dot,
which would vanish that way, get at least one black dot. Images are enlarged by repeating
whole pixels (nearest neighbour), never smoothed; reduced, the pixels that
fall together are averaged.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

POINTS_PER_MM = 72 / 25.4
# a drawn area may not exceed this; a label of 1 m × 1 m at 600 dpi would
MAX_PIXELS = 80_000_000
# nor a decoded image: an A4 scan at 600 dpi has 35 million pixels. Checked
# before decoding, independent of Pillow's own limit for decompression bombs.
MAX_SOURCE_PIXELS = 50_000_000
# the resolution an image file states is believed in this range; below it,
# it is mostly a program's default (72 or 96), not the scanner's
TRUSTED_DPI = (100.0, 2400.0)
# a pixel the smooth drawing darkens to this, with nothing at all in the
# sharp drawing around it, is part of a line thinner than a dot
THIN_INK = 240
# drawn areas are kept for the preview, which draws again on every change;
# large print renders are not kept (100 × 150 mm at 600 dpi takes 35 MB)
CACHE_BYTES = 96 * 2**20
CACHE_ITEM_BYTES = 40 * 2**20
# file signatures of images: such a file is never a PDF, whatever follows
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*", b"RIFF")
GREY_MODES = ("1", "L")
ORIENTATION = 0x0112  # EXIF tag: how the image is to be turned
# the copy of a PDF page is the same drawing, up to rounding: sharp, at most
# this part of its dots differ; smooth, at most this part of its pixels
# differ by more than SMOOTH_LEVELS (cairo writes numbers to a few
# decimals, so edges and image pixels may move by a fraction of a dot)
SHARP_SHARE = 0.0005
SMOOTH_SHARE = 0.001
SMOOTH_LEVELS = 2
# and in no TILE_PX square more than TILE_CHANGES pixels differ by more
# than a shift of one pixel: a changed digit or a missing code is not rounding
TILE_PX = 64
TILE_CHANGES = 4


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
    pages: int  # pages in the file (frames of a TIFF)
    pdf: bool
    dpi: float | None = None  # an image's resolution, if the file states a believable one

    def width_mm(self) -> float | None:
        """The page width in mm; None for an image without a stated resolution."""
        if self.pdf:
            return self.width / POINTS_PER_MM
        return self.width / self.dpi * 25.4 if self.dpi else None


def is_pdf(raw: bytes) -> bool:
    """A PDF starts with its header; some programs put a few bytes before it,
    so it is also looked for in the first kilobyte, unless the file starts
    like an image (whose comments or metadata may hold anything)."""
    if raw.startswith(b"%PDF-"):
        return True
    if raw.startswith(IMAGE_MAGIC):
        return False
    return b"%PDF-" in raw[:1024]


def _poppler() -> Any:
    try:
        import gi

        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
    except (ImportError, ValueError) as e:
        raise SourceError("no_pdf", "Poppler (poppler-glib) is not installed: %s" % e) from e
    return Poppler


def _decoded(data: str) -> bytes:
    try:
        return base64.b64decode(data)
    except (binascii.Error, ValueError) as e:
        raise SourceError("unreadable", str(e)) from e


def _starts_as_pdf(data: str) -> bool:
    """Whether the file is a PDF, from its first kilobyte only."""
    head = data[:1368]  # 1026 bytes; base64 comes in groups of four characters
    try:
        return is_pdf(base64.b64decode(head[: len(head) // 4 * 4]))
    except (binascii.Error, ValueError):
        return False


# --- caches ---------------------------------------------------------------------


class _Cache:
    """The most recently used values, at most items of them and at most
    max_bytes together; safe to use from several threads."""

    def __init__(self, items: int, max_bytes: int = 0, size: Callable[[Any], int] = len):
        self.items, self.max_bytes, self.size = items, max_bytes, size
        self._values: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.items or (
                self.max_bytes and sum(map(self.size, self._values.values())) > self.max_bytes
            ):
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        return len(self._values)


def _image_bytes(image: Image.Image) -> int:
    """Memory of the pixels: Pillow keeps RGB (and 32 bit) in 4 bytes."""
    return image.width * image.height * (1 if image.mode in ("1", "L", "P") else 4)


# one decoded image (the frame in use): the preview and a print share it
_pictures = _Cache(1)
# drawn areas, bounded by their memory
_drawings = _Cache(16, CACHE_BYTES, _image_bytes)
# Poppler documents are not safe to use from two threads: the preview (main
# thread) and a print in the background each open their own document
_threads = threading.local()


def cached(key: tuple, make: Callable[[], Image.Image]) -> Image.Image:
    """make()'s image, kept with the drawings (by the same limits): for
    work done on a drawing, like the black and white of a page area."""
    found = _drawings.get(key)
    if found is not None:
        return found
    image = make()
    if _image_bytes(image) <= CACHE_ITEM_BYTES:
        _drawings.put(key, image)
    return image


def clear_caches() -> None:
    """Forget decoded files and drawings (tests; the memory after a label)."""
    _pictures.clear()
    _drawings.clear()
    _threads.__dict__.clear()


# --- PDF documents --------------------------------------------------------------


def _open_pdf(raw: bytes) -> Any:
    poppler = _poppler()
    from gi.repository import GLib

    try:
        return poppler.Document.new_from_bytes(GLib.Bytes.new(raw), None)
    except Exception as e:  # noqa: BLE001 - GLib.Error: damaged or encrypted
        raise SourceError("unreadable", str(e)) from e


def _pdf(data: str) -> Any:
    """This thread's Poppler document of the file (the last one it used)."""
    cached = getattr(_threads, "pdf", None)
    if cached is not None and cached[0] == data:
        return cached[1]
    _threads.pdf = None  # the old document goes before the new one is opened
    doc = _open_pdf(_decoded(data))
    _threads.pdf = (data, doc)
    return doc


def _pdf_page(doc, index: int):
    count = doc.get_n_pages()
    if count < 1:
        raise SourceError("unreadable", "the PDF has no pages")
    return doc.get_page(min(max(0, index), count - 1)), count


# --- images ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Picture:
    image: Image.Image  # mode 1, L or RGB
    frames: int
    dpi: float | None


def _dpi(info: dict) -> float | None:
    dpi = info.get("dpi")
    try:
        value = float(dpi[0])  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return None
    low, high = TRUSTED_DPI
    return value if low <= value <= high else None


def _grey_palette(image: Image.Image) -> bool:
    palette = image.getpalette() or []
    return all(palette[i] == palette[i + 1] == palette[i + 2] for i in range(0, len(palette), 3))


def _to_8_bit(image: Image.Image) -> Image.Image:
    """16 and 32 bit grey and float images as 8 bit grey, by their range."""
    if image.mode.startswith("I;16"):
        return image.convert("I").point(lambda v: v * (1 / 257)).convert("L")
    high = float(image.getextrema()[1])  # type: ignore[arg-type]  # one band
    if image.mode == "F" and 0 < high <= 1.0:  # 0 to 1: black to white
        return image.point(lambda v: v * 255).convert("L")
    if high > 255:
        return image.point(lambda v: v * (255 / high)).convert("L")
    return image.convert("L")


def _on_paper(image: Image.Image) -> Image.Image:
    """The image on white paper (transparency becomes paper) in the fewest
    channels that hold it: black and white and grey images stay 1 or L,
    which takes a third (an eighth) of the memory of RGB."""
    if image.mode == "P":
        if "transparency" in image.info:
            image = image.convert("RGBA")
        else:
            return image.convert("L" if _grey_palette(image) else "RGB")
    if image.mode in ("PA", "RGBa", "La"):
        image = image.convert("RGBA" if image.mode != "La" else "LA")
    if image.mode.startswith("I") or image.mode == "F":
        return _to_8_bit(image)
    if image.mode in ("LA", "RGBA"):
        base = image.mode[0] if image.mode == "LA" else "RGB"
        paper = Image.new(base, image.size, 255 if base == "L" else (255, 255, 255))
        paper.paste(image.convert(base), mask=image.getchannel("A"))
        return paper
    if image.mode in (*GREY_MODES, "RGB"):
        return image  # no copy
    return image.convert("RGB")


def _decode_frame(raw: bytes, frame: int) -> tuple[Image.Image, int, float | None]:
    """The frame on paper, the number of frames and the resolution. Only
    one copy of the pixels where possible: an image that needs no turning
    and no conversion is the decoded image itself."""
    src: Image.Image = Image.open(io.BytesIO(raw))
    frames = max(1, getattr(src, "n_frames", 1))
    if frame:
        src.seek(min(max(0, frame), frames - 1))
    if src.width * src.height > MAX_SOURCE_PIXELS:
        raise SourceError("too_large", "%d × %d pixels" % src.size)
    dpi = _dpi(src.info)
    src.load()
    if src.getexif().get(ORIENTATION, 1) != 1:
        src = ImageOps.exif_transpose(src)
    image = _on_paper(src)
    if frames > 1 and image is src:
        image = src.copy()  # detached from the file, which stays open for its frames
    return image, frames, dpi


def _picture(data: str, frame: int) -> _Picture:
    """The decoded frame of an image file (cached: the last one used)."""
    key = (data, frame)
    cached = _pictures.get(key)
    if cached is not None:
        return cached
    _pictures.clear()  # the old image goes before the new one is decoded
    try:
        image, frames, dpi = _decode_frame(_decoded(data), frame)
    except SourceError:
        raise
    except Image.DecompressionBombError as e:
        raise SourceError("too_large", str(e)) from e
    except Exception as e:  # noqa: BLE001 - PIL raises many kinds
        raise SourceError("unreadable", str(e)) from e
    picture = _Picture(image, frames, dpi)
    _pictures.put(key, picture)
    return picture


def page_info(data: str, page: int = 0) -> PageInfo:
    """Size of a page and the number of pages; SourceError if unusable. A
    page after the last is the last."""
    if not _starts_as_pdf(data):
        picture = _picture(data, page)
        image = picture.image
        return PageInfo(image.width, image.height, picture.frames, False, picture.dpi)
    pdf_page, count = _pdf_page(_pdf(data), page)
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
        surface.get_data(),
        "raw",
        "BGRX",
        surface.get_stride(),
        1,
    ).copy()


def _render(pdf_page, region, rotation: int, size, scale: float, smooth: bool) -> Image.Image:
    """Draw region (x, y, w, h in points) turned by rotation into size pixels."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, *size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    if not smooth:
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
    pdf_page.render_for_printing(ctx)
    return _surface_image(surface)


def ink(smooth: Image.Image) -> Image.Image:
    """Where the smooth drawing has ink (255), for thin_lines()."""
    return smooth.convert("L").point(lambda v: 255 if v <= THIN_INK else 0)


def thin_lines(sharp: Image.Image, smooth_ink: Image.Image) -> Image.Image:
    """The sharp drawing with one black dot where only the smooth one has
    ink: without anti-aliasing a line thinner than a dot falls between the
    dots and vanishes. Next to anything drawn sharp nothing is added, so
    bars and letters keep their width."""
    drawn = sharp.convert("L").point(lambda v: 255 if v < 255 else 0)
    missing = ImageChops.subtract(smooth_ink, drawn.filter(ImageFilter.MaxFilter(3)))
    del drawn
    if missing.getbbox() is not None:
        sharp.paste((0, 0, 0), mask=missing)
    return sharp


def _draw_pdf(pdf_page, region, rotation: int, size, scale: float, sharp: bool) -> Image.Image:
    smooth = _render(pdf_page, region, rotation, size, scale, smooth=True)
    if not sharp:
        return smooth
    smooth_ink = ink(smooth)
    del smooth  # a page at 600 dpi takes 100 MB
    return thin_lines(_render(pdf_page, region, rotation, size, scale, smooth=False), smooth_ink)


TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,  # Pillow turns counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _draw_image(
    image: Image.Image, region, rotation: int, size, scale: float, sharp: bool = True
) -> Image.Image:
    x, y, w, h = region
    box = (
        math.floor(x),
        math.floor(y),
        min(image.width, math.ceil(x + w)),
        min(image.height, math.ceil(y + h)),
    )
    if image.mode == "1" and (scale < 1 or not sharp):  # averaging needs grey
        image = image.crop(box).convert("L")
        box = (0, 0, image.width, image.height)
    # the size before turning; at least the size: the whole pixels of the
    # crop may be a little smaller than the area, and cropping beyond the
    # image would add black edges
    across, along = (size[1], size[0]) if rotation % 180 else size
    width, height = box[2] - box[0], box[3] - box[1]
    scaled = (max(across, round(width * scale)), max(along, round(height * scale)))
    # enlarging: whole pixels, bars stay sharp; reducing: the average of the
    # pixels that fall together (no ringing around bars as with Lanczos).
    # Resized straight from the area of the image: no copy of all of it.
    # Smooth (not sharp): enlarged with interpolation, for a threshold
    # that finds the edges between the pixels (see shipping.mono).
    if scale < 1:
        resample = Image.Resampling.BOX
    else:
        resample = Image.Resampling.NEAREST if sharp else Image.Resampling.BICUBIC
    part = image.resize(scaled, resample, box=box)
    if rotation:
        part = part.transpose(TRANSPOSE[rotation])
    left, top = (part.width - size[0]) // 2, (part.height - size[1]) // 2
    return part.crop((left, top, left + size[0], top + size[1]))


def _check_size(size: tuple[int, int]) -> None:
    if size[0] * size[1] > MAX_PIXELS:
        raise SourceError("too_large", "%d × %d pixels" % size)


def draw(
    data: str,
    page: int,
    crop: tuple[float, float, float, float],
    rotation: int,
    box: tuple[int | None, int],
    fit: str = "fit",
    sharp: bool = True,
) -> Image.Image:
    """The area of the page, turned and drawn into box (see fitted()): RGB
    for PDF pages, an image's own mode (1, L or RGB) for images. Cached
    while small: the preview draws the label again on every change."""
    key = (data, page, tuple(crop), rotation, box, fit, sharp)
    cached = _drawings.get(key)
    if cached is not None:
        return cached
    info = page_info(data, page)
    region = area(crop, info.width, info.height)
    width, height, scale = fitted(turned(region[2], region[3], rotation), box, fit)
    _check_size((width, height))
    if info.pdf:
        pdf_page, _count = _pdf_page(_pdf(data), page)
        image = _draw_pdf(pdf_page, region, rotation, (width, height), scale, sharp)
    else:
        picture = _picture(data, page).image
        image = _draw_image(picture, region, rotation, (width, height), scale, sharp)
    if _image_bytes(image) <= CACHE_ITEM_BYTES:
        _drawings.put(key, image)
    return image


def preview(data: str, page: int, longest_px: int) -> Image.Image:
    """The whole page, smooth, its longer side longest_px: for the crop
    view and the detection."""
    info = page_info(data, page)
    scale = longest_px / max(info.width, info.height)
    box = (max(1, round(info.width * scale)), max(1, round(info.height * scale)))
    return draw(data, page, (0.0, 0.0, 1.0, 1.0), 0, box, "fit", sharp=False)


# --- keeping one page -----------------------------------------------------------

# the copy of a PDF page is compared with the original at these sizes (longer
# side, in pixels): smooth as in the preview, and sharp as printed
CHECK_PX = ((1000, True), (1800, False))


def _pdf_copy(pdf_page) -> bytes:
    """The page as a PDF of its own, drawn by Poppler into a cairo PDF."""
    import cairo

    width, height = pdf_page.get_size()
    buf = io.BytesIO()
    surface = cairo.PDFSurface(buf, width, height)
    pdf_page.render_for_printing(cairo.Context(surface))
    surface.show_page()
    surface.finish()
    return buf.getvalue()


def _same_drawing(page_a, page_b) -> bool:
    width, height = page_a.get_size()
    if (width, height) != page_b.get_size():
        return False
    region = (0.0, 0.0, width, height)
    for longest, smooth in CHECK_PX:
        scale = longest / max(width, height)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        a = _render(page_a, region, 0, size, scale, smooth)
        b = _render(page_b, region, 0, size, scale, smooth)
        levels, share = (SMOOTH_LEVELS, SMOOTH_SHARE) if smooth else (0, SHARP_SHARE)
        differing = _differing(a, b, levels)
        if differing > share * size[0] * size[1]:
            return False
        if differing and _changed_tile(_unexplained(a, b, levels)):
            return False
    return True


def _differing(a: Image.Image, b: Image.Image, levels: int) -> int:
    """Pixels where a channel of a and b differs by more than levels."""
    red, green, blue = ImageChops.difference(a, b).split()
    most = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return sum(most.histogram()[levels + 1 :])


def _outside(a: Image.Image, b: Image.Image, levels: int) -> Image.Image:
    """255 where b is darker or lighter (by more than levels) than all of a
    around the pixel: not a pixel of a moved by one."""
    from etiketto.shipping.mono import spread

    lowest, highest = spread(a, True, 3), spread(a, False, 3)
    darker = ImageChops.subtract(lowest, b).point(lambda v: 255 if v > levels else 0)
    lighter = ImageChops.subtract(b, highest).point(lambda v: 255 if v > levels else 0)
    return ImageChops.lighter(darker, lighter)


def _unexplained(a: Image.Image, b: Image.Image, levels: int) -> Image.Image:
    """Where a and b differ by more than a shift of one pixel (rounding):
    something drawn in one and not in the other (in grey: a change of
    colour alone is rare on a label and caught by _differing())."""
    grey_a, grey_b = a.convert("L"), b.convert("L")
    return ImageChops.lighter(_outside(grey_a, grey_b, levels), _outside(grey_b, grey_a, levels))


def _changed_tile(mask: Image.Image) -> bool:
    """Whether a tile of mask holds more than a few marked pixels: small
    changes in one place (a missing code, another digit, a missing line)
    count, rounding scattered over the page does not."""
    if mask.getbbox() is None:
        return False
    for top in range(0, mask.height, TILE_PX):
        for left in range(0, mask.width, TILE_PX):
            tile = mask.crop((left, top, left + TILE_PX, top + TILE_PX))
            if tile.histogram()[255] > TILE_CHANGES:
                return True
    return False


def only_page(data: str, page: int) -> tuple[str, int]:
    """The file with only this page, as (data, page): the other pages of a
    carrier's document (receipts, customs forms, other labels) stay out of
    the label file. A PDF page is copied only if the copy draws like the
    original (up to rounding, see SHARP_SHARE), else the whole file is
    kept; a frame of a TIFF becomes a PNG. Files of one page stay as they
    are."""
    info = page_info(data, page)
    if info.pages < 2:
        return data, page
    if not info.pdf:
        picture = _picture(data, page)
        buf = io.BytesIO()
        extra = {"dpi": (picture.dpi, picture.dpi)} if picture.dpi else {}
        picture.image.save(buf, format="PNG", **extra)
        return base64.b64encode(buf.getvalue()).decode(), 0
    original, _count = _pdf_page(_pdf(data), page)
    try:
        raw = _pdf_copy(original)
        copy, _count = _pdf_page(_open_pdf(raw), 0)
        same = _same_drawing(original, copy)
    except (SourceError, MemoryError):
        return data, page
    return (base64.b64encode(raw).decode(), 0) if same else (data, page)
