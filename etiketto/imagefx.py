"""Image options of an image element (crop, turn, mirror, brightness,
contrast, gamma, invert, rounded corners, a border) and the conversion of
parts of a label to black and white for mono printers.

Only Pillow: every step works on whole images, no pixel loops in Python.
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageChops, ImageColor, ImageEnhance, ImageFilter, ImageOps

# the ordered dithering's threshold map (Bayer, 8 × 8): a regular pattern
# that stays the same from label to label, unlike error diffusion
_BAYER2 = ((0, 2), (3, 1))
# a picture with less than this share of pixels between dark and light is
# a drawing (a logo, a scan of text): a threshold keeps its edges
AUTO_GREY_SHARE = 0.12
DARK, LIGHT = 48, 208
HALF = [0] * 128 + [255] * 128  # a threshold at the middle, as a table


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


def tiled(tile: Image.Image, size: tuple[int, int]) -> Image.Image:
    """A grey image of size filled with copies of tile, built row by row
    from bytes (no paste per copy)."""
    w, h = size
    tw, th = tile.size
    data = tile.tobytes()
    repeat = -(-w // tw)
    rows = [(data[y * tw : (y + 1) * tw] * repeat)[:w] for y in range(th)]
    return Image.frombytes("L", (w, h), b"".join(rows[y % th] for y in range(h)))


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


def framed(
    img: Image.Image, radius: int, border: int, color, antialias: bool = True
) -> Image.Image:
    """Corners cut round with radius pixels and a border of border pixels
    on the image's edge (inside it, so the size stays) in color. Smooth on
    colour printers; hard edged for black-and-white ones (antialias
    False: no grey)."""
    if radius <= 0 and border <= 0:
        return img
    from etiketto.shapes import mask_surface, rounded_rect, surface_mask

    img = img.convert("RGBA")
    if radius > 0:
        surface, ctx = mask_surface(img.size, antialias)
        rounded_rect(ctx, 0, 0, img.width, img.height, radius)
        ctx.fill()
        img.putalpha(ImageChops.multiply(img.getchannel("A"), surface_mask(surface)))
    if border > 0:
        surface, ctx = mask_surface(img.size, antialias)
        half = border / 2
        rounded_rect(
            ctx, half, half, img.width - border, img.height - border, max(0, radius - half)
        )
        ctx.set_line_width(border)
        ctx.stroke()
        ring = surface_mask(surface)
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
    return img.getchannel("A").point(HALF)


def threshold(grey: Image.Image, level: int = 128) -> Image.Image:
    return grey.point([0] * level + [255] * (256 - level))


def floyd_steinberg(grey: Image.Image) -> Image.Image:
    return grey.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def ordered(grey: Image.Image) -> Image.Image:
    """Ordered (Bayer) dithering: white where the grey is above the map."""
    above = ImageChops.subtract(grey, tiled(_bayer_tile(), grey.size))
    return above.point(lambda v: 255 if v > 0 else 0)


# a picture whose pixels are almost all (TONE_COVER) in at most DRAWING_TONES
# separate, narrow tones is a drawing, also in flat middle tones (a grey
# logo) and with the noise of JPEG compression around each tone. A tone:
# neighbouring bands of TONE_BAND grey levels that each hold more than
# TONE_SHARE of the pixels, at most TONE_WIDTH bands wide (wider: a
# gradient, a photo)
DRAWING_TONES = 4
TONE_COVER = 0.95
TONE_BAND = 8
TONE_SHARE = 0.015
TONE_WIDTH = 2


def is_drawing(grey: Image.Image, mask: Image.Image | None = None) -> bool:
    """A drawing, not a photo: few pixels between dark and light, or only a
    few distinct tones."""
    histogram = grey.histogram(mask)
    total = sum(histogram)
    if not total:
        return True
    if sum(histogram[DARK:LIGHT]) / total < AUTO_GREY_SHARE:
        return True
    return few_tones(histogram, total)


def few_tones(histogram: list[int], total: int) -> bool:
    """Whether a few separate, narrow tones hold almost all pixels (see
    DRAWING_TONES): flat colours, each with some noise around it; not a
    gradient or a photo, whose pixels spread over neighbouring bands."""
    return tone_count(histogram, total) is not None


def tone_count(histogram: list[int], total: int) -> int | None:
    """How many separate, narrow tones hold almost all pixels; None if they
    are not a few (a gradient, a photo)."""
    bands = [sum(histogram[i : i + TONE_BAND]) for i in range(0, 256, TONE_BAND)]
    tones, width, covered = 0, 0, 0
    for count in [*bands, 0]:
        if count > TONE_SHARE * total:
            width += 1
            covered += count
            continue
        if width:
            tones += 1
            if width > TONE_WIDTH:
                return None
        width = 0
    return tones if tones <= DRAWING_TONES and covered >= TONE_COVER * total else None


def otsu(histogram: list[int]) -> int:
    """The level that best splits a histogram into two classes (Otsu): a
    light yellow logo on white is split between yellow and white, not at
    the middle grey (which would leave it white). 128 for a single class."""
    total = sum(histogram)
    weighted = sum(i * n for i, n in enumerate(histogram))
    below = below_weighted = 0
    best, level = -1.0, 128
    for i, n in enumerate(histogram[:-1]):
        below += n
        below_weighted += i * n
        above = total - below
        if not below or not above:
            continue
        mean_below = below_weighted / below
        mean_above = (weighted - below_weighted) / above
        spread = below * above * (mean_below - mean_above) ** 2
        if spread > best:
            best, level = spread, i + 1
    return level


# thresholding a drawing that leaves less ink than this share of the
# pixels that are not white falls back to dithering
MIN_INK_SHARE = 0.05
NOT_WHITE = 250


def _drawing(grey: Image.Image, alpha: Image.Image) -> Image.Image:
    """A drawing: thresholded between its own two tones (otsu), dithered
    if that would leave almost no ink."""
    histogram = grey.histogram(alpha)
    bw = threshold(grey, otsu(histogram))
    coloured = sum(histogram[:NOT_WHITE])
    if coloured:
        ink = bw.histogram(alpha)[0]
        if ink < MIN_INK_SHARE * coloured:
            return floyd_steinberg(grey)
    return bw


def _auto(grey: Image.Image, alpha: Image.Image) -> Image.Image:
    """ "Automatic": a drawing in black and white or two tones thresholded
    between them (Otsu); a drawing in several flat tones as even patterns
    (ordered dithering keeps each tone apart, a threshold would merge them
    into a blob); a photo or gradient dithered (Floyd-Steinberg)."""
    histogram = grey.histogram(alpha)
    total = sum(histogram)
    if not total or sum(histogram[DARK:LIGHT]) / total < AUTO_GREY_SHARE:
        return _drawing(grey, alpha)
    tones = tone_count(histogram, total)
    if tones is None:
        return floyd_steinberg(grey)
    return _drawing(grey, alpha) if tones <= 2 else ordered(grey)


def _converted(grey: Image.Image, alpha: Image.Image, mode: str, level: int) -> Image.Image:
    if mode == "auto":
        return _auto(grey, alpha)
    if mode == "dither":
        return floyd_steinberg(grey)
    if mode == "ordered":
        return ordered(grey)
    return threshold(grey, level)


# the red of black-and-red printers: what brother_ql's red_mask() takes as red
RED = (255, 0, 0)


def red_mask(img: Image.Image) -> Image.Image:
    """Red-ish pixels (hue, saturation and value as a black-and-red
    printer splits them), 255 where red."""
    hue, saturation, value = img.convert("RGB").convert("HSV").split()
    red = ImageChops.multiply(
        hue.point(lambda v: 255 if v < 40 or v > 210 else 0),
        saturation.point(lambda v: 255 if v > 100 else 0),
    )
    return ImageChops.multiply(red, value.point(lambda v: 255 if v > 80 else 0))


def black_and_white(
    img: Image.Image, mode: str = "auto", level: int = 128, red: bool = False
) -> Image.Image:
    """The image (RGBA) as a black-and-white printer prints it: pure black
    and white, hard edged transparency. mode: "threshold" at level,
    "dither" (Floyd-Steinberg), "ordered" (Bayer) or "auto" (drawings
    thresholded between their own tones, photos dithered). red: a black
    and red printer; red pixels stay pure red."""
    img = img.convert("RGBA")
    alpha = _hard_alpha(img)
    reds = red_mask(img) if red else None
    grey = _on_white(img)
    if reds is not None:  # the red parts are left out of the grey
        grey.paste(255, (0, 0), reds)
    out = _converted(grey, alpha, mode, level).convert("RGBA")
    if reds is not None:
        out.paste(RED + (255,), (0, 0), reds)
    out.putalpha(alpha)
    return out


def hard(img: Image.Image, red: bool = False) -> Image.Image:
    """A part drawn in black and white (text, a code, a symbol, a line,
    their colours already mapped by mono_ink()) for a black-and-white
    printer: anti-aliased edges become whole dots, not dithered noise;
    transparency hard edged. Without red no compositing: the colours are
    black and white already."""
    if red:
        return black_and_white(img, "threshold", 128, True)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    grey = img.convert("L").point(HALF)
    return Image.merge("RGBA", (grey, grey, grey, img.getchannel("A").point(HALF)))


def _few_colours(colours: list, red: bool) -> str:
    """tones() of a part with these (count, RGBA) colours."""
    allowed = {(0, 0, 0), (255, 255, 255)} | ({RED} if red else set())
    if {c[:3] for _n, c in colours if c[3] >= 128} <= allowed:
        return "edges" if any(0 < c[3] < 255 for _n, c in colours) else "hard"
    return "colour"


def tones(img: Image.Image, red: bool = False) -> str:
    """What a part drawn in black and white holds: "hard" (only black and
    white, transparency hard edged: nothing to convert), "edges" (soft
    edges of transparency) or "colour" (opaque pixels between black and
    white, like a colour emoji in a text: a drawing's or photo's
    conversion). red: red is an ink too (a black-and-red printer)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    colours: list | None = img.getcolors(16)  # quick: a few colours, or None
    if colours is not None:
        return _few_colours(colours, red)
    alpha = img.getchannel("A")
    if sum(img.convert("L").histogram(alpha.point(HALF))[1:255]):
        return "colour"
    return "edges" if sum(alpha.histogram()[1:255]) else "hard"


def outlined_colours(img: Image.Image, converted: Image.Image, ink) -> Image.Image:
    """A converted part with a line of ink around its colour areas (a colour
    emoji in a text): a light face or a white square that the conversion
    leaves white still shows its shape. img: the part before conversion."""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    spread = ImageChops.subtract(
        ImageChops.lighter(ImageChops.lighter(r, g), b),
        ImageChops.darker(ImageChops.darker(r, g), b),
    )
    grey = rgb.convert("L").point([0] + [255] * 254 + [0])
    colour = ImageChops.lighter(spread.point([0] + [255] * 255), grey)
    alpha = img.getchannel("A").point(HALF) if img.mode == "RGBA" else None
    if alpha is not None:
        colour = ImageChops.multiply(colour, alpha)
    area = colour.filter(ImageFilter.MaxFilter(5))  # the emoji as a whole
    if alpha is not None:
        area = ImageChops.multiply(area, alpha)
    edge = ImageChops.subtract(area, area.filter(ImageFilter.MinFilter(3)))
    out = converted.copy()
    out.paste((*ink, 255), (0, 0), edge)
    return out


# --- inks -------------------------------------------------------------------------


def rgb(value: str, fallback: str = "#000000") -> tuple[int, int, int]:
    """A colour of the label file as RGB; the fallback if it is not one."""
    try:
        return ImageColor.getrgb(value)[:3]
    except ValueError:
        return ImageColor.getrgb(fallback)[:3]


def luma(rgb: tuple[int, int, int]) -> float:
    """Brightness 0-255 of a colour (ITU-R BT.601)."""
    r, g, b = rgb[:3]
    return (r * 299 + g * 587 + b * 114) / 1000


def mono_ink(
    rgb: tuple[int, int, int], background: float = 255.0, red: bool = False
) -> tuple[int, int, int]:
    """What a black-and-white printer prints for a colour on a background
    of the given brightness: white only if the colour is lighter than the
    background (a light symbol on a dark label), else black, so a light
    colour on white paper stays visible. red: a black and red printer
    keeps red."""
    if red and red_mask(Image.new("RGB", (1, 1), rgb[:3])).getpixel((0, 0)):
        return RED
    own = luma(rgb)
    lighter = own > background or (own == background and own >= 128)
    return (255, 255, 255) if lighter else (0, 0, 0)


def opposite(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """The other ink of black and white."""
    return (0, 0, 0) if luma(rgb) >= 128 else (255, 255, 255)
