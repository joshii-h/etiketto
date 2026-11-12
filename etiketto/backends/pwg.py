"""PWG Raster (PWG 5102.4): the page format every IPP Everywhere printer
takes. The "RaS2" sync word, then per page a 1796-byte header and the pixel
rows, compressed line by line (repeated lines, then runs of equal or of
different pixels).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

from PIL import Image

SYNC = b"RaS2"
HEADER_SIZE = 1796
SRGB, SGRAY = 19, 18  # cupsColorSpace values PWG uses
MAX_RUN = 128


def header(image: Image.Image, dpi: int, media_name: str = "", pages: int = 1) -> bytes:
    """The page header for an 8-bit sRGB ("RGB") or sGray ("L") image."""
    colors = 3 if image.mode == "RGB" else 1
    width, height = image.size
    out = bytearray(HEADER_SIZE)

    def put(offset: int, fmt: str, *values) -> None:
        struct.pack_into(">" + fmt, out, offset, *values)

    out[0:9] = b"PwgRaster"
    put(276, "II", dpi, dpi)  # HWResolution
    put(352, "II", round(width * 72 / dpi), round(height * 72 / dpi))  # PageSize, points
    put(372, "II", width, height)  # cupsWidth, cupsHeight
    put(384, "III", 8, 8 * colors, width * colors)  # bits per colour, per pixel, bytes/line
    put(400, "I", SRGB if colors == 3 else SGRAY)  # cupsColorSpace
    put(420, "I", colors)  # cupsNumColors
    put(452, "III", pages, 1, 1)  # TotalPageCount, CrossFeedTransform, FeedTransform
    name = media_name.encode("ascii", "replace")[:63]
    out[1732 : 1732 + len(name)] = name  # cupsPageSizeName
    return bytes(out)


def _repeats(pixels: list[bytes], i: int) -> int:
    """How often pixels[i] repeats from i on (at most MAX_RUN)."""
    run = 1
    while i + run < len(pixels) and run < MAX_RUN and pixels[i + run] == pixels[i]:
        run += 1
    return run


def _literals(pixels: list[bytes], i: int) -> int:
    """How many pixels from i on differ from their right neighbour."""
    count = 0
    while i + count < len(pixels) and count < MAX_RUN:
        if _repeats(pixels, i + count) > 1:
            break
        count += 1
    return count


def _line(row: bytes, step: int) -> bytes:
    """One row of pixels (step bytes each): runs of a repeated pixel, else
    literal pixels, at most 128 per run."""
    out = bytearray()
    pixels = [row[i : i + step] for i in range(0, len(row), step)]
    i = 0
    while i < len(pixels):
        run = _repeats(pixels, i)
        if run > 1:
            out.append(run - 1)  # 0..127: the next pixel, run times
            out += pixels[i]
            i += run
            continue
        count = _literals(pixels, i)
        # 129..255: 2 to 128 literal pixels; a single one is a run of one
        out.append(257 - count if count > 1 else 0)
        out += b"".join(pixels[i : i + count])
        i += count
    return bytes(out)


def _body(image: Image.Image) -> bytes:
    """The compressed pixel rows of an "RGB" or "L" image."""
    step = 3 if image.mode == "RGB" else 1
    data = image.tobytes()
    stride = image.width * step
    rows = [data[y * stride : (y + 1) * stride] for y in range(image.height)]
    out = bytearray()
    y = 0
    while y < len(rows):
        repeat = 1
        while y + repeat < len(rows) and repeat < 256 and rows[y + repeat] == rows[y]:
            repeat += 1
        out.append(repeat - 1)  # line repeat count
        out += _line(rows[y], step)
        y += repeat
    return bytes(out)


def _usable(image: Image.Image) -> Image.Image:
    return image if image.mode in ("RGB", "L") else image.convert("RGB")


def encode(image: Image.Image, dpi: int, media_name: str = "", pages: int = 1) -> bytes:
    """A PWG Raster document for an "RGB" or "L" image: the page once, or
    several times (copies for a printer that cannot make them itself)."""
    image = _usable(image)
    return SYNC + (header(image, dpi, media_name, pages) + _body(image)) * pages


def encode_pages(images: Iterable[Image.Image], dpi: int, total: int) -> bytes:
    """A PWG Raster document of several pages (total of them), each
    encoded when it comes."""
    out = bytearray(SYNC)
    for image in images:
        image = _usable(image)
        out += header(image, dpi, "", total) + _body(image)
    return bytes(out)


def decode(data: bytes) -> Image.Image:
    """The first page of a PWG Raster document (for tests and checks)."""
    return decode_all(data, 1)[0]


def decode_all(data: bytes, limit: int = 0) -> list[Image.Image]:
    """The pages of a PWG Raster document, all or the first limit ones (for
    tests and checks)."""
    if not data.startswith(SYNC):
        raise ValueError("not PWG Raster")
    pages: list[Image.Image] = []
    pos = 4
    while pos < len(data) and (not limit or len(pages) < limit):
        page, pos = _decode_page(data, pos)
        pages.append(page)
    return pages


def _decode_page(data: bytes, start: int) -> tuple[Image.Image, int]:
    head = data[start : start + HEADER_SIZE]
    width, height = struct.unpack_from(">II", head, 372)
    colors = struct.unpack_from(">I", head, 420)[0]
    pos, rows = start + HEADER_SIZE, list[bytes]()
    while len(rows) < height:
        repeat = data[pos] + 1
        pos += 1
        row = bytearray()
        while len(row) < width * colors:
            control = data[pos]
            pos += 1
            if control < 128:
                row += data[pos : pos + colors] * (control + 1)
                pos += colors
            else:
                count = 257 - control
                row += data[pos : pos + count * colors]
                pos += count * colors
        rows += [bytes(row)] * repeat
    mode = "RGB" if colors == 3 else "L"
    return Image.frombytes(mode, (width, height), b"".join(rows[:height])), pos
