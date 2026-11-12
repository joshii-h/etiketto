"""The design options as pixels: text style and boxes, image options,
shapes, the border's shape, an element's own alignment and spacing, and
crisp black and white on mono printers at 203 and 300 dpi."""

from __future__ import annotations

import base64
import io

import pytest
from conftest import ink_bbox
from PIL import Image, ImageChops, ImageDraw

from etiketto import imagefx, shapes
from etiketto import render as render_module
from etiketto.model import (
    CodeElement,
    ImageElement,
    Label,
    ShapeElement,
    SymbolElement,
    TextElement,
)
from etiketto.render import mm_to_px, render, render_image, render_text

WHITE = (255, 255, 255)
SQUARE = "M0,0H24V24H0Z"


def pixels(img: Image.Image) -> list:
    """All pixel values (getdata is deprecated in newer Pillow)."""
    return list(getattr(img, "get_flattened_data", img.getdata)())


def greys(image: Image.Image) -> int:
    """How many pixels are neither black nor white."""
    histogram = image.convert("L").histogram()
    return sum(histogram[1:255])


def b64(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def gradient(size=(64, 32)) -> str:
    """A photo-like image: grey from black to white."""
    img = Image.new("L", size)
    img.putdata([round(x * 255 / (size[0] - 1)) for _y in range(size[1]) for x in range(size[0])])
    return b64(img.convert("RGB"))


def quadrants() -> str:
    """Red top left, green top right, blue bottom left, white bottom right."""
    img = Image.new("RGB", (40, 20), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 19, 9), fill="red")
    draw.rectangle((20, 0, 39, 9), fill="lime")
    draw.rectangle((0, 10, 19, 19), fill="blue")
    return b64(img)


def every_option_label() -> Label:
    """A label with every element kind and the options that draw lines."""
    return Label(
        tape_mm=25,
        border_mm=0.3,
        border_radius_mm=2,
        border_style="dashed",
        elements=[
            TextElement(text="Hg", inverted=True, padding_mm=1, corner_mm=1),
            TextElement(text="Ab", underline=True, strikethrough=True, letter_spacing=10),
            TextElement(text="Out", outline_mm=0.3, outline_color="#000000", color="#ffffff"),
            TextElement(text="Sh", shadow_mm=0.4, color="#cc0000"),
            TextElement(text="Bx", box_color="#ffcc00", frame_mm=0.2, padding_mm=0.5),
            TextElement(text="Up", rotation=270),
            CodeElement(data="ABC"),
            SymbolElement(path=SQUARE, size_mm=5),
            ShapeElement(shape="separator", line_mm=0.1),
            ShapeElement(shape="line", arrows="both", line_style="dotted"),
            ShapeElement(shape="line", direction="up", height_mm=8, line_mm=0.15),
            ShapeElement(shape="rectangle", corner_mm=1.5, fill="hatched", height_mm=8),
            ShapeElement(shape="ellipse", fill="grid", line_mm=0.2),
            ShapeElement(shape="rectangle", fill="dotted", line_mm=0, line_style="dashed"),
        ],
    )


# --- black and white -------------------------------------------------------------


@pytest.mark.parametrize("dpi", [203, 300])
def test_mono_renders_every_drawn_element_without_grey(dpi):
    """Text, codes, symbols, lines, shapes and the border: whole dots."""
    image = render(every_option_label(), dpi=dpi, mono=True).image
    assert greys(image) == 0
    assert image.convert("L").histogram()[0] > 0  # there is ink


@pytest.mark.parametrize("dpi", [203, 300])
def test_mono_text_is_not_anti_aliased(dpi):
    """Text is laid out without anti-aliasing, not dithered afterwards: the
    letters are the same as a threshold of themselves (no noise along
    their edges) and differ from the anti-aliased ones."""
    element = TextElement(text="Etikett", size_pt=10, color="#3060c0")
    crisp = render_text(element, 1000, dpi, mono=True)
    assert set(crisp.getchannel("A").histogram()[1:255]) == {0}
    smooth = render_text(element, 1000, dpi)
    assert set(smooth.getchannel("A").histogram()[1:255]) != {0}
    label = Label(tape_mm=12, elements=[element])
    assert greys(render(label, dpi=dpi, mono=True).image) == 0


def test_colour_printers_keep_anti_aliasing():
    image = render(every_option_label(), dpi=300).image
    assert greys(image) > 0


def test_light_colours_print_white_on_mono():
    label = Label(tape_mm=12, margin_mm=0, background="#000000")
    label.elements = [TextElement(text="W", color="#ffff80")]
    image = render(label, dpi=203, safe_mm=0, mono=True).image.convert("L")
    assert image.histogram()[255] > 0 and greys(image) == 0


def test_the_backend_conversion_leaves_mono_parts_unchanged():
    """The preview's own dithering (apply_color_model) finds nothing left
    to change: what render made is what is printed."""
    from etiketto.backends.base import apply_color_model

    image = render(every_option_label(), dpi=203, mono=True).image
    assert ImageChops.difference(apply_color_model(image, "mono"), image).getbbox() is None


def test_a_grey_background_is_dithered_on_its_own():
    label = Label(tape_mm=12, background="#808080", elements=[TextElement(text="X")])
    image = render(label, dpi=203, mono=True).image.convert("L")
    assert greys(image) == 0
    histogram = image.histogram()
    assert 0.3 < histogram[0] / (histogram[0] + histogram[255]) < 0.8


def test_mono_codes_keep_their_problems_and_placement():
    label = Label(tape_mm=12, elements=[CodeElement(data="x" * 80)])
    colour = render(label, dpi=203)
    mono = render(label, dpi=203, mono=True)
    assert [w.key for w in mono.warnings] == [w.key for w in colour.warnings]


# --- image modes -------------------------------------------------------------------


def test_threshold_mode_and_level():
    element = ImageElement(data=gradient(), mode="threshold", threshold=64)
    label = Label(tape_mm=12, margin_mm=0, elements=[element])
    image = render(label, dpi=203, safe_mm=0, mono=True).image.convert("L")
    assert greys(image) == 0
    # a quarter of the gradient is darker than 64: black, the rest white
    row = image.crop((0, image.height // 2, image.width, image.height // 2 + 1)).tobytes()
    assert row.count(0) / len(row) == pytest.approx(0.25, abs=0.05)
    lighter = render(label, dpi=203, safe_mm=0, mono=True).image
    element.threshold = 200
    darker = render(label, dpi=203, safe_mm=0, mono=True).image
    assert darker.convert("L").histogram()[0] > lighter.convert("L").histogram()[0]


@pytest.mark.parametrize("mode", ["dither", "ordered"])
def test_dithering_keeps_the_middle_tones(mode):
    grey = imagefx.black_and_white(Image.new("RGBA", (64, 64), (128, 128, 128, 255)), mode)
    histogram = grey.convert("L").histogram()
    assert histogram[1:255] == [0] * 254
    assert histogram[0] / (64 * 64) == pytest.approx(0.5, abs=0.06)


def test_ordered_dithering_is_a_regular_pattern():
    grey = imagefx.ordered(Image.new("L", (16, 16), 100))
    assert grey.crop((0, 0, 8, 8)).tobytes() == grey.crop((8, 8, 16, 16)).tobytes()


def test_auto_mode_thresholds_drawings_and_dithers_photos():
    drawing = Image.new("RGBA", (40, 40), "white")
    ImageDraw.Draw(drawing).rectangle((10, 10, 30, 30), fill="black")
    assert imagefx.is_drawing(drawing.convert("L"))
    photo = Image.open(io.BytesIO(base64.b64decode(gradient()))).convert("RGBA")
    assert not imagefx.is_drawing(photo.convert("L"))
    auto = imagefx.black_and_white(photo, "auto")
    assert auto.tobytes() == imagefx.black_and_white(photo, "dither").tobytes()
    logo = imagefx.black_and_white(drawing, "auto")
    assert logo.tobytes() == imagefx.black_and_white(drawing, "threshold").tobytes()


def test_transparency_stays_transparent_in_black_and_white():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    img.putpixel((5, 5), (0, 0, 0, 255))
    out = imagefx.black_and_white(img)
    assert out.getchannel("A").getbbox() == (5, 5, 6, 6)


# --- image adjustments ---------------------------------------------------------------


def test_crop_rotate_mirror_and_flip():
    data = quadrants()
    plain = render_image(ImageElement(data=data), 20)
    assert plain.size == (40, 20)
    left = render_image(ImageElement(data=data, crop=[0.0, 0.0, 0.5, 1.0]), 20)
    assert left.size == (20, 20)
    assert left.getpixel((5, 5))[:3] == (255, 0, 0) and left.getpixel((5, 15))[:3] == (0, 0, 255)
    turned = render_image(ImageElement(data=data, rotation=90), 40)
    assert turned.size == (20, 40)
    assert turned.getpixel((15, 5))[:3] == (255, 0, 0)  # red top left goes top right
    mirrored = render_image(ImageElement(data=data, mirror=True), 20)
    assert mirrored.getpixel((5, 5))[:3] == (0, 255, 0)
    flipped = render_image(ImageElement(data=data, flip=True), 20)
    assert flipped.getpixel((5, 5))[:3] == (0, 0, 255)


def test_brightness_contrast_gamma_and_invert():
    grey = b64(Image.new("RGB", (4, 4), (100, 100, 100)))

    def level(**options) -> int:
        return render_image(ImageElement(data=grey, **options), 4).getpixel((1, 1))[0]

    assert level() == 100
    assert level(brightness=50) == 150
    assert level(brightness=-50) == 50
    assert level(invert=True) == 155
    assert level(gamma=2.0) > 100 > level(gamma=0.5)
    two = Image.new("RGB", (4, 4), (100, 100, 100))
    two.putpixel((0, 0), (200, 200, 200))
    assert imagefx.adjusted(two, contrast=100).getpixel((1, 1))[0] < 100


def test_adjusting_keeps_transparency():
    img = Image.new("RGBA", (4, 4), (10, 10, 10, 0))
    assert imagefx.adjusted(img, invert=True).getpixel((0, 0)) == (245, 245, 245, 0)


def test_rounded_corners_and_border():
    data = b64(Image.new("RGB", (100, 100), "red"))
    dpi = 254  # 10 dots per mm
    element = ImageElement(data=data, corner_mm=2, border_mm=0.3, border_color="#0000ff")
    img = render_image(element, 100, dpi)
    assert img.getpixel((0, 0))[3] == 0  # the corner is cut off
    assert img.getpixel((50, 1))[:3] == (0, 0, 255)  # the border, 3 dots
    assert img.getpixel((50, 3))[:3] == (255, 0, 0)
    assert img.getpixel((50, 50)) == (255, 0, 0, 255)


def test_a_hairline_border_is_at_least_one_dot():
    data = b64(Image.new("RGB", (100, 100), "red"))
    img = render_image(ImageElement(data=data, border_mm=0.01), 100, 203)
    assert img.getpixel((50, 0))[:3] == (0, 0, 0)
    assert img.getpixel((50, 1))[:3] == (255, 0, 0)


# --- text ---------------------------------------------------------------------------


def text(**options) -> Image.Image:
    return render_text(TextElement(text="Hxg", size_pt=20, **options), 1000, 300)


def test_underline_and_strikethrough_add_lines():
    plain = text()
    for option in ("underline", "strikethrough"):
        marked = text(**{option: True})
        assert marked.size == plain.size
        assert ImageChops.difference(marked, plain).getbbox()


def test_letter_spacing_widens_and_line_spacing_heightens():
    assert text(letter_spacing=50).width > text().width > text(letter_spacing=-20).width
    two = TextElement(text="A\nB", size_pt=20)
    normal = render_text(two, 1000, 300)
    two.line_spacing = 2.0
    assert render_text(two, 1000, 300).height > normal.height * 1.3


def test_inverted_text_is_cut_out_of_its_box():
    img = text(inverted=True, padding_mm=1)
    plain = text()
    assert img.width > plain.width and img.height > plain.height
    alpha = img.getchannel("A")
    assert alpha.getpixel((2, 2)) == 255  # the box
    holes = alpha.crop((10, 10, img.width - 10, img.height - 10)).histogram()[0]
    assert holes > 0  # the letters show the label through
    label = Label(tape_mm=12, margin_mm=0, elements=[TextElement(text="I", inverted=True)])
    image = render(label, dpi=203, safe_mm=0, mono=True).image.convert("L")
    assert image.histogram()[0] > image.histogram()[255] / 4  # mostly the black box


def test_box_frame_and_corner_radius():
    boxed = text(box_color="#00ff00", padding_mm=1)
    assert boxed.getpixel((1, 1)) == (0, 255, 0, 255)
    rounded = text(box_color="#00ff00", padding_mm=1, corner_mm=1)
    assert rounded.getpixel((0, 0))[3] < 255
    framed = text(frame_mm=0.3, padding_mm=1, color="#0000ff")
    assert framed.getpixel((1, framed.height // 2))[:3] == (0, 0, 255)
    assert framed.getpixel((framed.width // 2, 1))[3] == 255
    assert framed.getpixel((6, framed.height // 2))[3] == 0  # inside: padding


def test_outline_and_shadow():
    plain = text()
    outlined = text(outline_mm=0.5, outline_color="#ff0000")
    assert outlined.width > plain.width
    reds = [p for p in pixels(outlined) if p[3] == 255 and p[0] > 200 and p[1] < 50]
    assert reds
    shadowed = text(shadow_mm=0.5, shadow_color="#00ff00")
    assert shadowed.width == plain.width + mm_to_px(0.5, 300)
    assert any(p[1] > 200 and p[0] < 50 and p[3] == 255 for p in pixels(shadowed))


@pytest.mark.parametrize("dpi", [203, 300])
def test_mono_outline_and_shadow_are_whole_dots(dpi):
    element = TextElement(
        text="Hg", size_pt=14, outline_mm=0.01, shadow_mm=0.01, shadow_color="#c0c0c0"
    )
    img = render_text(element, 1000, dpi, mono=True)
    plain = render_text(TextElement(text="Hg", size_pt=14), 1000, dpi, mono=True)
    assert img.width == plain.width + 2 + 1  # an outline dot each side, one dot of shadow
    colours = {p[:3] for p in pixels(img) if p[3]}
    assert colours <= {(0, 0, 0), (255, 255, 255)}
    assert (255, 255, 255) in colours  # the white outline
    assert set(img.getchannel("A").histogram()[1:255]) == {0}


def test_mono_shadow_is_solid():
    element = TextElement(text="Hg", size_pt=14, shadow_mm=0.3, shadow_color="#c0c0c0")
    img = render_text(element, 1000, 203, mono=True)
    assert {p[:3] for p in pixels(img) if p[3]} == {(0, 0, 0)}


@pytest.mark.parametrize("rotation", [90, 270])
def test_vertical_text_fills_the_height_with_its_length(rotation):
    img = render_text(TextElement(text="Vertical", rotation=rotation), 300, 300)
    assert img.height <= 300 and img.height > 0.85 * 300
    assert img.height > img.width  # turned


def test_vertical_text_reads_the_other_way_round():
    down = render_text(TextElement(text="AB", rotation=90, size_pt=20), 1000, 300)
    up = render_text(TextElement(text="AB", rotation=270, size_pt=20), 1000, 300)
    assert ImageChops.difference(down, up.rotate(180)).getbbox() is None


def test_shrink_fits_a_fixed_length():
    element = TextElement(text="A rather long text", size_pt=40, shrink=True)
    wide = render_text(element, 300, 300)
    assert wide.width > 400
    fitted = render_text(element, 300, 300, room=400)
    assert fitted.width <= 400 and fitted.width > 0.9 * 400


def test_shrink_stops_at_the_smallest_size():
    element = TextElement(text="A rather long text", size_pt=40, shrink=True, min_size_pt=20)
    limited = render_text(element, 300, 300, room=100)
    at_min = render_text(TextElement(text="A rather long text", size_pt=20), 300, 300)
    assert limited.size == at_min.size


def test_shrink_fits_the_height_with_fill():
    element = TextElement(text="Text", shrink=True)
    img = render_text(element, 200, 300, room=150)
    assert img.width <= 150 and img.height <= 200


def test_wrapping_at_a_width():
    element = TextElement(text="one two three four five six", size_pt=12, max_width_mm=15)
    img = render_text(element, 1000, 300)
    assert img.width <= mm_to_px(15, 300) + 2
    single = render_text(TextElement(text="one two three four five six", size_pt=12), 1000, 300)
    assert img.height > 2 * single.height


def test_wrapped_text_fills_the_height():
    element = TextElement(text="one two three four five six", max_width_mm=20)
    img = render_text(element, 300, 300)
    assert 0.6 * 300 < img.height <= 300
    assert img.width <= mm_to_px(20, 300) + 2


def test_shrinking_wrapped_text_narrower_than_a_long_word():
    element = TextElement(text="Supercalifragilistic", shrink=True, max_width_mm=10)
    img = render_text(element, 300, 300)
    assert img.width <= mm_to_px(10, 300)


def test_text_options_appear_in_the_label(make_label):
    label = make_label(elements=[TextElement(text="Hi", inverted=True, padding_mm=0.5)])
    image = render(label).image
    bbox = ink_bbox(image, WHITE)
    assert bbox and image.getpixel(((bbox[0] + bbox[2]) // 2, bbox[1] + 2)) == (0, 0, 0)


# --- shapes --------------------------------------------------------------------------


def shape(dpi=300, height=100, mono=False, **options) -> Image.Image:
    return render_module.render_shape(ShapeElement(**options), height, dpi, mono)


def test_separator_spans_the_height():
    img = shape(shape="separator", line_mm=0.5)
    assert img.size == (mm_to_px(0.5, 300), 100)
    assert img.getchannel("A").getextrema() == (255, 255)


def test_lines_are_at_least_one_dot():
    for line_mm in (0, 0.01):
        img = shape(dpi=203, shape="separator", line_mm=line_mm)
        assert img.width == 1


def test_horizontal_line_with_arrows():
    plain = shape(shape="line", width_mm=10, line_mm=0.3, mono=True)
    assert plain.size == (mm_to_px(10, 300), mm_to_px(0.3, 300))
    arrows = shape(shape="line", width_mm=10, line_mm=0.3, arrows="both", mono=True)
    assert arrows.height > plain.height  # the heads are wider than the line
    alpha = arrows.getchannel("A")
    middle = arrows.height // 2
    assert alpha.getpixel((arrows.width // 2, middle)) == 255
    assert alpha.getpixel((arrows.width // 2, 0)) == 0  # the line is thin
    assert alpha.getpixel((4, middle - 3)) == 255 or alpha.getpixel((5, middle - 2)) == 255


def test_vertical_and_diagonal_lines():
    vertical = shape(shape="line", direction="vertical", line_mm=0.3)
    assert vertical.size == (mm_to_px(0.3, 300), 100)
    up = shape(shape="line", direction="up", width_mm=5, height_mm=5, line_mm=0.2, mono=True)
    alpha = up.getchannel("A")
    assert alpha.getpixel((2, up.height - 3)) == 255 and alpha.getpixel((up.width - 3, 2)) == 255
    assert alpha.getpixel((2, 2)) == 0
    down = shape(shape="line", direction="down", width_mm=5, height_mm=5, line_mm=0.2, mono=True)
    assert down.getchannel("A").getpixel((2, 2)) == 255


def test_dashed_and_dotted_lines_have_gaps():
    for style in ("dashed", "dotted"):
        img = shape(shape="line", width_mm=20, line_mm=0.3, line_style=style, mono=True)
        row = img.getchannel("A").crop((0, img.height // 2, img.width, img.height // 2 + 1))
        data = row.tobytes()
        assert 0 in data and 255 in data


def test_rectangle_outline_fill_and_corners():
    box = shape(shape="rectangle", width_mm=10, height_mm=5, line_mm=0.3, mono=True)
    alpha = box.getchannel("A")
    assert box.size == (mm_to_px(10, 300), mm_to_px(5, 300))
    assert alpha.getpixel((0, 0)) == 255 and alpha.getpixel((box.width // 2, box.height // 2)) == 0
    solid = shape(shape="rectangle", width_mm=10, height_mm=5, fill="solid", line_mm=0)
    assert solid.getchannel("A").getextrema() == (255, 255)
    rounded = shape(shape="rectangle", width_mm=10, height_mm=5, corner_mm=2, mono=True)
    assert rounded.getchannel("A").getpixel((0, 0)) == 0


@pytest.mark.parametrize("fill", ["hatched", "dotted", "grid"])
def test_patterns_are_one_bit_and_partly_inked(fill):
    img = shape(dpi=203, shape="rectangle", width_mm=10, height_mm=10, fill=fill, line_mm=0)
    histogram = img.getchannel("A").histogram()
    assert histogram[1:255] == [0] * 254
    share = histogram[255] / (img.width * img.height)
    assert 0.05 < share < 0.7


def test_ellipse():
    img = shape(shape="ellipse", width_mm=10, height_mm=5, line_mm=0.2, mono=True)
    alpha = img.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((img.width // 2, 0)) == 255
    assert alpha.getpixel((img.width // 2, img.height // 2)) == 0


def test_shape_colour_and_mono_ink():
    red = shape(shape="separator", color="#ff0000")
    assert red.getpixel((0, 0)) == (255, 0, 0, 255)
    mono = shape(shape="separator", color="#ff0000", mono=True)
    assert mono.getpixel((0, 0)) == (0, 0, 0, 255)


def test_pattern_helper_covers_every_fill():
    assert shapes.pattern("solid", (4, 4), 300).getextrema() == (255, 255)
    assert shapes.pattern("none", (4, 4), 300).getextrema() == (0, 0)


# --- placement --------------------------------------------------------------------


def block(png_b64, valign="center", gap_mm=None) -> ImageElement:
    return ImageElement(data=png_b64((10, 10), "red"), valign=valign, gap_mm=gap_mm)


def test_vertical_alignment_of_a_smaller_element(png_b64):
    def top_of(valign):
        element = TextElement(text="x", size_pt=6, valign=valign)
        label = Label(tape_mm=25, margin_mm=0, elements=[element])
        return ink_bbox(render(label, safe_mm=1.5).image, WHITE)

    top, middle, bottom = top_of("top"), top_of("center"), top_of("bottom")
    assert top[1] < middle[1] < bottom[1]
    safe = mm_to_px(1.5)
    assert top[1] >= safe and bottom[3] <= mm_to_px(25) - safe


def test_an_element_keeps_its_own_spacing(png_b64):
    label = Label(tape_mm=25, margin_mm=2, elements=[block(png_b64), block(png_b64)])
    normal = render(label).image.width
    label.elements[0].gap_mm = 0
    assert render(label).image.width == normal - mm_to_px(2)
    label.elements[0].gap_mm = 5
    assert render(label).image.width == normal - mm_to_px(2) + mm_to_px(5)
    label.elements[1].gap_mm = 30  # after the last one: no space
    assert render(label).image.width == normal - mm_to_px(2) + mm_to_px(5)


def test_own_spacing_scales_with_a_fixed_length(png_b64):
    label = Label(
        tape_mm=25,
        auto_length=False,
        length_mm=20,
        elements=[block(png_b64, gap_mm=4), block(png_b64)],
    )
    rendered = render(label)
    assert rendered.overflow and rendered.image.width == mm_to_px(20)


# --- the border ---------------------------------------------------------------------


def border(**options) -> Image.Image:
    options.setdefault("border_mm", 0.5)
    label = Label(tape_mm=12, auto_length=False, length_mm=40, **options)
    return render(label, dpi=254, safe_mm=1, mono=True).image.convert("L")


def test_border_inset_and_radius():
    plain = border()
    assert plain.getpixel((10, 60)) == 0  # 1 mm in: the border
    inset = border(border_inset_mm=1)
    assert inset.getpixel((10, 60)) == 255 and inset.getpixel((20, 60)) == 0
    rounded = border(border_radius_mm=3)
    assert rounded.getpixel((10, 10)) == 255  # the corner is round
    assert rounded.getpixel((10, 60)) == 0


@pytest.mark.parametrize("style", ["dashed", "dotted"])
def test_border_styles_have_gaps(style):
    img = border(border_style=style)
    row = img.crop((20, 12, img.width - 20, 13)).tobytes()
    assert 0 in row and 255 in row
    assert greys(img) == 0


def test_double_border_is_two_lines():
    img = border(border_style="double", border_mm=1)
    column = img.crop((img.width // 2, 0, img.width // 2 + 1, 40)).tobytes()
    runs = sum(1 for a, b in zip(column, column[1:], strict=False) if a != b)
    assert runs >= 4  # white, black, white, black, white


def test_border_in_colour_is_anti_aliased_when_round():
    label = Label(tape_mm=12, border_mm=0.5, border_radius_mm=2, border_color="#0000ff")
    image = render(label, dpi=254).image
    assert greys(image) > 0
