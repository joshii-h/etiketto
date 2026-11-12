"""PWG Raster pages: header fields and the line compression, checked by
decoding them again (and once against CUPS's own pwgtopdf, see docs)."""

from __future__ import annotations

import struct

import pytest
from PIL import Image

from tavletta.backends import pwg


def _stripes(width=300, height=7, mode="RGB"):
    image = Image.new(mode, (width, height), "white")
    for x in range(0, width, 3):  # runs of one, literals and repeats on every line
        image.putpixel((x, 1), (255, 136, 0) if mode == "RGB" else 128)
    return image


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_round_trip(mode):
    image = _stripes(mode=mode)
    image.putpixel((299, 6), (1, 2, 3) if mode == "RGB" else 7)  # a lone last pixel
    assert pwg.decode(pwg.encode(image, 300)).tobytes() == image.tobytes()


def test_other_modes_become_rgb():
    image = Image.new("RGBA", (4, 2), (10, 20, 30, 255))
    page = pwg.decode(pwg.encode(image, 300))
    assert page.mode == "RGB" and page.getpixel((0, 0)) == (10, 20, 30)


def test_header_fields():
    data = pwg.encode(Image.new("RGB", (600, 300), "white"), 600, "iso_a6_105x148mm")
    head = data[4 : 4 + pwg.HEADER_SIZE]
    assert data[:4] == b"RaS2" and head[:9] == b"PwgRaster"
    assert struct.unpack_from(">II", head, 276) == (600, 600)  # HWResolution
    assert struct.unpack_from(">II", head, 352) == (72, 36)  # PageSize in points
    assert struct.unpack_from(">IIIII", head, 372)[:2] == (600, 300)  # width, height
    assert struct.unpack_from(">III", head, 384) == (8, 24, 1800)  # bits, bits, bytes/line
    assert struct.unpack_from(">I", head, 400) == (pwg.SRGB,)
    assert head[1732:1748] == b"iso_a6_105x148mm"


def test_identical_lines_are_sent_once():
    blank = pwg.encode(Image.new("L", (100, 500), 255), 300)
    # 500 white lines: two line groups (256 + 244), one run each
    assert len(blank) < 4 + pwg.HEADER_SIZE + 20


def test_long_runs_and_literals_are_split():
    noise = Image.frombytes("L", (300, 1), bytes(range(256)) + bytes(range(44)))
    flat = Image.new("L", (300, 1), 0)
    for image in (noise, flat):
        assert pwg.decode(pwg.encode(image, 300)).tobytes() == image.tobytes()


def test_decode_refuses_other_data():
    with pytest.raises(ValueError, match="not PWG Raster"):
        pwg.decode(b"%PDF-1.4")


def test_join_sets_the_page_count_and_keeps_the_pages():
    red = pwg.encode(Image.new("RGB", (3, 2), "red"), 300)
    grey = pwg.encode(Image.new("L", (2, 2), 128), 600)
    joined = pwg.join([red, grey, red])
    pages = pwg.decode_all(joined)
    assert [p.mode for p in pages] == ["RGB", "L", "RGB"]
    assert pages[1].getpixel((0, 0)) == 128
    offsets = [4, 4 + len(red) - 4, 4 + len(red) - 4 + len(grey) - 4]
    assert [struct.unpack_from(">I", joined, o + 452)[0] for o in offsets] == [3, 3, 3]
