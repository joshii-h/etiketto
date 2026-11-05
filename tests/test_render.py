import pytest

from zinklabel.model import CodeElement, Label, TextElement
from zinklabel.render import MIN_LENGTH_MM, mm_to_px, print_image, render


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
