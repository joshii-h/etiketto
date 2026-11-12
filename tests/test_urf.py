"""Apple Raster (URF) pages: the bytes libcups writes for a known page, the
line compression checked against a plain reading of libcups's rules, and
decoding again."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from tavletta.backends import urf

# one 8 × 2 white sGray page at 300 dpi, as libcups writes it
GOLDEN = bytes.fromhex(
    "554E495241535400 00000001"  # UNIRAST\0, 1 page
    "08 00 01 04 00 00 000000000000"  # 8 bits, sGray, one-sided, normal, auto, auto
    "00000008 00000002 0000012C 0000000000000000"  # 8 × 2 px, 300 dpi
    "01 07FF"  # 2 identical lines: 8 times FF
)


def _reference_line(line: bytes, bpp: int, rep: int) -> bytes:
    """libcups's line rules, pixel by pixel: runs of two or more equal
    pixels, else literals until the next two equal ones, 128 at most; a lone
    pixel is a run of one."""
    px = [line[i * bpp : (i + 1) * bpp] for i in range(len(line) // bpp)]
    out = bytearray([rep - 1])
    i = 0
    while i < len(px):
        if i + 1 < len(px) and px[i] == px[i + 1]:
            j = i + 2
            while j < len(px) and j - i < 128 and px[j] == px[i]:
                j += 1
            out += bytes([j - i - 1]) + px[i]
        else:
            j = i + 1
            while j < len(px) and j - i < 128 and not (j + 1 < len(px) and px[j] == px[j + 1]):
                j += 1
            out += bytes([0 if j - i == 1 else 257 - (j - i)]) + b"".join(px[i:j])
        i = j
    return bytes(out)


def _reference(image: Image.Image) -> bytes:
    bpp = 3 if image.mode == "RGB" else 1
    stride = image.width * bpp
    data = image.tobytes()
    rows = [data[y * stride : (y + 1) * stride] for y in range(image.height)]
    out = bytearray()
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and j - i < 256 and rows[j] == rows[i]:
            j += 1
        out += _reference_line(rows[i], bpp, j - i)
        i = j
    return bytes(out)


def test_golden_bytes():
    assert urf.encode([Image.new("L", (8, 2), 255)], 300, 1) == GOLDEN


def test_golden_bytes_decode():
    (page,) = urf.decode(GOLDEN)
    assert page.mode == "L" and page.size == (8, 2) and set(page.tobytes()) == {255}
    assert page.info["dpi"] == (300, 300)
    assert page.info["urf"] == {"duplex": 1, "quality": 4, "media_type": 0, "source": 0}


def test_header_fields():
    data = urf.encode([Image.new("RGB", (600, 300), "white")], 600, 1)
    head = data[12 : 12 + urf.HEADER_SIZE]
    assert data[:8] == b"UNIRAST\x00" and struct.unpack_from(">I", data, 8) == (1,)
    assert head[:6] == bytes([24, urf.SRGB, 1, 4, 0, 0]) and head[6:12] == bytes(6)
    assert struct.unpack_from(">III", head, 12) == (600, 300, 600) and head[24:] == bytes(8)


def _images(mode: str):
    colors = 3 if mode == "RGB" else 1
    # few pixel values: runs, literals and repeated lines all happen
    pixel = st.sampled_from(
        [bytes([v] * colors) for v in (0, 7, 255)] + [b"\x01\x02\x03"[:colors]]
    )
    return st.integers(1, 300).flatmap(
        lambda width: st.lists(
            st.lists(pixel, min_size=width, max_size=width).map(b"".join), min_size=1, max_size=6
        ).map(lambda rows: Image.frombytes(mode, (width, len(rows)), b"".join(rows)))
    )


@settings(max_examples=150, deadline=None)
@given(st.sampled_from(["L", "RGB"]).flatmap(_images))
def test_round_trip_and_libcups_rules(image):
    data = urf.encode([image], 300, 1)
    assert data[12 + urf.HEADER_SIZE :] == _reference(image)
    (page,) = urf.decode(data)
    assert page.mode == image.mode and page.tobytes() == image.tobytes()


@settings(max_examples=30, deadline=None)
@given(
    st.lists(st.sampled_from(["L", "RGB"]).flatmap(_images), min_size=1, max_size=3),
    st.integers(1, 3),
)
def test_several_pages_repeated(images, repeat):
    once = urf.encode(iter(images), 600, len(images))  # a generator: no len()
    pages = urf.decode(urf.join([once] * repeat))
    assert [p.tobytes() for p in pages] == [i.tobytes() for i in images] * repeat
    assert struct.unpack_from(">I", urf.join([once] * repeat), 8) == (len(images) * repeat,)
    assert all(p.info["dpi"] == (600, 600) for p in pages)


def test_long_runs_literals_and_many_identical_lines_are_split():
    noise = Image.frombytes("L", (300, 1), bytes(range(256)) + bytes(range(44)))
    flat = Image.new("L", (300, 600), 0)  # 600 lines: 256 + 256 + 88
    for image in (noise, flat):
        assert urf.decode(urf.encode([image], 300, 1))[0].tobytes() == image.tobytes()
    assert len(urf.encode([flat], 300, 1)) == 12 + urf.HEADER_SIZE + 3 * 7  # 128 + 128 + 44 each


def test_other_modes_become_rgb():
    (page,) = urf.decode(urf.encode([Image.new("RGBA", (4, 2), (10, 20, 30, 255))], 300, 1))
    assert page.mode == "RGB" and page.getpixel((0, 0)) == (10, 20, 30)


def test_the_page_count_must_match():
    with pytest.raises(ValueError, match="1 pages, not 2"):
        urf.encode(iter([Image.new("L", (2, 2))]), 300, 2)


def test_the_rest_of_a_line_white():
    # 0x80 (never written by libcups, readers take it): black pixel, then white
    data = GOLDEN[: 12 + urf.HEADER_SIZE] + bytes.fromhex("01 0000 80")
    (page,) = urf.decode(data)
    assert page.tobytes() == (b"\x00" + b"\xff" * 7) * 2


@pytest.mark.parametrize(
    "data, message",
    [
        (b"%PDF-1.4", "not Apple Raster"),
        (GOLDEN[:-1], "cut short"),
        (GOLDEN[:30], "cut short"),
        (GOLDEN[:-3] + bytes.fromhex("01 FA 00"), "cut short"),  # literal of 7, one pixel
        (GOLDEN[:-3] + bytes.fromhex("01 08FF"), "a line of 9 bytes"),  # runs past the end
        (GOLDEN[:8] + b"\x00\x00\x00\x02" + GOLDEN[12:], "1 pages, header says 2"),
        (GOLDEN[:12] + b"\x20\x06" + GOLDEN[14:], "32 bits in colour space 6"),  # CMYK
    ],
)
def test_decode_refuses_broken_data(data, message):
    with pytest.raises(ValueError, match=message):
        urf.decode(data)
