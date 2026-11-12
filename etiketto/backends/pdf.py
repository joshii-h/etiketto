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

import zlib
from collections.abc import Iterable

from PIL import Image, ImageChops

POINTS_PER_MM = 72 / 25.4
LEVEL = 6  # zlib: good enough, and quick on a full A4 page


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


def encode(pages: Iterable[Image.Image], dpi: float) -> bytes:
    """The pages as one PDF document."""
    # each object: its parts, joined once at the end
    objects: list[list[bytes]] = [[], []]  # 1: catalog, 2: page tree, filled in at the end
    kids = []
    for image in pages:
        width, height = _size_pt(image, dpi)
        space, bits, raw = pixels(image)
        data = zlib.compress(raw, LEVEL)
        del raw
        number = len(objects) + 1  # the page; its content and image follow
        kids.append(b"%d 0 R" % number)
        objects.append(
            [
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
                b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (width, height, number + 2, number + 1)
            ]
        )
        content = b"q %.3f 0 0 %.3f 0 0 cm /Im0 Do Q" % (width, height)
        objects.append([b"<< /Length %d >>\nstream\n" % len(content), content, b"\nendstream"])
        objects.append(
            [
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /%s "
                b"/BitsPerComponent %d /Filter /FlateDecode /Length %d >>\nstream\n"
                % (image.width, image.height, space.encode(), bits, len(data)),
                data,
                b"\nendstream",
            ]
        )
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
