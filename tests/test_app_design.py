"""The design options in the window: the rows and pages of text, image,
shape and border, the crop dialog, undo and redo, the element shortcuts
and the shortcuts window."""

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
from etiketto.render import RenderWarning  # noqa: E402
from etiketto.ui import shortcuts, texts  # noqa: E402
from etiketto.ui.image_crop import CropDialog, crop_text  # noqa: E402


def png(size=(40, 20), color="red") -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def element_row(win, title: str) -> Adw.ExpanderRow:
    return one(win.elements_group, Adw.ExpanderRow, title=title)


def open_page(win, element_title: str, page: str) -> Adw.NavigationPage:
    """Open a page of further options of an element; the page shown."""
    one(element_row(win, element_title), Adw.ActionRow, title=page).emit("activated")
    pump()
    shown = win.sidebar_nav.get_visible_page()
    assert shown.get_title() == page
    return shown


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
    page = open_page(window, "Label", "Spacing and fitting")
    one(page, Adw.SpinRow, title="Letter spacing").set_value(20)
    one(page, Adw.SpinRow, title="Line spacing").set_value(1.5)
    one(page, Adw.SwitchRow, title="Shrink to fit").set_active(True)
    one(page, Adw.SpinRow, title="Smallest size").set_value(6)
    one(page, Adw.SpinRow, title="Wrap width").set_value(30)
    assert (element.letter_spacing, element.line_spacing) == (20, 1.5)
    assert (element.shrink, element.min_size_pt, element.max_width_mm) == (True, 6, 30)
    one(page, Adw.ComboRow, title="Rotation").set_selected(1)
    assert element.rotation == 90
    render_now(window)
    assert window.rendered is not None


def test_the_page_names_the_element_and_goes_back(window):
    page = open_page(window, "Label", "Box and frame")
    title = one(page, Adw.WindowTitle)
    assert (title.get_title(), title.get_subtitle()) == ("Box and frame", "Label")
    window.sidebar_nav.pop()
    pump()
    assert window.sidebar_nav.get_visible_page() is window.sidebar_root
    old = open_page(window, "Label", "Box and frame")
    window.rebuild_sidebar()  # its rows belong to the old sidebar: opened again
    shown = window.sidebar_nav.get_visible_page()
    assert shown is not old and shown.get_title() == "Box and frame"
    window.remove_element(0)  # the element is gone: back to the sidebar
    assert window.sidebar_nav.get_visible_page() is window.sidebar_root


def test_no_expander_inside_an_expander(window):
    for row in find(window.sidebar, Adw.ExpanderRow):
        assert not find(row, Adw.ExpanderRow), row.get_title()


def test_text_box_and_effect_rows(window):
    element = window.label.elements[0]
    page = open_page(window, "Label", "Box and frame")
    one(page, Adw.SwitchRow, title="White on black").set_active(True)
    assert element.inverted
    box = one(page, Adw.ActionRow, title="Background box")
    colour = one(box, Gtk.ColorDialogButton)
    assert colour.get_tooltip_text() == "Colour of the background box"
    assert not colour.get_sensitive()
    one(box, Gtk.Switch).set_active(True)
    assert element.box_color == "#ffff00" and colour.get_sensitive()
    one(box, Gtk.Switch).set_active(False)
    assert element.box_color == ""
    for title, attr in (("Padding", "padding_mm"), ("Frame", "frame_mm")):
        one(page, Adw.SpinRow, title=title).set_value(1)
        assert getattr(element, attr) == 1, attr
    one(page, Adw.SpinRow, title="Corner radius").set_value(2)
    assert element.corner_mm == 2
    window.sidebar_nav.pop()
    page = open_page(window, "Label", "Outline and shadow")
    for title, attr, tip in (
        ("Outline", "outline_mm", "Colour of the outline"),
        ("Shadow", "shadow_mm", "Colour of the shadow"),
    ):
        row = one(page, Adw.SpinRow, title=title)
        row.set_value(1)
        assert getattr(element, attr) == 1, attr
        assert one(row, Gtk.ColorDialogButton).get_tooltip_text() == tip


def test_position_rows(window):
    element = window.label.elements[0]
    page = open_page(window, "Label", "Position")
    one(page, Adw.ComboRow, title="Vertical alignment").set_selected(0)
    assert element.valign == "top"
    gap = one(page, Adw.SpinRow, title="Space after it")
    assert not gap.get_sensitive()
    one(page, Adw.SwitchRow, title="Own spacing").set_active(True)
    assert element.gap_mm == 2.0 and gap.get_sensitive()
    gap.set_value(5)
    assert element.gap_mm == 5
    one(page, Adw.SwitchRow, title="Own spacing").set_active(False)
    assert element.gap_mm is None


# --- images --------------------------------------------------------------------------


def test_image_rows(window):
    window.add_element(ImageElement(data=png(), name="logo.png"))
    element = window.label.elements[1]
    page = open_page(window, "logo.png", "Adjust")
    for title, attr, value in (
        ("Brightness", "brightness", 20),
        ("Contrast", "contrast", -10),
        ("Gamma", "gamma", 1.5),
    ):
        one(page, Adw.SpinRow, title=title).set_value(value)
        assert getattr(element, attr) == value
    for title, attr in (("Invert", "invert"), ("Mirror", "mirror"), ("Flip", "flip")):
        one(page, Adw.SwitchRow, title=title).set_active(True)
        assert getattr(element, attr)
    window.sidebar_nav.pop()
    page = open_page(window, "logo.png", "Black and white")
    level = one(page, Adw.SpinRow, title="Threshold")
    assert not level.get_sensitive()  # automatic
    one(page, Adw.ComboRow, title="Method").set_selected(1)
    assert element.mode == "threshold" and level.get_sensitive()
    level.set_value(90)
    assert element.threshold == 90
    window.sidebar_nav.pop()
    page = open_page(window, "logo.png", "Corners and border")
    border = one(page, Adw.SpinRow, title="Border")
    border.set_value(0.5)
    assert one(border, Gtk.ColorDialogButton).get_tooltip_text() == "Colour of the border"
    one(page, Adw.SpinRow, title="Corner radius").set_value(2)
    assert (element.border_mm, element.corner_mm) == (0.5, 2)
    render_now(window)
    assert window.rendered is not None and not window.rendered.warnings


def test_a_new_image_resets_the_options_of_the_old_one(window, monkeypatch):
    window.add_element(ImageElement(data=png(), name="old.png", rotation=90, brightness=20))
    element = window.label.elements[1]
    monkeypatch.setattr(
        window.files, "choose_image", lambda then: then(ImageElement(data=png(), name="new.png"))
    )
    button = [
        b for b in find(element_row(window, "old.png"), Gtk.Button) if b.get_label() == "Choose…"
    ]
    button[0].emit("clicked")
    assert (element.name, element.rotation, element.brightness) == ("new.png", 0, 0.0)
    assert [t.get_title() for t in window.toast_log] == [
        "Crop, turn and adjustments reset for the new image"
    ]
    button = [
        b for b in find(element_row(window, "new.png"), Gtk.Button) if b.get_label() == "Choose…"
    ]
    button[0].emit("clicked")
    assert len(window.toast_log) == 1  # nothing to reset


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
    dialog.spins[1].set_value(10)  # typed: the top, the height gives way
    assert dialog.crop == pytest.approx([0.25, 0.1, 0.5, 0.9])
    assert dialog.spins[3].get_value() == 90  # the field shows it
    dialog.apply()
    assert element.crop == pytest.approx([0.25, 0.1, 0.5, 0.9])
    assert crop_row.get_subtitle() == crop_text(element.crop)
    whole = one(crop_row, Gtk.Button, tooltip="Whole image")
    whole.emit("clicked")
    assert element.crop == [0.0, 0.0, 1.0, 1.0]


def test_crop_dialog_with_a_damaged_image(window):
    window.add_element(ImageElement(data="bm90IGFuIGltYWdl", name="broken.png"))
    crop_row = one(window.sidebar, Adw.ActionRow, title="Crop")
    [b for b in find(crop_row, Gtk.Button) if b.get_label() == "Crop…"][0].emit("clicked")
    pump()
    assert not isinstance(window.last_dialog, CropDialog)
    assert [t.get_title() for t in window.toast_log] == ["The image cannot be read"]


def test_crop_text():
    assert crop_text([0.0, 0.0, 1.0, 1.0]) == "Whole image"
    assert crop_text([0.1, 0.2, 0.5, 0.6]) == "50 × 60 %, from 10 %, 20 %"


# --- shapes and the border -------------------------------------------------------------


def visible_titles(row) -> set[str]:
    return {r.get_title() for r in find(row, Adw.PreferencesRow) if r.get_visible()}


def test_shape_rows_follow_the_shape_and_direction(window):
    activate(window, "add-shape")
    element = window.label.elements[-1]
    assert isinstance(element, ShapeElement)
    row = element_row(window, "Line")
    shown = visible_titles(row)
    assert {"Arrows", "Length", "Direction"} <= shown
    assert "Height" not in shown and "Fill" not in shown  # a horizontal line
    one(row, Adw.ComboRow, title="Direction").set_selected(1)
    assert element.direction == "vertical"
    shown = visible_titles(row)
    assert "Height" in shown and "Length" not in shown
    one(row, Adw.ComboRow, title="Direction").set_selected(2)
    assert {"Height", "Length"} <= visible_titles(row)
    one(row, Adw.ComboRow, title="Shape").set_selected(3)
    assert element.shape == "separator"
    assert not {"Height", "Length", "Arrows"} & visible_titles(row)
    one(row, Adw.ComboRow, title="Shape").set_selected(1)
    assert element.shape == "rectangle"
    fill = one(row, Adw.ComboRow, title="Fill")
    assert fill.get_visible() and not one(row, Adw.ComboRow, title="Arrows").get_visible()
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
    one(window.label_group, Adw.SpinRow, title="Corner radius").set_value(3)
    assert window.label.border_radius_mm == 3


def test_contexts_for_none_and_solid():
    assert dict(texts.FILLS)["none"] == "None"
    assert dict(texts.FILLS)["solid"] == "Solid"
    assert dict(texts.LINE_STYLES)["solid"] == "Solid"
    assert dict(texts.ARROWS)["none"] == "None"


def test_new_warning_texts():
    assert texts.warning_text(RenderWarning("no_contrast", {"n": 2})).startswith(
        "Element 2: a black-and-white printer prints it in the colour of the background"
    )
    assert texts.warning_text(RenderWarning("shape_invisible", {"n": 1})) == (
        "Element 1: the shape has neither a line nor a fill"
    )


# --- undo and redo -------------------------------------------------------------------


def test_undo_and_redo_of_typing(window):
    assert not enabled(window, "undo") and not enabled(window, "redo")
    view = one(window.sidebar, Gtk.TextView)
    assert not view.get_buffer().get_enable_undo()  # the window's undo counts
    for text in ("A", "AB", "ABC"):
        view.get_buffer().set_text(text)
    assert enabled(window, "undo")
    view.grab_focus()
    activate(window, "undo")
    assert window.label.elements[0].text == "Label"  # the typing was one step
    assert enabled(window, "redo") and not enabled(window, "undo")
    view = one(window.sidebar, Gtk.TextView)
    assert view.get_buffer().get_property("text") == "Label"
    assert window.get_focus() is view  # the focus came back to the field
    activate(window, "redo")
    assert window.label.elements[0].text == "ABC"


def test_switches_and_choices_are_steps_of_their_own(window):
    toggles = {t.get_icon_name(): t for t in find(window.sidebar, Gtk.ToggleButton)}
    toggles["format-text-italic-symbolic"].set_active(True)
    toggles["format-text-underline-symbolic"].set_active(True)
    activate(window, "undo")
    assert (window.label.elements[0].italic, window.label.elements[0].underline) == (True, False)


def test_undo_back_to_the_saved_state_is_clean(window, tmp_path):
    path = tmp_path / "saved.etiketto"
    window.files.write(path)
    assert not window.dirty
    window.change(margin_mm=5)
    assert window.dirty
    activate(window, "undo")
    assert not window.dirty and not window.window_title.get_subtitle().startswith("•")
    activate(window, "redo")
    assert window.dirty


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


def test_undo_of_the_data_sheet_reads_it_again(window, monkeypatch):
    loads = []
    window.label.data_file = "/x/rows.ods"
    window.history.reset(window.label)
    monkeypatch.setattr(window.data, "load", lambda: loads.append(window.label.data_sheet))
    window.label.data_sheet = "Second"
    window.mark_changed()
    activate(window, "undo")
    assert window.label.data_sheet == "" and loads == [""]


# --- the open element -------------------------------------------------------------------


def test_expanding_a_row_makes_it_the_open_element(window):
    activate(window, "add-code")
    window.expand_index = -1
    window.rebuild_sidebar()
    first = element_row(window, "Label")
    second = element_row(window, "QR code: ⁨https://⁩")
    first.set_expanded(True)
    assert window.expand_index == 0
    second.set_expanded(True)
    assert window.expand_index == 1
    second.set_expanded(False)
    assert window.expand_index == 0  # the other one is still open
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


def test_editing_shortcuts_do_nothing_behind_a_dialog(window):
    window.expand_index = 0
    dialog = Adw.AlertDialog(heading="x")
    dialog.add_response("ok", "OK")
    window.present_dialog(dialog)
    pump()
    activate(window, "remove-element")
    activate(window, "add-text")
    assert len(window.label.elements) == 1
    dialog.force_close()
    pump()
    activate(window, "remove-element")
    assert window.label.elements == []


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
    assert enabled(window, "shortcuts")
    activate(window, "shortcuts")
    assert isinstance(window.last_dialog, Adw.ShortcutsDialog)
    titles = [title for _heading, items in shortcuts.sections() for title, _accel in items]
    assert "Undo" in titles and "Duplicate the open element" in titles
    for _heading, items in shortcuts.sections():
        for _title, accel in items:
            assert Gtk.accelerator_parse(accel)[0], accel
    window.last_dialog.force_close()


def test_without_a_shortcuts_window_the_action_is_off(make_window, gtk_app, monkeypatch):
    monkeypatch.setattr(shortcuts, "available", lambda: False)
    accels = []
    original = gtk_app.set_accels_for_action
    monkeypatch.setattr(
        gtk_app,
        "set_accels_for_action",
        lambda name, keys: (accels.append(name), original(name, keys)),
    )
    win = make_window()
    assert not enabled(win, "shortcuts")
    assert "win.shortcuts" not in accels and "win.open" in accels


def test_undo_buttons_in_the_header(window):
    buttons = [b for b in find(window, Gtk.Button) if b.get_action_name() == "win.undo"]
    assert len(buttons) == 1 and buttons[0].get_tooltip_text() == "Undo"


# --- review 2 ---------------------------------------------------------------------------


def test_undo_with_the_focus_on_other_widgets(window):
    """A spin row, a switch on a page of options, the page itself: undo
    works and leaves the history consistent."""
    window.change(margin_mm=5)
    spin = one(window.sidebar, Adw.SpinRow, title="Size")
    spin.grab_focus()
    activate(window, "undo")
    assert window.label.margin_mm == 2
    activate(window, "redo")
    assert window.label.margin_mm == 5
    page = open_page(window, "Label", "Box and frame")
    switch = one(page, Adw.SwitchRow, title="White on black")
    switch.set_active(True)
    switch.grab_focus()
    activate(window, "undo")
    assert not window.label.elements[0].inverted
    assert window.sidebar_nav.get_visible_page().get_title() == "Box and frame"
    window.sidebar.grab_focus()
    activate(window, "undo")
    assert window.label.margin_mm == 2 and not enabled(window, "undo")


def test_undo_keeps_the_cursor_in_its_place(window):
    view = one(window.sidebar, Gtk.TextView)
    view.get_buffer().set_text("Hello world")
    window.change(margin_mm=4)
    view = one(window.sidebar, Gtk.TextView)
    buffer = view.get_buffer()
    buffer.place_cursor(buffer.get_iter_at_offset(5))
    view.grab_focus()
    activate(window, "undo")
    view = one(window.sidebar, Gtk.TextView)
    buffer = view.get_buffer()
    assert window.get_focus() is view
    assert buffer.get_iter_at_mark(buffer.get_insert()).get_offset() == 5


def test_no_field_has_its_own_undo(window):
    open_page(window, "Label", "Spacing and fitting")
    texts_found = find(window.sidebar_nav, Gtk.Text)
    assert texts_found and not any(t.get_enable_undo() for t in texts_found)
    assert not any(v.get_buffer().get_enable_undo() for v in find(window.sidebar, Gtk.TextView))


def test_the_page_header_shows_the_title_as_it_is(window):
    window.label.elements[0].text = "A & B <x>"
    window.rebuild_sidebar()
    page = open_page(window, "A &amp; B &lt;x&gt;", "Position")
    assert one(page, Adw.WindowTitle).get_subtitle() == "A & B <x>"


def test_undo_stays_on_the_open_page(window):
    page = open_page(window, "Label", "Outline and shadow")
    one(page, Adw.SpinRow, title="Outline").set_value(0.5)
    activate(window, "undo")
    shown = window.sidebar_nav.get_visible_page()
    assert shown.get_title() == "Outline and shadow"
    assert one(shown, Adw.SpinRow, title="Outline").get_value() == 0


def test_a_printed_counter_keeps_the_label_unsaved(window, tmp_path):
    window.files.write(tmp_path / "a.etiketto")
    window.change(margin_mm=5)
    window.set_counter_next(9)
    activate(window, "undo")
    assert window.dirty  # the counter moved on and is not saved


def test_an_unsaved_shipping_label_stays_unsaved(window):
    window.set_label(Label(), None)
    window.dirty = window.dirty_outside = True  # as _use_shipping_label leaves it
    window.change(margin_mm=5)
    activate(window, "undo")
    assert window.dirty


def test_rows_without_effect_are_off(window):
    element = window.label.elements[0]
    page = open_page(window, "Label", "Spacing and fitting")
    smallest = one(page, Adw.SpinRow, title="Smallest size")
    assert not smallest.get_sensitive()
    one(page, Adw.SwitchRow, title="Shrink to fit").set_active(True)
    assert smallest.get_sensitive()
    window.sidebar_nav.pop()
    page = open_page(window, "Label", "Box and frame")
    padding = one(page, Adw.SpinRow, title="Padding")
    assert not padding.get_sensitive() and padding.get_subtitle() == "mm, with a box or frame"
    one(page, Adw.SwitchRow, title="White on black").set_active(True)
    assert padding.get_sensitive()
    window.sidebar_nav.pop()
    page = open_page(window, "Label", "Outline and shadow")
    outline = one(page, Adw.SpinRow, title="Outline")
    colour = one(outline, Gtk.ColorDialogButton)
    assert not colour.get_sensitive()
    outline.set_value(0.3)
    assert colour.get_sensitive() and element.outline_mm == 0.3


def test_the_border_style_needs_a_border(window):
    style = one(window.label_group, Adw.ExpanderRow, title="Border style")
    assert not style.get_sensitive() and style.get_subtitle() == "Set a border width first"
    one(window.label_group, Adw.SpinRow, title="Border").set_value(0.5)
    assert style.get_sensitive()
    assert style.get_subtitle() == "Corners, line and distance from the edge"


# --- review 3 ---------------------------------------------------------------------------


def page_header(win) -> tuple[str, str]:
    page = win.sidebar_nav.get_visible_page()
    if page is win.sidebar_root:
        return ("", "")
    title = one(page, Adw.WindowTitle)
    return (title.get_title(), title.get_subtitle())


def two_texts(win):
    win.label.elements[0].text = "First"
    win.add_element(TextElement(text="Second"))
    win.expand_index = -1
    win.rebuild_sidebar()


def test_the_open_page_follows_its_element_when_moved(window):
    two_texts(window)
    open_page(window, "Second", "Box and frame")
    window.move_element(1, 0)
    assert page_header(window) == ("Box and frame", "Second")
    window.move_element(0, 1)
    assert page_header(window) == ("Box and frame", "Second")


def test_the_open_page_follows_its_element_through_delete_and_undo(window):
    two_texts(window)
    open_page(window, "Second", "Position")
    window.remove_element(0)  # another element goes: the page stays on "Second"
    assert page_header(window) == ("Position", "Second")
    activate(window, "undo")  # it comes back before "Second"
    assert page_header(window) == ("Position", "Second")
    activate(window, "redo")
    assert page_header(window) == ("Position", "Second")


def test_adding_an_element_keeps_the_page(window):
    two_texts(window)
    open_page(window, "First", "Spacing and fitting")
    window.add_element(ShapeElement())
    assert page_header(window) == ("Spacing and fitting", "First")


def test_removing_the_open_element_closes_its_page(window):
    two_texts(window)
    open_page(window, "Second", "Box and frame")
    window.remove_element(1)
    assert window.sidebar_nav.get_visible_page() is window.sidebar_root
    activate(window, "undo")  # back: the page is not reopened by itself
    assert len(window.label.elements) == 2


def test_a_duplicate_is_another_element(window):
    from etiketto.model import element_id

    window.expand_index = 0
    activate(window, "duplicate-element")
    first, second = window.label.elements
    assert element_id(first) != element_id(second)


def test_undo_gives_the_focus_back_to_rows(window):
    window.change(margin_mm=5)
    spin = one(window.sidebar, Adw.SpinRow, title="Spacing")
    spin.grab_focus()
    activate(window, "undo")
    assert window.label.margin_mm == 2
    focus = window.get_focus()
    spin = one(window.sidebar, Adw.SpinRow, title="Spacing")
    assert focus is spin or focus.is_ancestor(spin)
    page = open_page(window, "Label", "Box and frame")
    switch = one(page, Adw.SwitchRow, title="White on black")
    switch.set_active(True)
    switch.grab_focus()
    activate(window, "undo")
    page = window.sidebar_nav.get_visible_page()
    switch = one(page, Adw.SwitchRow, title="White on black")
    focus = window.get_focus()
    assert focus is switch or focus.is_ancestor(switch)


def test_hints_on_rows_and_buttons_without_effect(window):
    window.add_element(ImageElement(data=png(), name="logo.png"))
    page = open_page(window, "logo.png", "Black and white")
    level = one(page, Adw.SpinRow, title="Threshold")
    assert level.get_subtitle() == "Only for the method Threshold"
    one(page, Adw.ComboRow, title="Method").set_selected(1)
    assert level.get_subtitle() == "Lighter pixels print white"
    window.sidebar_nav.pop()
    page = open_page(window, "logo.png", "Corners and border")
    border = one(page, Adw.SpinRow, title="Border")
    colour = one(border, Gtk.ColorDialogButton)
    assert colour.get_tooltip_text() == "Choose a width first"
    border.set_value(0.5)
    assert colour.get_tooltip_text() == "Colour of the border"
