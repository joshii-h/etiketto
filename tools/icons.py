#!/usr/bin/env python3
"""Render Tavletta's icon from its SVG sources into PNG, ICO and ICNS files.

    python3 tools/icons.py [output folder]     (default: build/icons)

Sources (all SVG Tiny 1.2, see tests/test_icons.py):

- data/xyz.joshuahirsig.Tavletta.svg: the icon, 128 x 128, used from 48 px up
- data/icons/tavletta-16.svg, -20.svg, -24.svg, -32.svg: the same tablet
  redrawn on a 16, 20, 24 and 32 px grid with a thicker frame and fewer
  details (20 px only for Windows at 125 %)
- data/icons/tavletta-macos.svg: the tablet on a macOS plate (1024 x 1024)

Output:

- hicolor/<n>x<n>/apps/xyz.joshuahirsig.Tavletta.png for n in HICOLOR
- tavletta.ico: Windows, the sizes in ICO; small sizes as 32-bit BMP (what
  every Windows version reads), 256 px as PNG
- tavletta.icns: macOS, 16 to 1024 px including the @2x sizes, every entry
  PNG. Written here rather than with Pillow, whose ICNS writer leaves out
  16 and 32 px at 1x (icp4, icp5); iconutil only exists on macOS.

Needs PyGObject with librsvg (Rsvg 2.0), pycairo and Pillow. Linux packages
do not need the PNGs: setup.py installs the SVGs (the small variants as
the hicolor 16, 24 and 32 px icons).
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
SMALL = {n: ROOT / "data" / "icons" / f"tavletta-{n}.svg" for n in (16, 20, 24, 32)}
MACOS = ROOT / "data" / "icons" / "tavletta-macos.svg"

HICOLOR = (16, 24, 32, 48, 64, 128, 256, 512)
ICO = (16, 20, 24, 32, 40, 48, 64, 256)
ICO_PNG_FROM = 256  # this size and larger as PNG, smaller ones as BMP
# ICNS type: pixel size (the @2x entries are the next 1x size's pixels)
ICNS = {
    b"icp4": 16,
    b"icp5": 32,
    b"ic11": 32,  # 16@2x
    b"ic12": 64,  # 32@2x
    b"ic07": 128,
    b"ic13": 256,  # 128@2x
    b"ic08": 256,
    b"ic14": 512,  # 256@2x
    b"ic09": 512,
    b"ic10": 1024,  # 512@2x
}


def source(size: int) -> Path:
    """The SVG drawn for this size: the small drawing nearest above, else the icon.

    40 px uses the 32 px drawing: scaled up a little it stays sharper than
    the 128 px icon scaled down a lot."""
    for drawn in sorted(SMALL):
        if size <= drawn:
            return SMALL[drawn]
    return SMALL[32] if size <= 40 else MAIN


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


def bmp_entry(image: Image.Image) -> bytes:
    """An ICO image as BITMAPINFOHEADER, BGRA rows bottom up, then the AND mask."""
    size = image.width
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    # the mask is unused with 32-bit colour, but must be there: 1 bit per
    # pixel, rows padded to 4 bytes, all 0 (opaque; the alpha channel rules)
    mask = bytes(((size + 31) // 32) * 4 * size)
    return header + pixels + mask


def ico(images: dict[int, Image.Image]) -> bytes:
    sizes = sorted(images)
    entries = [png_bytes(images[n]) if n >= ICO_PNG_FROM else bmp_entry(images[n]) for n in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for n, data in zip(sizes, entries, strict=True):
        side = n if n < 256 else 0  # 0 means 256
        out += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(entries)


def icns(images: dict[int, Image.Image]) -> bytes:
    body = b""
    for kind, size in ICNS.items():
        data = png_bytes(images[size])
        body += kind + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


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
    path = out / "tavletta.icns"
    path.write_bytes(icns({n: render(MACOS, n) for n in set(ICNS.values())}))
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
