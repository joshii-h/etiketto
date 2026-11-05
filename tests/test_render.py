import pytest

from etiketto.model import CodeElement, Label, TextElement
from etiketto.render import MIN_LENGTH_MM, mm_to_px, print_image, render


@pytest.mark.parametrize("tape", [9, 12, 19, 25, 50])
def test_height_matches_tape(tape):
    rendered = render(Label(tape_mm=tape, elements=[TextElement(text="Test")]))
    assert rendered.image.height == mm_to_px(tape)


def test_auto_length_grows_with_text():
    short = render(Label(elements=[TextElement(text="A")]))
    long = render(Label(elements=[TextElement(text="A much longer label text")]))
    assert long.length_mm > short.length_mm
    assert short.length_mm >= MIN_LENGTH_MM


def test_fixed_length_and_overflow():
    rendered = render(
        Label(tape_mm=25, auto_length=False, length_mm=40, elements=[TextElement(text="x" * 60)])
    )
    assert rendered.image.width == mm_to_px(40)
    assert rendered.overflow


def test_fixed_size_text():
    small = render(Label(elements=[TextElement(text="Hi", size_pt=12)]))
    big = render(Label(elements=[TextElement(text="Hi")]))
    assert small.length_mm < big.length_mm


def test_codes_render():
    for code in (
        CodeElement(data="https://example.org"),
        CodeElement(data="ABC-123", symbology="code128"),
        CodeElement(data="4006381333931", symbology="ean13"),
    ):
        assert not render(Label(elements=[code])).warnings


def test_invalid_code_gives_warning_not_crash():
    rendered = render(Label(elements=[CodeElement(data="abc", symbology="ean13")]))
    assert rendered.warnings


def test_empty_label_has_minimum_length():
    assert render(Label(elements=[])).length_mm >= MIN_LENGTH_MM


def test_print_orientation():
    rendered = render(Label(tape_mm=19, elements=[TextElement(text="Kabel")]))
    out = print_image(rendered)
    assert out.width == rendered.image.height
    assert out.height == rendered.image.width


def test_border_is_inset_from_the_edges():
    rendered = render(
        Label(tape_mm=25, border_mm=0.5, border_color="#ff0000", elements=[]), safe_mm=1.5
    )
    img = rendered.image
    inset = mm_to_px(1.5)
    assert img.getpixel((img.width // 2, 0)) == (255, 255, 255)  # tape edge stays free
    assert img.getpixel((img.width // 2, inset + 1)) == (255, 0, 0)  # border inside
    assert img.getpixel((inset + 1, img.height // 2)) == (255, 0, 0)


def test_background_is_edge_to_edge_and_content_respects_safe_margin():
    label = Label(
        tape_mm=25, background="#00ff00", margin_mm=0, elements=[TextElement(text="Wide")]
    )
    img = render(label, safe_mm=2).image
    inset = mm_to_px(2) - 1
    green = (0, 255, 0)
    for x in range(0, img.width, 7):
        assert img.getpixel((x, 0)) == green and img.getpixel((x, inset)) == green
        assert img.getpixel((x, img.height - 1 - inset)) == green
    for y in range(0, img.height, 7):
        assert (
            img.getpixel((inset, y)) == green and img.getpixel((img.width - 1 - inset, y)) == green
        )


def test_trailing_newline_does_not_shrink_text():
    plain = render(Label(elements=[TextElement(text="Hello world")]))
    newline = render(Label(elements=[TextElement(text="Hello world\n")]))
    assert abs(plain.length_mm - newline.length_mm) < 1
