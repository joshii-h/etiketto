"""Lines, rectangles, ellipses and the label's border, drawn with cairo at
the printer's resolution.

Every line is a whole number of printer dots wide, at least one, so it
neither vanishes nor changes its width from label to label; dashes and
dots repeat in whole dots. For black-and-white printers nothing is
anti-aliased and fills are 1-bit patterns, never grey.
"""

from __future__ import annotations

import math

import cairo
from PIL import Image, ImageChops, ImageDraw

from etiketto.model import ShapeElement

# patterns of the fills, in printer dots per mm (period about 0.6 mm)
PATTERN_MM = 0.6


def px(mm: float, dpi: float) -> int:
    """mm in whole printer dots; anything above 0 is at least one dot."""
    if mm <= 0:
        return 0
    return max(1, round(mm / 25.4 * dpi))


def rounded_rect(ctx: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    """A rectangle with corners of radius r as the current path."""
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        ctx.rectangle(x, y, w, h)
        return
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def dashes(style: str, width: int) -> list[float]:
    """The dash pattern of a line style in whole dots."""
    if style == "dashed":
        return [3.0 * width, 2.0 * width]
    if style == "dotted":
        return [float(width), float(width)]
    return []


def mask_surface(size: tuple[int, int], antialias: bool) -> tuple:
    surface = cairo.ImageSurface(cairo.FORMAT_A8, max(1, size[0]), max(1, size[1]))
    ctx = cairo.Context(surface)
    if not antialias:
        ctx.set_antialias(cairo.ANTIALIAS_NONE)
    return surface, ctx


def surface_mask(surface: cairo.ImageSurface) -> Image.Image:
    surface.flush()
    size = (surface.get_width(), surface.get_height())
    return Image.frombuffer(
        "L", size, bytes(surface.get_data()), "raw", "L", surface.get_stride(), 1
    ).copy()


def colored(mask: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", mask.size, (*color, 0))
    img.putalpha(mask)
    return img


# --- patterns ------------------------------------------------------------------


def pattern(fill: str, size: tuple[int, int], dpi: float) -> Image.Image:
    """A 1-bit fill pattern (255 = ink) of the given size."""
    if fill == "solid":
        return Image.new("L", size, 255)
    period = max(3, px(PATTERN_MM, dpi))
    line = max(1, period // 3)
    img = Image.new("L", size, 0)
    draw = ImageDraw.Draw(img)
    w, h = size
    if fill == "hatched":
        for start in range(-h, w + h, period):
            draw.line((start, h, start + h, 0), fill=255, width=line)
    elif fill == "dotted":
        dot = (period + 1) // 2
        for y in range(0, h, period):
            for x in range(0, w, period):
                draw.rectangle((x, y, x + dot - 1, y + dot - 1), fill=255)
    elif fill == "grid":
        for x in range(0, w, period):
            draw.rectangle((x, 0, x + line - 1, h), fill=255)
        for y in range(0, h, period):
            draw.rectangle((0, y, w, y + line - 1), fill=255)
    return img


# --- shape elements --------------------------------------------------------------


def shape_size(element: ShapeElement, height: int, dpi: float) -> tuple[int, int]:
    """The element's size in dots: a separator as wide as its line; a
    horizontal line as high as its line; else width × height (0 = all of
    the content height)."""
    line = max(1, px(element.line_mm, dpi))
    tall = height if element.height_mm <= 0 else min(height, px(element.height_mm, dpi))
    if element.shape == "separator":
        return line, height
    wide = px(element.width_mm, dpi)
    if element.shape == "line":
        if element.direction == "horizontal":
            return wide, max(line, _arrow(line) if element.arrows != "none" else line)
        if element.direction == "vertical":
            return max(line, _arrow(line) if element.arrows != "none" else line), tall
    return wide, tall


def _arrow(line: int) -> int:
    """The width of an arrow head (and its length) for a line."""
    return max(3, 4 * line)


def render_shape(element: ShapeElement, height: int, dpi: float, color, mono: bool):
    """The shape in its colour on transparent."""
    w, h = shape_size(element, height, dpi)
    line = px(element.line_mm, dpi)
    if element.shape in ("line", "separator"):
        mask = _line_mask(element, (w, h), max(1, line), not mono)
    else:
        mask = _area_mask(element, (w, h), line, dpi, not mono)
    return colored(mask, color)


def _line_ends(element: ShapeElement, size: tuple[int, int], line: int):
    w, h = size
    if element.shape == "separator" or element.direction == "vertical":
        return (w / 2, 0.0), (w / 2, float(h))
    if element.direction == "horizontal":
        return (0.0, h / 2), (float(w), h / 2)
    half = line / 2
    if element.direction == "up":  # rising: bottom left to top right
        return (half, h - half), (w - half, half)
    return (half, half), (w - half, h - half)


def _line_mask(element: ShapeElement, size, line: int, antialias: bool) -> Image.Image:
    surface, ctx = mask_surface(size, antialias)
    start, end = _line_ends(element, size, line)
    heads = _heads(element)
    length = math.dist(start, end)
    head = _arrow(line) if heads else 0
    unit = ((end[0] - start[0]) / max(length, 1e-6), (end[1] - start[1]) / max(length, 1e-6))
    # the line stops where an arrow head begins, so its end stays pointed
    a = _along(start, unit, head if "start" in heads else 0)
    b = _along(end, unit, -head if "end" in heads else 0)
    ctx.set_line_width(line)
    ctx.set_dash(dashes(element.line_style, line))
    ctx.move_to(*a)
    ctx.line_to(*b)
    ctx.stroke()
    for which in heads:
        tip, sign = (start, 1) if which == "start" else (end, -1)
        _arrow_head(ctx, tip, (unit[0] * sign, unit[1] * sign), head)
    return surface_mask(surface)


def _heads(element: ShapeElement) -> tuple[str, ...]:
    if element.shape != "line" or element.arrows == "none":
        return ()
    return {"start": ("start",), "end": ("end",), "both": ("start", "end")}[element.arrows]


def _along(point, unit, distance: float):
    return (point[0] + unit[0] * distance, point[1] + unit[1] * distance)


def _arrow_head(ctx: cairo.Context, tip, unit, size: int) -> None:
    """A filled triangle with its tip at tip, pointing against unit."""
    base = _along(tip, unit, size)
    normal = (-unit[1] * size / 2, unit[0] * size / 2)
    ctx.move_to(*tip)
    ctx.line_to(base[0] + normal[0], base[1] + normal[1])
    ctx.line_to(base[0] - normal[0], base[1] - normal[1])
    ctx.close_path()
    ctx.fill()


def _area_path(element: ShapeElement, ctx, size, inset: float, dpi: float) -> None:
    w, h = size
    x, y, iw, ih = inset, inset, max(0.0, w - 2 * inset), max(0.0, h - 2 * inset)
    if element.shape == "ellipse":
        ctx.save()
        ctx.translate(x + iw / 2, y + ih / 2)
        ctx.scale(max(iw, 1e-6) / 2, max(ih, 1e-6) / 2)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.restore()
    else:
        rounded_rect(ctx, x, y, iw, ih, px(element.corner_mm, dpi) - inset)


def _area_mask(element: ShapeElement, size, line: int, dpi: float, antialias: bool):
    """A rectangle or ellipse: the fill (a pattern) inside, the outline on
    the edge, both inside size."""
    mask = Image.new("L", size, 0)
    if element.fill != "none":
        surface, ctx = mask_surface(size, antialias)
        _area_path(element, ctx, size, line / 2 if line else 0, dpi)
        ctx.fill()
        inside = surface_mask(surface)
        mask = ImageChops.multiply(inside, pattern(element.fill, size, dpi))
    if line:
        surface, ctx = mask_surface(size, antialias)
        _area_path(element, ctx, size, line / 2, dpi)
        ctx.set_line_width(line)
        ctx.set_dash(dashes(element.line_style, line))
        ctx.stroke()
        mask = ImageChops.lighter(mask, surface_mask(surface))
    return mask


# --- the label's border ------------------------------------------------------------


def border_mask(size, box, width: int, radius: int, style: str, antialias: bool) -> Image.Image:
    """The border (255 = ink) of an image of size: its outer edge on box
    (x0, y0, x1, y1, inclusive), width dots wide. "double": two lines of a
    third of the width each, a third apart (each at least one dot)."""
    surface, ctx = mask_surface(size, antialias)
    x0, y0, x1, y1 = box
    if style == "double":
        thin = max(1, round(width / 3))
        for inset in (0, 2 * thin):
            _border_line(
                ctx, (x0 + inset, y0 + inset, x1 - inset, y1 - inset), thin, radius - inset
            )
    else:
        ctx.set_dash(dashes(style, width))
        _border_line(ctx, box, width, radius)
    return surface_mask(surface)


def _border_line(ctx: cairo.Context, box, width: int, radius: float) -> None:
    x0, y0, x1, y1 = box
    half = width / 2
    rounded_rect(
        ctx,
        x0 + half,
        y0 + half,
        x1 - x0 + 1 - width,
        y1 - y0 + 1 - width,
        max(0.0, radius - half),
    )
    ctx.set_line_width(width)
    ctx.stroke()
