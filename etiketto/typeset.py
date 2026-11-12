"""Lay out and draw a text element with Pango and cairo: the font, line and
letter spacing, underline and strikethrough, wrapping, and what is drawn
around the letters (an outline, a shadow, a box or a frame, the text cut
out of its box when inverted). Fitting the size: etiketto.render.

Black-and-white printers get no anti-aliasing at all (crisp 1-bit text),
an outline and a shadow at least one printer dot wide, a solid shadow
kept apart from the letters by one dot (see mono_text()). Letter spacing
leaves out scripts whose letters join (CURSIVE).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo  # noqa: E402
from PIL import Image  # noqa: E402

from etiketto import imagefx  # noqa: E402
from etiketto.model import TextElement  # noqa: E402
from etiketto.shapes import px, rounded_rect  # noqa: E402

# scripts whose letters join: letter spacing would tear them apart, so it
# is left out for their runs
CURSIVE = frozenset(
    GLib.UnicodeScript(value)
    for value in (
        GLib.UnicodeScript.ARABIC,
        GLib.UnicodeScript.SYRIAC,
        GLib.UnicodeScript.NKO,
        GLib.UnicodeScript.MONGOLIAN,
        GLib.UnicodeScript.MANDAIC,
        GLib.UnicodeScript.DEVANAGARI,
        GLib.UnicodeScript.BENGALI,
        GLib.UnicodeScript.GURMUKHI,
        GLib.UnicodeScript.GUJARATI,
        GLib.UnicodeScript.ORIYA,
        GLib.UnicodeScript.TAMIL,
        GLib.UnicodeScript.TELUGU,
        GLib.UnicodeScript.KANNADA,
        GLib.UnicodeScript.MALAYALAM,
        GLib.UnicodeScript.SINHALA,
        GLib.UnicodeScript.TIBETAN,
    )
)


NEUTRAL = frozenset(
    GLib.UnicodeScript(value)
    for value in (GLib.UnicodeScript.COMMON, GLib.UnicodeScript.INHERITED)
)


def _rgb(value: str, fallback: str = "#000000") -> tuple[float, float, float]:
    r, g, b = imagefx.rgb(value, fallback)
    return r / 255, g / 255, b / 255


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def mono_text(element: TextElement, background: float = 255.0, red: bool = False) -> TextElement:
    """The element as a black-and-white printer prints it on a background
    of that brightness: each colour black or white against what lies behind
    it (imagefx.mono_ink), an outline always the other ink than the
    letters, the shadow solid in the other ink than what lies behind."""
    behind = background
    box = ""
    if element.inverted or element.box_color:
        box_rgb = imagefx.mono_ink(
            imagefx.rgb(element.color if element.inverted else element.box_color), background, red
        )
        box, behind = _hex(box_rgb), imagefx.luma(box_rgb)
    text = imagefx.mono_ink(imagefx.rgb(element.color), behind, red)
    outline = imagefx.mono_ink(imagefx.rgb(element.outline_color, "#ffffff"), behind, red)
    if outline == text:
        outline = imagefx.opposite(text)
    shadow = imagefx.opposite((255, 255, 255) if behind >= 128 else (0, 0, 0))
    return replace(
        element,
        color=box if element.inverted else _hex(text),
        outline_color=_hex(outline),
        box_color=box if element.box_color else "",
        shadow_color=_hex(shadow),
    )


@dataclass(frozen=True)
class Extras:
    """What is drawn around the letters, in printer dots."""

    outline: int = 0
    shadow: int = 0
    padding: int = 0
    frame: int = 0
    corner: int = 0
    boxed: bool = False  # a box or a frame around the text

    @property
    def pad(self) -> int:
        """Space on every side of the text's own extents."""
        return self.outline + (self.padding + self.frame if self.boxed else 0)

    def any(self) -> bool:
        return bool(self.outline or self.shadow or self.boxed)


def extras(element: TextElement, dpi: float) -> Extras:
    boxed = bool(element.inverted or element.box_color or element.frame_mm > 0)
    return Extras(
        outline=px(element.outline_mm, dpi),
        shadow=px(element.shadow_mm, dpi),
        padding=px(element.padding_mm, dpi) if boxed else 0,
        frame=px(element.frame_mm, dpi),
        corner=px(element.corner_mm, dpi),
        boxed=boxed,
    )


def layout(
    element: TextElement, size_px: float, antialias: bool = True, wrap_px: int | None = None
) -> Pango.Layout:
    desc = Pango.FontDescription.from_string(element.font or "Sans")
    desc.set_weight(Pango.Weight.BOLD if element.bold else Pango.Weight.NORMAL)
    desc.set_style(Pango.Style.ITALIC if element.italic else Pango.Style.NORMAL)
    desc.set_absolute_size(max(1.0, size_px) * Pango.SCALE)

    measure = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    text = PangoCairo.create_layout(cairo.Context(measure))
    if not antialias:
        options = cairo.FontOptions()
        options.set_antialias(cairo.ANTIALIAS_NONE)
        options.set_hint_style(cairo.HINT_STYLE_FULL)
        PangoCairo.context_set_font_options(text.get_context(), options)
    text.set_font_description(desc)
    text.set_alignment(
        {
            "center": Pango.Alignment.CENTER,
            "right": Pango.Alignment.RIGHT,
        }.get(element.align, Pango.Alignment.LEFT)
    )
    attributes = _attributes(element, size_px)
    if attributes is not None:
        text.set_attributes(attributes)
    if element.line_spacing != 1.0:
        text.set_line_spacing(element.line_spacing)
    if wrap_px:
        text.set_width(max(1, wrap_px) * Pango.SCALE)
        text.set_wrap(Pango.WrapMode.WORD_CHAR)
    text.set_text(_shown(element), -1)
    return text


def _attributes(element: TextElement, size_px: float) -> Pango.AttrList | None:
    found = []
    if element.underline:
        found.append(Pango.attr_underline_new(Pango.Underline.SINGLE))
    if element.strikethrough:
        found.append(Pango.attr_strikethrough_new(True))
    if element.letter_spacing:
        spacing = round(size_px * element.letter_spacing / 100 * Pango.SCALE)
        for start, end in spaced_runs(_shown(element)):
            attribute = Pango.attr_letter_spacing_new(spacing)
            attribute.start_index, attribute.end_index = start, end
            found.append(attribute)
    if not found:
        return None
    attributes = Pango.AttrList()
    for attribute in found:
        attributes.insert(attribute)
    return attributes


def _shown(element: TextElement) -> str:
    # a trailing newline (easy to type) would add an empty line and halve the text
    return element.text.rstrip("\n") or " "


def spaced_runs(text: str) -> list[tuple[int, int]]:
    """Byte ranges (UTF-8, as Pango counts) of the text outside cursive
    scripts: only these get letter spacing."""
    runs: list[tuple[int, int]] = []
    start = position = 0
    cursive = False
    for char in text:
        size = len(char.encode())
        script = GLib.unichar_get_script(char)
        if script not in NEUTRAL:  # spaces and marks go with the letters before
            cursive = script in CURSIVE
        if cursive:
            if position > start:
                runs.append((start, position))
            start = position + size
        position += size
    if position > start:
        runs.append((start, position))
    return runs


def size_of(element: TextElement, size_px: float, dpi: float, antialias: bool, wrap_px) -> tuple:
    """(width, height) of the block, turned as it will be printed."""
    _ink, logical = layout(element, size_px, antialias, wrap_px).get_pixel_extents()
    extra = extras(element, dpi)
    w = max(1, logical.width) + 2 * extra.pad + extra.shadow
    h = max(1, logical.height) + 2 * extra.pad + extra.shadow
    return (h, w) if element.rotation in (90, 270) else (w, h)


def block(
    element: TextElement,
    size_px: float,
    dpi: float,
    antialias: bool = True,
    wrap_px: int | None = None,
) -> Image.Image:
    """The text laid out into RGBA, turned as it will be printed; without
    anti-aliasing every pixel is either ink or not."""
    text = layout(element, size_px, antialias, wrap_px)
    _ink, logical = text.get_pixel_extents()
    extra = extras(element, dpi)
    width = max(1, logical.width) + 2 * extra.pad + extra.shadow
    height = max(1, logical.height) + 2 * extra.pad + extra.shadow
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    if not antialias:
        ctx.set_antialias(cairo.ANTIALIAS_NONE)
    origin = (extra.pad - logical.x, extra.pad - logical.y)
    if extra.any():
        _decorate(ctx, text, element, extra, (width, height), origin)
        if extra.shadow and not extra.outline and not antialias:
            _gap(ctx, text, element, origin)
    ctx.set_source_rgb(*_rgb(element.color))
    if element.inverted:  # cut out of the box: the label shows through
        ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.move_to(*origin)
    PangoCairo.update_layout(ctx, text)
    PangoCairo.show_layout(ctx, text)
    surface.flush()
    img = Image.frombuffer(
        "RGBA", (width, height), bytes(surface.get_data()), "raw", "BGRa", surface.get_stride(), 1
    ).copy()
    return _turned(img, element.rotation)


def _gap(ctx, text, element: TextElement, origin) -> None:
    """Black and white: one dot of what lies behind between the letters and
    their solid shadow, so the letters do not look fatter."""
    ctx.save()
    behind = element.color if element.inverted else element.box_color
    if behind:
        ctx.set_source_rgb(*_rgb(behind))
    else:  # the label shows through
        ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.move_to(*origin)
    PangoCairo.layout_path(ctx, text)
    ctx.set_line_width(2)  # one dot outside the letters
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.stroke()
    ctx.restore()


def _turned(img: Image.Image, rotation: int) -> Image.Image:
    turns = {
        90: Image.Transpose.ROTATE_270,  # clockwise; Pillow turns the other way
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }
    return img.transpose(turns[rotation]) if rotation in turns else img


def _decorate(ctx, text, element: TextElement, extra: Extras, size, origin) -> None:
    """Box, frame, shadow and outline, under the letters."""
    width, height = size
    box_w, box_h = width - extra.shadow, height - extra.shadow
    fill = element.color if element.inverted else element.box_color
    if fill:
        rounded_rect(ctx, 0, 0, box_w, box_h, extra.corner)
        ctx.set_source_rgb(*_rgb(fill))
        ctx.fill()
    if extra.frame and not element.inverted:
        half = extra.frame / 2
        rounded_rect(
            ctx, half, half, box_w - extra.frame, box_h - extra.frame, max(0, extra.corner - half)
        )
        ctx.set_source_rgb(*_rgb(element.color))
        ctx.set_line_width(extra.frame)
        ctx.stroke()
    if extra.shadow:
        ctx.move_to(origin[0] + extra.shadow, origin[1] + extra.shadow)
        PangoCairo.layout_path(ctx, text)
        ctx.set_source_rgb(*_rgb(element.shadow_color, "#808080"))
        ctx.fill()
    if extra.outline:
        ctx.move_to(*origin)
        PangoCairo.layout_path(ctx, text)
        ctx.set_source_rgb(*_rgb(element.outline_color, "#ffffff"))
        ctx.set_line_width(2 * extra.outline)  # half of it lies under the letters
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        ctx.stroke()
