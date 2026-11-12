#!/usr/bin/env python3
"""Render Tavletta's icon from its SVG sources into PNG, ICO and ICNS files.

    python3 tools/icons.py [output folder]     (default: build/icons)

Sources (all SVG Tiny 1.2, see tests/test_icons.py):

- data/xyz.joshuahirsig.Tavletta.svg: the icon, 128 x 128, for 64 px and up
- data/icons/tavletta-<n>.svg for n = 16, 20, 22, 24, 32: the tablet redrawn
  on an n px grid with a thicker frame and fewer details; 40 and 48 px are
  the 20 and 24 px drawings at twice the size (whole pixels, sharp)
- data/icons/tavletta-macos.svg: the tablet on a macOS plate, 1024 x 1024
  (written by tools/macos_icon.py); tavletta-macos-16.svg and -32.svg the
  same on the 16 and 32 px grid

Output:

- hicolor/<n>x<n>/apps/xyz.joshuahirsig.Tavletta.png for n in HICOLOR: for
  packagers who need PNGs (AppImage, distributions or desktops without an
  SVG loader) and for builds of other platforms; Tavletta's own packages
  (setup.py, Flatpak) install the SVGs
- tavletta.ico: Windows, the sizes in ICO; up to 128 px as 32-bit BMP with
  an AND mask (what every Windows version reads), 256 px as PNG
- tavletta.icns: macOS. Every Retina and large size as PNG (ic07-ic14);
  16 and 32 px at 1x as ARGB (ic04, ic05) and, for systems before macOS
  11, as RGB with an 8-bit mask (is32 + s8mk, il32 + l8mk). Not as PNG
  (icp4, icp5): macOS 10.15.7 and later draw those scrambled inside an
  app bundle, which is why Pillow's ICNS writer leaves the two sizes out.
  Written here because iconutil only exists on macOS.

Needs PyGObject with librsvg (Rsvg 2.0), pycairo and Pillow.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import cairo
import gi
from PIL import Image

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "xyz.joshuahirsig.Tavletta"
MAIN = ROOT / "data" / f"{APP_ID}.svg"
ICONS = ROOT / "data" / "icons"
SMALL = {n: ICONS / f"tavletta-{n}.svg" for n in (16, 20, 22, 24, 32)}
MACOS = ICONS / "tavletta-macos.svg"
MACOS_SMALL = {n: ICONS / f"tavletta-macos-{n}.svg" for n in (16, 32)}

# below this size the icon is not drawn from MAIN
MAIN_FROM = 64
# sizes drawn from a small drawing at a whole multiple of its grid
DOUBLED = {40: 20, 48: 24}

HICOLOR = (16, 20, 22, 24, 32, 48, 64, 128, 256, 512)
ICO = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ICO_MAX = 256  # the largest size an ICO directory entry can hold
ICO_PNG_FROM = 256  # this size as PNG, smaller ones as BMP
# ICNS PNG entries: type -> pixel size (the @2x entries are the next 1x size's pixels)
ICNS_PNG = {
    b"ic11": 32,  # 16@2x
    b"ic12": 64,  # 32@2x
    b"ic07": 128,
    b"ic13": 256,  # 128@2x
    b"ic08": 256,
    b"ic14": 512,  # 256@2x
    b"ic09": 512,
    b"ic10": 1024,  # 512@2x
}
# 16 and 32 px at 1x: ARGB, and RGB with its mask for older systems
ICNS_ARGB = {b"ic04": 16, b"ic05": 32}
ICNS_RGB = {16: (b"is32", b"s8mk"), 32: (b"il32", b"l8mk")}


def source(size: int) -> Path:
    """The SVG drawn for this size.

    A small drawing at its own size or at twice its size (40, 48 px), the
    icon from 64 px up. Other sizes below 64 px have no drawing that stays
    sharp: refused, add a drawing instead."""
    if size in SMALL:
        return SMALL[size]
    if size in DOUBLED:
        return SMALL[DOUBLED[size]]
    if size >= MAIN_FROM:
        return MAIN
    raise ValueError("no drawing for %d px" % size)


def macos_source(size: int) -> Path:
    return MACOS_SMALL.get(size, MACOS)


def render(svg: Path, size: int) -> Image.Image:
    """The SVG drawn into a size x size RGBA image."""
    handle = Rsvg.Handle.new_from_file(str(svg))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    viewport = Rsvg.Rectangle()
    viewport.x = viewport.y = 0
    viewport.width = viewport.height = size
    handle.render_document(cairo.Context(surface), viewport)
    surface.flush()
    data = bytes(surface.get_data())
    # cairo: premultiplied BGRA in native byte order (little endian here)
    return Image.frombuffer(
        "RGBA", (size, size), data, "raw", "BGRa", surface.get_stride(), 1
    ).copy()


def png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


# --- ICO ----------------------------------------------------------------------


def and_mask(image: Image.Image) -> bytes:
    """1 bit per pixel, set where the pixel is fully transparent; rows bottom
    up, most significant bit first, each row padded to 4 bytes."""
    size = image.width
    alpha = image.getchannel("A").transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
    stride = ((size + 31) // 32) * 4
    out = bytearray(stride * size)
    for y in range(size):
        for x in range(size):
            if alpha[y * size + x] == 0:
                out[y * stride + x // 8] |= 0x80 >> (x % 8)
    return bytes(out)


def bmp_entry(image: Image.Image) -> bytes:
    """An ICO image as BITMAPINFOHEADER, BGRA rows bottom up, then the AND mask."""
    size = image.width
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    return header + pixels + and_mask(image)


def ico(images: dict[int, Image.Image]) -> bytes:
    sizes = sorted(images)
    too_large = [n for n in sizes if n > ICO_MAX]
    if too_large:
        raise ValueError("ICO holds at most %d px, not %s" % (ICO_MAX, too_large))
    entries = [png_bytes(images[n]) if n >= ICO_PNG_FROM else bmp_entry(images[n]) for n in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for n, data in zip(sizes, entries, strict=True):
        side = n if n < 256 else 0  # 0 means 256
        out += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(entries)


# --- ICNS ---------------------------------------------------------------------


def icns_rle(channel: bytes) -> bytes:
    """Apple's run-length encoding of one channel: a byte below 0x80 is
    followed by that many plus one literal bytes, from 0x80 up it repeats
    the next byte (value - 0x80 + 3) times (3 to 130)."""
    out = bytearray()
    literal = bytearray()
    i = 0
    while i < len(channel):
        run = 1
        while i + run < len(channel) and run < 130 and channel[i + run] == channel[i]:
            run += 1
        if run >= 3:
            if literal:
                out += bytes([len(literal) - 1]) + literal
                literal.clear()
            out += bytes([0x80 + run - 3, channel[i]])
            i += run
            continue
        literal.append(channel[i])
        i += 1
        if len(literal) == 128:
            out += bytes([127]) + literal
            literal.clear()
    if literal:
        out += bytes([len(literal) - 1]) + literal
    return bytes(out)


def icns_entry(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack(">I", 8 + len(data)) + data


def icns(images: dict[int, Image.Image]) -> bytes:
    body = b""
    for kind, size in ICNS_ARGB.items():
        channels = [images[size].getchannel(c).tobytes() for c in "ARGB"]
        body += icns_entry(kind, b"ARGB" + b"".join(icns_rle(c) for c in channels))
    for size, (rgb, mask) in ICNS_RGB.items():
        channels = [images[size].getchannel(c).tobytes() for c in "RGB"]
        body += icns_entry(rgb, b"".join(icns_rle(c) for c in channels))
        body += icns_entry(mask, images[size].getchannel("A").tobytes())
    for kind, size in ICNS_PNG.items():
        body += icns_entry(kind, png_bytes(images[size]))
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


# --- all of it -----------------------------------------------------------------


def build(out: Path) -> list[Path]:
    """Write every icon file into out; returns the files written."""
    written = []
    for size in HICOLOR:
        path = out / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        render(source(size), size).save(path, "PNG", optimize=True)
        written.append(path)
    path = out / "tavletta.ico"
    path.write_bytes(ico({n: render(source(n), n) for n in ICO}))
    written.append(path)
    sizes = set(ICNS_PNG.values()) | set(ICNS_ARGB.values()) | set(ICNS_RGB)
    path = out / "tavletta.icns"
    path.write_bytes(icns({n: render(macos_source(n), n) for n in sizes}))
    written.append(path)
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "build" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for path in build(out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
