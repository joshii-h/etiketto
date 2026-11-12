"""A PDF of image pages: each page one JPEG (DCTDecode) filling it, the page
exactly the size in mm the image carries (info["size_mm"]), else its pixels
at the given resolution. Written page by page, so that a long sheet job
never holds more than one page image in memory.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from PIL import Image

POINTS_PER_MM = 72 / 25.4


def _jpeg(image: Image.Image, dpi: float) -> bytes:
    buf = io.BytesIO()
    # best quality, no chroma subsampling: small text stays sharp
    image.convert("RGB").save(buf, format="JPEG", quality=95, subsampling=0, dpi=(dpi, dpi))
    return buf.getvalue()


def _size_pt(image: Image.Image, dpi: float) -> tuple[float, float]:
    size_mm = image.info.get("size_mm")
    if size_mm:
        return size_mm[0] * POINTS_PER_MM, size_mm[1] * POINTS_PER_MM
    return image.width * 72 / dpi, image.height * 72 / dpi


def encode(pages: Iterable[Image.Image], dpi: float) -> bytes:
    """The pages as one PDF document."""
    objects: list[bytes] = [b"", b""]  # 1: catalog, 2: page tree, filled in at the end
    kids = []
    for image in pages:
        width, height = _size_pt(image, dpi)
        data = _jpeg(image, dpi)
        number = len(objects) + 1  # the page; its content and image follow
        kids.append(b"%d 0 R" % number)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (width, height, number + 2, number + 1)
        )
        content = b"q %.3f 0 0 %.3f 0 0 cm /Im0 Do Q" % (width, height)
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n%s\nendstream"
            % (image.width, image.height, len(data), data)
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    return _document(objects)


def _document(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
