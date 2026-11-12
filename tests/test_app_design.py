"""The design options in the window: the rows of text, image, shape and
border, the crop dialog, undo and redo, the element shortcuts and the
shortcuts window."""

from __future__ import annotations

import base64
import io

import pytest
from conftest import gtk_available, pump, render_now

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402
from PIL import Image  # noqa: E402
from test_app import find, one  # noqa: E402

from etiketto.model import (  # noqa: E402
    CodeElement,
    ImageElement,
    Label,
    ShapeElement,
    TextElement,
)
from etiketto.ui import shortcuts  # noqa: E402
from etiketto.ui.image_crop import CropDialog, crop_text  # noqa: E402


def png(size=(40, 20), color="red") -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def element_row(win, title: str) -> Adw.ExpanderRow:
    return one(win.elements_group, Adw.ExpanderRow, title=title)


def activate(win, name: str) -> None:
    win.activate_action("win." + name, None)
    pump()


def enabled(win, name: str) -> bool:
    return win.lookup_action(name).get_enabled()


# --- text ---------------------------------------------------------------------------


def test_text_style_rows(window):
    element = window.label.elements[0]
    toggles = {t.get_icon_name(): t for t in find(window.sidebar, Gtk.ToggleButton)}
    toggles["format-text-underline-symbolic"].set_active(True)
    toggles["format-text-strikethrough-symbolic"].set_active(True)
    assert (element.underline, element.strikethrough) == (True, True)
    assert toggles["format-text-underline-symbolic"].get_tooltip_text() == "Underline"
    one(window.sidebar, Adw.SpinRow, title="Letter spacing").set_value(20)
    one(window.sidebar, Adw.SpinRow, title="Line spacing").set_value(1.5)
    one(window.sidebar, Adw.SwitchRow, title="Shrink to fit").set_active(True)
    one(window.sidebar, Adw.SpinRow, title="Smallest size").set_value(6)
    one(window.sidebar, Adw.SpinRow, title="Wrap width").set_value(30)
    assert (element.letter_spacing, element.line_spacing) == (20, 1.5)
    assert (element.shrink, element.min_size_pt, element.max_width_mm) == (True, 6, 30)
    rotation = [r for r in find(window.sidebar, Adw.ComboRow) if r.get_title() == "Rotation"]
    rotation[0].set_selected(1)
    assert element.rotation == 90
    render_now(window)
    assert window.rendered is not None


def test_text_box_and_effect_rows(window):
    element = window.label.elements[0]
    one(window.sidebar, Adw.SwitchRow, title="White on black").set_active(True)
    assert element.inverted
    box = one(window.sidebar, Adw.ActionRow, title="Background box")
    colour = one(box, Gtk.ColorDialogButton)
    assert not colour.get_sensitive()
    one(box, Gtk.Switch).set_active(True)
    assert element.box_color == "#ffff00" and colour.get_sensitive()
    one(box, Gtk.Switch).set_active(False)
    assert element.box_color == ""
    for title, attr in (
        ("Padding", "padding_mm"),
        ("Frame", "frame_mm"),
        ("Outline", "outline_mm"),
        ("Shadow", "shadow_mm"),
    ):
        one(window.sidebar, Adw.SpinRow, title=title).set_value(1)
        assert getattr(element, attr) == 1, attr
    row = element_row(window, "Label")
    one(row, Adw.SpinRow, title="Corner radius").set_value(2)
    assert element.corner_mm == 2


def test_position_rows(window):
    element = window.label.elements[0]
    one(window.sidebar, Adw.ComboRow, title="Vertical alignment").set_selected(0)
    assert element.valign == "top"
    gap = one(window.sidebar, Adw.SpinRow, title="Space after it")
    assert not gap.get_sensitive()
    one(window.sidebar, Adw.SwitchRow, title="Own spacing").set_active(True)
    assert element.gap_mm == 2.0 and gap.get_sensitive()
    gap.set_value(5)
    assert element.gap_mm == 5
    one(window.sidebar, Adw.SwitchRow, title="Own spacing").set_active(False)
    assert element.gap_mm is None


# --- images --------------------------------------------------------------------------


def test_image_rows(window):
    window.add_element(ImageElement(data=png(), name="logo.png"))
    element = window.label.elements[1]
    for title, attr, value in (
        ("Brightness", "brightness", 20),
        ("Contrast", "contrast", -10),
        ("Gamma", "gamma", 1.5),
    ):
        one(window.sidebar, Adw.SpinRow, title=title).set_value(value)
        assert getattr(element, attr) == value
    for title, attr in (("Invert", "invert"), ("Mirror", "mirror"), ("Flip", "flip")):
        one(window.sidebar, Adw.SwitchRow, title=title).set_active(True)
        assert getattr(element, attr)
    level = one(window.sidebar, Adw.SpinRow, title="Threshold")
    assert not level.get_sensitive()  # automatic
    one(window.sidebar, Adw.ComboRow, title="Method").set_selected(1)
    assert element.mode == "threshold" and level.get_sensitive()
    level.set_value(90)
    assert element.threshold == 90
    row = element_row(window, "logo.png")
    one(row, Adw.SpinRow, title="Border").set_value(0.5)
    one(row, Adw.SpinRow, title="Corner radius").set_value(2)
    assert (element.border_mm, element.corner_mm) == (0.5, 2)
    render_now(window)
    assert window.rendered is not None and not window.rendered.warnings


def test_crop_dialog(window):
    window.add_element(ImageElement(data=png((100, 50)), name="logo.png"))
    element = window.label.elements[1]
    crop_row = one(window.sidebar, Adw.ActionRow, title="Crop")
    assert crop_row.get_subtitle() == "Whole image"
    button = [b for b in find(crop_row, Gtk.Button) if b.get_label() == "Crop…"][0]
    button.emit("clicked")
    assert pump(lambda: isinstance(window.last_dialog, CropDialog))
    dialog = window.last_dialog
    dialog.view.set_crop((0.25, 0.0, 0.5, 1.0))  # as dragged
    dialog.view.on_change(dialog.view.crop, True)
    assert dialog.spins[0].get_value() == 25 and dialog.spins[2].get_value() == 50
    dialog.spins[1].set_value(10)  # typed: the top, the height shrinks to fit
    assert dialog.crop == pytest.approx([0.25, 0.1, 0.5, 0.9])
    dialog.apply()
    assert element.crop == pytest.approx([0.25, 0.1, 0.5, 0.9])
    assert crop_row.get_subtitle() == crop_text(element.crop)
    whole = one(crop_row, Gtk.Button, tooltip="Whole image")
    whole.emit("clicked")
    assert element.crop == [0.0, 0.0, 1.0, 1.0]


def test_crop_text():
    assert crop_text([0.0, 0.0, 1.0, 1.0]) == "Whole image"
    assert crop_text([0.1, 0.2, 0.5, 0.6]) == "50 × 60 %, from 10 %, 20 %"


# --- shapes and the border -------------------------------------------------------------


def test_shape_rows_follow_the_shape(window):
    activate(window, "add-shape")
    element = window.label.elements[-1]
    assert isinstance(element, ShapeElement)
    row = element_row(window, "Line")
    arrows = one(row, Adw.ComboRow, title="Arrows")
    fill = one(row, Adw.ComboRow, title="Fill")
    assert arrows.get_visible() and not fill.get_visible()
    one(row, Adw.ComboRow, title="Shape").set_selected(1)
    assert element.shape == "rectangle"
    assert fill.get_visible() and not arrows.get_visible()
    fill.set_selected(2)
    assert element.fill == "hatched"
    one(row, Adw.SpinRow, title="Line width").set_value(1)
    one(row, Adw.SpinRow, title="Length").set_value(20)
    assert (element.line_mm, element.width_mm) == (1, 20)
    render_now(window)
    assert window.rendered is not None
    assert row.get_title() == "Rectangle"


def test_border_style_rows(window):
    one(window.sidebar, Adw.ComboRow, title="Line style").set_selected(3)
    assert window.label.border_style == "double"
    one(window.sidebar, Adw.SpinRow, title="Inset").set_value(1)
    assert window.label.border_inset_mm == 1
    label_group = window.label_group
    one(label_group, Adw.SpinRow, title="Corner radius").set_value(3)
    assert window.label.border_radius_mm == 3


# --- undo and redo -------------------------------------------------------------------


def test_undo_and_redo_of_typing(window):
    assert not enabled(window, "undo") and not enabled(window, "redo")
    view = one(window.sidebar, Gtk.TextView)
    for text in ("A", "AB", "ABC"):
        view.get_buffer().set_text(text)
    assert enabled(window, "undo")
    activate(window, "undo")
    assert window.label.elements[0].text == "Label"  # the typing was one step
    assert enabled(window, "redo") and not enabled(window, "undo")
    assert one(window.sidebar, Gtk.TextView).get_buffer().get_property("text") == "Label"
    activate(window, "redo")
    assert window.label.elements[0].text == "ABC"


def test_undo_of_structure_changes(window):
    activate(window, "add-code")
    activate(window, "add-shape")
    assert len(window.label.elements) == 3
    activate(window, "undo")
    assert [type(e) for e in window.label.elements] == [TextElement, CodeElement]
    activate(window, "undo")
    assert len(window.label.elements) == 1
    window.change(margin_mm=5)
    assert not enabled(window, "redo")  # a new change drops what was undone
    activate(window, "undo")
    assert window.label.margin_mm == 2


def test_undo_does_not_take_back_the_printed_counter(window):
    window.change(margin_mm=5)
    window.set_counter_next(17)
    activate(window, "undo")
    assert window.label.margin_mm == 2 and window.label.counter_next == 17


def test_a_new_label_has_no_history(window):
    window.change(margin_mm=5)
    window.set_label(Label(), None)
    assert not enabled(window, "undo")


def test_undo_follows_the_printer_and_data_file(window, tmp_path):
    window.set_backend("cab")
    assert window.label.printer == "cab"
    activate(window, "undo")
    assert window.label.printer == "vc500w"
    rows = tmp_path / "rows.csv"
    rows.write_text("Name\nA\n", encoding="utf-8")
    window.data.attach(rows)
    assert pump(lambda: window.data.columns == ["Name"])
    activate(window, "undo")
    assert window.label.data_file == ""
    assert pump(lambda: not window.data.active)


# --- the open element -------------------------------------------------------------------


def test_expanding_a_row_makes_it_the_open_element(window):
    activate(window, "add-code")
    first = element_row(window, "Label")
    first.set_expanded(True)
    assert window.expand_index == 0
    first.set_expanded(False)
    assert window.expand_index == -1


def test_duplicate_remove_and_move_the_open_element(window):
    activate(window, "duplicate-element")  # nothing open: nothing happens
    assert len(window.label.elements) == 1
    window.expand_index = 0
    activate(window, "duplicate-element")
    assert [e.text for e in window.label.elements] == ["Label", "Label"]
    assert window.label.elements[0] is not window.label.elements[1]
    assert window.expand_index == 1
    window.label.elements[1].text = "Second"
    activate(window, "element-up")
    assert [e.text for e in window.label.elements] == ["Second", "Label"]
    assert window.expand_index == 0
    activate(window, "element-up")  # already first
    assert window.expand_index == 0
    activate(window, "element-down")
    assert [e.text for e in window.label.elements] == ["Label", "Second"]
    activate(window, "remove-element")
    assert [e.text for e in window.label.elements] == ["Label"]
    activate(window, "remove-element")  # nothing open any more
    assert len(window.label.elements) == 1


def test_editing_shortcuts_are_bound_in_the_bubble_phase(window):
    controllers = [
        c
        for c in window.observe_controllers()
        if isinstance(c, Gtk.ShortcutController)
        and c.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE
    ]
    bound = {}
    for controller in controllers:
        for shortcut in controller:
            action = shortcut.get_action()
            if isinstance(action, Gtk.NamedAction):
                bound[action.get_action_name()] = {
                    Gtk.accelerator_parse(part)
                    for part in shortcut.get_trigger().to_string().split("|")
                }
    for action, accels in shortcuts.EDITING:
        assert bound[action] == {Gtk.accelerator_parse(accel) for accel in accels}


def test_add_shortcuts_add_elements(window):
    activate(window, "add-text")
    assert isinstance(window.label.elements[-1], TextElement)
    assert window.expand_index == 1


def test_shortcuts_window(window):
    if not shortcuts.available():
        pytest.skip("libadwaita without a shortcuts window")
    activate(window, "shortcuts")
    assert isinstance(window.last_dialog, Adw.ShortcutsDialog)
    titles = [title for _heading, items in shortcuts.sections() for title, _accel in items]
    assert "Undo" in titles and "Duplicate the open element" in titles
    for _heading, items in shortcuts.sections():
        for _title, accel in items:
            assert Gtk.accelerator_parse(accel)[0], accel
    window.last_dialog.force_close()


def test_undo_buttons_in_the_header(window):
    buttons = [b for b in find(window, Gtk.Button) if b.get_action_name() == "win.undo"]
    assert len(buttons) == 1 and buttons[0].get_tooltip_text() == "Undo"
