"""Render geometry with exact pixel numbers.

Reference values at 317 dpi: 25 mm tape = 312 px, 1.5 mm safety margin = 19 px,
2 mm spacing = 25 px, 0.5 mm border = 6 px, 15 mm minimum length = 188 px.
Solid-colour image elements keep the geometry independent of installed fonts.
"""

import base64
import io

import pytest
from conftest import ink_bbox
from PIL import Image, ImageChops

from etiketto import render as render_module
from etiketto.model import CodeElement, ImageElement, Label, TextElement
from etiketto.render import (
    RenderWarning,
    mm_to_px,
    px_to_mm,
    render,
)

WHITE = (255, 255, 255)
TAPE = 312  # 25 mm
SAFE = 19  # 1.5 mm
MARGIN = 25  # 2 mm
FRAME = SAFE
INNER = TAPE - 2 * FRAME  # 274
PART_W = 2 * INNER  # a 40x20 image scaled to the content height


def pixels(img):
    """All pixel values (getdata is deprecated in newer Pillow)."""
    return list(getattr(img, "get_flattened_data", img.getdata)())


@pytest.fixture
def wide_image(png_b64):
    return ImageElement(data=png_b64((40, 20), "red"), name="red.png")


# --- units -------------------------------------------------------------------


def test_fixed_conversions():
    assert mm_to_px(25) == 312
    assert mm_to_px(9) == 112
    assert mm_to_px(1.5) == 19
    assert mm_to_px(25.4) == 317
    assert mm_to_px(25.4, dpi=264) == 264
    assert mm_to_px(0) == 1  # never zero pixels
    assert px_to_mm(317) == pytest.approx(25.4)
    assert px_to_mm(264, dpi=264) == pytest.approx(25.4)


@pytest.mark.parametrize("tape, px", [(9, 112), (12, 150), (19, 237), (25, 312), (50, 624)])
def test_height_matches_tape(tape, px):
    assert render(Label(tape_mm=tape)).image.size[1] == px


def test_empty_label_has_minimum_length():
    rendered = render(Label(tape_mm=25), min_length_mm=15)
    assert rendered.image.size == (188, TAPE)
    assert rendered.length_mm == pytest.approx(px_to_mm(188))
    assert rendered.length_mm >= 15
    assert rendered.warnings == [] and not rendered.overflow


# --- placement ---------------------------------------------------------------


def test_auto_length_layout(make_label, wide_image):
    rendered = render(make_label(elements=[wide_image]))
    img = rendered.image
    assert img.size == (PART_W + 2 * (MARGIN + FRAME), TAPE)
    assert ink_bbox(img, WHITE) == (FRAME + MARGIN, FRAME, FRAME + MARGIN + PART_W, TAPE - FRAME)
    assert img.getpixel((FRAME + MARGIN + 10, TAPE // 2)) == (255, 0, 0)
    assert rendered.length_mm == pytest.approx(px_to_mm(img.width))


def test_auto_length_ignores_align(make_label, wide_image):
    start = render(make_label(elements=[wide_image], align="start")).image
    end = render(make_label(elements=[wide_image], align="end")).image
    assert ImageChops.difference(start, end).getbbox() is None


def test_gap_between_elements(make_label, wide_image, png_b64):
    blue = ImageElement(data=png_b64((20, 20), "blue"))
    img = render(make_label(elements=[wide_image, blue])).image
    x0 = FRAME + MARGIN
    assert img.width == PART_W + MARGIN + INNER + 2 * (MARGIN + FRAME)
    assert img.getpixel((x0 + PART_W - 1, TAPE // 2)) == (255, 0, 0)
    for x in range(x0 + PART_W, x0 + PART_W + MARGIN):
        assert img.getpixel((x, TAPE // 2)) == WHITE
    assert img.getpixel((x0 + PART_W + MARGIN, TAPE // 2)) == (0, 0, 255)
    assert ink_bbox(img, WHITE)[2] == img.width - FRAME - MARGIN


LENGTH_100 = 1248  # 100 mm


@pytest.mark.parametrize(
    "align, x0",
    [
        ("start", FRAME + MARGIN),
        ("center", FRAME + (LENGTH_100 - 2 * FRAME - PART_W) // 2),
        ("end", LENGTH_100 - FRAME - MARGIN - PART_W),
    ],
)
def test_fixed_length_alignment(make_label, wide_image, align, x0):
    rendered = render(
        make_label(auto_length=False, length_mm=100, align=align, elements=[wide_image])
    )
    assert rendered.image.size == (LENGTH_100, TAPE)
    bbox = ink_bbox(rendered.image, WHITE)
    assert bbox == (x0, FRAME, x0 + PART_W, TAPE - FRAME)
    assert bbox[0] >= FRAME + MARGIN and bbox[2] <= LENGTH_100 - FRAME - MARGIN
    assert not rendered.overflow and rendered.warnings == []


def test_fixed_length_below_minimum_is_extended(make_label):
    assert render(make_label(auto_length=False, length_mm=5), min_length_mm=15).image.width == 188
    assert render(make_label(auto_length=False, length_mm=5)).image.width == 62  # no minimum


def test_overflow_scales_content(make_label, wide_image):
    length = mm_to_px(30)  # 374 px, room 286 px for 548 px of content
    rendered = render(make_label(auto_length=False, length_mm=30, elements=[wide_image]))
    assert rendered.overflow
    assert rendered.image.width == length
    x0, y0, x1, y1 = ink_bbox(rendered.image, WHITE)
    room = length - 2 * (MARGIN + FRAME)
    assert x1 - x0 == room
    assert x0 >= FRAME + MARGIN and x1 <= length - FRAME - MARGIN
    height = round(INNER * room / PART_W)  # aspect kept
    assert y1 - y0 == height
    assert y0 == (TAPE - height) // 2  # still centred across the tape


def test_overflow_with_several_elements_keeps_scaled_gap(make_label, wide_image):
    rendered = render(
        make_label(auto_length=False, length_mm=40, margin_mm=4, elements=[wide_image, wide_image])
    )
    length, margin = mm_to_px(40), mm_to_px(4)
    x0, _y0, x1, _y1 = ink_bbox(rendered.image, WHITE)
    assert rendered.overflow
    # scaled widths are rounded: up to 1 px of the spacing may be used
    assert x0 >= FRAME + margin - 1 and x1 <= length - FRAME - margin + 1
    assert x0 >= FRAME and x1 <= length - FRAME  # never into the safety margin
    scale = (length - 2 * (margin + FRAME)) / (2 * PART_W + margin)
    gap_start = x0 + round(PART_W * scale)
    assert rendered.image.getpixel((gap_start + 2, TAPE // 2)) == WHITE
    assert rendered.image.getpixel((gap_start - 2, TAPE // 2)) == (255, 0, 0)


def test_fixed_length_without_room_warns(make_label, wide_image):
    rendered = render(
        make_label(auto_length=False, length_mm=15, margin_mm=20, elements=[wide_image])
    )
    assert rendered.warnings == [RenderWarning("fixed_too_short")]
    assert not rendered.overflow


def test_fixed_length_without_room_but_empty_is_fine(make_label):
    assert render(make_label(auto_length=False, length_mm=15, margin_mm=20)).warnings == []


def test_edge_spacing(make_label, png_b64):
    square = ImageElement(data=png_b64((10, 10), "red"))
    edge = mm_to_px(1)  # 13 px
    img = render(make_label(edge_mm=1, margin_mm=0, elements=[square])).image
    inner = TAPE - 2 * (edge + FRAME)
    x0, y0, x1, y1 = ink_bbox(img, WHITE)
    assert (y0, y1) == (edge + FRAME, TAPE - edge - FRAME)
    assert x1 - x0 == inner


def test_safe_zero_uses_the_whole_tape(make_label, wide_image):
    img = render(make_label(margin_mm=0, elements=[wide_image]), safe_mm=0).image
    assert img.size == (2 * TAPE, TAPE)
    assert ink_bbox(img, WHITE) == (0, 0, 2 * TAPE, TAPE)


def test_too_little_room_warns(make_label, wide_image):
    rendered = render(Label(tape_mm=9, edge_mm=1, elements=[wide_image]), safe_mm=3)
    assert rendered.warnings == [RenderWarning("no_room")]
    assert rendered.image.height == 112
    # extreme: the edge spacing alone is wider than the tape
    rendered = render(Label(tape_mm=9, edge_mm=10, elements=[wide_image]), safe_mm=3)
    assert rendered.warnings == [RenderWarning("no_room")]
    assert rendered.image.height == 112


# --- border and background ---------------------------------------------------


@pytest.mark.parametrize("safe_mm, inset", [(1.5, 19), (0, 0)])
def test_border_exact_position_on_all_sides(make_label, safe_mm, inset):
    border = mm_to_px(0.5)  # 6 px
    img = render(
        make_label(border_mm=0.5, border_color="#ff0000", background="#00ff00"), safe_mm=safe_mm
    ).image
    w, h = img.size
    red, green = (255, 0, 0), (0, 255, 0)
    mid_x, mid_y = w // 2, h // 2

    def ring(k):  # the four pixels at distance k from the edges, on the middle lines
        return {
            img.getpixel((mid_x, k)),
            img.getpixel((mid_x, h - 1 - k)),
            img.getpixel((k, mid_y)),
            img.getpixel((w - 1 - k, mid_y)),
        }

    if inset:
        assert ring(inset - 1) == {green}
        assert ring(0) == {green}
    assert ring(inset) == {red}
    assert ring(inset + border - 1) == {red}
    assert ring(inset + border) == {green}


def test_border_shifts_content(make_label, wide_image):
    border = mm_to_px(0.5)
    frame = SAFE + border
    # border in the background colour: only the content differs from white
    img = render(make_label(border_mm=0.5, border_color="#ffffff", elements=[wide_image])).image
    assert img.size[0] == 2 * (TAPE - 2 * frame) + 2 * (MARGIN + frame)
    assert ink_bbox(img, WHITE) == (
        frame + MARGIN,
        frame,
        img.width - frame - MARGIN,
        TAPE - frame,
    )


def test_background_is_edge_to_edge(make_label, wide_image):
    img = render(make_label(background="#0000ff", margin_mm=0, elements=[wide_image])).image
    for x, y in ((0, 0), (img.width - 1, img.height - 1), (FRAME - 1, FRAME - 1)):
        assert img.getpixel((x, y)) == (0, 0, 255)


def test_background_image_covers_label(make_label, png_b64):
    rendered = render(make_label(background_image=ImageElement(data=png_b64((5, 50), "blue"))))
    img = rendered.image
    assert rendered.warnings == []
    for xy in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
        assert img.getpixel(xy) == (0, 0, 255)


def test_broken_background_image_warns(make_label):
    rendered = render(make_label(background="#00ff00", background_image=ImageElement(data="Zm9v")))
    assert rendered.warnings == [RenderWarning("background_unreadable")]
    assert rendered.image.getpixel((0, 0)) == (0, 255, 0)
    # an image element without data is ignored
    assert render(make_label(background_image=ImageElement())).warnings == []


def test_invalid_colours_fall_back():
    assert render_module._color("not a colour") == (0, 0, 0)
    assert render_module._color("nope", "#ffffff") == (255, 255, 255)
    assert render_module._color("#11223344") == (0x11, 0x22, 0x33)
    img = render(Label(tape_mm=25, background="bogus", border_mm=1, border_color="bogus")).image
    assert img.getpixel((0, 0)) == WHITE
    assert img.getpixel((img.width // 2, SAFE)) == (0, 0, 0)


# --- print orientation ---------------------------------------------------------


def test_text_fits_tape_height():
    block = render_module.render_text(TextElement(text="Hgjy"), INNER, 317)
    assert 0.8 * INNER < block.height <= INNER
    assert block.mode == "RGBA"


def test_text_fill_in_label(make_label):
    img = render(make_label(elements=[TextElement(text="Hgjy")])).image
    x0, y0, x1, y1 = ink_bbox(img, WHITE)
    assert y0 >= FRAME and y1 <= TAPE - FRAME
    assert x0 >= FRAME + MARGIN and x1 <= img.width - FRAME - MARGIN
    assert y1 - y0 > 0.5 * INNER  # the ink really fills the tape


def test_huge_fixed_size_is_scaled_to_tape():
    inner = mm_to_px(9) - 2 * SAFE
    element = TextElement(text="Hg", size_pt=300)
    assert render_module.render_text(element, inner, 317).height > inner  # would not fit unscaled
    img = render(Label(tape_mm=9, elements=[element])).image
    x0, y0, x1, y1 = ink_bbox(img, WHITE)
    assert y0 >= SAFE and y1 <= img.height - SAFE


def test_fixed_size_scales_with_points():
    small = render_module.render_text(TextElement(text="Hg", size_pt=12), 1000, 317)
    big = render_module.render_text(TextElement(text="Hg", size_pt=24), 1000, 317)
    assert big.height == pytest.approx(2 * small.height, rel=0.15)
    assert 12 / 72 * 317 <= small.height <= 3 * 12 / 72 * 317
    long = render(Label(elements=[TextElement(text="Hi", size_pt=12)]))
    fill = render(Label(elements=[TextElement(text="Hi")]))
    assert long.length_mm < fill.length_mm


def test_text_colour_and_alignment():
    def lower_line(align):
        block = render_module.render_text(
            TextElement(text="WWWWWWWW\nI", align=align, color="#ff0000"), 200, 317
        )
        lower = block.crop((0, block.height * 3 // 4, block.width, block.height))
        return block, lower.getchannel("A").getbbox()

    block, left = lower_line("left")
    r, g, b, a = max(pixels(block), key=lambda p: p[3])
    assert a == 255 and r > 200 and g < 60 and b < 60
    _block, right = lower_line("right")
    _block, centre = lower_line("center")
    w = block.width
    assert left[2] < w / 3
    assert right[0] > 2 * w / 3
    assert w / 3 < (centre[0] + centre[2]) / 2 < 2 * w / 3


def test_bold_and_italic_change_the_glyphs():
    plain = render_module.render_text(TextElement(text="Etikett", bold=False), 100, 317)
    bold = render_module.render_text(TextElement(text="Etikett", bold=True), 100, 317)
    italic = render_module.render_text(
        TextElement(text="Etikett", bold=False, italic=True), 100, 317
    )
    assert plain.tobytes() != bold.tobytes()
    assert plain.tobytes() != italic.tobytes()


def test_auto_length_grows_with_text():
    short = render(Label(elements=[TextElement(text="A")]))
    long = render(Label(elements=[TextElement(text="A much longer label text")]))
    assert long.length_mm > short.length_mm


def test_trailing_newline_does_not_shrink_text():
    plain = render(Label(elements=[TextElement(text="Hello world")]))
    newline = render(Label(elements=[TextElement(text="Hello world\n")]))
    assert abs(plain.length_mm - newline.length_mm) < 1


def test_empty_text_renders():
    assert render_module.render_text(TextElement(text=""), 100, 317).height > 0


# --- images --------------------------------------------------------------------


def test_image_keeps_aspect(png_b64):
    assert render_module.render_image(ImageElement(data=png_b64((40, 20))), 100).size == (200, 100)
    assert render_module.render_image(ImageElement(data=png_b64((10, 40))), 100).size == (25, 100)


def test_image_cache_returns_copies(png_b64):
    element = ImageElement(data=png_b64((40, 20), "red"))
    first = render_module.render_image(element, 50)
    first.paste((0, 0, 255, 255), (0, 0, 10, 10))
    assert render_module.render_image(element, 50).getpixel((0, 0)) == (255, 0, 0, 255)


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (40, 20), "red")
    img.paste((0, 0, 255), (0, 0, 10, 10))  # blue marker top left
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated: display needs 90° clockwise
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif, quality=100)
    part = render_module.render_image(
        ImageElement(data=base64.b64encode(buf.getvalue()).decode()), 80
    )
    assert part.size == (40, 80)
    r, _g, b, _a = part.getpixel((35, 5))  # marker now top right
    assert b > 200 and r < 60


def test_garbage_image_gives_warning_and_placeholder(make_label):
    rendered = render(make_label(margin_mm=0, elements=[ImageElement(data="!!not base64!!")]))
    assert rendered.warnings == [RenderWarning("image_unreadable", {"n": 1})]
    img = rendered.image
    assert img.width == INNER + 2 * FRAME
    assert img.getpixel((FRAME + INNER // 2, FRAME + 5)) == (255, 230, 230)
    assert img.getpixel((FRAME + INNER // 2, FRAME + INNER // 2)) == (200, 0, 0)


def test_element_errors_are_numbered(make_label, wide_image):
    rendered = render(make_label(elements=[wide_image, CodeElement(data="")]))
    assert rendered.warnings == [RenderWarning("code_empty", {"n": 2})]


# --- codes ---------------------------------------------------------------------


def test_qr_is_square_coloured_and_transparent():
    part = render_module.render_code(CodeElement(data="https://example.org", color="#0000ff"), 120)
    assert part.size == (120, 120)
    colours = set(pixels(part))
    assert (0, 0, 255, 255) in colours
    assert any(p[3] == 0 for p in colours)


def test_qr_on_label_shows_background(make_label):
    img = render(make_label(background="#00ff00", elements=[CodeElement(data="x" * 20)])).image
    x0, y0, x1, y1 = ink_bbox(img, (0, 255, 0))
    # 25 modules of 10 px (274 / 25 = 10.96): centred in the element's space
    assert (x1 - x0, y1 - y0) == (250, 250)
    assert y0 == FRAME + (INNER - 250) // 2
    colours = set(pixels(img.crop((x0, y0, x1, y1))))
    assert colours == {(0, 0, 0), (0, 255, 0)}


@pytest.mark.parametrize("symbology, data", [("code128", "ABC-123"), ("ean13", "4006381333931")])
def test_barcodes_transparent_background_and_colour(symbology, data):
    part = render_module.render_code(
        CodeElement(data=data, symbology=symbology, color="#ff0000"), 100
    )
    assert part.height == 100 and part.width > 100
    alphas = {p[3] for p in pixels(part)}
    assert alphas == {0, 255}
    opaque = [p for p in pixels(part) if p[3] == 255]
    assert all(p[0] > 150 and p[1] < 100 and p[2] < 100 for p in opaque[:50])
    assert not [p for p in pixels(part) if p[:3] == (255, 255, 255) and p[3] == 255]


@pytest.mark.parametrize(
    "symbology, data, key",
    [
        ("qr", "", "code_empty"),
        ("code128", "", "code_empty"),
        ("ean13", "", "code_empty"),
        ("ean13", "abc", "ean13_digits"),
        ("ean13", "40063813339", "ean13_digits"),  # 11 digits
        ("ean13", "40063813339310", "ean13_digits"),  # 14 digits
        ("ean13", "４００６３８１３３３９３", "ean13_digits"),  # full-width digits
        ("ean13", "4006381333932", "ean13_check"),  # library would "correct" it
        ("code128", "Grüße", "code128_charset"),
        ("code128", "tab\there", "code128_charset"),  # would be dropped silently
        ("code128", "日本", "code128_charset"),
    ],
)
def test_invalid_codes_warn(make_label, symbology, data, key):
    rendered = render(make_label(elements=[CodeElement(data=data, symbology=symbology)]))
    assert rendered.warnings == [RenderWarning(key, {"n": 1})]


@pytest.mark.parametrize(
    "symbology, data",
    [("ean13", "400638133393"), ("ean13", "4006381333931"), ("code128", " ~Az09!")],
)
def test_check_code_accepts(symbology, data):
    render_module.check_code(CodeElement(data=data, symbology=symbology))  # no exception


def _runs(row):
    """Lengths of the runs of equal values in a row of pixels."""
    runs, last = [], None
    for value in row:
        if value == last:
            runs[-1] += 1
        else:
            runs.append(1)
            last = value
    return runs


def _pattern(symbology, data):
    import barcode

    pattern = "".join(barcode.get_barcode_class(symbology)(data).build())
    return [len(run) for run in pattern.replace("01", "0 1").replace("10", "1 0").split()]


def _on_white(part):
    page = Image.new("L", part.size, 255)
    page.paste(part.convert("L"), (0, 0), part)
    return page


@pytest.mark.parametrize("dpi", [203, 300, 600])
@pytest.mark.parametrize(
    "symbology, data", [("code128", "1Z999AA10123456784"), ("ean13", "4006381333931")]
)
def test_barcode_modules_are_whole_pixels(symbology, data, dpi):
    height = mm_to_px(12, dpi)
    page = _on_white(render_module.render_code(CodeElement(data, symbology=symbology), height))
    assert set(pixels(page)) == {0, 255}  # 1 bit: nothing a printer has to round
    y = round(height * 6 / render_module.BAR_TOTAL_MM)  # through the bars
    row = list(pixels(page.crop((0, y, page.width, y + 1))))
    runs = _runs(row)[1:-1]  # without the quiet zones
    module = min(runs)
    assert [run / module for run in runs] == _pattern(symbology, data)
    modules = sum(runs) // module
    designed = page.width * 0.3 / (2 + modules * 0.3)  # pixels
    # the nearest whole module, unless the bars would go beyond the element
    nearest = max(1, round(designed))
    assert module == (nearest if nearest * modules <= page.width else page.width // modules)
    assert sum(runs) <= page.width


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_qr_modules_are_whole_square_pixels(dpi):
    height = mm_to_px(12, dpi)
    part = render_module.render_code(CodeElement("https://example.org/etiketto"), height)
    assert part.size == (height, height)  # the element keeps its size
    page = _on_white(part)
    assert set(pixels(page)) == {0, 255}
    x0, y0, x1, y1 = ink_bbox(page, 255)
    count = 29  # version 3
    module = (x1 - x0) // count
    assert (x1 - x0, y1 - y0) == (count * module, count * module)
    assert module == height // count  # the widest that fits
    assert (x0, y0) == ((height - count * module) // 2,) * 2  # centred
    # finder pattern: 7 modules dark-light-dark-light-dark 1:1:3:1:1
    row = list(pixels(page.crop((x0, y0 + 3 * module, x0 + 7 * module, y0 + 3 * module + 1))))
    assert _runs(row) == [module, module, 3 * module, module, module]


@pytest.mark.parametrize(
    "designed, modules, box, module",
    [
        (2.6, 10, 30, 3),  # the nearest
        (2.4, 10, 30, 2),
        (2.6, 10, 29, 2),  # 3 would go beyond the box: rounded down
        (0.4, 10, 30, 1),  # never less than one pixel
        (0.4, 10, 5, 1),
    ],
)
def test_module_width(designed, modules, box, module):
    assert render_module._module(designed, modules, box) == module


def test_a_code_with_one_dot_modules_warns(make_label):
    long = CodeElement("1Z999AA10123456784" * 2, symbology="code128")
    rendered = render(make_label(tape_mm=12, elements=[TextElement("x"), long]), dpi=203)
    assert RenderWarning("code_thin", {"n": 2}) in rendered.warnings
    qr = render(make_label(elements=[CodeElement("https://example.org")]), dpi=203)
    assert qr.warnings == []  # 4 dots a module


def test_a_barcode_without_its_text_still_has_its_bars(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no font")

    monkeypatch.setattr(render_module, "_text_block", broken)
    part = render_module.render_code(CodeElement("4006381333931", symbology="ean13"), 163)
    _x0, _y0, _x1, y1 = ink_bbox(_on_white(part), 255)
    assert y1 == round(163 * 11 / render_module.BAR_TOTAL_MM)  # the bars end there


def test_a_code_scaled_to_fit_keeps_whole_modules(make_label):
    code = CodeElement("1Z999AA10123456784", symbology="code128")
    label = make_label(elements=[code], auto_length=False, length_mm=40)
    rendered = render(label, dpi=300)
    assert rendered.overflow
    grey = rendered.image.convert("L")
    assert set(pixels(grey)) == {0, 255}  # drawn again, not resampled
    _x0, y0, _x1, y1 = ink_bbox(grey, 255)
    row = list(pixels(grey.crop((0, y0 + 2, grey.width, y0 + 3))))
    runs = _runs(row)[1:-1]
    module = min(runs)
    assert [run / module for run in runs] == _pattern("code128", code.data)


def test_other_element_errors(make_label, monkeypatch):
    def broken(element, height):
        raise RuntimeError("segno broke")

    monkeypatch.setattr(render_module, "render_code", broken)
    rendered = render(make_label(elements=[CodeElement(data="x")]))
    assert rendered.warnings == [RenderWarning("element_failed", {"n": 1})]


def test_valid_codes_render_without_warnings(make_label):
    for code in (
        CodeElement(data="https://example.org"),
        CodeElement(data="ABC-123", symbology="code128"),
        CodeElement(data="4006381333931", symbology="ean13"),
    ):
        assert render(make_label(elements=[code])).warnings == []


def test_text_fit_shrinks_when_estimate_is_too_big(monkeypatch):
    # block height not proportional to the size: the first estimate overshoots
    sizes = []

    def fake_block(element, size_px):
        sizes.append(size_px)
        return Image.new("RGBA", (10, round(size_px + 40)))

    monkeypatch.setattr(render_module, "_text_block", fake_block)
    block = render_module.render_text(TextElement(text="x"), 100, 317)
    assert block.height <= 100
    assert sizes[1] == pytest.approx(100 * 100 / 140)  # linear estimate from the probe
    assert len(sizes) > 2 and sizes[-1] < sizes[1]
    assert block.height > 100 * 0.9  # shrinks in small steps, not more than needed


# --- exact print geometry: tape height and bleed ---------------------------------


def test_tape_px_and_bleed(make_label, png_b64):
    square = ImageElement(data=png_b64((10, 10), "red"))
    label = make_label(background="#00ff00", margin_mm=0, border_mm=0.5, elements=[square])
    rendered = render(label, dpi=320, safe_mm=1.5, tape_px=320, bleed_px=7)
    img = rendered.image
    safe, border = mm_to_px(1.5, 320), mm_to_px(0.5, 320)  # 19, 6
    frame = safe + border
    assert img.height == 320 + 2 * 7
    # background across the bleed, content and border keep their place on the tape
    assert img.getpixel((img.width // 2, 0)) == (0, 255, 0)
    assert img.getpixel((img.width // 2, img.height - 1)) == (0, 255, 0)
    assert ink_bbox(img, (0, 255, 0))[1] == 7 + safe  # border top edge
    inner = 320 - 2 * frame
    assert img.getpixel((frame + 2, 7 + frame + 2)) == (255, 0, 0)
    assert img.getpixel((frame + 2, 7 + frame - 1)) != (255, 0, 0)
    assert img.getpixel((frame + 2, 7 + frame + inner - 1)) == (255, 0, 0)
    assert img.getpixel((frame + 2, 7 + frame + inner)) != (255, 0, 0)
    # the border is drawn on the tape, not in the bleed
    assert img.getpixel((img.width // 2, 7 + safe)) == (0, 0, 0)
    assert img.getpixel((img.width // 2, 7 + 320 - 1 - safe)) == (0, 0, 0)
    assert img.getpixel((img.width // 2, 7 + safe - 1)) == (0, 255, 0)


def test_bleed_zero_is_the_default(make_label, wide_image):
    label = make_label(elements=[wide_image])
    plain = render(label).image
    assert ImageChops.difference(plain, render(label, bleed_px=0).image).getbbox() is None
    assert render(label, bleed_px=-3).image.size == plain.size
