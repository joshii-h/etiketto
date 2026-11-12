"""Image options of an image element (crop, turn, mirror, brightness,
contrast, gamma, invert, rounded corners, a border) and the conversion of
parts of a label to black and white for mono printers.

Only Pillow: every step works on whole images, no pixel loops in Python.
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps

# the ordered dithering's threshold map (Bayer, 8 × 8): a regular pattern
# that stays the same from label to label, unlike error diffusion
_BAYER2 = ((0, 2), (3, 1))
# a picture with less than this share of pixels between dark and light is
# a drawing (a logo, a scan of text): a threshold keeps its edges
AUTO_GREY_SHARE = 0.12
DARK, LIGHT = 48, 208


def _bayer(n: int) -> list[list[int]]:
    if n == 2:
        return [list(row) for row in _BAYER2]
    half = _bayer(n // 2)
    size = n // 2
    return [
        [4 * half[y % size][x % size] + _BAYER2[y // size][x // size] for x in range(n)]
        for y in range(n)
    ]


@lru_cache(maxsize=1)
def _bayer_tile() -> Image.Image:
    matrix = _bayer(8)
    tile = Image.new("L", (8, 8))
    tile.putdata([round((value + 0.5) * 256 / 64) for row in matrix for value in row])
    return tile


def _tiled(tile: Image.Image, size: tuple[int, int]) -> Image.Image:
    out = Image.new("L", size)
    for y in range(0, size[1], tile.height):
        for x in range(0, size[0], tile.width):
            out.paste(tile, (x, y))
    return out


# --- adjustments ------------------------------------------------------------


def cropped(img: Image.Image, crop) -> Image.Image:
    """The part of the image given as fractions x, y, width, height."""
    x, y, w, h = crop
    if (x, y, w, h) == (0.0, 0.0, 1.0, 1.0):
        return img
    box = (
        round(x * img.width),
        round(y * img.height),
        round((x + w) * img.width),
        round((y + h) * img.height),
    )
    left, top = min(box[0], img.width - 1), min(box[1], img.height - 1)
    return img.crop((left, top, max(left + 1, box[2]), max(top + 1, box[3])))


_TURNS = {
    90: Image.Transpose.ROTATE_270,  # Pillow turns counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def turned(img: Image.Image, rotation: int, mirror: bool = False, flip: bool = False):
    """Turned clockwise by rotation degrees, then mirrored and flipped."""
    if rotation in _TURNS:
        img = img.transpose(_TURNS[rotation])
    if mirror:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img


def adjusted(
    img: Image.Image,
    brightness: float = 0.0,
    contrast: float = 0.0,
    gamma: float = 1.0,
    invert: bool = False,
) -> Image.Image:
    """Brightness and contrast (-100 … 100, 0 = unchanged), gamma (> 1
    lightens the middle tones) and inverting, on the colours; the alpha
    channel stays."""
    if (brightness, contrast, gamma, invert) == (0.0, 0.0, 1.0, False):
        return img
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    rgb = img.convert("RGB")
    if brightness:
        rgb = ImageEnhance.Brightness(rgb).enhance(1 + brightness / 100)
    if contrast:
        rgb = ImageEnhance.Contrast(rgb).enhance(1 + contrast / 100)
    if gamma != 1.0:
        table = [round(255 * (v / 255) ** (1 / gamma)) for v in range(256)]
        rgb = rgb.point(table * 3)
    if invert:
        rgb = ImageOps.invert(rgb)
    if alpha is not None:
        rgb.putalpha(alpha)
    return rgb


def rounded(img: Image.Image, radius: int) -> Image.Image:
    """Corners cut round with radius pixels (hard edged: no grey for mono)."""
    if radius <= 0:
        return img
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.width - 1, img.height - 1), radius=radius, fill=255
    )
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return img


def bordered(img: Image.Image, width: int, color, radius: int = 0) -> Image.Image:
    """A border of width pixels drawn on the image's edge (inside it, so the
    size stays), following rounded corners."""
    if width <= 0:
        return img
    img = img.convert("RGBA")
    ring = Image.new("L", img.size, 0)
    ImageDraw.Draw(ring).rounded_rectangle(
        (0, 0, img.width - 1, img.height - 1), radius=radius, outline=255, width=width
    )
    img.paste(Image.new("RGBA", img.size, (*color, 255)), (0, 0), ring)
    return img


# --- black and white ----------------------------------------------------------


def _on_white(img: Image.Image) -> Image.Image:
    """The grey levels as printed on white paper (transparency = white)."""
    if img.mode != "RGBA":
        return img.convert("L")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, img).convert("L")


def _hard_alpha(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        return Image.new("L", img.size, 255)
    return img.getchannel("A").point(lambda v: 255 if v >= 128 else 0)


def threshold(grey: Image.Image, level: int = 128) -> Image.Image:
    return grey.point(lambda v: 255 if v >= level else 0)


def floyd_steinberg(grey: Image.Image) -> Image.Image:
    return grey.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def ordered(grey: Image.Image) -> Image.Image:
    """Ordered (Bayer) dithering: white where the grey is above the map."""
    above = ImageChops.subtract(grey, _tiled(_bayer_tile(), grey.size))
    return above.point(lambda v: 255 if v > 0 else 0)


def is_drawing(grey: Image.Image, mask: Image.Image | None = None) -> bool:
    """Few pixels between dark and light: a drawing, not a photo."""
    histogram = grey.histogram(mask)
    total = sum(histogram)
    if not total:
        return True
    return sum(histogram[DARK:LIGHT]) / total < AUTO_GREY_SHARE


def black_and_white(img: Image.Image, mode: str = "auto", level: int = 128) -> Image.Image:
    """The image (RGBA) as a black-and-white printer prints it: pure black
    and white, hard edged transparency. mode: "threshold" at level,
    "dither" (Floyd-Steinberg), "ordered" (Bayer) or "auto" (threshold for
    drawings, Floyd-Steinberg for photos)."""
    grey = _on_white(img)
    alpha = _hard_alpha(img)
    if mode == "auto":
        mode = "threshold" if is_drawing(grey, alpha) else "dither"
    if mode == "dither":
        bw = floyd_steinberg(grey)
    elif mode == "ordered":
        bw = ordered(grey)
    else:
        bw = threshold(grey, level)
    out = bw.convert("RGBA")
    out.putalpha(alpha)
    return out


def hard(img: Image.Image) -> Image.Image:
    """A part drawn in colours (text, a code, a symbol, a line) for a
    black-and-white printer: each pixel black or white by its brightness
    (a light colour stays white on a dark background), transparency hard
    edged. Anti-aliased edges become whole dots, not dithered noise."""
    return black_and_white(img.convert("RGBA"), "threshold")
