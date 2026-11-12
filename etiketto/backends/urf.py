"""Apple Raster ("URF", image/urf): the page format of AirPrint printers,
for those that take neither PDF nor PWG Raster. "UNIRAST\\0" and the page
count, then per page a 32-byte header and the pixel rows, compressed like
PWG Raster's.

The byte layout follows the open source implementations that define the
format (OpenPrinting libcups, cups/raster-stream.c, and PAPPL, both
Apache-2.0); this is a new implementation, no code is copied.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

from PIL import Image

from etiketto.backends.pwg import compress

SYNC = b"UNIRAST\x00"
HEADER_SIZE = 32
SGRAY, SRGB = 0, 1  # colour space: urf-supported W8 and SRGB24
SIMPLEX = 1
NORMAL = 4  # print quality, as IPP print-quality
# media type and media source: auto, what the printer has loaded (libcups
# raster-stream.c apple_media_types[0] is "auto"; PAPPL printer-driver.c:
# auto (0) is always present)
AUTO = 0


def header(image: Image.Image, dpi: int) -> bytes:
    """The page header for an 8-bit sRGB ("RGB") or sGray ("L") image."""
    colors = 3 if image.mode == "RGB" else 1
    head = bytes((8 * colors, SRGB if colors == 3 else SGRAY, SIMPLEX, NORMAL, AUTO, AUTO))
    return head + bytes(6) + struct.pack(">III", image.width, image.height, dpi) + bytes(8)


def encode(images: Iterable[Image.Image], dpi: int, total: int) -> bytes:
    """An Apple Raster document of total pages (e.g. the pages of label
    sheets), each compressed when it comes; join() repeats it for copies.
    "RGB" and "L" images stay as they are, other modes become "RGB"."""
    body = bytearray()
    count = 0
    for image in images:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        body += header(image, dpi) + compress(image)
        count += 1
    if count != total:
        raise ValueError("Apple Raster: %d pages, not %d" % (count, total))
    return SYNC + struct.pack(">I", total) + bytes(body)


def join(documents: Iterable[bytes]) -> bytes:
    """One document of the pages of several documents (the same one again
    for copies: compressed once), the page count set to all."""
    parts = [document[len(SYNC) :] for document in documents]
    count = sum(struct.unpack_from(">I", part)[0] for part in parts)
    return SYNC + struct.pack(">I", count) + b"".join(part[4:] for part in parts)


def decode(data: bytes) -> list[Image.Image]:
    """The pages of an Apple Raster document (for tests and checks), each
    with its resolution in info["dpi"] and the header fields in info["urf"]."""
    if not data.startswith(SYNC):
        raise ValueError("not Apple Raster")
    try:
        (count,) = struct.unpack_from(">I", data, len(SYNC))
        pos, pages = len(SYNC) + 4, []
        while pos < len(data):
            page, pos = _page(data, pos)
            pages.append(page)
    except (IndexError, struct.error) as e:
        raise ValueError("Apple Raster cut short") from e
    if count and count != len(pages):
        raise ValueError("Apple Raster: %d pages, header says %d" % (len(pages), count))
    return pages


def _page(data: bytes, pos: int) -> tuple[Image.Image, int]:
    if len(data) < pos + HEADER_SIZE:
        raise IndexError(pos)
    bits, space, duplex, quality, media_type, source = data[pos : pos + 6]
    width, height, dpi = struct.unpack_from(">III", data, pos + 12)
    if (bits, space) not in ((8, SGRAY), (24, SRGB)):
        raise ValueError("Apple Raster: %d bits in colour space %d" % (bits, space))
    colors = bits // 8
    pos += HEADER_SIZE
    rows: list[bytes] = []
    while len(rows) < height:
        repeat = data[pos] + 1
        row, pos = _row(data, pos + 1, colors, width * colors)
        rows += [row] * repeat
    image = Image.frombytes(
        "RGB" if colors == 3 else "L", (width, height), b"".join(rows[:height])
    )
    image.info["dpi"] = (dpi, dpi)
    image.info["urf"] = {
        "duplex": duplex,
        "quality": quality,
        "media_type": media_type,
        "source": source,
    }
    return image, pos


def _row(data: bytes, pos: int, colors: int, stride: int) -> tuple[bytes, int]:
    """One line from pos: runs of a repeated pixel, literal pixels and
    0x80, white to the end of the line."""
    row = bytearray()
    while len(row) < stride:
        control = data[pos]
        if control == 0x80:
            return bytes(row) + b"\xff" * (stride - len(row)), pos + 1
        pixels, times = (1, control + 1) if control < 0x80 else (257 - control, 1)
        chunk = data[pos + 1 : pos + 1 + pixels * colors]
        if len(chunk) < pixels * colors:
            raise IndexError(pos)
        row += chunk * times
        pos += 1 + len(chunk)
    if len(row) != stride:
        raise ValueError("Apple Raster: a line of %d bytes, not %d" % (len(row), stride))
    return bytes(row), pos
