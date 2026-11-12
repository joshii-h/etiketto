"""The free layout: every element at its own position on the label
(Label.layout "free"), instead of one after another along the tape.

Positions (x_mm, y_mm) run from the label's top left corner as it is read
(label space: x along the tape, y across it); the safety margin, the
spacing and the alignment of the flowing layout do not apply. Each element
has its size of its own:

- a text its font size (size_pt; the size in mm is the font's em, as label
  designers give it) and its weight. Its position is the top left corner
  of its ink, not of its line: so the numbers of another label designer,
  which places the image of the text's ink, can be taken over as they are;
- a code its module size (module_mm), drawn as a whole number of printer
  dots (0.67 mm at 12 dots/mm: 8 dots); its position is the top left
  corner of its modules, the quiet zone lies outside (leave room for it);
  a linear code its bars' height (height_mm); a quarter turn (rotation);
- an image its height (height_mm), a symbol its size (size_mm), a shape its
  width and height; a document page fills the label from its position to
  the right and bottom edge.

Size 0 means FREE_SIZE_MM (text: FREE_TEXT_MM), so an element added in the
free layout is visible right away. adopt() gives the elements of a label
switched to the free layout the positions and sizes they had in the flow.
"""

from __future__ import annotations

import math
from dataclasses import replace

from PIL import Image

from tavletta import codes, imagefx, typeset
from tavletta import render as flow
from tavletta.model import (
    CodeElement,
    Element,
    ImageElement,
    Label,
    PageElement,
    ShapeElement,
    SymbolElement,
    TextElement,
)
from tavletta.render import Inks, Rendered, RenderWarning, mm_to_px, px_to_mm

FREE_SIZE_MM = 10.0  # an image, symbol or shape of size 0
FREE_TEXT_MM = 4.0  # a text of size 0: its em
FREE_MODULE_MM = 0.5  # a 2D code's module of size 0
FREE_BAR_MM = 0.3  # a linear code's narrowest bar of size 0

Box = tuple[int, int, int, int]  # left, top, right, bottom (exclusive)


def mm_to_pt(mm: float) -> float:
    return mm / 25.4 * 72


def pt_to_mm(pt: float) -> float:
    return pt * 25.4 / 72


def dots(mm: float, dpi: float) -> int:
    """mm as whole printer dots, the nearest (not at least 1)."""
    return round(mm / 25.4 * dpi)


# --- the parts ------------------------------------------------------------------


class Part:
    """An element drawn for the free layout: its image and the point of it
    that goes to the element's position (anchor)."""

    def __init__(self, image: Image.Image, anchor: tuple[int, int] = (0, 0)):
        self.image, self.anchor = image, anchor


def ink_box(image: Image.Image) -> Box | None:
    """Where an RGBA image has ink (alpha at least half)."""
    return image.getchannel("A").point(imagefx.HALF).getbbox()


def text_part(element: TextElement, dpi: float, inks: Inks) -> Part:
    """The text at its size (0: FREE_TEXT_MM), anchored at its ink."""
    pt = element.size_pt if element.size_pt > 0 else mm_to_pt(FREE_TEXT_MM)
    if inks.mono:
        element = typeset.mono_text(element, inks.background, inks.red)
    wrap = mm_to_px(element.max_width_mm, dpi) if element.max_width_mm > 0 else None
    block = typeset.block(element, pt / 72 * dpi, dpi, not inks.mono, wrap, exact=True)
    box = ink_box(block)
    return Part(block, box[:2] if box else (0, 0))


def module_px(element: CodeElement, dpi: float, steps: tuple[int, int] = (1, 1)) -> int:
    """The module in whole printer dots (a multiple of both steps, along
    and across, see render.render())."""
    default = FREE_MODULE_MM if codes.SPECS[element.symbology].layout in _MATRIX else FREE_BAR_MM
    module = max(1, dots(element.module_mm or default, dpi))
    unit = math.lcm(*steps)
    return max(unit, module // unit * unit)


_MATRIX = ("matrix", "stacked")


def code_part(
    element: CodeElement, dpi: float, inks: Inks, steps: tuple[int, int] = (1, 1)
) -> Part:
    """The code with modules of module_px() dots, anchored at its first
    module; the quiet zone outside (a linear code's lies in its image)."""
    if inks.mono:
        element = replace(element, color=inks.color(element.color))
    flow.check_text(element.data, lines=False)  # within limits, as it is filled in
    symbol = codes.encode(element)
    spec = codes.SPECS[element.symbology]
    module = module_px(element, dpi, steps)
    problems = ("code_thin",) if module < flow.THIN_MODULE * max(steps) else ()
    if spec.layout in _MATRIX:
        image = _modules(element, symbol, spec.row_modules, module, problems)
        box = (0, 0, image.width, image.height)
    else:
        image, box = _bars(element, symbol, spec, module, dpi)
    image.code_problems += codes.notes(element)  # type: ignore[attr-defined]
    return _turned(image, box, element.rotation)


def _modules(element, symbol, row_modules: int, module: int, problems) -> Image.Image:
    rows = symbol.rows
    size = (symbol.width * module, len(rows) * row_modules * module)
    flow.check_size(*size)  # within limits: before the mask is made
    mask = Image.new("L", size, 0)
    row_px = row_modules * module
    for index, row in enumerate(rows):
        top = index * row_px
        for start, end in flow._runs(row):
            mask.paste(255, (start * module, top, end * module, top + row_px))
    return flow._dark(element, mask, flow._Fit(module, 0, problems))


def _bars(element, symbol, spec, module: int, dpi: float) -> tuple[Image.Image, Box]:
    """A linear code as the flowing layout draws it, at exactly this module:
    its bars height_mm high (0: as designed for the module)."""
    pattern, quiet = flow._linear_pattern(symbol, spec)
    designed = sum(quiet) + len(pattern)
    if element.height_mm > 0:
        bars = max(1, dots(element.height_mm, dpi))
    else:
        bars = round(module * flow.BAR_HEIGHT_MM / flow.BAR_MODULE_MM)
    height = round(bars * flow.BAR_TOTAL_MM / flow.BAR_HEIGHT_MM)
    image = flow._barcode(element, symbol, spec, height, designed * module)
    left = quiet[0] * module
    top = round(flow.BAR_TOP_MM * height / flow.BAR_TOTAL_MM)
    return image, (left, top, left + len(pattern) * module, top + bars)


TURNS = {
    90: Image.Transpose.ROTATE_270,  # clockwise; Pillow turns the other way
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def turned_box(box: Box, size: tuple[int, int], rotation: int) -> Box:
    """Where a box of an image of that size lies once the image is turned
    clockwise by rotation degrees."""
    left, top, right, bottom = box
    width, height = size
    if rotation == 90:
        return (height - bottom, left, height - top, right)
    if rotation == 180:
        return (width - right, height - bottom, width - left, height - top)
    if rotation == 270:
        return (top, width - right, bottom, width - left)
    return box


def _turned(image: Image.Image, box: Box, rotation: int) -> Part:
    if rotation not in TURNS:
        return Part(image, box[:2])
    turned = image.transpose(TURNS[rotation])
    for name in ("code_module", "code_problems"):
        setattr(turned, name, getattr(image, name))
    return Part(turned, turned_box(box, image.size, rotation)[:2])


def element_part(
    element: Element,
    dpi: float,
    inks: Inks,
    room: tuple[int, int],
    steps: tuple[int, int] = (1, 1),
) -> Part:
    """The element drawn for the free layout; room: from its position to
    the label's right and bottom edge (a page fills it, a separator and a
    shape of height 0 reach down to the bottom)."""
    if isinstance(element, TextElement):
        return text_part(element, dpi, inks)
    if isinstance(element, CodeElement):
        return code_part(element, dpi, inks, steps)
    size = mm_to_px(FREE_SIZE_MM, dpi)
    if isinstance(element, ImageElement):
        height = mm_to_px(element.height_mm, dpi) if element.height_mm > 0 else size
        return Part(flow.render_image(element, height, dpi, inks.mono))
    if isinstance(element, SymbolElement):
        side = mm_to_px(element.size_mm, dpi) if element.size_mm > 0 else size
        return Part(flow.render_symbol(element, side, dpi, inks=inks))
    if isinstance(element, ShapeElement):
        return Part(flow.render_shape(element, max(1, room[1]), dpi, inks=inks))
    assert isinstance(element, PageElement)
    return Part(flow.render_page(element, max(1, room[0]), max(1, room[1]), inks.mono))


# --- the label ------------------------------------------------------------------


def render(
    label: Label,
    dpi: float,
    safe_mm: float,
    raster: tuple[int | None, int],
    min_length_mm: float,
    colours: tuple[bool, bool],
    steps: tuple[int, int] = (1, 1),
) -> Rendered:
    """render.render() of a label in the free layout; raster: tape_px and
    bleed_px, colours: mono and red (see there). The label's top edge is
    where the nominal tape's would be, the printable part centred on it."""
    mono, red = colours
    warnings: list[RenderWarning] = []
    inks = _inks(label, mono, red)
    frame = flow._frame(label, dpi, safe_mm, raster[0], raster[1], [])
    shortest = mm_to_px(min_length_mm, dpi) if min_length_mm > 0 else 1
    flow._source_budget(label)
    images, places, rooms, length = _drawn(label, frame, (dpi, inks, steps), shortest, warnings)
    # a fixed length is the user's, though rounded to whole pixel pairs
    # (as render.render())
    user_length = not label.auto_length and label.length_mm >= min_length_mm
    length += -length % steps[0]
    flow.check_label(length, frame.tape + 2 * frame.bleed, dpi)
    nominal = mm_to_px(label.tape_mm, dpi)
    top = frame.bleed + (frame.tape - nominal) // 2
    warnings += flow._code_warnings(label, images, dpi)
    warnings += _warnings(label, images, places, (length, nominal), top)
    img = flow._canvas(label, (length, frame.tape + 2 * frame.bleed), warnings)
    if mono:
        images = _mono(label, images, places, img, (dpi, inks, steps, rooms), warnings)
        img = flow._dithered(label, img, inks)
    for image, place in zip(images, places, strict=True):
        img.paste(image, place, image)
    flow._draw_border(img, label, frame, dpi, inks, warnings)
    img.info["black_and_white"] = mono and not any(
        isinstance(e, PageElement) for e in label.elements
    )
    length_mm = label.length_mm if user_length else px_to_mm(length, dpi)
    return Rendered(img, length_mm, warnings, False)


def _drawn(label: Label, frame, how, shortest: int, warnings):
    """Each element drawn and where it goes: images, places (their top left
    corners), rooms (see element_part()) and the label's length."""
    dpi, _inks, steps = how
    nominal = mm_to_px(label.tape_mm, dpi)
    top = frame.bleed + (frame.tape - nominal) // 2
    # endless tape: as long as the content, found once it is drawn
    length = max(shortest, mm_to_px(label.length_mm, dpi))
    origins = [(dots(e.x_mm, dpi), top + dots(e.y_mm, dpi)) for e in label.elements]
    rooms = [(length - x, top + nominal - y) for x, y in origins]
    parts, used = [], 0
    for index, (element, room) in enumerate(zip(label.elements, rooms, strict=True), 1):
        part = _part(element, index, room, how, warnings)
        used += part.image.width * part.image.height
        flow._check_budget(used, "elements")  # within limits: as they are drawn
        parts.append(part)
    places = [_place(part, origin, steps) for part, origin in zip(parts, origins, strict=True)]
    images = [part.image for part in parts]
    if label.auto_length:
        right = max((x + i.width for i, (x, _y) in zip(images, places, strict=True)), default=0)
        length = max(shortest, right + frame.margin + frame.frame)  # as in the flow
    return images, places, rooms, length


def _inks(label: Label, mono: bool, red: bool) -> Inks:
    """The first choice of inks, as render.render() makes it."""
    inks = Inks(mono, mono and red, flow.background_brightness(label) if mono else 255.0)
    if inks.red:
        red_bg = imagefx.red_mask(Image.new("RGB", (1, 1), flow._color(label.background)))
        inks = replace(inks, behind_red=bool(red_bg.getpixel((0, 0))))
    return inks


def _part(element: Element, index: int, room, how, warnings) -> Part:
    """The element (number index) drawn, or a placeholder and a warning if
    it cannot be."""
    dpi, inks, steps = how
    try:
        return element_part(element, dpi, inks, room, steps)
    except flow.RenderTooLarge:
        raise
    except Exception as e:  # noqa: BLE001 - show any element error, keep going
        warnings.append(flow._element_warning(element, index, e))
        return Part(flow._placeholder(mm_to_px(FREE_SIZE_MM, dpi)))


def _place(part: Part, origin: tuple[int, int], steps: tuple[int, int]) -> tuple[int, int]:
    """The image's top left corner: its anchor at the origin; a code's
    first module and row on multiples of the steps (render.render())."""
    x, y = origin[0] - part.anchor[0], origin[1] - part.anchor[1]
    if hasattr(part.image, "code_module"):
        x -= x % steps[0]
        y -= y % steps[1]
    return x, y


def _warnings(label: Label, images, places, size: tuple[int, int], top: int):
    """A shape that draws nothing; an element whose ink reaches beyond the
    label (size: its length and height), where it is cut off."""
    found = []
    length, height = size
    for index, (element, image, (x, y)) in enumerate(
        zip(label.elements, images, places, strict=True), 1
    ):
        if flow._invisible_shape(element):
            found.append(RenderWarning("shape_invisible", {"n": index}))
            continue
        box = ink_box(image)
        if box and (
            x + box[0] < 0 or x + box[2] > length or y + box[1] < top or y + box[3] > top + height
        ):
            found.append(RenderWarning("outside_label", {"n": index}))
    return found


def _mono(label: Label, images, places, canvas, how, warnings) -> list[Image.Image]:
    """Black and white, as render._mono_parts(): each element's inks against
    the background under it (drawn again where they differ), converted on
    its own; a warning for an element that does not show."""
    dpi, inks, steps, rooms = how
    plain = not (label.background_image and label.background_image.data)
    done = []
    for index, (element, image, (x, y), room) in enumerate(
        zip(label.elements, images, places, rooms, strict=True), 1
    ):
        local, lost = inks, 0.0
        if not plain and not isinstance(element, PageElement):
            area = canvas.crop((x, y, x + image.width, y + image.height))
            mask = image.getchannel("A").point(imagefx.HALF)
            local = flow._local_inks(area, mask, inks)
            if flow._ink_choice(element, local) != flow._ink_choice(element, inks):
                image = _again(element, image, (dpi, local, steps), room)
            lost = flow._lost(
                area, mask, flow._main_ink(element, local), flow._contrast(element), local.red
            )
        image = flow._mono_part(element, image, local, dpi)
        too_much = flow.CODE_LOST_SHARE if isinstance(element, CodeElement) else flow.LOST_SHARE
        if flow._shows_ink(element) and (
            lost > too_much or not flow._ink_shows(element, image, local)
        ):
            warnings.append(RenderWarning("no_contrast", {"n": index}))
        done.append(image)
    return done


def _again(element: Element, image: Image.Image, how, room) -> Image.Image:
    """The element drawn again in other inks; the same size, so the same place."""
    dpi, inks, steps = how
    try:
        drawn = element_part(element, dpi, inks, room, steps).image
    except flow.RenderTooLarge:
        raise
    except Exception:  # noqa: BLE001 - it was drawn before: keep that
        return image
    return drawn if drawn.size == image.size else image


# --- from the flowing layout ----------------------------------------------------------


def adopt(
    label: Label,
    dpi: float,
    safe_mm: float = flow.DEFAULT_SAFE_MM,
    mono: bool = False,
    red: bool = False,
) -> None:
    """Give each element of a label that was laid out one after another the
    position and size it had there, so switching to the free layout moves
    nothing: a text gets the size it was drawn at, a code its module (and a
    linear code its bars' height), an image, a symbol and a shape the size
    they were drawn at. Only the elements matter, not the label's layout.
    mono, red: as the label is rendered (render()), so a text reduced to
    fit a label of fixed length keeps the ink it had in the preview."""
    flowing = replace(label, layout="flow")
    warnings: list[RenderWarning] = []
    frame = flow._frame(flowing, dpi, safe_mm, None, 0, warnings)
    inks = _inks(flowing, mono, red)
    parts = flow._parts(flowing, frame, dpi, warnings, flow._room(flowing, frame, dpi, 0), inks)
    # the size each text got before it was reduced to fit (a text drawn
    # again in black and white no longer tells); None: not a drawn text
    sizes = [_size_px(part) for part in parts]
    length, parts, gaps, _ = flow._fit_length(flowing, parts, frame, dpi, warnings, 0, inks)
    # a text of a label of fixed length that fits is drawn only now (_LaterText)
    parts = [flow._drawn(part) for part in parts]
    places = flow._positions(length, flowing, parts, gaps, frame)
    for element, part, place, size in zip(label.elements, parts, places, sizes, strict=True):
        _adopt(element, part, place, (dpi, inks, size))


def _size_px(part) -> float | None:
    if isinstance(part, Image.Image):
        return part.info.get("size_px")
    return part.size_px  # a _LaterText


def _drawn_size(element: TextElement, part: Image.Image, dpi: float, size: float) -> float:
    """The size (pixels) a text was drawn at, also when it was reduced to
    fit a label of fixed length afterwards (its height tells by how much)."""
    wrap = mm_to_px(element.max_width_mm, dpi) if element.max_width_mm > 0 else None
    height = typeset.size_of(element, size, dpi, antialias=True, wrap_px=wrap)[1]
    return size * part.height / max(1, height)


MATCH_TRIES = 4  # layouts of a text to find the size its ink had


def _matched_size(element: TextElement, part: Image.Image, how) -> float:
    """The size (points, to a hundredth) at which the free layout draws the
    text with the ink it had: its drawn size first, then corrected by how
    much its ink's long side differs (a text reduced to fit a label of
    fixed length was resampled, or drawn a pixel larger or smaller)."""
    dpi, inks, size = how
    pt = round(_drawn_size(element, part, dpi, size) / dpi * 72, 2)
    target = ink_box(part)
    if not target:
        return pt
    axis = 0 if target[2] - target[0] >= target[3] - target[1] else 1
    want = target[axis + 2] - target[axis]
    best = (math.inf, pt)
    for _ in range(MATCH_TRIES):
        got = ink_box(text_part(replace(element, size_pt=pt), dpi, inks).image)
        if not got:  # nothing drawn at that size: keep the best so far
            break
        have = got[axis + 2] - got[axis]
        best = min(best, (abs(have - want), pt))
        if have == want:
            break
        pt = round(pt * want / have, 2)
    return best[1]


def _adopt_size(element: Element, part: Image.Image, how) -> None:
    """The size the element was drawn at, as its size in the free layout."""
    dpi = how[0]

    def mm(px: float) -> float:
        return round(px_to_mm(px, dpi), 2)  # type: ignore[arg-type]

    if isinstance(element, TextElement) and how[2]:
        element.size_pt = _matched_size(element, part, how)
    elif isinstance(element, CodeElement) and hasattr(part, "code_module"):
        element.module_mm = mm(part.code_module)
        if codes.SPECS[element.symbology].layout not in _MATRIX:
            element.height_mm = mm(part.height * flow.BAR_HEIGHT_MM / flow.BAR_TOTAL_MM)
    elif isinstance(element, ImageElement):
        element.height_mm = mm(part.height)
    elif isinstance(element, SymbolElement):
        element.size_mm = mm(part.height)
    elif isinstance(element, ShapeElement):
        element.width_mm, element.height_mm = mm(part.width), mm(part.height)


def _adopt(element: Element, part: Image.Image, place: tuple[int, int], how) -> None:
    dpi = how[0]

    def mm(px: float) -> float:
        return round(px_to_mm(px, dpi), 2)  # type: ignore[arg-type]

    x, y = place
    if isinstance(element, (TextElement, CodeElement)):
        box = ink_box(part) or (0, 0, 0, 0)
        x, y = x + box[0], y + box[1]
    _adopt_size(element, part, how)
    element.x_mm, element.y_mm = mm(max(0, x)), mm(max(0, y))
