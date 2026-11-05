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

from etiketto import render as R
from etiketto.model import CodeElement, ImageElement, Label, TextElement
from etiketto.render import MIN_LENGTH_MM, Rendered, mm_to_px, print_image, px_to_mm, render

WHITE = (255, 255, 255)
TAPE = 312  # 25 mm
SAFE = 19  # 1.5 mm
MARGIN = 25  # 2 mm
FRAME = SAFE
INNER = TAPE - 2 * FRAME  # 274
PART_W = 2 * INNER  # a 40x20 image scaled to the content height


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
    rendered = render(Label(tape_mm=25))
    assert rendered.image.size == (188, TAPE)
    assert rendered.length_mm == pytest.approx(px_to_mm(188))
    assert rendered.length_mm >= MIN_LENGTH_MM
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
    rendered = render(make_label(auto_length=False, length_mm=100, align=align, elements=[wide_image]))
    assert rendered.image.size == (LENGTH_100, TAPE)
    bbox = ink_bbox(rendered.image, WHITE)
    assert bbox == (x0, FRAME, x0 + PART_W, TAPE - FRAME)
    assert FRAME + MARGIN <= bbox[0] and bbox[2] <= LENGTH_100 - FRAME - MARGIN
    assert not rendered.overflow and rendered.warnings == []


def test_fixed_length_below_minimum_is_extended(make_label):
    assert render(make_label(auto_length=False, length_mm=5)).image.width == 188


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
    rendered = render(make_label(auto_length=False, length_mm=15, margin_mm=20, elements=[wide_image]))
    assert "the fixed length leaves no room for the content" in rendered.warnings
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
    assert rendered.warnings == ["safety margin, border and edge spacing leave almost no room"]
    assert rendered.image.height == 112
    # extreme: the edge spacing alone is wider than the tape
    rendered = render(Label(tape_mm=9, edge_mm=10, elements=[wide_image]), safe_mm=3)
    assert rendered.warnings[0].startswith("safety margin")
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
    assert ink_bbox(img, WHITE) == (frame + MARGIN, frame, img.width - frame - MARGIN, TAPE - frame)


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
    assert len(rendered.warnings) == 1
    assert rendered.warnings[0].startswith("background image: ")
    assert rendered.image.getpixel((0, 0)) == (0, 255, 0)
    # an image element without data is ignored
    assert render(make_label(background_image=ImageElement())).warnings == []


def test_invalid_colours_fall_back():
    assert R._color("not a colour") == (0, 0, 0)
    assert R._color("nope", "#ffffff") == (255, 255, 255)
    assert R._color("#11223344") == (0x11, 0x22, 0x33)
    img = render(Label(tape_mm=25, background="bogus", border_mm=1, border_color="bogus")).image
    assert img.getpixel((0, 0)) == WHITE
    assert img.getpixel((img.width // 2, SAFE)) == (0, 0, 0)


# --- print orientation ---------------------------------------------------------


def test_print_image_rotates_clockwise():
    img = Image.new("RGB", (30, 10), WHITE)
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((29, 0), (0, 0, 255))
    out = print_image(Rendered(img, px_to_mm(30)))
    assert out.size == (10, 30)
    assert out.getpixel((9, 0)) == (255, 0, 0)  # top left -> top right
    assert out.getpixel((9, 29)) == (0, 0, 255)  # top right -> bottom right


# --- text --------------------------------------------------------------------


def test_text_fits_tape_height():
    block = R.render_text(TextElement(text="Hgjy"), INNER, 317)
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
    assert R.render_text(element, inner, 317).height > inner  # would not fit unscaled
    img = render(Label(tape_mm=9, elements=[element])).image
    x0, y0, x1, y1 = ink_bbox(img, WHITE)
    assert y0 >= SAFE and y1 <= img.height - SAFE


def test_fixed_size_scales_with_points():
    small = R.render_text(TextElement(text="Hg", size_pt=12), 1000, 317)
    big = R.render_text(TextElement(text="Hg", size_pt=24), 1000, 317)
    assert big.height == pytest.approx(2 * small.height, rel=0.15)
    assert 12 / 72 * 317 <= small.height <= 3 * 12 / 72 * 317
    long = render(Label(elements=[TextElement(text="Hi", size_pt=12)]))
    fill = render(Label(elements=[TextElement(text="Hi")]))
    assert long.length_mm < fill.length_mm


def test_text_colour_and_alignment():
    def lower_line(align):
        block = R.render_text(
            TextElement(text="WWWWWWWW\nI", align=align, color="#ff0000"), 200, 317
        )
        lower = block.crop((0, block.height * 3 // 4, block.width, block.height))
        return block, lower.getchannel("A").getbbox()

    block, left = lower_line("left")
    r, g, b, a = max(block.getdata(), key=lambda p: p[3])
    assert a == 255 and r > 200 and g < 60 and b < 60
    _block, right = lower_line("right")
    _block, centre = lower_line("center")
    w = block.width
    assert left[2] < w / 3
    assert right[0] > 2 * w / 3
    assert w / 3 < (centre[0] + centre[2]) / 2 < 2 * w / 3


def test_bold_and_italic_change_the_glyphs():
    plain = R.render_text(TextElement(text="Etikett", bold=False), 100, 317)
    bold = R.render_text(TextElement(text="Etikett", bold=True), 100, 317)
    italic = R.render_text(TextElement(text="Etikett", bold=False, italic=True), 100, 317)
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
    assert R.render_text(TextElement(text=""), 100, 317).height > 0


# --- images --------------------------------------------------------------------


def test_image_keeps_aspect(png_b64):
    assert R.render_image(ImageElement(data=png_b64((40, 20))), 100).size == (200, 100)
    assert R.render_image(ImageElement(data=png_b64((10, 40))), 100).size == (25, 100)


def test_image_cache_returns_copies(png_b64):
    element = ImageElement(data=png_b64((40, 20), "red"))
    first = R.render_image(element, 50)
    first.paste((0, 0, 255, 255), (0, 0, 10, 10))
    assert R.render_image(element, 50).getpixel((0, 0)) == (255, 0, 0, 255)


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (40, 20), "red")
    img.paste((0, 0, 255), (0, 0, 10, 10))  # blue marker top left
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated: display needs 90° clockwise
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif, quality=100)
    part = R.render_image(ImageElement(data=base64.b64encode(buf.getvalue()).decode()), 80)
    assert part.size == (40, 80)
    r, _g, b, _a = part.getpixel((35, 5))  # marker now top right
    assert b > 200 and r < 60


def test_garbage_image_gives_warning_and_placeholder(make_label):
    rendered = render(make_label(margin_mm=0, elements=[ImageElement(data="!!not base64!!")]))
    assert len(rendered.warnings) == 1 and rendered.warnings[0].startswith("element 1: ")
    img = rendered.image
    assert img.width == INNER + 2 * FRAME
    assert img.getpixel((FRAME + INNER // 2, FRAME + 5)) == (255, 230, 230)
    assert img.getpixel((FRAME + INNER // 2, FRAME + INNER // 2)) == (200, 0, 0)


def test_element_errors_are_numbered(make_label, wide_image):
    rendered = render(make_label(elements=[wide_image, CodeElement(data="")]))
    assert rendered.warnings == ["element 2: empty code"]


# --- codes ---------------------------------------------------------------------


def test_qr_is_square_coloured_and_transparent():
    part = R.render_code(CodeElement(data="https://example.org", color="#0000ff"), 120)
    assert part.size == (120, 120)
    colours = {p for p in part.getdata()}
    assert (0, 0, 255, 255) in colours
    assert any(p[3] == 0 for p in colours)


def test_qr_on_label_shows_background(make_label):
    img = render(make_label(background="#00ff00", elements=[CodeElement(data="x" * 20)])).image
    x0, y0, x1, y1 = ink_bbox(img, (0, 255, 0))
    assert (x1 - x0, y1 - y0) == (INNER, INNER)
    colours = set(img.crop((x0, y0, x1, y1)).getdata())
    assert colours == {(0, 0, 0), (0, 255, 0)}


@pytest.mark.parametrize("symbology, data", [("code128", "ABC-123"), ("ean13", "4006381333931")])
def test_barcodes_transparent_background_and_colour(symbology, data):
    part = R.render_code(CodeElement(data=data, symbology=symbology, color="#ff0000"), 100)
    assert part.height == 100 and part.width > 100
    alphas = {p[3] for p in part.getdata()}
    assert alphas == {0, 255}
    opaque = [p for p in part.getdata() if p[3] == 255]
    assert all(p[0] > 150 and p[1] < 100 and p[2] < 100 for p in opaque[:50])
    assert not [p for p in part.getdata() if p[:3] == (255, 255, 255) and p[3] == 255]


@pytest.mark.parametrize(
    "code, message",
    [
        (CodeElement(data=""), "empty code"),
        (CodeElement(data="", symbology="code128"), "empty code"),
        (CodeElement(data="abc", symbology="ean13"), ""),
    ],
)
def test_invalid_codes_warn(make_label, code, message):
    rendered = render(make_label(elements=[code]))
    assert len(rendered.warnings) == 1
    assert rendered.warnings[0].startswith("element 1: " + message)


def test_valid_codes_render_without_warnings(make_label):
    for code in (
        CodeElement(data="https://example.org"),
        CodeElement(data="ABC-123", symbology="code128"),
        CodeElement(data="4006381333931", symbology="ean13"),
    ):
        assert render(make_label(elements=[code])).warnings == []
