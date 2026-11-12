"""The design options as pixels: text style and boxes, image options,
shapes, the border's shape, an element's own alignment and spacing, and
crisp black and white on mono printers at 203 and 300 dpi."""

from __future__ import annotations

import base64
import io

import pytest
from conftest import ink_bbox
from PIL import Image, ImageChops, ImageDraw

from tavletta import imagefx, shapes
from tavletta import render as render_module
from tavletta.model import (
    CodeElement,
    ImageElement,
    Label,
    ShapeElement,
    SymbolElement,
    TextElement,
)
from tavletta.render import mm_to_px, render, render_image, render_text

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
    from tavletta.backends.base import apply_color_model

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


# --- review: light colours, inks against the background -------------------------------


@pytest.mark.parametrize("dpi", [203, 300])
def test_light_colours_stay_visible_on_white(dpi):
    label = Label(
        tape_mm=12,
        elements=[
            TextElement(text="Yellow", color="#ffee00"),
            SymbolElement(path=SQUARE, color="#e0e0e0", size_mm=4),
            CodeElement(data="ABC", color="#ffff80"),
            ShapeElement(shape="rectangle", fill="solid", line_mm=0, color="#f0f0a0"),
        ],
    )
    rendered = render(label, dpi=dpi, mono=True)
    assert rendered.warnings == []
    image = rendered.image.convert("L")
    assert greys(image) == 0
    assert image.histogram()[0] > 0.1 * image.width * image.height / 4


def test_white_on_white_warns_on_mono():
    label = Label(tape_mm=12, elements=[CodeElement(data="ABC", color="#ffffff")])
    keys = [w.key for w in render(label, dpi=203, mono=True).warnings]
    assert keys == ["no_contrast"]
    assert [w.key for w in render(label, dpi=203).warnings] == []


def test_inks_follow_a_background_image():
    dark = b64(Image.new("RGB", (8, 8), (20, 20, 20)))
    label = Label(tape_mm=12, margin_mm=0, background_image=ImageElement(data=dark))
    label.elements = [SymbolElement(path=SQUARE, color="#808080")]
    image = render(label, dpi=203, safe_mm=0, mono=True).image
    assert image.getpixel((image.height // 2, image.height // 2)) == (255, 255, 255)


def test_inverted_and_boxed_text_use_the_other_ink():
    """White text on a yellow box on white paper: the box prints black, the
    letters white on it; an outline is never the letters' own ink."""
    element = TextElement(
        text="HH", size_pt=20, color="#ffffff", box_color="#ffdd00", padding_mm=1
    )
    img = render_text(element, 1000, 203, mono=True)
    colours = {p[:3] for p in pixels(img) if p[3]}
    assert colours == {(0, 0, 0), (255, 255, 255)}
    assert img.getpixel((1, 1))[:3] == (0, 0, 0)  # the box
    outlined = TextElement(text="HH", size_pt=20, outline_mm=0.3, outline_color="#202020")
    img = render_text(outlined, 1000, 203, mono=True)
    assert (255, 255, 255) in {p[:3] for p in pixels(img) if p[3]}


def test_light_drawing_keeps_its_ink_in_auto_mode():
    logo = Image.new("RGB", (60, 30), "white")
    ImageDraw.Draw(logo).rectangle((10, 5, 50, 25), fill=(250, 230, 60))
    out = imagefx.black_and_white(logo.convert("RGBA"), "auto")
    histogram = out.convert("L").histogram()
    assert histogram[0] == 41 * 21  # the yellow rectangle, all black
    assert histogram[1:255] == [0] * 254


def test_a_drawing_without_two_tones_is_dithered():
    flat = Image.new("RGBA", (40, 40), (240, 240, 120, 255))  # all one light colour
    out = imagefx.black_and_white(flat, "auto")
    share = out.convert("L").histogram()[0] / (40 * 40)
    assert 0.05 < share < 0.3


def test_otsu_splits_between_the_tones():
    histogram = [0] * 256
    histogram[200], histogram[255] = 100, 300
    assert 200 < imagefx.otsu(histogram) <= 255
    assert imagefx.otsu([0] * 255 + [10]) == 128


# --- review: the border's inset and the frame -----------------------------------------


def test_the_inset_border_keeps_clear_of_the_content(png_b64):
    def inner_box(inset):
        label = Label(
            tape_mm=12,
            border_mm=0.5,
            border_inset_mm=inset,
            border_color="#0000ff",
            margin_mm=0,
            elements=[ImageElement(data=png_b64((10, 10), "red"))],
        )
        image = render(label, dpi=254, safe_mm=1).image
        reds = Image.eval(image.convert("RGB").split()[0], lambda v: 255 if v > 200 else 0)
        blue = image.convert("RGB").split()[2]
        return ink_bbox(image, WHITE), reds, blue, image

    plain = inner_box(0)[0]
    bbox, _reds, _blue, image = inner_box(1)
    assert bbox[0] == 20 and bbox[1] == 20  # the border, 1 mm inside the safety margin
    red_part = [p for p in pixels(image.convert("RGB")) if p == (255, 0, 0)]
    assert len(red_part) == (120 - 2 * (10 + 10 + 5)) ** 2  # smaller by the inset
    assert plain[1] == 10  # without inset: the border on the safety margin


def test_no_room_draws_the_border_as_before():
    """A border too wide for the tape: on the safety margin, as before the
    inset existed, and the warning."""
    label = Label(tape_mm=9, border_mm=5, border_inset_mm=3, elements=[TextElement(text="x")])
    rendered = render(label, dpi=254, safe_mm=1)
    assert "no_room" in [w.key for w in rendered.warnings]
    assert rendered.image.getpixel((5, 10)) == WHITE  # the safety margin
    assert rendered.image.getpixel((12, 12)) == (0, 0, 0)  # the border starts on it


def test_double_border_stays_within_its_width():
    img = border(border_style="double", border_mm=0.5)  # 5 dots
    column = img.crop((img.width // 2, 0, img.width // 2 + 1, 30)).tobytes()
    inked = [i for i, v in enumerate(column) if v == 0]
    assert inked and min(inked) == 10 and max(inked) == 14  # within the 5 dots
    assert 255 in column[10:15]  # with a gap
    narrow = border(border_style="double", border_mm=0.2)  # 2 dots: one line
    column = narrow.crop((narrow.width // 2, 0, narrow.width // 2 + 1, 30)).tobytes()
    assert [i for i, v in enumerate(column) if v == 0] == [10, 11]


# --- review: fitting decorated text ----------------------------------------------------


def test_boxed_text_fills_the_height():
    element = TextElement(text="Hg", inverted=True, padding_mm=3, frame_mm=0.3)
    img = render_text(element, 300, 300)
    assert 0.9 * 300 <= img.height <= 300


def test_huge_padding_does_not_stall():
    import time

    element = TextElement(text="Hg", box_color="#ff0000", padding_mm=20)
    started = time.monotonic()
    img = render_text(element, 100, 300)
    assert time.monotonic() - started < 2
    assert img.height > 100  # cannot fit: the smallest size, render() scales it


# --- review: reduced to a fixed length -------------------------------------------------


def test_shapes_and_mono_text_are_drawn_again_when_reduced():
    label = Label(
        tape_mm=12,
        auto_length=False,
        length_mm=20,
        elements=[
            ShapeElement(shape="rectangle", width_mm=30, height_mm=6, fill="hatched", line_mm=0),
            TextElement(text="Long text here"),
        ],
    )
    colour = render(label, dpi=300, safe_mm=0)
    assert colour.overflow
    shape_area = colour.image.crop((0, 0, colour.image.width // 3, colour.image.height))
    assert greys(shape_area) == 0  # the pattern drawn again, not resampled
    assert "redraw" in render_text(TextElement(text="x"), 100, 300, mono=True).info
    assert "redraw" not in render_text(TextElement(text="x"), 100, 300).info
    mono = render(label, dpi=300, safe_mm=0, mono=True)
    assert greys(mono.image) == 0


def test_redrawn_shape_keeps_lines_at_least_one_dot():
    part = render_module.render_shape(ShapeElement(shape="separator", line_mm=0.2), 100, 203)
    smaller = render_module._resize(part, 1, 30)
    assert smaller.size == (1, 30) and smaller.getchannel("A").getextrema() == (255, 255)


# --- review: images on mono ------------------------------------------------------------


def test_mono_image_border_is_solid_after_dithering():
    element = ImageElement(data=gradient((100, 100)), mode="dither", border_mm=0.5)
    label = Label(tape_mm=12, margin_mm=0, elements=[element])
    image = render(label, dpi=254, safe_mm=0, mono=True).image.convert("L")
    top = image.crop((10, 0, image.width - 10, 5)).tobytes()
    assert set(top) == {0}  # the border: not dithered
    assert greys(image) == 0


def test_colour_image_corners_and_border_are_smooth():
    data = b64(Image.new("RGB", (100, 100), "red"))
    img = render_image(ImageElement(data=data, corner_mm=2, border_mm=0.3), 100, 254)
    alphas = set(img.getchannel("A").histogram()[1:255])
    assert alphas != {0}  # anti-aliased edge of the corners


def test_large_images_are_adjusted_at_the_label_size(monkeypatch):
    sizes = []
    original = imagefx.adjusted

    def recording(img, *args, **kwargs):
        sizes.append(img.size)
        return original(img, *args, **kwargs)

    monkeypatch.setattr(imagefx, "adjusted", recording)
    data = b64(Image.new("RGB", (1600, 1200), "grey"))
    render_image(ImageElement(data=data, brightness=10), 60)
    assert sizes == [(80, 60)]


# --- review: black and red ------------------------------------------------------------


def test_red_mode_keeps_red_and_converts_the_rest():
    label = Label(
        tape_mm=12,
        background="#e0e0e0",
        elements=[
            TextElement(text="Red", color="#d00000"),
            ImageElement(data=gradient(), mode="ordered"),
            TextElement(text="Black"),
        ],
    )
    image = render(label, dpi=300, mono=True, red=True).image
    colours = {c for _n, c in image.getcolors(1 << 24)}
    assert colours == {(0, 0, 0), (255, 255, 255), (255, 0, 0)}


def test_the_red_mode_of_a_brother_ql_honours_the_image_method():
    from tavletta.backends.base import PrintOptions
    from tavletta.backends.brother_ql import BrotherQL

    backend = BrotherQL()
    label = Label.new_for(backend.id)
    label.elements = [ImageElement(data=gradient(), mode="threshold", threshold=40)]
    threshold = backend.prepare(label, PrintOptions(mode="red")).convert("L")
    label.elements[0].threshold = 220
    darker = backend.prepare(label, PrintOptions(mode="red")).convert("L")
    assert darker.histogram()[0] > threshold.histogram()[0]


# --- review: letter spacing, shadows, shapes ----------------------------------------------


def test_letter_spacing_leaves_joined_scripts_alone():
    arabic = TextElement(text="سلام عليكم", size_pt=20)
    plain = render_text(arabic, 1000, 300)
    arabic.letter_spacing = 50
    assert render_text(arabic, 1000, 300).size == plain.size
    from tavletta.typeset import spaced_runs

    assert spaced_runs("ab") == [(0, 2)]
    assert spaced_runs("aسb") == [(0, 1), (3, 4)]
    assert spaced_runs("a b") == [(0, 3)]
    assert spaced_runs("س b") == [(3, 4)]  # the space goes with the Arabic
    assert spaced_runs("नमस्ते") == []


def test_mono_shadow_is_kept_apart_from_the_letters():
    element = TextElement(text="I", size_pt=30, bold=True, shadow_mm=0.6)
    img = render_text(element, 1000, 300, mono=True)
    alpha = img.getchannel("A")
    row = alpha.crop((0, img.height // 2, img.width, img.height // 2 + 1)).tobytes()
    runs = [k for k, (a, b) in enumerate(zip(row, row[1:], strict=False)) if a != b]
    assert len(runs) >= 4  # letter, gap, shadow


def test_invisible_shapes_warn():
    label = Label(elements=[ShapeElement(shape="rectangle", line_mm=0)])
    assert [w.key for w in render(label).warnings] == ["shape_invisible"]
    assert [w.key for w in render(label, dpi=203, mono=True).warnings] == ["shape_invisible"]


def test_arrow_heads_have_a_smallest_size_and_short_lines_work():
    thin = ShapeElement(shape="line", width_mm=10, line_mm=0.05, arrows="end")
    img = render_module.render_shape(thin, 100, 203, mono=True)
    assert img.height >= shapes.px(shapes.ARROW_MM, 203)
    short = ShapeElement(shape="line", width_mm=0.5, line_mm=0.3, arrows="both")
    img = render_module.render_shape(short, 100, 300, mono=True)
    assert img.getchannel("A").getbbox()


# --- review: speed ----------------------------------------------------------------------


def test_tiling_matches_pasting():
    tile = Image.new("L", (3, 2))
    tile.putdata([1, 2, 3, 4, 5, 6])
    tiled = imagefx.tiled(tile, (7, 5))
    for y in range(5):
        for x in range(7):
            assert tiled.getpixel((x, y)) == tile.getpixel((x % 3, y % 2))


def test_patterns_repeat_with_their_period():
    period = max(3, shapes.px(shapes.PATTERN_MM, 203))
    for fill in ("hatched", "dotted", "grid"):
        img = shapes.pattern(fill, (4 * period, 4 * period), 203)
        first = img.crop((0, 0, period, period)).tobytes()
        assert img.crop((period, 2 * period, 2 * period, 3 * period)).tobytes() == first


def test_a_converted_render_is_not_dithered_again(monkeypatch):
    from tavletta.backends.base import apply_color_model

    image = render(every_option_label(), dpi=203, mono=True).image
    assert image.info.get("black_and_white")
    calls = []
    original = Image.Image.convert

    def counting(self, mode=None, *args, **kwargs):
        calls.append(mode)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", counting)
    apply_color_model(image, "mono")
    assert "1" not in calls


# --- review 2: redrawn texts, inks under each element, speed, edge cases ----------------


def short_label(*elements, length=20):
    return Label(tape_mm=12, auto_length=False, length_mm=length, elements=list(elements))


@pytest.mark.parametrize(
    "options",
    [
        {"frame_mm": 0.4, "padding_mm": 1},
        {"box_color": "#000000", "padding_mm": 1, "corner_mm": 1},
        {"shadow_mm": 0.5},
        {"outline_mm": 0.3, "color": "#ffffff", "outline_color": "#000000"},
        {"max_width_mm": 15, "size_pt": 14},
    ],
)
def test_mono_texts_reduced_to_a_fixed_length_scale_every_length(options):
    """Laid out again with padding, frame, outline, shadow and wrap width
    scaled: it fits where the colour render's text goes, no closer to the
    edges."""
    element = TextElement(text="Regal 12 links", **options)
    label = short_label(element, ShapeElement(width_mm=10))
    colour = render(label, dpi=300, safe_mm=1)
    mono = render(label, dpi=300, safe_mm=1, mono=True)
    assert colour.overflow and mono.overflow
    assert greys(mono.image) == 0
    c, m = ink_bbox(colour.image, WHITE), ink_bbox(mono.image, WHITE)
    assert m[0] >= c[0] - 1 and m[1] >= c[1] - 1
    assert m[2] <= c[2] + 1 and m[3] <= c[3] + 1


def test_text_redraw_fits_its_size_and_keeps_no_image():
    element = TextElement(text="Hg", frame_mm=0.5, padding_mm=2, size_pt=30)
    block = render_text(element, 1000, 300, mono=True)
    redraw = block.info["redraw"]
    assert not any(isinstance(value, Image.Image) for value in vars(redraw).values())
    small = redraw(block.width // 2, block.height // 2)
    assert small.width <= block.width // 2 and small.height <= block.height // 2
    alpha = small.getchannel("A")
    assert alpha.getpixel((0, small.height // 2)) == 255  # the frame, on its edge
    assert alpha.getpixel((small.width // 2, small.height - 1)) == 255
    assert redraw(1, 1) is None  # nothing fits: the caller resamples
    assert render_module._resize(block, 1, 1).size == (1, 1)


def test_a_coloured_code_stays_visible_when_reduced_on_mono():
    code = CodeElement(data="ABC123", symbology="code128", color="#ffe066")
    label = short_label(code, TextElement(text="wide text here"), length=25)
    rendered = render(label, dpi=300, safe_mm=1, mono=True)
    assert rendered.overflow
    assert "no_contrast" not in [w.key for w in rendered.warnings]
    first = rendered.image.crop((0, 0, rendered.image.width // 2, rendered.image.height))
    assert first.convert("L").histogram()[0] > 500  # the bars


def half_dark(width=800, dark=400) -> str:
    img = Image.new("RGB", (width, 200), "white")
    ImageDraw.Draw(img).rectangle((0, 0, dark, 200), fill="#102040")
    return b64(img)


def test_inks_follow_the_background_under_each_element():
    """White text over the dark part of a background image prints white,
    black text over its light part black."""
    label = Label(
        tape_mm=12,
        auto_length=False,
        length_mm=80,
        align="start",
        margin_mm=1,
        background_image=ImageElement(data=half_dark()),
        elements=[
            TextElement(text="Logo", color="#c0c0c0", gap_mm=25),
            TextElement(text="Name", color="#606060"),
        ],
    )
    rendered = render(label, dpi=300, safe_mm=0, mono=True)
    assert rendered.warnings == []
    image = rendered.image.convert("L")
    left = image.crop((0, 0, image.width // 3, image.height))
    right = image.crop((image.width * 2 // 3, 0, image.width, image.height))
    # left: dark background with white letters; right: white with black letters
    assert left.histogram()[255] > 0 and right.histogram()[0] > 0
    colour = render(label, dpi=300, safe_mm=0)
    assert colour.image.size == rendered.image.size


def test_ink_as_bright_as_what_lies_under_it_warns():
    label = Label(
        tape_mm=12,
        background_image=ImageElement(data=b64(Image.new("RGB", (40, 10), (15, 15, 15)))),
        elements=[TextElement(text="x", color="#000000")],
    )
    assert "no_contrast" in [w.key for w in render(label, dpi=203, mono=True).warnings]


def test_red_on_red_warns_on_black_and_red():
    label = Label(
        tape_mm=12, background="#ff0000", elements=[TextElement(text="x", color="#e00000")]
    )
    keys = [w.key for w in render(label, dpi=300, mono=True, red=True).warnings]
    assert keys == ["no_contrast"]
    label.elements[0].color = "#000000"
    assert render(label, dpi=300, mono=True, red=True).warnings == []


def test_plain_backgrounds_take_the_quick_way(monkeypatch):
    calls = []
    for name in ("black_and_white", "red_mask"):
        original = getattr(imagefx, name)
        monkeypatch.setattr(
            imagefx, name, lambda *a, _o=original, _n=name, **k: (calls.append(_n), _o(*a, **k))[1]
        )
    label = Label(
        tape_mm=12, background="#e0e0e0", elements=[TextElement(text="Hi"), CodeElement()]
    )
    image = render(label, dpi=300, mono=True).image
    assert calls == []
    assert greys(image) == 0
    assert image.crop((0, 0, 8, 8)).tobytes() == image.crop((8, 8, 16, 16)).tobytes()  # a pattern


def test_a_border_without_room_does_not_fail():
    for length in (2, 4, 6):
        label = Label(
            tape_mm=12, border_mm=1, border_inset_mm=2, auto_length=False, length_mm=length
        )
        label.elements = [TextElement(text="i")]
        rendered = render(label, dpi=300, safe_mm=1, mono=True)
        assert "no_room" in [w.key for w in rendered.warnings]


def test_a_too_large_inset_stays_inside_the_label():
    label = Label(tape_mm=12, border_mm=0.5, border_inset_mm=10, auto_length=False, length_mm=15)
    rendered = render(label, dpi=254, safe_mm=1, mono=True)
    assert "no_room" in [w.key for w in rendered.warnings]
    assert rendered.image.convert("L").histogram()[0] > 0  # the border is still drawn


def test_padding_larger_than_the_tape_warns():
    label = Label(tape_mm=9, elements=[TextElement(text="x", box_color="#ff0000", padding_mm=6)])
    assert "padding_too_large" in [w.key for w in render(label).warnings]
    label.elements[0].padding_mm = 0.5
    assert render(label).warnings == []


def test_adjusting_a_photo_uses_the_cached_scaled_image():
    data = b64(Image.new("RGB", (1200, 800), "grey"))
    render_module._scaled_cut_cached.cache_clear()
    element = ImageElement(data=data, rotation=90)
    render_image(element, 60)
    for brightness in (10, 20, 30):
        element.brightness = brightness
        render_image(element, 60)
    info = render_module._scaled_cut_cached.cache_info()
    assert info.misses == 1 and info.hits == 3


def test_colour_emoji_in_mono_text_are_dithered_not_blown_out():
    part = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(part)
    draw.ellipse((10, 10, 90, 90), fill=(255, 204, 77, 255))  # a yellow face
    draw.rectangle((30, 40, 34, 44), fill=(60, 40, 0, 255))
    out = render_module._mono_part(TextElement(), part, render_module.Inks(True), 300)
    histogram = out.convert("L").histogram(out.getchannel("A"))
    assert histogram[1:255] == [0] * 254
    assert histogram[0] > 0.1 * sum(histogram)  # the face shows as dots
    plain = render_module._mono_part(
        TextElement(), Image.new("RGBA", (10, 10), (0, 0, 0, 255)), render_module.Inks(True), 300
    )
    assert plain.getpixel((5, 5)) == (0, 0, 0, 255)


def test_a_flat_grey_logo_is_a_drawing():
    logo = Image.new("RGB", (60, 60), "white")
    ImageDraw.Draw(logo).rectangle((10, 10, 49, 49), fill=(128, 128, 128))
    assert imagefx.is_drawing(logo.convert("L"))
    out = imagefx.black_and_white(logo.convert("RGBA"), "auto").convert("L")
    assert out.crop((12, 12, 48, 48)).getextrema() == (0, 0)  # solid, not dithered


def test_short_lines_keep_whole_arrow_heads_within_the_line():
    line = ShapeElement(shape="line", width_mm=1, line_mm=0.3, arrows="both")
    img = render_module.render_shape(line, 100, 300, mono=True)
    assert img.width == shapes.px(1, 300)
    alpha = img.getchannel("A")
    assert alpha.getbbox() and alpha.getbbox()[2] <= img.width


@pytest.mark.parametrize(
    "emoji",
    [
        "\U0001f389",
        "\u2b50",
        "\u231a",
        "\u23f0",
        "\u2b06",
        "\u25b6",
        "\u25c0",
        "\u23e9",
        "\u3030",
        "\u3297",
        "\u2600",
    ],
)
def test_colour_emoji_never_reach_a_mono_printer_in_colour(emoji):
    """Decided from the pixels, not from code points."""
    label = Label(tape_mm=12, elements=[TextElement(text="Hi " + emoji)])
    for red in (False, True):
        image = render(label, dpi=300, mono=True, red=red).image.convert("RGB")
        colours = {c for _n, c in image.getcolors(1 << 24)}
        allowed = {(0, 0, 0), (255, 255, 255)} | ({(255, 0, 0)} if red else set())
        assert colours <= allowed, (emoji, red, colours - allowed)


def test_a_plain_mono_text_is_used_as_drawn():
    block = render_text(TextElement(text="Hg", size_pt=20), 1000, 300, mono=True)
    assert imagefx.tones(block) == "hard"
    assert render_module._mono_part(TextElement(), block, render_module.Inks(True), 300) is block


def test_a_box_or_outline_makes_a_text_show():
    """A white box on a pale label shows its black letters; white letters
    with a black outline show; white letters alone on a pale label warn."""
    label = Label(
        tape_mm=12,
        background="#ffeb3b",
        elements=[
            TextElement(text="Box", box_color="#ffffff", color="#c62828", padding_mm=0.5),
            TextElement(text="Out", color="#ffffff", outline_mm=0.3),
            TextElement(text="White", color="#ffffff"),
        ],
    )
    warnings = render(label, dpi=300, mono=True).warnings
    assert warnings == [render_module.RenderWarning("no_contrast", {"n": 3})]


# --- review 3 ----------------------------------------------------------------------------


def test_an_inset_too_large_across_the_tape_is_reduced():
    label = Label(tape_mm=12, border_mm=0.5, border_inset_mm=10, elements=[TextElement(text="x")])
    rendered = render(label, dpi=254, safe_mm=1)
    assert [w.key for w in rendered.warnings] == ["no_room"]
    f = render_module._frame(label, 254, 1, None, 0, [])
    assert f.inset > 0 and f.inner == shapes.px(2, 254) + (f.inner - shapes.px(2, 254))
    assert f.inner >= shapes.px(2, 254)


@pytest.mark.parametrize(
    "options, warns",
    [
        ({"color": "#ff0000", "box_color": "#e53935", "red": True}, False),  # white on red
        ({"color": "#ff0000", "box_color": "#e53935"}, False),  # black box, white letters
        ({"color": "#ffffff", "box_color": "#ffffff"}, False),  # black letters in white
        ({"color": "#000000", "box_color": "#ffffff"}, False),
        ({"color": "#ffffff", "box_color": "#ffffff", "outline_mm": 0.3}, False),
        ({"color": "#000000", "inverted": True}, False),  # cut out of a black box
        ({"color": "#ffffff", "inverted": True}, True),  # a white box on white
    ],
)
def test_letters_in_a_box_need_another_ink_than_the_box(options, warns):
    options = dict(options)
    red = options.pop("red", False)
    label = Label(tape_mm=12, elements=[TextElement(text="Box", padding_mm=0.5, **options)])
    keys = [w.key for w in render(label, dpi=300, mono=True, red=red).warnings]
    assert (keys == ["no_contrast"]) is warns, keys


@pytest.mark.parametrize(
    "background, colour, text_warns, code_warns",
    [
        ("#c8c8c8", "#ffffff", True, True),  # white on light grey
        ("#303030", "#000000", True, True),  # black on dark grey
        ("#808080", "#000000", False, True),  # a code needs more than text
        ("#ffffff", "#000000", False, False),
        ("#000000", "#ffffff", False, False),
    ],
)
def test_low_contrast_warns_codes_more_strictly(background, colour, text_warns, code_warns):
    def warns(element):
        label = Label(tape_mm=12, background=background, elements=[element])
        return [w.key for w in render(label, dpi=300, mono=True).warnings] == ["no_contrast"]

    assert warns(TextElement(text="Text", color=colour)) is text_warns
    assert warns(CodeElement(data="x", color=colour)) is code_warns


def straddling(dark_share: float) -> Label:
    """A text over a background that is dark on its left part, light on
    the rest; the text starts at the very left."""
    img = Image.new("RGB", (1000, 100), "white")
    ImageDraw.Draw(img).rectangle((0, 0, round(1000 * dark_share), 100), fill="#101010")
    return Label(
        tape_mm=12,
        auto_length=False,
        length_mm=60,
        margin_mm=0,
        align="start",
        background_image=ImageElement(data=b64(img)),
        elements=[TextElement(text="MMMMMMMMMM", color="#808080", size_pt=20)],
    )


def test_a_straddling_text_takes_the_ink_of_most_of_its_letters():
    mostly_light = render(straddling(0.15), dpi=300, safe_mm=0, mono=True)
    right = mostly_light.image.convert("L").crop((400, 0, 600, mostly_light.image.height))
    assert right.histogram()[0] > 0  # black letters on the light part
    mostly_dark = render(straddling(0.85), dpi=300, safe_mm=0, mono=True)
    left = mostly_dark.image.convert("L").crop((50, 0, 250, mostly_dark.image.height))
    assert left.histogram()[255] > 0  # white letters on the dark part


def test_a_straddling_text_warns_when_much_of_it_is_lost():
    assert render(straddling(0.05), dpi=300, safe_mm=0, mono=True).warnings == []
    keys = [w.key for w in render(straddling(0.45), dpi=300, safe_mm=0, mono=True).warnings]
    assert keys == ["no_contrast"]


def test_a_flat_logo_saved_as_jpeg_is_a_drawing():
    logo = Image.new("RGB", (200, 120), "white")
    draw = ImageDraw.Draw(logo)
    draw.rectangle((20, 20, 180, 100), fill=(120, 120, 120))
    draw.ellipse((60, 40, 140, 80), fill=(40, 40, 40))
    buf = io.BytesIO()
    logo.save(buf, "JPEG", quality=70)
    jpeg = Image.open(io.BytesIO(buf.getvalue())).convert("L")
    assert len(set(jpeg.histogram())) > 20  # noisy tones
    assert imagefx.is_drawing(jpeg)
    photo = Image.open(io.BytesIO(base64.b64decode(gradient((256, 40))))).convert("L")
    assert not imagefx.is_drawing(photo)


def test_a_reduced_mono_text_comes_close_to_its_size():
    element = TextElement(text="Regal 12 links", frame_mm=0.3, padding_mm=1, size_pt=30)
    block = render_text(element, 1000, 300, mono=True)
    target = (round(block.width * 0.63), round(block.height * 0.63))
    small = block.info["redraw"](*target)
    assert small.width <= target[0] and small.height <= target[1]
    assert small.width > 0.97 * target[0] or small.height > 0.97 * target[1]


def test_a_thin_outline_keeps_a_dot_when_reduced():
    element = TextElement(text="O", size_pt=40, outline_mm=0.09, outline_color="#000000")
    block = render_text(element, 1000, 300, mono=True)
    small = block.info["redraw"](block.width // 4, block.height // 4)
    assert (255, 255, 255) in {p[:3] for p in pixels(small) if p[3]}  # the outline is there


def test_a_transparent_background_image_shows_the_label_colour():
    logo = Image.new("RGBA", (200, 50), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((5, 5, 45, 45), fill=(200, 0, 0, 255))
    label = Label(
        tape_mm=12,
        background="#ffff00",
        background_image=ImageElement(data=b64(logo)),
        auto_length=False,
        length_mm=48,
    )
    image = render(label, dpi=100, safe_mm=0).image
    assert image.getpixel((image.width - 3, 3)) == (255, 255, 0)
    assert render_module.background_brightness(label) > 150


def test_a_redraw_that_fails_keeps_the_first_part(monkeypatch):
    label = Label(
        tape_mm=12,
        background_image=ImageElement(data=half_dark(dark=250)),
        auto_length=False,
        length_mm=80,
        align="start",
        elements=[TextElement(text="Lo", color="#a0a0a0", size_pt=10)],
    )
    calls = []
    original = render_module.render_element

    def failing(*args, **kwargs):
        calls.append(kwargs.get("inks"))
        if len(calls) > 1:  # the redraw against the dark part
            raise RuntimeError("no font")
        return original(*args, **kwargs)

    monkeypatch.setattr(render_module, "render_element", failing)
    rendered = render(label, dpi=300, safe_mm=0, mono=True)
    assert len(calls) == 2 and calls[1].background < 128  # redrawn for the dark part
    assert greys(rendered.image) == 0


def test_an_element_is_drawn_again_in_the_inks_of_its_place(monkeypatch):
    """Over the dark part of a background image a grey text is drawn again
    with white letters at the size it was placed at."""
    label = Label(
        tape_mm=12,
        background_image=ImageElement(data=half_dark(dark=250)),
        auto_length=False,
        length_mm=80,
        align="start",
        elements=[TextElement(text="Lo", color="#a0a0a0", size_pt=10)],
    )
    calls = []
    original = render_module._redrawn

    def recording(element, part, *args):
        drawn = original(element, part, *args)
        calls.append((part.size, drawn.size))
        return drawn

    monkeypatch.setattr(render_module, "_redrawn", recording)
    rendered = render(label, dpi=300, safe_mm=0, mono=True)
    assert len(calls) == 1 and calls[0][0] == calls[0][1]  # the same size
    letters = rendered.image.convert("L").crop(
        (0, 0, rendered.image.width // 4, rendered.image.height)
    )
    assert letters.histogram()[255] > 0 and rendered.warnings == []


def test_an_image_over_a_background_image():
    label = Label(
        tape_mm=12,
        background_image=ImageElement(data=half_dark()),
        elements=[
            ImageElement(data=gradient(), border_mm=0.3, border_color="#808080"),
            TextElement(text="Box", box_color="#ffffff", padding_mm=0.5),
        ],
    )
    rendered = render(label, dpi=300, mono=True)
    assert greys(rendered.image) == 0
    assert rendered.warnings == []


def test_local_inks_and_lost_share_edge_cases():
    inks = render_module.Inks(True)
    empty = Image.new("RGB", (0, 0))
    assert render_module._local_inks(empty, None, inks) is inks
    area = Image.new("RGB", (4, 4), (10, 10, 10))
    no_ink = Image.new("L", (4, 4), 0)
    assert render_module._local_inks(area, no_ink, inks).background == pytest.approx(10)
    assert render_module._lost(area, no_ink, "#000000", 80) == 0.0
    full = Image.new("L", (4, 4), 255)
    assert render_module._lost(area, full, "#000000", 80) == 1.0
    assert render_module._lost(area, full, "#ffffff", 80) == 0.0


# --- review 4 ----------------------------------------------------------------------------


def example(name: str) -> Label:
    from pathlib import Path

    return Label.load(Path(__file__).parent.parent / "docs" / "examples" / (name + ".tavletta"))


def test_photos_and_gradients_are_no_drawings():
    from pathlib import Path

    from PIL import ImageFilter

    sunset = Image.open(Path(__file__).parent.parent / "docs/examples/sunset.png")
    assert not imagefx.is_drawing(sunset.convert("L"))
    flower = Image.open(Path(__file__).parent.parent / "docs/examples/flower.png").convert("RGBA")
    assert not imagefx.is_drawing(flower.convert("L"), flower.getchannel("A").point(imagefx.HALF))
    smooth = Image.linear_gradient("L").resize((200, 100)).point(lambda v: 64 + v // 2)
    assert not imagefx.is_drawing(smooth)
    noise = Image.effect_noise((200, 200), 60).filter(ImageFilter.GaussianBlur(4))
    assert not imagefx.is_drawing(noise)
    dark = Image.radial_gradient("L").resize((200, 200)).point(lambda v: v // 3)
    assert not imagefx.is_drawing(dark)


def test_the_sticker_flower_is_dithered_not_a_blob():
    image = render(example("sticker"), dpi=300, mono=True).image.convert("L")
    histogram = image.histogram()
    share = histogram[0] / (histogram[0] + histogram[255])
    assert share < 0.6  # dithered dots, not a black area
    # a dithered area changes from black to white often in a row
    row = image.crop((0, image.height // 2, image.width, image.height // 2 + 1)).tobytes()
    assert sum(1 for a, b in zip(row, row[1:], strict=False) if a != b) > 100


@pytest.mark.parametrize(
    "name", ["spice", "party", "sticker", "cable", "shelf", "serial", "design"]
)
def test_the_examples_have_no_warnings(name):
    label = example(name)
    for dpi in (203, 300):
        assert render(label, dpi=dpi).warnings == []
        assert render(label, dpi=dpi, mono=True).warnings == []


def test_an_outline_in_the_other_ink_is_no_lost_text():
    dark = b64(Image.new("RGB", (100, 20), (5, 5, 5)))
    label = Label(
        tape_mm=12,
        background_image=ImageElement(data=dark),
        elements=[
            TextElement(text="Out", color="#000000", outline_mm=0.3, outline_color="#ffffff")
        ],
    )
    assert render(label, dpi=300, mono=True).warnings == []


def test_black_and_red_on_background_images():
    black = b64(Image.new("RGB", (100, 20), (0, 0, 0)))
    red = b64(Image.new("RGB", (100, 20), (230, 0, 0)))
    halves = Image.new("RGB", (100, 20), (230, 0, 0))
    ImageDraw.Draw(halves).rectangle((50, 0, 100, 20), fill="white")
    cases = [
        (black, TextElement(text="Red", color="#e00000"), []),
        (black, ShapeElement(shape="rectangle", fill="solid", color="#e00000"), []),
        (black, CodeElement(data="x", color="#e00000"), []),
        (red, TextElement(text="Black", color="#000000"), []),
        (b64(halves), TextElement(text="Black on both", color="#000000"), []),
        (red, TextElement(text="Red", color="#e00000"), ["no_contrast"]),
    ]
    for background, element, expected in cases:
        label = Label(
            tape_mm=12, background_image=ImageElement(data=background), elements=[element]
        )
        keys = [w.key for w in render(label, dpi=300, mono=True, red=True).warnings]
        assert keys == expected, (element, keys)


def test_dark_letters_on_a_light_box_print_white_on_black():
    element = TextElement(
        text="Box", color="#000000", box_color="#ffeb3b", padding_mm=1, size_pt=20
    )
    label = Label(tape_mm=12, elements=[element])
    rendered = render(label, dpi=300, mono=True)
    assert rendered.warnings == []
    block = render_text(element, 1000, 300, mono=True)
    colours = {p[:3] for p in pixels(block) if p[3]}
    assert colours == {(0, 0, 0), (255, 255, 255)}  # black box, white letters


def test_a_wrapped_text_keeps_its_lines_when_reduced():
    element = TextElement(text="A long text that overflows", max_width_mm=20)
    label = Label(tape_mm=6, auto_length=False, length_mm=15, elements=[element])
    colour = ink_bbox(render(label, dpi=300, safe_mm=1).image, WHITE)
    mono = ink_bbox(render(label, dpi=300, safe_mm=1, mono=True).image, WHITE)
    colour_w, mono_w = colour[2] - colour[0], mono[2] - mono[0]
    assert mono_w > 0.85 * colour_w
    from tavletta import typeset

    lines = typeset.wrapped_lines(TextElement(text="aa bb cc"), 40, True, 80)
    assert len(lines) > 1 and " ".join(lines) == "aa bb cc"


def test_a_redraw_far_below_the_size_still_comes_close():
    element = TextElement(text="Hg", size_pt=200)
    block = render_text(element, 10000, 300, mono=True)
    target = (round(block.width * 0.05), round(block.height * 0.05))
    small = block.info["redraw"](*target)
    assert small.width <= target[0] and small.height <= target[1]
    assert small.width > 0.9 * target[0] or small.height > 0.9 * target[1]


def test_pure_red_is_a_colour_on_a_black_and_white_printer():
    part = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    assert imagefx.tones(part) == "colour"
    assert imagefx.tones(part, red=True) == "hard"


def test_a_code_partly_on_the_wrong_background_warns():
    """A code loses far less before it warns than a text."""
    img = Image.new("RGB", (1000, 200), "white")
    ImageDraw.Draw(img).rectangle((0, 0, 150, 100), fill="#101010")
    label = Label(
        tape_mm=12,
        auto_length=False,
        length_mm=60,
        margin_mm=0,
        align="start",
        background_image=ImageElement(data=b64(img)),
        elements=[CodeElement(data="https://example.org/a")],
    )
    assert [w.key for w in render(label, dpi=300, safe_mm=0, mono=True).warnings] == [
        "no_contrast"
    ]
    label.elements = [TextElement(text="MMMMMMMMMMMMMMMM", size_pt=20)]
    assert render(label, dpi=300, safe_mm=0, mono=True).warnings == []


def test_colour_emoji_keep_their_shape_on_mono():
    """A white square emoji would vanish on white: its outline shows."""
    label = Label(tape_mm=12, margin_mm=0, elements=[TextElement(text="⬜", size_pt=20)])
    image = render(label, dpi=300, safe_mm=0, mono=True).image.convert("L")
    assert image.histogram()[0] > 50
