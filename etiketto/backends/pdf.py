"""A PDF of image pages: each page one image filling it, the page exactly
the size in mm the image carries (info["size_mm"]), else its pixels at the
given resolution.

The images are embedded without loss (Flate): 1 bit per pixel when a page
is only black and white, 8-bit grey when it is grey, else RGB. A label's
thin lines, small text and barcodes arrive exactly as rendered, and a
mostly white page compresses to little. Pages are compressed one at a
time, as they come: only the compressed ones are kept.
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Iterable

from PIL import Image, ImageChops

POINTS_PER_MM = 72 / 25.4
LEVEL = 6  # zlib: good enough, and quick on a full A4 page
INSET = 0.01  # pt: the image this much smaller than the page, centred (see encode)
DCT_ABOVE = 8_000_000  # bytes: a colour page larger than this without loss goes as JPEG


def pixels(image: Image.Image) -> tuple[str, int, bytes]:
    """The image's colour space, bits per component and raw rows, in the
    smallest form that keeps every pixel: DeviceGray at 1 bit (black and
    white only), DeviceGray at 8 bits (grey), else DeviceRGB."""
    if image.mode not in ("1", "L", "RGB"):
        image = image.convert("RGB")
    if image.mode == "RGB":
        red, green, blue = image.split()
        if (
            ImageChops.difference(red, green).getbbox()
            or ImageChops.difference(red, blue).getbbox()
        ):
            return "DeviceRGB", 8, image.tobytes()
        image = red  # grey: all channels alike
    if image.mode == "L":
        histogram = image.histogram()
        if any(histogram[1:255]):
            return "DeviceGray", 8, image.tobytes()
        image = image.convert("1", dither=Image.Dither.NONE)
    # mode "1": rows padded to whole bytes, 1 = white, as DeviceGray at 1 bit
    return "DeviceGray", 1, image.tobytes()


def _size_pt(image: Image.Image, dpi: float) -> tuple[float, float]:
    size_mm = image.info.get("size_mm")
    if size_mm:
        return size_mm[0] * POINTS_PER_MM, size_mm[1] * POINTS_PER_MM
    return image.width * 72 / dpi, image.height * 72 / dpi


def _placement(
    image: Image.Image, dpi: float, width: float, height: float
) -> tuple[float, float, float, float]:
    """Where the image goes on the page (width x height pt): its size and
    lower left corner. One image pixel is one printer dot at dpi: the
    image keeps its own size from the top left corner when that is within
    a pixel of the page (A4 at 300 dpi is 2480.3 pixels wide, the image
    2480), else it fills the page. And it is INSET pt smaller, centred on
    that place: Poppler's Splash (cups-filters, pdftoppm) otherwise
    resamples an image that exactly fills whole device pixels, with grey
    edges and lost hairlines; Ghostscript places it pixel for pixel
    either way."""
    pixel = 72 / dpi
    own = image.width * pixel, image.height * pixel
    if abs(own[0] - width) <= pixel and abs(own[1] - height) <= pixel:
        box = own
    else:
        box = width, height
    return (
        box[0] - INSET,
        box[1] - INSET,
        INSET / 2,
        height - box[1] + INSET / 2,
    )


def _compressed(image: Image.Image) -> tuple[str, int, bytes, bytes]:
    """Colour space, bits, filter and the compressed pixels: without loss,
    except a colour page (a photo) that would be larger than DCT_ABOVE
    bytes: JPEG at quality 95 without chroma subsampling."""
    space, bits, raw = pixels(image)
    data = zlib.compress(raw, LEVEL)
    del raw
    if space == "DeviceRGB" and len(data) > DCT_ABOVE:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=95, subsampling=0)
        return space, bits, b"DCTDecode", buf.getvalue()
    return space, bits, b"FlateDecode", data


def encode(pages: Iterable[Image.Image], dpi: float, limit: int = 0) -> bytes:
    """The pages as one PDF document. With a limit (bytes), no further
    page is taken from pages once another one as large as the last would
    take the document over it (the first page always goes in): the caller
    counts the pages it handed out."""
    # each object: its parts, joined once at the end
    objects: list[list[bytes]] = [[], []]  # 1: catalog, 2: page tree, filled in at the end
    kids = []
    size = 0
    for image in pages:
        width, height = _size_pt(image, dpi)
        space, bits, method, data = _compressed(image)
        size += len(data)
        number = len(objects) + 1  # the page; its content and image follow
        kids.append(b"%d 0 R" % number)
        objects.append(
            [
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
                b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (width, height, number + 2, number + 1)
            ]
        )
        content = b"q %.3f 0 0 %.3f %.3f %.3f cm /Im0 Do Q" % _placement(image, dpi, width, height)
        objects.append([b"<< /Length %d >>\nstream\n" % len(content), content, b"\nendstream"])
        objects.append(
            [
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /%s "
                b"/BitsPerComponent %d /Filter /%s /Length %d >>\nstream\n"
                % (image.width, image.height, space.encode(), bits, method, len(data)),
                data,
                b"\nendstream",
            ]
        )
        if limit and size + len(data) > limit:
            break
    objects[0] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    objects[1] = [b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))]
    return _document(objects)


def _document(objects: list[list[bytes]]) -> bytes:
    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    length = len(parts[0])
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(length)
        for part in (b"%d 0 obj\n" % number, *body, b"\nendobj\n"):
            parts.append(part)
            length += len(part)
    parts.append(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    parts += [b"%010d 00000 n \n" % offset for offset in offsets]
    parts.append(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, length)
    )
    return b"".join(parts)
