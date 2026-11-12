"""The free layout in the window: the choice of layout, the page of an
element's position and size, a text's size in mm, the font's weights and
a QR code's error correction."""

from __future__ import annotations

import pytest
from conftest import gtk_available, pump, render_now

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402
from test_app import find, one  # noqa: E402
from test_app_design import element_row, open_page  # noqa: E402

from etiketto import freelayout  # noqa: E402
from etiketto.model import CodeElement, ImageElement, TextElement  # noqa: E402
from etiketto.render import RenderWarning  # noqa: E402
from etiketto.ui import freelayout as ui_free  # noqa: E402
from etiketto.ui import texts  # noqa: E402

QR = "QR code: \u2068100001\u2069"
BARS = "Code 128: \u206812345\u2069"


def layout_row(win) -> Adw.ComboRow:
    return one(win.elements_group, Adw.ComboRow, title="Layout")


def to_free(win) -> None:
    layout_row(win).set_selected(1)
    pump()


def test_one_after_another_by_default(window):
    assert layout_row(window).get_selected() == 0
    assert "one after another" in window.elements_group.get_description()
    assert find(element_row(window, "Label"), Adw.ActionRow, title="Position")


def test_switching_keeps_the_places(window):
    element = window.label.elements[0]
    render_now(window)
    flowing = window.rendered.image.copy()
    to_free(window)
    assert window.label.layout == "free" and window.dirty
    assert "own position" in window.elements_group.get_description()
    assert element.size_pt > 0 and (element.x_mm, element.y_mm) != (0, 0)
    render_now(window)
    placed = window.rendered.image
    # an automatic length ends after the text, laid out at the size taken
    # over (to a hundredth of a point): a dot at most, as the glyphs below
    assert placed.height == flowing.height and abs(placed.width - flowing.width) <= 1

    def ink(image):
        return image.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()

    # glyphs on exact positions in the free layout: a dot at most
    assert all(abs(a - b) <= 1 for a, b in zip(ink(placed), ink(flowing), strict=True))
    window.undo()
    assert window.label.layout == "flow"


def test_switching_again_keeps_what_was_set(window):
    element = window.label.elements[0]
    element.x_mm, element.y_mm = 3.0, 4.0
    to_free(window)
    assert (element.x_mm, element.y_mm) == (3.0, 4.0)
    layout_row(window).set_selected(0)
    pump()
    assert window.label.layout == "flow"
    layout_row(window).set_selected(0)  # the same again: nothing happens
    assert window.label.layout == "flow"


def test_position_and_size_page(window):
    element = window.label.elements[0]
    to_free(window)
    page = open_page(window, "Label", "Position and size")
    x = one(page, Adw.SpinRow, title="X")
    assert x.get_digits() == 2 and "ink" in x.get_subtitle()
    x.set_value(2.57)
    one(page, Adw.SpinRow, title="Y").set_value(14.69)
    assert (element.x_mm, element.y_mm) == (2.57, 14.69)


def test_text_size_in_mm(window):
    element = window.label.elements[0]
    to_free(window)
    size = one(element_row(window, "Label"), Adw.SpinRow, title="Size")
    assert size.get_subtitle() == "mm, the font's height"
    size.set_value(4.5)
    assert element.size_pt == pytest.approx(freelayout.mm_to_pt(4.5))


def test_text_size_in_inches(make_window):
    window = make_window({"units": "in"})
    to_free(window)
    size = one(element_row(window, "Label"), Adw.SpinRow, title="Size")
    assert size.get_digits() == 3
    size.set_value(0.25)
    assert freelayout.pt_to_mm(window.label.elements[0].size_pt) == pytest.approx(6.35)


def test_weight_row(window):
    element = window.label.elements[0]
    element.font = "Source Sans 3"
    window.rebuild_sidebar()
    weight = one(element_row(window, "Label"), Adw.ComboRow, title="Weight")
    names = [weight.get_model().get_string(i) for i in range(weight.get_model().get_n_items())]
    assert names[0] == "As the bold button sets it"
    weight.set_selected(names.index("Semibold") if "Semibold" in names else 1)
    assert element.weight in (600, 400)
    toggles = {t.get_icon_name(): t for t in find(window.sidebar, Gtk.ToggleButton)}
    toggles["format-text-bold-symbolic"].set_active(not element.bold)
    assert element.weight == 0 and weight.get_selected() == 0


def test_weights_of_a_family():
    assert ui_free.font_weights("No Such Family Anywhere") in (
        [(400, "Regular"), (700, "Bold")],
        ui_free.font_weights("Sans"),  # fontconfig may substitute
    )


def test_an_unknown_weight_is_listed(window):
    element = window.label.elements[0]
    element.weight = 650
    window.rebuild_sidebar()
    weight = one(element_row(window, "Label"), Adw.ComboRow, title="Weight")
    assert weight.get_model().get_string(weight.get_selected()) in ("650", "Semibold")


def test_code_rows(window):
    window.add_element(CodeElement(data="100001"))
    element = window.label.elements[1]
    row = element_row(window, QR)
    correction = one(row, Adw.ComboRow, title="Error correction")
    assert correction.get_visible()
    correction.set_selected(1)
    assert element.error_correction == "L"
    one(row, Adw.ComboRow, title="Type").set_selected(1)
    assert not correction.get_visible()
    element.symbology = "qr"
    to_free(window)
    page = open_page(window, QR, "Position and size")
    module = one(page, Adw.SpinRow, title="Module size")
    module.set_value(0.67)
    assert element.module_mm == 0.67
    assert not find(page, Adw.SpinRow, title="Bar height")
    one(page, Adw.ComboRow, title="Rotation").set_selected(2)
    assert element.rotation == 180


def test_barcode_and_image_rows(window, png_b64):
    window.add_element(CodeElement(data="12345", symbology="code128"))
    window.add_element(ImageElement(data=png_b64(), name="logo.png"))
    to_free(window)
    page = open_page(window, BARS, "Position and size")
    one(page, Adw.SpinRow, title="Bar height").set_value(6)
    assert window.label.elements[1].height_mm == 6
    window.sidebar_nav.pop()
    pump()
    page = open_page(window, "logo.png", "Position and size")
    one(page, Adw.SpinRow, title="Height").set_value(8)
    assert window.label.elements[2].height_mm == 8


def test_outside_warning_text():
    text = texts.warning_text(RenderWarning("outside_label", {"n": 2}))
    assert text.startswith("Element 2 reaches beyond")


def test_free_label_in_the_preview(window):
    window.label.elements[0] = TextElement(text="X", size_pt=10, x_mm=1, y_mm=200)
    to_free(window)
    render_now(window)
    assert RenderWarning("outside_label", {"n": 1}) in window.rendered.warnings
