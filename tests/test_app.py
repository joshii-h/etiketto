"""The GTK window in the test process: real widgets, faked printer and dialogs.

device.query/print_label/discover, Gtk.FileDialog and Gtk.UriLauncher are
replaced (see conftest.gui_env and the fixtures below); config, settings and
templates live in tmp_path.
"""

from __future__ import annotations

import errno
import html
import io
import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import gtk_available, pump, render_now, toast_titles

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from etiketto import app, model  # noqa: E402
from etiketto.backends.base import (  # noqa: E402
    BadCassette,
    CassetteInfo,
    ColorAdjust,
    CoverOpen,
    FoundPrinter,
    LabelTooLong,
    NoCassette,
    PaperJam,
    PortClosed,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    Progress,
    UsbNotFound,
)
from etiketto.model import CodeElement, ImageElement, Label, TextElement  # noqa: E402
from etiketto.render import mm_to_px, render  # noqa: E402
from etiketto.serial import SeriesProgress  # noqa: E402
from etiketto.ui import i18n, preview, texts, widgets  # noqa: E402
from etiketto.ui import settings as config  # noqa: E402
from etiketto.ui import window as ui_window  # noqa: E402
from etiketto.ui.settings import Settings  # noqa: E402
from etiketto.units import INCHES, MILLIMETRES  # noqa: E402

# --- helpers -------------------------------------------------------------------


def walk(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def find(root, cls, **props):
    found = []
    for widget in walk(root):
        if not isinstance(widget, cls):
            continue
        if "title" in props and widget.get_title() != props["title"]:
            continue
        if "tooltip" in props and widget.get_tooltip_text() != props["tooltip"]:
            continue
        found.append(widget)
    return found


def one(root, cls, **props):
    found = find(root, cls, **props)
    assert len(found) == 1, (cls, props, found)
    return found[0]


def subtitle(win):
    return win.window_title.get_subtitle()


def respond(dialog, response):
    """Answer an Adw.AlertDialog like a click on one of its buttons."""
    dialog.force_close()
    dialog.emit("response", response)
    pump()


class FakeFileDialog:
    """Stands in for Gtk.FileDialog; answers immediately with `result`."""

    result = None  # path, GLib.Error or exception for *_finish
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initial_name = None
        self.initial_folder = None
        self.method = None
        type(self).instances.append(self)

    def set_filters(self, filters):
        self.kwargs["filters"] = filters

    def set_initial_name(self, name):
        self.initial_name = name

    def set_initial_folder(self, folder):
        self.initial_folder = folder.get_path()

    def open(self, parent, cancellable, callback):
        self.method = "open"
        callback(self, "result")

    def save(self, parent, cancellable, callback):
        self.method = "save"
        callback(self, "result")

    def _finish(self, result):
        assert result == "result"
        value = type(self).result
        if isinstance(value, list):  # one answer per dialog
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        if str(value).startswith("https:"):  # a file that is not local
            return Gio.File.new_for_uri(str(value))
        return Gio.File.new_for_path(str(value))

    open_finish = save_finish = _finish


@pytest.fixture
def file_dialog(monkeypatch):
    class Dialog(FakeFileDialog):
        instances = []

    monkeypatch.setattr(Gtk, "FileDialog", Dialog)
    return Dialog


@pytest.fixture
def launched(monkeypatch):
    urls = []

    class Launcher:
        @staticmethod
        def new(url):
            launcher = Launcher()
            launcher.url = url
            return launcher

        def launch(self, parent, cancellable, callback):
            urls.append(self.url)
            callback(self, "result")

        def launch_finish(self, result):
            assert result == "result"
            if urls.fail:
                raise GLib.Error("no browser")
            return True

    class Urls(list):
        fail = False

    urls = Urls()
    monkeypatch.setattr(Gtk, "UriLauncher", Launcher)
    return urls


def cancelled():
    return GLib.Error("Dismissed by user")


def png_file(path, size=(40, 20), color="red"):
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return path


# --- small helpers of the interface ------------------------------------------------------


def test_colour_conversion():
    rgba = widgets.hex_to_rgba("#ff8000")
    assert widgets.rgba_to_hex(rgba) == "#ff8000"
    assert widgets.rgba_to_hex(widgets.hex_to_rgba("not a colour")) == "#000000"


def test_element_titles_and_icons():
    assert texts.element_title(TextElement(text="Erste Zeile\nzweite")) == "Erste Zeile"
    assert texts.element_title(TextElement(text="x" * 60)) == "x" * 40
    assert texts.element_title(TextElement(text="")) == "Text"
    assert texts.element_title(ImageElement(name="foto.png")) == "foto.png"
    assert texts.element_title(ImageElement()) == "Image"
    # the code content is isolated so it stays intact inside right-to-left text
    assert (
        texts.element_title(CodeElement(data="y" * 50)) == "QR code: \u2068" + "y" * 30 + "\u2069"
    )
    assert (
        texts.element_title(CodeElement(data="123", symbology="ean13"))
        == "EAN-13: \u2068123\u2069"
    )
    assert texts.element_icon(TextElement()) == "format-text-bold-symbolic"
    assert texts.element_icon(ImageElement()) == "image-x-generic-symbolic"
    assert texts.element_icon(CodeElement()) == "view-grid-symbolic"


def test_image_from_path(tmp_path):
    element = texts.image_from_path(str(png_file(tmp_path / "a.png")))
    assert element.name == "a.png" and element.raw()[:4] == b"\x89PNG"
    with pytest.raises(ValueError, match="Only local files"):
        texts.image_from_path(None)
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(ValueError, match="Not an image: notes.txt"):
        texts.image_from_path(str(tmp_path / "notes.txt"))


def test_settings_roundtrip(gui_env):
    assert Settings.load().data == {}
    settings = {"units": "in", "printers": {"vc500w": {"host": "p"}}}
    Settings(settings).save()
    assert Settings.load().data == settings
    assert not config.SETTINGS_FILE.with_suffix(".tmp").exists()
    config.SETTINGS_FILE.write_text("{broken")
    assert Settings.load().data == {}
    config.SETTINGS_FILE.write_text("[1, 2]")
    assert Settings.load().data == {}


def test_settings_of_etiketto_0_1_are_migrated(gui_env):
    Settings({"host": "p", "safe_mm": 1.0, "cut": "half", "mode": "normal", "units": "mm"}).save()
    assert Settings.load().data == {
        "units": "mm",
        "printers": {"vc500w": {"host": "p", "safe_mm": 1.0, "cut": "half", "mode": "color"}},
    }


def test_migrate_settings_keeps_newer_values():
    settings = {"host": "old", "mode": "vivid", "printers": {"vc500w": {"host": "new"}}}
    assert config.migrate(settings) == {"printers": {"vc500w": {"host": "new", "mode": "vivid"}}}
    assert config.migrate({"host": "h", "printers": "junk"}) == {
        "printers": {"vc500w": {"host": "h"}}
    }
    assert config.migrate({"units": "in"}) == {"units": "in"}


def test_locale_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "__file__", str(tmp_path / "pkg" / "ui" / "i18n.py"))
    monkeypatch.setattr(i18n.sys, "prefix", str(tmp_path / "prefix"))
    assert i18n._locale_dir() == tmp_path / "pkg" / "locale"  # nothing found: package dir
    mo = tmp_path / "prefix" / "share" / "locale" / "de" / "LC_MESSAGES" / "etiketto.mo"
    mo.parent.mkdir(parents=True)
    mo.write_bytes(b"")
    assert i18n._locale_dir() == tmp_path / "prefix" / "share" / "locale"


# --- window state ----------------------------------------------------------------


def test_new_window(window, gui_env):
    assert subtitle(window) == "Unsaved label"
    assert not window.dirty
    assert window.label == Label(elements=[TextElement(text="Label")])
    assert window.rendered is not None
    assert window.preview.picture.get_paintable() is not None
    assert window.preview.status.get_text().startswith("50 × ")
    # no host configured: the printer is searched for on the network
    assert gui_env.discovers == 1
    assert window.printer.row.get_title() == "No printer found"


def test_dirty_tracking_and_title(window, tmp_path):
    window.change(margin_mm=3.0)
    assert window.label.margin_mm == 3.0
    assert window.dirty and subtitle(window) == "• Unsaved label"
    assert window._render_source
    path = tmp_path / "kabel.etiketto"
    window.files.write(path)
    assert not window.dirty and subtitle(window) == "kabel.etiketto"
    assert toast_titles(window) == ["Saved kabel.etiketto"]
    assert Label.load(path) == window.label
    window.mark_changed()
    assert subtitle(window) == "• kabel.etiketto"


def test_write_failure_keeps_dirty(window, tmp_path):
    window.change(tape_mm=12)
    window.files.write(tmp_path / "missing" / "x.etiketto")
    assert window.dirty and window.path is None
    assert toast_titles(window) == ["Cannot save x.etiketto"]  # no path, no errno
    assert window.toast_log[0].get_button_label() == "Report"


def test_element_list_operations(window):
    first = window.label.elements[0]
    code = CodeElement(data="abc")
    window.add_element(code)
    assert window.label.elements == [first, code] and window.expand_index == 1
    assert window.dirty
    window.move_element(1, 0)
    assert window.label.elements == [code, first] and window.expand_index == 0
    window.remove_element(0)
    assert window.label.elements == [first] and window.expand_index == -1
    window.remove_element(0)
    rows = find(window.sidebar, Adw.ActionRow, title="No content yet")
    assert len(rows) == 1


def test_sidebar_buttons_move_and_remove(window):
    window.add_element(CodeElement(data="second"))
    down = find(window.sidebar, Gtk.Button, tooltip="Move down")
    up = find(window.sidebar, Gtk.Button, tooltip="Move up")
    assert [b.get_sensitive() for b in down] == [True, False]
    assert [b.get_sensitive() for b in up] == [False, True]
    down[0].emit("clicked")
    assert [type(e).__name__ for e in window.label.elements] == ["CodeElement", "TextElement"]
    find(window.sidebar, Gtk.Button, tooltip="Remove")[-1].emit("clicked")
    assert [type(e).__name__ for e in window.label.elements] == ["CodeElement"]


def test_text_element_widgets(window):
    element = window.label.elements[0]
    view = one(window.sidebar, Gtk.TextView)
    view.get_buffer().set_text("Neuer Text\nZeile 2")
    assert element.text == "Neuer Text\nZeile 2"
    assert window.dirty
    expander = one(window.sidebar, Adw.ExpanderRow)
    assert expander.get_title() == "Neuer Text"
    toggles = {t.get_icon_name(): t for t in find(window.sidebar, Gtk.ToggleButton)}
    bold = toggles["format-text-bold-symbolic"]
    italic = toggles["format-text-italic-symbolic"]
    bold.set_active(False)
    italic.set_active(True)
    assert (element.bold, element.italic) == (False, True)
    one(window.sidebar, Adw.SpinRow, title="Size").set_value(18)
    assert element.size_pt == 18
    one(window.sidebar, Adw.ComboRow, title="Line alignment").set_selected(2)
    assert element.align == "right"
    one(window.sidebar, Gtk.FontDialogButton).set_font_desc(
        Pango.FontDescription.from_string("Serif 12")
    )
    assert element.font == "Serif"
    expander_buttons = find(expander, Gtk.ColorDialogButton)
    expander_buttons[0].set_rgba(widgets.hex_to_rgba("#00ff00"))
    assert element.color == "#00ff00"


def test_code_element_widgets(window):
    window.add_element(CodeElement(data="x"))
    element = window.label.elements[1]
    kind = one(window.sidebar, Adw.ComboRow, title="Type")
    assert kind.get_subtitle() == "Links, text, Wi-Fi access; phones read it"
    keys = [key for key, _name in texts.SYMBOLOGIES]
    kind.set_selected(keys.index("gs1_128"))
    assert element.symbology == "gs1_128"
    assert kind.get_subtitle().startswith("Application identifiers in brackets")
    kind.set_selected(keys.index("code128"))
    assert element.symbology == "code128"
    assert kind.get_subtitle() == "Latin letters, digits and punctuation"
    kind.activate()  # the list opens with a heading above each group
    pump()
    labels = find(kind, Gtk.Label)
    headings = {label.get_label() for label in labels if label.has_css_class("heading")}
    assert {"2D codes", "Linear barcodes", "GS1"} <= headings
    one(window.sidebar, Adw.EntryRow, title="Content (link, text, number)").set_text("ABC")
    assert element.data == "ABC"
    colour_row = one(window.sidebar, Adw.ActionRow, title="Colour")
    one(colour_row, Gtk.ColorDialogButton).set_rgba(widgets.hex_to_rgba("#0000ff"))
    assert element.color == "#0000ff"
    assert find(window.sidebar, Adw.ExpanderRow)[1].get_title() == "Code 128: \u2068ABC\u2069"


def test_image_element_widgets(window, file_dialog, tmp_path):
    window.add_element(ImageElement(data="", name=""))
    element = window.label.elements[1]
    file_dialog.result = png_file(tmp_path / "logo.png")
    button = [b for b in find(window.sidebar, Gtk.Button) if b.get_label() == "Choose…"][0]
    button.emit("clicked")
    assert element.name == "logo.png" and element.data
    assert one(window.sidebar, Adw.ActionRow, title="Image file").get_subtitle() == "logo.png"
    assert window.dirty


def test_label_settings_widgets(window):
    lab = window.label
    auto = one(window.sidebar, Adw.SwitchRow, title="Automatic length")
    length = one(window.sidebar, Adw.SpinRow, title="Length")
    align = one(window.sidebar, Adw.ComboRow, title="Alignment")
    assert not length.get_sensitive() and not align.get_sensitive()
    auto.set_active(False)
    assert lab.auto_length is False
    assert length.get_sensitive() and align.get_sensitive()
    length.set_value(80)
    assert lab.length_mm == 80
    align.set_selected(2)
    assert lab.align == "end"
    one(window.sidebar, Adw.SpinRow, title="Spacing").set_value(4)
    one(window.sidebar, Adw.SpinRow, title="Edge spacing").set_value(1)
    border = one(window.sidebar, Adw.SpinRow, title="Border")
    border.set_value(0.5)
    one(border, Gtk.ColorDialogButton).set_rgba(widgets.hex_to_rgba("#ff0000"))
    bg = one(window.sidebar, Adw.ActionRow, title="Background colour")
    one(bg, Gtk.ColorDialogButton).set_rgba(widgets.hex_to_rgba("#ffff00"))
    window.label_group.tape_row.set_selected(0)
    assert (lab.margin_mm, lab.edge_mm, lab.border_mm) == (4, 1, 0.5)
    assert (lab.border_color, lab.background, lab.tape_mm) == ("#ff0000", "#ffff00", 9)
    assert window.dirty


def test_background_image(window, file_dialog, tmp_path):
    row = one(window.sidebar, Adw.ActionRow, title="Background image")
    assert row.get_subtitle() == "None"
    file_dialog.result = png_file(tmp_path / "bg.png")
    one(row, Gtk.Button, tooltip="Choose image").emit("clicked")
    assert window.label.background_image.name == "bg.png"
    row = one(window.sidebar, Adw.ActionRow, title="Background image")
    assert row.get_subtitle() == "bg.png"
    one(row, Gtk.Button, tooltip="Remove").emit("clicked")
    assert window.label.background_image is None


def test_choose_image(window, file_dialog, tmp_path):
    picked = []
    file_dialog.result = cancelled()
    window.files.choose_image(picked.append)
    assert picked == [] and window.toast_log == []
    (tmp_path / "x.txt").write_text("no")
    file_dialog.result = tmp_path / "x.txt"
    window.files.choose_image(picked.append)
    assert picked == [] and toast_titles(window) == ["Not an image: x.txt"]
    file_dialog.result = png_file(tmp_path / "ok.png")
    window.activate_action("win.add-image", None)
    assert window.label.elements[-1].name == "ok.png"
    assert file_dialog.instances[-1].kwargs["title"] == "Choose image"


def test_add_actions(window):
    window.activate_action("win.add-text", None)
    window.activate_action("win.add-code", None)
    assert window.label.elements[1:] == [TextElement(text="Text"), CodeElement()]


# --- preview -----------------------------------------------------------------------


def test_render_info_overflow_and_warnings(window):
    window.set_label(
        Label(
            tape_mm=25,
            auto_length=False,
            length_mm=20,
            elements=[TextElement(text="x" * 40), CodeElement(data="")],
        ),
        None,
    )
    render_now(window)
    info = window.preview.status.get_text()
    assert info.startswith("25 × 20 mm")
    assert "content scaled down to fit" in info
    assert "Problem: Element 2: the code is empty" in info


def test_render_failure_is_shown(window, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(ui_window, "render", broken)
    render_now(window)
    assert window.rendered is None
    assert window.preview.picture.get_paintable() is None
    assert window.preview.status.get_text() == "The preview cannot be drawn"
    window.preview.apply_zoom()  # nothing rendered: no crash, no size change
    # one toast with a report button, not one per keystroke
    render_now(window)
    assert toast_titles(window) == ["The preview cannot be drawn"]
    assert window.toast_log[0].get_button_label() == "Report"


def test_debounced_render(window):
    window.rendered = None
    window.schedule_render()
    first = window._render_source
    window.schedule_render()
    assert window._render_source != first
    assert pump(lambda: window.rendered is not None, timeout=2)
    assert window._render_source == 0


def test_zoom(window):
    length = window.rendered.length_mm
    window.preview.scroller.get_hadjustment().set_page_size(656)
    window.preview.scroller.get_vadjustment().set_page_size(356)
    px_per_mm = min(600 / length, 300 / 50, preview.PREVIEW_PX_PER_MM * 4)
    assert window.preview.zoom == pytest.approx(px_per_mm / preview.PREVIEW_PX_PER_MM)
    assert window.preview.picture.get_size_request() == (
        round(length * px_per_mm),
        round(50 * px_per_mm),
    )
    window.preview.set_zoom(2)
    assert not window.preview.fit_zoom
    assert window.preview.picture.get_size_request() == (round(length * 12), 600)
    window.preview.scroller.get_hadjustment().set_page_size(200)  # no refit when zoomed
    assert window.preview.zoom == 2
    window.preview.set_zoom(100)
    assert window.preview.zoom == 8.0
    window.preview.set_zoom(0.001)
    assert window.preview.zoom == 0.1
    window.preview.set_zoom(None)
    assert window.preview.fit_zoom
    assert window.preview.zoom == pytest.approx(min(144 / length, 6) / preview.PREVIEW_PX_PER_MM)


def test_zoom_buttons(window):
    window.preview.set_zoom(1.0)
    one(window, Gtk.Button, tooltip="Zoom in").emit("clicked")
    assert window.preview.zoom == pytest.approx(1.25)
    one(window, Gtk.Button, tooltip="Zoom out").emit("clicked")
    one(window, Gtk.Button, tooltip="Zoom out").emit("clicked")
    assert window.preview.zoom == pytest.approx(0.8)
    one(window, Gtk.Button, tooltip="Fit to window").emit("clicked")
    assert window.preview.fit_zoom


# --- units and printer settings ------------------------------------------------------


def test_units_switch(window, gui_env):
    assert window.unit is MILLIMETRES
    assert window.label_group.tape_row.get_model().get_string(0) == "9 mm"
    units = one(window.sidebar, Adw.ComboRow, title="Units")
    assert units.get_model().get_string(0) == "Automatic (Millimetres)"
    units.set_selected(2)
    assert Settings.load().data["units"] == "in"
    assert window.unit is INCHES
    assert window.label_group.tape_row.get_model().get_string(0) == "3/8″"
    assert one(window.sidebar, Adw.SpinRow, title="Length").get_subtitle() == "in"
    render_now(window)
    assert "″" in window.preview.status.get_text()


def test_units_automatic_follows_locale(make_window, gui_env, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    win = make_window()
    assert win.unit is INCHES
    units = one(win.sidebar, Adw.ComboRow, title="Units")
    assert units.get_model().get_string(0) == "Automatic (Inches)"


@pytest.mark.parametrize(
    "units, mm, step, value, digits, back",
    [
        ("mm", 25.0, 1, 25.0, 0, 30.0),
        ("mm", 1.5, 0.1, 1.5, 1, 30.0),
        ("in", 25.4, 1, 1.0, 2, 762.0),
        ("in", 1.5, 0.1, 0.06, 2, 762.0),
    ],
)
def test_length_row_converts(make_window, units, mm, step, value, digits, back):
    win = make_window(settings={"units": units})
    got = []
    row = widgets.length_row(win.unit, "Test", mm, 0, 1000, step, got.append, "%(unit)s here")
    assert row.get_value() == pytest.approx(value)
    assert row.get_digits() == digits
    assert row.get_subtitle() == ("%s here" % units)
    assert row.get_adjustment().get_step_increment() == win.unit.step(step)
    row.set_value(30.0)
    assert got == [back]
    assert widgets.length_row(win.unit, "Plain", 1, 0, 2, 1, got.append).get_subtitle() == units


def test_safety_margin_is_not_part_of_the_label(window):
    assert window.printer_settings.safe_mm == 1.5
    one(window.sidebar, Adw.SpinRow, title="Safety margin").set_value(0.04)
    assert window.printer_settings.safe_mm == 0.0
    assert Settings.load().data["printers"]["vc500w"]["safe_mm"] == 0.0
    assert not window.dirty
    render_now(window)
    assert window.rendered.image.tobytes() == render(window.label, safe_mm=0).image.tobytes()
    assert window.rendered.image.tobytes() != render(window.label).image.tobytes()


def test_host_entry(window, gui_env):
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == ""
    entry.set_text("  192.0.2.5 ")
    entry.emit("apply")
    assert Settings.load().data["printers"]["vc500w"]["host"] == "192.0.2.5"
    assert pump(lambda: window.printer.info is not None)
    assert gui_env.queries == ["192.0.2.5"]


def test_host_from_labelprinter_config(make_window, gui_env):
    gui_env.configured = "VC.local"
    win = make_window()
    assert gui_env.queries == ["VC.local"] and gui_env.discovers == 0
    assert one(win.sidebar, Adw.EntryRow, title="Host name or IP address").get_text() == "VC.local"
    Settings({"host": "192.0.2.1"}).save()
    win.settings = Settings.load()
    assert win.printer.host == "192.0.2.1"  # own setting wins


# --- printer status -----------------------------------------------------------------


def test_printer_found_adopts_tape_of_new_label(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    assert win.label.tape_mm == 25
    assert win.label_group.tape_row.get_selected() == 3
    assert not win.dirty and subtitle(win) == "Unsaved label"
    assert win.printer.row.get_title() == "Brother VC-500W"
    assert win.printer.row.get_subtitle() == "\u206825 mm\u2069 cassette · 4 m of 5 m left"


@pytest.mark.parametrize("state", ["dirty", "path", "template"])
def test_printer_found_keeps_changed_or_saved_label(window, tmp_path, state):
    if state == "dirty":
        window.change(margin_mm=1.0)
    elif state == "path":
        window.path = tmp_path / "x.etiketto"
    else:  # a template is a new label, but its tape was chosen on purpose
        window.set_label(Label(tape_mm=50), None)
    window.printer.found(PrinterInfo("Wedge", 12, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer.row.get_subtitle() == "\u206812 mm\u2069 cassette"


def test_printer_without_cassette(window):
    window.printer.found(PrinterInfo("Wedge", None, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer.row.get_subtitle() == "No cassette inserted"


def test_printer_status_in_inches(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window(settings={"units": "in"})
    assert win.printer.row.get_subtitle() == "\u20681″\u2069 cassette · 13.1 ft of 16.4 ft left"


def test_printer_failed(make_window, gui_env):
    gui_env.configured = "p"
    gui_env.query_error = PortClosed("Connection to 'p:9100' was refused.\nPossible solutions")
    win = make_window()
    assert win.printer.info is None
    assert win.printer.row.get_title() == "Printer not reachable"
    # what to do, not the host or labelprinter's English text
    # compare plain text: GLib/libadwaita versions escape the apostrophe differently
    assert html.unescape(win.printer.row.get_subtitle()) == (
        "The printer refuses the connection. Switch it off and on again. If its web interface "
        "offers a “Port 9100” option, turn it on."
    )
    assert not win.printer.details_button.get_sensitive()


def test_refresh_button(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    one(win.sidebar, Gtk.Button, tooltip="Refresh").emit("clicked")
    assert win.printer.row.get_title() == "Connecting…"
    assert pump(lambda: len(gui_env.queries) == 2 and win.printer.info is not None)


# --- discovery ---------------------------------------------------------------------


def found(*names):
    return [
        FoundPrinter("Brother VC-500W (%s)" % n, "VC-%s.local" % n, "192.0.2.%d" % i)
        for i, n in enumerate(names, 10)
    ]


def test_discover_none(window, gui_env):
    window.printer.discover()
    assert window.printer.row.get_title() == "Searching for printers…"
    assert pump(lambda: window.printer.row.get_title() == "No printer found")
    assert window.printer.row.get_subtitle().startswith("Make sure the printer is switched on")
    assert window.printer.host is None


def test_discover_one_is_used(window, gui_env):
    gui_env.found = found("A")
    one(window.sidebar, Gtk.Button, tooltip="Search for printers").emit("clicked")
    assert pump(lambda: window.printer.info is not None)
    assert Settings.load().data["printers"]["vc500w"]["host"] == "VC-A.local"
    assert gui_env.queries == ["VC-A.local"]
    assert toast_titles(window) == ["Found Brother VC-500W (A)"]
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == "VC-A.local"


def test_discover_several_never_guesses(window, gui_env):
    # also the search on its own at the start: in an office, the first one
    # found is not necessarily the right one
    gui_env.found = found("A", "B")
    window.printer.discover()
    assert pump(lambda: window.get_visible_dialog() is not None)
    assert window.printer.host is None


def test_discover_several_lets_the_user_choose(window, gui_env):
    gui_env.found = found("A", "B")
    window.printer.discover()
    assert pump(lambda: window.get_visible_dialog() is not None)
    dialog = window.get_visible_dialog()
    assert dialog.get_heading() == "Choose a printer"
    rows = find(dialog.get_extra_child(), Adw.ActionRow)
    assert [r.get_subtitle() for r in rows] == [
        "\u2068VC-A.local\u2069 · \u2068192.0.2.10\u2069",
        "\u2068VC-B.local\u2069 · \u2068192.0.2.11\u2069",
    ]
    # no second search that would take the first printer behind the dialog
    pump(timeout=0.2)
    assert window.printer.host is None and gui_env.queries == []
    assert window.printer.row.get_title() == "Choose a printer"
    rows[1].emit("activated")
    assert window.printer.host == "VC-B.local"
    assert pump(lambda: "VC-B.local" in gui_env.queries)
    assert window.get_visible_dialog() is None


def test_discover_outdated_results_are_dropped(window, gui_env):
    window.printer.discover()
    first = window.printer.generation
    window.printer.discover()  # searched again before the answer came
    assert pump(lambda: window.printer.row.get_title() == "No printer found")
    assert window.printer.discovered(found("A", "B"), first) is False
    assert window.get_visible_dialog() is None and window.printer.host is None


def test_discover_never_opens_two_choosers(window, gui_env):
    window.printer.discovered(found("A", "B"), window.printer.generation)
    first = window.get_visible_dialog()
    window.printer.discovered(found("A", "B", "C"), window.printer.generation)
    second = window.get_visible_dialog()
    assert second is not first and window.printer.chooser is second
    assert len(find(second.get_extra_child(), Adw.ActionRow)) == 3
    window.printer.chooser_closed(first)  # a late signal of the old one changes nothing
    assert window.printer.chooser is second
    second.force_close()
    assert pump(lambda: window.printer.chooser is None)


def test_discover_several_with_host_refreshes_current(make_window, gui_env):
    gui_env.configured = "VC-A.local"
    win = make_window()
    gui_env.found = found("A", "B")
    win.printer.discover()
    assert pump(lambda: win.get_visible_dialog() is not None and len(gui_env.queries) == 2)
    assert gui_env.queries == ["VC-A.local", "VC-A.local"]
    win.get_visible_dialog().force_close()


# --- printing ------------------------------------------------------------------------


@pytest.fixture
def ready(make_window, gui_env):
    """A window connected to a (fake) printer with a 25 mm cassette."""
    gui_env.configured = "VC.local"
    win = make_window()
    assert win.label.tape_mm == 25
    return win


def test_print_guards(ready):
    ready.print_flow.busy = True
    ready.print_flow.on_print()
    assert ready.get_visible_dialog() is None
    ready.print_flow.busy = False
    ready.rendered = None
    ready.print_flow.on_print()
    ready.print_flow.dialog()  # the screenshot tool opens it directly
    assert ready.get_visible_dialog() is None
    ready.set_label(Label(tape_mm=25), None)
    render_now(ready)
    ready.print_flow.on_print()
    assert toast_titles(ready) == ["The label is empty"]
    assert ready.get_visible_dialog() is None


def test_print_background_only_is_not_empty(ready, tmp_path):
    ready.set_label(
        Label(
            tape_mm=25, background_image=texts.image_from_path(str(png_file(tmp_path / "b.png")))
        ),
        None,
    )
    render_now(ready)
    ready.print_flow.on_print()
    assert ready.get_visible_dialog().get_heading() == "Print label"


@pytest.mark.parametrize("answer, prints", [("cancel", False), ("print", True)])
def test_wrong_cassette_asks(ready, answer, prints):
    ready.change(tape_mm=12)
    render_now(ready)
    ready.activate_action("win.print", None)
    dialog = ready.get_visible_dialog()
    assert dialog.get_heading() == "Wrong cassette"
    assert "12 mm" in dialog.get_body() and "25 mm" in dialog.get_body()
    respond(dialog, answer)
    print_dialog = ready.get_visible_dialog()
    if prints:
        assert print_dialog is ready.last_dialog
        assert print_dialog.get_heading() == "Print label"
    else:
        assert print_dialog is None


def test_print_flow(ready, gui_env):
    ready.print_flow.on_print()
    dialog = ready.last_dialog
    assert dialog.get_heading() == "Print label"
    assert dialog.get_body() == MILLIMETRES.label_size(25, ready.rendered.length_mm)
    box = dialog.get_extra_child()
    copies, cut, mode, colour = (box.get_row_at_index(i) for i in range(4))
    assert box.get_row_at_index(4) is None
    assert [mode.get_model().get_string(i) for i in range(2)] == ["Vivid", "Normal"]
    assert colour.get_title() == "Colour adjustment"
    copies.set_value(3)
    cut.set_selected(1)
    mode.set_selected(1)
    dialog.emit("response", "print")

    assert ready.print_flow.busy
    assert not ready.print_button.get_sensitive()
    assert ready.print_button.get_label() == "Printing…"
    assert Settings.load().data["printers"]["vc500w"] == {"cut": "half", "mode": "color"}

    assert pump(lambda: not ready.print_flow.busy)
    assert len(gui_env.prints) == 1
    image, host, options = gui_env.prints[0]
    assert host == "VC.local"
    assert options == PrintOptions(
        copies=3, cut="half", mode="color", data_format="rawrgb", host="VC.local"
    )
    # rendered in the worker for the printer: rawrgb, 25 mm at 320 dpi with bleed
    assert image.height == 333
    assert ready.print_button.get_sensitive()
    assert ready.print_button.get_label() == "Print"
    assert toast_titles(ready) == ["Printed"]
    assert pump(lambda: len(gui_env.queries) == 2)  # status refreshed after printing

    ready.print_flow.on_print()  # last choices are the defaults now
    box = ready.last_dialog.get_extra_child()
    assert box.get_row_at_index(1).get_selected() == 1
    assert box.get_row_at_index(2).get_selected() == 1


def test_print_dialog_cancel(ready, gui_env):
    ready.print_flow.on_print()
    respond(ready.last_dialog, "cancel")
    assert not ready.print_flow.busy and gui_env.prints == []


def test_print_without_host(window, gui_env):
    window.print_flow.start(PrintOptions())
    assert toast_titles(window) == ["No printer yet – search for one or enter its address"]
    assert not window.print_flow.busy and gui_env.prints == []


def test_print_error_dialog_with_details_and_report(ready, gui_env, launched):
    gui_env.print_error = PaperJam("print_state=ERROR print_job_error=JAM")
    ready.print_flow.start(PrintOptions(copies=2))
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.print_button.get_label() == "Print"
    assert ready.toast_log == []  # a dialog, not a toast
    dialog = ready.get_visible_dialog()
    assert dialog is ready.last_dialog
    assert dialog.get_heading() == "Printing failed"
    assert dialog.get_body() == "The label is jammed. Open the cover, remove it and try again."
    expander = dialog.get_extra_child()
    assert isinstance(expander, Gtk.Expander) and not expander.get_expanded()
    assert expander.get_child().get_label() == "print_state=ERROR print_job_error=JAM"
    respond(dialog, "close")
    assert launched == []
    ready.print_flow.done(PaperJam("print_state=ERROR print_job_error=JAM"))
    respond(ready.get_visible_dialog(), "report")
    query = parse_qs(urlparse(launched[0]).query)
    assert query["title"] == [
        "Problem: The label is jammed. Open the cover, remove it and try again."
    ]
    body = query["body"][0]
    assert "print_job_error=JAM" in body
    assert "- Printer: Brother VC-500W, 25 mm cassette, IDLE" in body


def test_report_problem(window, launched):
    window.activate_action("win.report", None)
    url = launched[0]
    assert url.startswith("https://git.joshuahirsig.xyz/joshii/etiketto/issues/new?")
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    assert query["title"] == [""]
    body = query["body"][0]
    assert "- Printer: not connected" in body
    assert (
        "GTK %d.%d, libadwaita %d.%d"
        % (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Adw.get_major_version(),
            Adw.get_minor_version(),
        )
        in body
    )
    window.printer.info = PrinterInfo("Wedge", None, None, None, "ERROR", firmware="2026031504")
    window.report_problem("boom")
    body = parse_qs(urlparse(launched[1]).query)["body"][0]
    assert "- Printer: Brother VC-500W, no cassette, ERROR, firmware 2026031504" in body
    assert window.toast_log == []  # the browser opened


# --- files -----------------------------------------------------------------------------


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / "saved.etiketto"
    Label(tape_mm=19, elements=[CodeElement(data="42")]).save(path)
    return path


def test_open_file(window, saved):
    window.change(margin_mm=5.0)
    window.files.open_file(saved)
    assert window.label.tape_mm == 19 and window.path == saved
    assert not window.dirty and subtitle(window) == "saved.etiketto"
    assert window.expand_index == -1


def test_open_broken_file(window, tmp_path):
    for name, content in (("bad.etiketto", "{"), ("wide.etiketto", json.dumps({"tape_mm": 7}))):
        (tmp_path / name).write_text(content)
        window.files.open_file(tmp_path / name)
    (tmp_path / "new.etiketto").write_text(json.dumps({"version": 99}))
    window.files.open_file(tmp_path / "new.etiketto")
    window.files.open_file(tmp_path / "missing.etiketto")
    assert toast_titles(window) == [
        "bad.etiketto is not an Etiketto label",
        "The file is damaged: wide.etiketto",
        "This label needs a newer version of Etiketto",
        "Cannot open missing.etiketto",
    ]
    assert window.path is None


def test_on_open(window, file_dialog, saved):
    file_dialog.result = cancelled()
    window.activate_action("win.open", None)
    assert window.path is None
    file_dialog.result = saved
    window.activate_action("win.open", None)
    assert window.path == saved
    assert file_dialog.instances[-1].method == "open"
    filters = file_dialog.instances[-1].kwargs["filters"]
    assert filters.get_item(0).get_name() == "Etiketto labels"


def test_on_open_asks_before_discarding(window, file_dialog, saved):
    file_dialog.result = saved
    window.change(margin_mm=5.0)
    window.files.open()
    assert [d.method for d in file_dialog.instances] == [None]  # not shown yet
    dialog = window.get_visible_dialog()
    assert dialog.get_heading() == "Discard changes?"
    respond(dialog, "cancel")
    assert [d.method for d in file_dialog.instances] == [None] and window.dirty
    window.files.open()
    respond(window.get_visible_dialog(), "discard")
    assert window.path == saved and not window.dirty


def test_save_and_save_as(window, file_dialog, tmp_path):
    file_dialog.result = cancelled()
    window.activate_action("win.save", None)  # no path yet: save as
    assert file_dialog.instances[-1].initial_name == "label.etiketto"
    assert window.path is None
    file_dialog.result = tmp_path / "Regal 1.2"
    window.files.save()
    assert window.path == tmp_path / "Regal 1.2.etiketto"  # the ".2" stays in the name
    file_dialog.result = tmp_path / "neu"
    window.files.save_as()
    assert window.path == tmp_path / "neu.etiketto" and window.path.exists()
    file_dialog.result = tmp_path / "GROSS.ETIKETTO"
    window.files.save_as()
    assert window.path == tmp_path / "GROSS.etiketto"  # what the template menu lists
    window.path = tmp_path / "neu.etiketto"
    window.change(tape_mm=9)
    window.files.save()  # known path: no dialog
    assert len(file_dialog.instances) == 4
    assert Label.load(tmp_path / "neu.etiketto").tape_mm == 9
    file_dialog.result = cancelled()
    window.activate_action("win.save-as", None)
    assert file_dialog.instances[-1].initial_name == "neu.etiketto"


def test_files_that_are_not_local_are_refused(window, file_dialog):
    file_dialog.result = "https://example.org/label"
    window.files.save_as()
    assert window.path is None
    window.files.open()
    assert toast_titles(window) == ["Only local files can be used"] * 2


def test_save_as_does_not_ask_twice_for_the_same_file(window, file_dialog, tmp_path):
    # a file system that ignores case: X.ETIKETTO and X.etiketto are one file
    (tmp_path / "x.etiketto").write_text("{}")
    (tmp_path / "x.ETIKETTO").hardlink_to(tmp_path / "x.etiketto")
    file_dialog.result = [tmp_path / "x.ETIKETTO"]  # overwriting it was confirmed
    window.files.save_as()
    assert window.path == tmp_path / "x.etiketto" and len(file_dialog.instances) == 1


def test_save_as_asks_again_before_overwriting_the_completed_name(window, file_dialog, tmp_path):
    (tmp_path / "taken.etiketto").write_text("{}")
    # typed "taken": the dialog had nothing to overwrite, but taken.etiketto exists
    file_dialog.result = [tmp_path / "taken", cancelled()]
    window.files.save_as()
    again = file_dialog.instances[-1]
    assert again.initial_name == "taken.etiketto" and again.initial_folder == str(tmp_path)
    assert window.path is None and (tmp_path / "taken.etiketto").read_text() == "{}"
    file_dialog.result = [tmp_path / "taken.etiketto"]  # confirmed in the dialog
    window.files.save_as()
    assert window.path == tmp_path / "taken.etiketto"


def test_save_template(window, file_dialog, gui_env):
    window.change(tape_mm=12)
    file_dialog.result = cancelled()
    window.activate_action("win.save-template", None)
    assert config.TEMPLATE_DIR.is_dir()
    assert file_dialog.instances[-1].initial_folder == str(config.TEMPLATE_DIR)
    assert file_dialog.instances[-1].kwargs["title"] == "Save as template…"
    file_dialog.result = config.TEMPLATE_DIR / "Kabel"
    window.files.save_template()
    assert Label.load(config.TEMPLATE_DIR / "Kabel.etiketto").tape_mm == 12
    assert toast_titles(window) == ["Saved Kabel.etiketto"]
    assert window.path is None and window.dirty  # the open label is unchanged
    file_dialog.result = gui_env.tmp / "missing" / "x.etiketto"
    window.files.save_template()
    assert toast_titles(window)[-1] == "Cannot save x.etiketto"


def test_export_png(window, file_dialog, tmp_path):
    file_dialog.result = cancelled()
    window.activate_action("win.export", None)
    assert file_dialog.instances[-1].initial_name == "label.png"
    file_dialog.result = tmp_path / "out"
    window.files.export()
    from PIL import Image

    with Image.open(tmp_path / "out.png") as img:
        assert img.size == window.rendered.image.size
        assert round(img.info["dpi"][0]) == 317
    assert toast_titles(window) == ["Exported"]
    file_dialog.result = tmp_path / "missing" / "x.PNG"
    window.files.export()
    assert toast_titles(window)[-1] == "Cannot save x.PNG"


def test_export_png_render_failure(window, tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(ui_window, "render", broken)
    window.files.export_png(tmp_path / "x.png")
    assert toast_titles(window) == ["The preview cannot be drawn"]
    assert not (tmp_path / "x.png").exists()


def test_templates_menu(window, gui_env):
    window.files.fill_templates()
    menu = window.template_menu
    assert menu.get_n_items() == 1
    label = menu.get_item_attribute_value(0, "label", GLib.VariantType("s")).get_string()
    assert label.startswith("No templates yet")
    config.TEMPLATE_DIR.mkdir(parents=True)
    for name in ("b", "a"):
        Label(tape_mm=9).save(config.TEMPLATE_DIR / ("%s.etiketto" % name))
    (config.TEMPLATE_DIR / "ignored.txt").write_text("")
    templates = [b for b in find(window, Gtk.MenuButton) if b.get_tooltip_text() == "Templates"]
    templates[0].set_active(True)  # opening the menu fills it
    templates[0].set_active(False)
    assert menu.get_n_items() == 2
    labels = [
        menu.get_item_attribute_value(i, "label", GLib.VariantType("s")).get_string()
        for i in range(2)
    ]
    assert labels == ["a", "b"]
    target = menu.get_item_attribute_value(1, "target", GLib.VariantType("s")).get_string()
    assert target == str(config.TEMPLATE_DIR / "b.etiketto")


def test_open_template_starts_unsaved_label(window, gui_env, tmp_path):
    path = tmp_path / "t.etiketto"
    Label(tape_mm=9, elements=[TextElement(text="Vorlage")]).save(path)
    window.path = tmp_path / "current.etiketto"
    window.activate_action("win.open-template", GLib.Variant.new_string(str(path)))
    assert window.label.tape_mm == 9 and window.path is None and not window.dirty
    window.activate_action("win.open-template", GLib.Variant.new_string(str(tmp_path / "no")))
    assert toast_titles(window) == ["Cannot open template no"] and window.label.tape_mm == 9


def test_new_label(window):
    window.change(tape_mm=9)
    window.activate_action("win.new", None)
    respond(window.get_visible_dialog(), "discard")
    assert window.label == Label(tape_mm=50, elements=[TextElement(text="Label")])
    window.printer.info = PrinterInfo("Wedge", 19, None, None, "IDLE")
    window.files.new()
    assert window.label.tape_mm == 19 and not window.dirty


# --- drag and drop ------------------------------------------------------------------


def drop(window, file):
    controllers = window.observe_controllers()
    targets = [
        controllers.get_item(i)
        for i in range(controllers.get_n_items())
        if isinstance(controllers.get_item(i), Gtk.DropTarget)
    ]
    assert len(targets) == 1
    from gi.repository import GObject

    return targets[0].emit("drop", GObject.Value(Gio.File, file), 0.0, 0.0)


def test_drop_image_and_label(window, tmp_path, saved):
    assert drop(window, Gio.File.new_for_path(str(png_file(tmp_path / "d.png")))) is True
    assert window.label.elements[-1].name == "d.png"
    window.dirty = False
    assert drop(window, Gio.File.new_for_path(str(saved))) is True
    assert window.path == saved


def test_drop_rejects(window, tmp_path):
    assert drop(window, Gio.File.new_for_uri("https://example.org/x.png")) is False
    (tmp_path / "t.txt").write_text("x")
    assert drop(window, Gio.File.new_for_path(str(tmp_path / "t.txt"))) is False
    assert toast_titles(window) == ["Only local files can be dropped", "Not an image: t.txt"]
    assert len(window.label.elements) == 1


def test_drop_label_on_dirty_window_asks(window, saved):
    window.change(margin_mm=4.0)
    assert drop(window, Gio.File.new_for_path(str(saved))) is True
    assert window.path is None
    respond(window.get_visible_dialog(), "discard")
    assert window.path == saved


# --- closing -------------------------------------------------------------------------


def test_close_request(window):
    window.print_flow.busy = True
    assert window._on_close_request(window) is True
    assert toast_titles(window) == ["Please wait until printing has finished"]
    window.print_flow.busy = False
    window.dirty = True
    assert window._on_close_request(window) is True
    dialog = window.get_visible_dialog()
    assert dialog.get_heading() == "Discard changes?"
    respond(dialog, "cancel")
    assert window.dirty
    window.dirty = False
    assert window._on_close_request(window) is False


def test_close_dirty_window_after_discard(window):
    closed = []
    window.close = lambda: closed.append(window.dirty)  # unmapped windows ignore close()
    window.change(margin_mm=1.0)
    assert window.emit("close-request") is True
    assert window.get_visible_dialog() is not None and not closed
    respond(window.get_visible_dialog(), "discard")
    assert closed == [False]  # dirty flag cleared, so the second request passes
    assert window.emit("close-request") is False


# --- application -------------------------------------------------------------------


_app_ids = iter(range(1000))


@pytest.fixture
def application(gui_env):
    application = app.App()
    # a fresh id: each registered application exports its own D-Bus object
    application.set_application_id("%s.Test%d" % (app.APP_ID, next(_app_ids)))
    application.set_flags(Gio.ApplicationFlags.NON_UNIQUE | Gio.ApplicationFlags.HANDLES_OPEN)
    assert application.register(None)
    yield application
    for win in list(application.get_windows()):
        win.dirty = False
        win.destroy()
    pump()


def test_zoom_before_the_first_render():
    view = preview.Preview()
    view.apply_zoom()  # nothing to size yet
    assert view.picture.get_size_request() == (-1, -1)


def test_app_activate_reuses_window(application):
    application.do_activate()
    first = application.get_active_window()
    assert isinstance(first, ui_window.Window)
    application.do_activate()
    assert application.get_active_window() is first
    assert len(application.get_windows()) == 1
    assert application.get_accels_for_action("win.print") == ["<Control>p"]
    # title bar icon for decoration layouts that show one
    assert Gtk.Window.get_default_icon_name() == app.APP_ID


def test_app_open_file(application, saved):
    application.do_open([Gio.File.new_for_path(str(saved))], 1, "")
    win = application.get_active_window()
    assert win.path == saved and win.label.tape_mm == 19
    application.do_open([Gio.File.new_for_uri("https://example.org/a.etiketto")], 1, "")
    assert application.get_active_window() is win and win.path == saved
    application.do_open([], 0, "")
    assert len(application.get_windows()) == 1


def test_about_dialog(application):
    application.do_activate()
    application.activate_action("about", None)
    dialog = application.get_active_window().get_visible_dialog()
    assert isinstance(dialog, Adw.AboutDialog)
    assert dialog.get_application_name() == "Etiketto"
    assert dialog.get_version() == app.__version__
    assert dialog.get_license_type() == Gtk.License.AGPL_3_0


def test_rtl_detection_follows_the_loaded_catalog():
    class Catalog:
        def __init__(self, language):
            self._language = language

        def info(self):
            return {"language": self._language}

    assert i18n.translation_is_rtl(Catalog("ar"))
    assert i18n.translation_is_rtl(Catalog("he_IL"))
    assert not i18n.translation_is_rtl(Catalog("de"))
    assert not i18n.translation_is_rtl(Catalog(""))
    assert not i18n.translation_is_rtl(object())  # NullTranslations without info()


# --- messages ---------------------------------------------------------------------------

I = "⁨%s⁩"  # noqa: E741 - isolated text, as in the interface


def test_warning_texts():
    from etiketto.render import RenderWarning

    expected = {
        "code_empty": "Element 2: the code is empty",
        "ean13_digits": "Element 2: EAN-13 needs exactly 12 digits",
        "ean13_check": "Element 2: the EAN-13 check digit is wrong",
        "code128_charset": "Element 2: Code 128 only accepts Latin letters, digits and punctuation",
        "image_unreadable": "Element 2: the image cannot be read",
        "element_failed": "Element 2 cannot be drawn",
    }
    for key, text in expected.items():
        assert texts.warning_text(RenderWarning(key, {"n": 2})) == text
    assert texts.warning_text(RenderWarning("no_room")) == (
        "Safety margin, border and edge spacing leave no room – reduce them"
    )
    assert texts.warning_text(RenderWarning("fixed_too_short")) == (
        "The fixed length is shorter than the spacing – increase it"
    )
    assert texts.warning_text(RenderWarning("background_unreadable")) == (
        "The background image cannot be read"
    )
    assert texts.warning_text(RenderWarning("from_the_future")) == "from_the_future"
    gs1 = RenderWarning("gs1_check", {"n": 1, "ai": "01"})
    assert texts.warning_text(gs1) == (
        "Element 1: the check digit of application identifier (01) is wrong"
    )
    code39 = texts.warning_text(RenderWarning("code39_charset", {"n": 3}))
    assert code39.endswith("spaces and - . $ / + %")  # %% in the message


def test_every_code_problem_has_a_text_and_every_type_help():
    from etiketto import codes, model
    from etiketto.render import RenderWarning

    keys = {key for _length, key in codes.DIGITS.values()}
    keys |= {key for _chars, key in codes.CHARSETS.values()}
    keys |= {"code_empty", "code_check", "ean13_check", "gs1_syntax", "gs1_value"}
    keys |= {"gs1_check", "code_too_long", "code_invalid", "code_unavailable"}
    keys |= set(codes.GS1_ERRORS.values())
    for key in keys:
        assert texts.warning_text(RenderWarning(key, {"n": 1, "ai": "10"})) != key
    assert set(texts.SYMBOLOGY_HELP) == {key for key, _name in texts.SYMBOLOGIES}
    assert sorted(key for key, _name in texts.SYMBOLOGIES) == sorted(model.SYMBOLOGIES)


@pytest.mark.parametrize(
    "error, start",
    [
        (PrinterNotFound(), "The printer cannot be found."),
        (PrinterTimeout(), "The printer does not answer."),
        (PortClosed(), "The printer refuses the connection."),
        (NoCassette(), "No cassette is inserted."),
        (CoverOpen(), "The printer's cover is open."),
        (PaperJam(), "The label is jammed."),
        (BadCassette(), "The cassette is not recognised."),
        (PrinterBusy(), "The printer is busy."),
        (PrinterError("SEQUENCE ERROR"), "The printer reported a problem."),
        (OSError("anything else"), "The printer reported a problem."),
    ],
)
def test_printer_error_texts(error, start):
    text = texts.printer_error_text(error, MILLIMETRES)
    assert text.startswith(start)
    assert "SEQUENCE" not in text and "anything" not in text  # details stay out


def test_printer_error_too_long_uses_the_unit():
    error = LabelTooLong("17.5 in", max_mm=431.8)
    assert texts.printer_error_text(error, MILLIMETRES) == (
        "The label is too long for this printer: at most 431.8 mm."
    )
    assert texts.printer_error_text(error, INCHES).endswith("at most 17.00 in.")


def test_stage_texts():
    assert texts.stage_text("PREHEATING") == "Warming up…"
    assert texts.stage_text("COOLDOWN") == "Cooling down…"
    assert texts.stage_text("CLEANING") == "Cleaning…"
    assert texts.stage_text("READY_FOR_CUT") == "Swipe the printer's panel to cut the label"
    assert texts.stage_text("PRINTING") is None and texts.stage_text("") is None
    # the system printer waits for these to be fixed
    assert texts.stage_text("OUT_OF_PAPER") == "Out of paper – load paper to continue"
    assert texts.stage_text("COVER_OPEN") == "Cover open – close it to continue"
    assert texts.stage_text("MEDIA_JAM") == "Paper jammed – remove it to continue"


def test_label_and_save_error_texts():
    from etiketto.model import LabelError

    assert texts.label_error_text(LabelError("not_label", "x"), "a.etiketto") == (
        "a.etiketto is not an Etiketto label"
    )
    assert texts.label_error_text(LabelError("newer", "x"), "a.etiketto") == (
        "This label needs a newer version of Etiketto"
    )
    assert texts.label_error_text(LabelError("damaged", "x"), "a.etiketto") == (
        "The file is damaged: a.etiketto"
    )
    for code in (errno.EACCES, errno.EPERM, errno.EROFS):
        assert texts.save_error_text(OSError(code, "x"), "a") == (
            "Cannot save a: no permission to write there"
        )
    for code in (errno.ENOSPC, errno.EDQUOT):
        assert texts.save_error_text(OSError(code, "x"), "a") == "Cannot save a: the disk is full"
    assert texts.save_error_text(OSError(errno.EIO, "x"), "a") == "Cannot save a"


def _with_cassette(made):
    return PrinterInfo("Wedge", 25, None, None, "IDLE", cassette=CassetteInfo(made=made))


def test_cassette_age_hint():
    today = date(2026, 11, 5)
    assert texts.cassette_age_hint(_with_cassette(date(2018, 5, 29)), today) == (
        "This cassette seems to be from May 2018. Old ZINK paper prints paler colours."
    )
    assert texts.cassette_age_hint(_with_cassette(today - timedelta(days=731)), today)
    assert texts.cassette_age_hint(_with_cassette(today - timedelta(days=730)), today) is None
    assert texts.cassette_age_hint(_with_cassette(None), today) is None
    assert texts.cassette_age_hint(PrinterInfo("Wedge", 25, None, None, "IDLE"), today) is None
    assert texts.cassette_age_hint(None, today) is None
    assert [i18n.month_name(m) for m in (1, 12)] == ["January", "December"]


def test_month_names_have_a_context(monkeypatch):
    class Catalog:
        def pgettext(self, context, message):
            return "%s|%s" % (context, message)

    monkeypatch.setattr(i18n, "TRANSLATION", Catalog())
    assert i18n.month_name(5) == "month name|May"


# --- printer details and the cassette ------------------------------------------------------

ALL_MODES = (PrintMode("vivid", 317, 0), PrintMode("color", 264, 1), PrintMode("bw", 400, 2))


@pytest.fixture
def detailed(make_window, gui_env):
    """Connected to a printer that reports modes, firmware and an old cassette."""
    gui_env.configured = "VC.local"
    gui_env.query_result = PrinterInfo(
        "Wedge",
        25,
        4000,
        5000,
        "IDLE",
        modes=ALL_MODES,
        firmware="2026031504",
        cassette=CassetteInfo("ABC123456", "180529-0116G", date(2018, 5, 29), 24.1),
    )
    return make_window()


def test_printer_details_dialog(detailed):
    assert detailed.printer.details_button.get_sensitive()
    one(detailed.sidebar, Gtk.Button, tooltip="Printer details").emit("clicked")
    dialog = detailed.get_visible_dialog()
    assert dialog is detailed.last_dialog and dialog.get_heading() == "Printer details"
    rows = find(dialog.get_extra_child(), Adw.ActionRow)
    assert {r.get_title(): r.get_subtitle() for r in rows} == {
        "Model": "Brother VC-500W",
        "Address": I % "VC.local",
        "Firmware": "2026031504",
        "Cassette": I % "25 mm",
        "Remaining tape": "4 m of 5 m left",
        "Printable width": "24.1 mm",
        "Lot number": I % "ABC123456",
        "Serial number": I % "180529-0116G",
        "Probably made": "May 2018",
    }
    assert all(r.has_css_class("property") for r in rows)
    assert dialog.get_body().startswith("This cassette seems to be from May 2018.")
    respond(dialog, "close")


def test_printer_details_only_what_is_known(window):
    window.printer.show_details()  # not connected: nothing to show
    assert window.get_visible_dialog() is None
    window.printer.info = PrinterInfo("Wedge", None, None, None, "IDLE")
    assert window.printer.details() == [("Model", "Brother VC-500W")]
    window.printer.show_details()
    assert window.get_visible_dialog().get_body() == ""
    respond(window.get_visible_dialog(), "close")


def test_old_cassette_hint_row(detailed, gui_env):
    hint = detailed.printer.hint
    assert hint.get_visible()
    assert hint.get_title() == (
        "This cassette seems to be from May 2018. Old ZINK paper prints paler colours."
    )
    gui_env.query_result = PrinterInfo("Wedge", 25, None, None, "IDLE")
    detailed.printer.refresh()
    assert pump(lambda: not hint.get_visible())
    detailed.printer.found(_with_cassette(date(2018, 1, 1)))
    assert hint.get_visible()
    detailed.printer.failed(PrinterTimeout())
    assert not hint.get_visible()


def test_old_printer_answers_are_dropped(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    current = win.printer.generation
    win.printer.found(PrinterInfo("Wedge", 12, None, None, "IDLE"), current - 1)
    assert win.printer.info.tape_mm == 25
    win.printer.failed(PortClosed(), current - 1)
    assert win.printer.row.get_title() == "Brother VC-500W"
    win.printer.discover()  # a search makes a pending status answer outdated, too
    assert win.printer.generation == current + 1
    win.printer.found(PrinterInfo("Wedge", 12, None, None, "IDLE"), current)
    assert win.printer.info.tape_mm == 25
    assert pump(lambda: not win.printer.row.get_title().startswith("Searching"))


# --- printing: options from the capabilities -------------------------------------------------


def test_print_dialog_offers_the_printers_modes(detailed, gui_env):
    detailed.print_flow.on_print()
    box = detailed.last_dialog.get_extra_child()
    mode = box.get_row_at_index(2)
    assert [mode.get_model().get_string(i) for i in range(3)] == [
        "Vivid",
        "Normal",
        "Black and white (sharper)",
    ]
    mode.set_selected(2)
    detailed.last_dialog.emit("response", "print")
    assert pump(lambda: not detailed.print_flow.busy)
    assert gui_env.prints[0][2].mode == "bw"


def test_saved_mode_the_printer_lacks_falls_back(ready):
    ready.printer_settings.update(mode="bw", cut="sideways")
    assert ready.print_flow.choices() == {
        "copies": 1,
        "cut": "full",
        "mode": "vivid",
        "quality": "",  # the VC-500W has no choice of quality
    }


def test_colour_adjustment(ready, gui_env):
    ready.print_flow.on_print()
    colour = ready.last_dialog.get_extra_child().get_row_at_index(3)
    assert colour.get_subtitle() == "Not adjusted"
    reset = [b for b in find(colour, Gtk.Button) if b.get_label() == "Reset"][0]
    assert not reset.get_sensitive()  # nothing to reset
    spins = find(colour, Adw.SpinRow)
    assert [s.get_title() for s in spins] == ["Brightness", "Red", "Green", "Blue"]
    assert all(s.get_adjustment().get_lower() == -3 for s in spins)
    assert all(s.get_adjustment().get_upper() == 3 for s in spins)
    spins[0].set_value(2)
    spins[2].set_value(-3)
    assert Settings.load().data["printers"]["vc500w"]["color_adjust"] == {
        "brightness": 2,
        "red": 0,
        "green": -3,
        "blue": 0,
    }
    assert colour.get_subtitle() == "Adjusted" and reset.get_sensitive()
    assert not ready.dirty  # a printer setting, not part of the label
    ready.last_dialog.emit("response", "print")
    assert pump(lambda: not ready.print_flow.busy)
    assert gui_env.prints[0][2].color == ColorAdjust(2, 0, -3, 0)

    ready.print_flow.on_print()  # remembered; the reset button puts everything back
    colour = ready.last_dialog.get_extra_child().get_row_at_index(3)
    spins = find(colour, Adw.SpinRow)
    assert [s.get_value() for s in spins] == [2, 0, -3, 0]
    reset = [b for b in find(colour, Gtk.Button) if b.get_label() == "Reset"][0]
    assert reset.get_sensitive()
    reset.emit("clicked")
    assert [s.get_value() for s in spins] == [0, 0, 0, 0]
    assert (
        ready.printer_settings.color_adjust.is_neutral()
        and colour.get_subtitle() == "Not adjusted"
    )
    assert not reset.get_sensitive()
    respond(ready.last_dialog, "cancel")


@pytest.mark.parametrize(
    "setting, expected", [(None, "rawrgb"), ("png", "rawrgb"), ("jpeg", "jpeg")]
)
def test_data_format_setting(make_window, gui_env, setting, expected):
    gui_env.configured = "VC.local"
    printer = {} if setting is None else {"data_format": setting}
    win = make_window(settings={"printers": {"vc500w": printer}})
    win.print_flow.on_print()
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    image, _host, options = gui_env.prints[0]
    assert options.data_format == expected
    # rawrgb: rendered with bleed for the exact width (25 mm at 320 dpi: 333 px)
    assert image.height == (333 if expected == "rawrgb" else mm_to_px(25))


def test_print_with_another_mode_is_reported(ready, gui_env):
    gui_env.print_result = PrintResult(mode="vivid")
    ready.print_flow.start(PrintOptions(mode="bw"))
    assert pump(lambda: not ready.print_flow.busy)
    assert toast_titles(ready) == [
        "Colour mode “Black and white (sharper)” is not available on this printer, "
        "printed with “Vivid”"
    ]


def test_print_result_with_the_requested_mode_is_just_printed(ready, gui_env):
    gui_env.print_result = PrintResult(mode="color")
    ready.print_flow.start(PrintOptions(mode="color"))
    assert pump(lambda: not ready.print_flow.busy)
    assert toast_titles(ready) == ["Printed"]


def test_a_queued_job_is_reported_as_sent(ready, gui_env):
    gui_env.print_result = PrintResult(queued=True)
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    assert toast_titles(ready) == ["Sent to the printer – it prints when its turn comes"]


def test_a_job_the_printer_did_not_confirm_is_reported_as_sent(ready, gui_env):
    gui_env.print_result = PrintResult(unconfirmed=True)
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    assert toast_titles(ready) == ["Sent to the printer – it did not confirm printing"]


def test_bw_mode_shows_a_grey_preview(make_window, gui_env):
    from PIL import Image

    gui_env.configured = "VC.local"
    gui_env.query_result = PrinterInfo("Wedge", 25, 4000, 5000, "IDLE", modes=ALL_MODES)
    win = make_window(settings={"printers": {"vc500w": {"mode": "bw"}}})
    win.change(background="#ff0000")
    render_now(win)
    assert win.print_flow.mode == "bw"
    png = win.preview.picture.get_paintable().save_to_png_bytes().get_data()
    assert all(r == g == b for r, g, b, *_ in _colours(Image.open(io.BytesIO(png))))
    # choosing a colour mode in the print dialog shows colours again
    win.print_flow.on_print()
    mode = win.last_dialog.get_extra_child().get_row_at_index(2)
    mode.set_selected(0)
    assert Settings.load().data["printers"]["vc500w"]["mode"] == "vivid"
    render_now(win)
    png = win.preview.picture.get_paintable().save_to_png_bytes().get_data()
    assert (255, 0, 0) in {c[:3] for c in _colours(Image.open(io.BytesIO(png)))}
    respond(win.last_dialog, "cancel")


def test_length_row_is_limited_by_the_printer(window):
    length = one(window.sidebar, Adw.SpinRow, title="Length")
    adjustment = length.get_adjustment()
    assert (adjustment.get_lower(), adjustment.get_upper()) == (15, 431)


def test_too_long_label_is_reported(window):
    window.change(auto_length=False, length_mm=600)
    render_now(window)
    assert "Too long for this printer: at most 431.8 mm" in window.preview.status.get_text()
    window.change(length_mm=400)
    render_now(window)
    assert "Too long" not in window.preview.status.get_text()


def test_printer_stages_in_a_banner(ready, gui_env):
    banner = ready.banner
    ready.print_flow.busy = True
    ready.print_flow.progress(Progress(1, 2, "PREHEATING"))
    assert banner.get_revealed() and banner.get_title() == "Warming up…"
    assert ready.print_button.get_label() == "Printing 1/2…"
    ready.print_flow.progress(Progress(2, 2, "PRINTING"))
    assert not banner.get_revealed()
    assert ready.print_button.get_label() == "Printing 2/2…"
    ready.print_flow.progress(Progress(2, 2, "READY_FOR_CUT"))
    assert html.unescape(banner.get_title()) == ("Swipe the printer's panel to cut the label")
    ready.print_flow.done(None)
    assert not banner.get_revealed()
    ready.print_flow.progress(Progress(1, 1, "COOLDOWN"))  # late event after the end
    assert not banner.get_revealed() and ready.print_button.get_label() == "Print"

    gui_env.stages = ["COOLDOWN"]
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    assert not banner.get_revealed()
    assert toast_titles(ready)[-1] == "Printed"


def test_print_prepare_failure(ready, gui_env, monkeypatch):
    def broken(label, options, safe_mm):
        raise RuntimeError("boom")

    monkeypatch.setattr(gui_env.backend, "prepare", broken)
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    dialog = ready.get_visible_dialog()
    assert dialog.get_body() == "The label cannot be prepared for printing."
    assert dialog.get_extra_child().get_child().get_label() == "RuntimeError: boom"
    assert gui_env.prints == []
    respond(dialog, "close")


def test_print_too_long(ready, gui_env):
    gui_env.print_error = LabelTooLong("17.5 in", max_mm=431.8)
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    dialog = ready.get_visible_dialog()
    assert dialog.get_body() == "The label is too long for this printer: at most 431.8 mm."
    assert dialog.get_extra_child().get_child().get_label() == "17.5 in"
    respond(dialog, "close")


def test_report_without_browser_offers_the_link(window, launched, monkeypatch):
    launched.fail = True
    window.report_problem("broken")
    toast = window.toast_log[-1]
    assert toast.get_title() == "The browser cannot be opened"
    assert toast.get_button_label() == "Copy link"
    copied = []
    monkeypatch.setattr(window, "get_clipboard", lambda: SimpleNamespace(set=copied.append))
    toast.emit("button-clicked")
    assert copied == [launched[0]]


def test_length_rows_do_not_change_on_focus(window):
    """The spin button rounds to its digits when it loses focus: the value it
    shows must already be rounded, or the label would become "changed"."""
    got = []
    for mm in (15.0, 60.25):
        row = widgets.length_row(window.unit, "Length", mm, 15, 1000, 1, got.append)
        row.update()
        assert row.get_value() == round(mm)
    assert got == []


# --- other printer models ------------------------------------------------------------------


@pytest.fixture
def mono(gui_env):
    from fake_backends import FakeMono

    import etiketto.backends as registry

    backend = FakeMono()
    registry.register(backend)
    yield backend
    registry.unregister(backend.id)


def _colours(image):
    return {c for _n, c in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def test_mono_printer(make_window, mono, gui_env):
    from PIL import Image

    win = make_window()
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    names = _items(model)
    assert names[0] == "Brother VC-500W" and "Fake Mono 200" in names
    model.set_selected(names.index("Fake Mono 200"))
    assert win.label.printer == "fake-mono" and win.backend is mono
    assert win.label.tape_mm == 24 and win.dirty  # nearest width the printer has
    tape = win.label_group.tape_row.get_model()
    assert [tape.get_string(i) for i in range(tape.get_n_items())] == ["12 mm", "24 mm"]
    # no host for this printer yet: searched, found and remembered for it alone
    assert pump(lambda: win.printer.info is mono.info)
    assert Settings.load().data["printers"]["fake-mono"]["host"] == "mono.local"
    assert mono.queries == ["mono.local"]
    assert "host" not in Settings.load().data["printers"].get("vc500w", {})
    # no remaining tape: the printer does not report it
    assert win.printer.row.get_subtitle() == "⁨12 mm⁩ cassette"

    render_now(win)
    png = win.preview.picture.get_paintable().save_to_png_bytes().get_data()
    assert _colours(Image.open(io.BytesIO(png))) <= {(0, 0, 0), (255, 255, 255)}

    win.change(tape_mm=12)
    render_now(win)
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    assert box.get_row_at_index(0).get_title() == "Copies"
    assert box.get_row_at_index(1) is None  # no cut, a single mode, no colour correction
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    image, host, options = mono.jobs[0]
    assert host == "mono.local"
    assert (options.cut, options.mode, options.data_format) == ("", "standard", "jpeg")
    assert _colours(image) <= {(0, 0, 0), (255, 255, 255)}
    assert image.height == mm_to_px(12, 203)

    win.dirty = False
    win.files.new()  # a new label stays with this printer
    assert win.label.printer == "fake-mono" and win.label.tape_mm == 12
    win.printer.info = None
    win.files.new()
    assert win.label.tape_mm == 24  # no 50 mm here: the widest tape


def test_no_length_rows_for_die_cut_printers(make_window, mono, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(mono, "capabilities", replace(mono.capabilities, continuous=False))
    win = make_window()
    assert find(win.sidebar, Adw.SpinRow, title="Length")
    win.set_backend("fake-mono")
    assert not find(win.sidebar, Adw.SpinRow, title="Length")
    assert not find(win.sidebar, Adw.SwitchRow, title="Automatic length")
    assert not find(win.sidebar, Adw.ComboRow, title="Tape width")
    # no sizes of its own: 50 × 25 mm, entered by hand
    size = one(win.sidebar, Adw.ComboRow, title="Label size")
    assert size.get_model().get_n_items() == 1 and win.label.media_size() == (50, 25)


# --- cab: die-cut labels ---------------------------------------------------------------------


@pytest.fixture
def cab_env(gui_env, monkeypatch):
    """The cab backend with faked query/print/discover; a 50 × 25 mm roll with
    3 mm gaps (a label every 28 mm) in the printer."""
    from etiketto.backends import get_backend

    backend = get_backend("cab")
    env = SimpleNamespace(
        backend=backend,
        info=PrinterInfo(
            "EOS2/300", None, None, None, "IDLE", firmware="5.47.7 Jan 22, 2026", pitch_mm=28.0
        ),
        error=None,
        queries=[],
        prints=[],
    )

    def query(host):
        env.queries.append(host)
        if env.error:
            raise env.error
        return env.info

    def print_label(image, host, options, progress=None):
        env.prints.append((image, host, options))
        progress(Progress(1, options.copies))

    monkeypatch.setattr(backend, "query", query)
    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(
        backend,
        "discover",
        lambda timeout=3.0: [
            FoundPrinter("cab EOS2/300 (aa:bb:cc)", "printer04.local", "192.0.2.21")
        ],
    )
    return env


@pytest.fixture
def cab_window(make_window, cab_env):
    win = make_window()
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    model.set_selected(_items(model).index("cab EOS2, EOS5, SQUIX 4, MACH 4S"))
    assert pump(lambda: win.printer.info is cab_env.info)
    render_now(win)
    return win


def _items(row):
    model = row.get_model()
    return [model.get_string(i) for i in range(model.get_n_items())]


def _selected(row):
    return _items(row)[row.get_selected()]


def test_cab_printer(cab_window, cab_env):
    win = cab_window
    assert win.label.printer == "cab" and win.dirty
    assert win.label.media_size() == (50, 25)
    assert win.label.orientation == "across" and not win.label.auto_length
    assert Settings.load().data["printers"]["cab"]["host"] == "printer04.local"
    assert win.printer.row.get_title() == "cab EOS2/300"
    assert win.printer.row.get_subtitle() == "Ready · a label every 28.0 mm"
    size = one(win.sidebar, Adw.ComboRow, title="Label size")
    assert _items(size) == [
        "50 × 25 mm",
        "38 × 25 mm",
        "57 × 32 mm",
        "76 × 51 mm",
        "100 × 50 mm",
        "100 × 100 mm",
        "100 × 150 mm",
        "Custom size",
    ]
    assert _selected(size) == "50 × 25 mm"
    for title in ("Tape width", "Automatic length"):
        assert not find(win.sidebar, Adw.ComboRow, title=title)
        assert not find(win.sidebar, Adw.SwitchRow, title=title)
    assert one(win.sidebar, Adw.SpinRow, title="Width").get_adjustment().get_upper() == 105.6
    assert win.preview.status.get_text().startswith("50 × 25 mm")
    # the preview is the label as read: 50 mm wide, 25 mm high
    assert win.rendered.image.size == (mm_to_px(50, 305), mm_to_px(25, 305))


def test_cab_label_sizes(cab_window):
    win = cab_window
    size = one(win.sidebar, Adw.ComboRow, title="Label size")
    width = one(win.sidebar, Adw.SpinRow, title="Width")
    height = one(win.sidebar, Adw.SpinRow, title="Height")
    size.set_selected(4)
    assert win.label.media_size() == (100, 50)
    assert (width.get_value(), height.get_value()) == (100, 50)
    height.set_value(40)
    assert win.label.media_size() == (100, 40) and _selected(size) == "Custom size"
    height.set_value(50)
    assert _selected(size) == "100 × 50 mm"  # a known size again
    size.set_selected(7)  # "Custom size" alone changes nothing
    assert win.label.media_size() == (100, 50)
    one(win.sidebar, Adw.SpinRow, title="Gap").set_value(2)
    assert win.label.gap_mm == 2 and win.label.pitch_mm() == 52


def test_cab_sizes_in_inches(cab_window):
    win = cab_window
    win.set_units("in")
    size = one(win.sidebar, Adw.ComboRow, title="Label size")
    assert _items(size)[0] == "1.97 × 0.98″"
    one(win.sidebar, Adw.ComboRow, title="Label size").set_selected(4)
    assert win.label.media_size() == (100, 50)  # not rounded through the inch rows


def test_cab_orientation(cab_window):
    win = cab_window
    direction = one(win.sidebar, Adw.ComboRow, title="Content direction")
    assert _items(direction) == ["Across the roll", "Along the roll"]
    direction.set_selected(1)
    assert win.label.orientation == "along" and win.label.media_size() == (50, 25)
    render_now(win)
    # read along the roll: the preview is 25 mm long and 50 mm high
    assert win.rendered.image.size == (mm_to_px(25, 305), mm_to_px(50, 305))
    assert win.preview.status.get_text().startswith("50 × 25 mm")


def test_cab_print(cab_window, cab_env):
    win = cab_window
    win.print_flow.on_print()
    dialog = win.last_dialog
    assert dialog.get_heading() == "Print label" and dialog.get_body() == "50 × 25 mm"
    box = dialog.get_extra_child()
    assert box.get_row_at_index(0).get_title() == "Copies"
    assert box.get_row_at_index(1) is None  # no cutter, one mode
    dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    image, host, options = cab_env.prints[0]
    assert host == "printer04.local" and options.gap_mm == 3
    assert image.size == (600, 300)  # printer orientation, 12 dots per mm
    assert toast_titles(win)[-1] == "Printed"


@pytest.mark.parametrize("answer, prints", [("cancel", False), ("print", True)])
def test_cab_different_labels_loaded(cab_window, cab_env, answer, prints):
    win = cab_window
    cab_env.info.pitch_mm = 20.0
    win.print_flow.on_print()
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Different labels loaded"
    body = dialog.get_body()
    assert "50 × 25 mm" in body and "3.0 mm" in body and "28.0 mm" in body and "20.0 mm" in body
    respond(dialog, answer)
    following = win.get_visible_dialog()
    assert (following is not None and following.get_heading() == "Print label") == prints


def test_cab_close_enough_pitch_prints_directly(cab_window, cab_env):
    cab_env.info.pitch_mm = 28.8  # the sensor measures in whole mm
    cab_window.print_flow.on_print()
    assert cab_window.get_visible_dialog().get_heading() == "Print label"


def test_cab_too_wide(cab_window):
    win = cab_window
    win.label.set_media_size(120, 25)
    render_now(win)
    assert "Too wide for this printer: at most 105.6 mm" in win.preview.status.get_text()


def test_cab_printer_states(make_window, cab_env):
    from etiketto.backends.base import HeadOpen

    win = make_window()
    win.set_backend("cab")
    cab_env.info.error, cab_env.info.state = "out_of_labels", "ERROR"
    win.printer.refresh()
    assert pump(lambda: win.printer.info is cab_env.info)
    assert win.printer.row.get_title() == "Printer needs attention"
    assert win.printer.row.get_subtitle().startswith("The printer is out of labels")
    cab_env.info.error, cab_env.info.state = "", "BUSY"
    win.printer.update_row()
    assert win.printer.row.get_subtitle() == "Printing"
    cab_env.info.state, cab_env.info.pitch_mm = "IDLE", None
    win.printer.update_row()
    assert win.printer.row.get_subtitle() == "Ready"
    cab_env.error = HeadOpen("ESC s YD000000N")
    win.printer.refresh()
    assert pump(lambda: win.printer.row.get_title() == "Printer needs attention")
    assert win.printer.row.get_subtitle() == "The print head is open. Close it and try again."
    cab_env.error = PrinterTimeout("timed out")
    win.printer.refresh()
    assert pump(lambda: win.printer.row.get_title() == "Printer not reachable")


def test_tape_printer_reporting_a_problem(make_window, gui_env):
    gui_env.configured = "VC.local"
    gui_env.query_result = PrinterInfo("Wedge", 25, 4000, 5000, "ERROR", error="cover_open")
    win = make_window()
    assert win.printer.row.get_title() == "Printer needs attention"
    assert html.unescape(win.printer.row.get_subtitle()) == (
        "The printer's cover is open. Close it and try again."
    )


def test_cab_details_and_report(cab_window):
    win = cab_window
    details = dict(win.printer.details())
    assert details["Model"] == "cab EOS2/300"
    assert details["Label distance"] == "28.0 mm"
    assert "Cassette" not in details and "Remaining tape" not in details
    assert (
        win.printer.description()
        == "cab EOS2/300, label every 28 mm, IDLE, firmware 5.47.7 Jan 22, 2026"
    )
    win.printer.info.pitch_mm = None
    assert "label every ? mm" in win.printer.description()


def test_cab_back_to_tape_after_a_preset(cab_window):
    win = cab_window
    one(win.sidebar, Adw.ComboRow, title="Label size").set_selected(4)  # 100 × 50 mm
    win.set_backend("vc500w")
    assert win.label.tape_mm == 50 and isinstance(win.label.tape_mm, int)
    assert _selected(one(win.sidebar, Adw.ComboRow, title="Tape width")) == "50 mm"


def test_opening_a_label_for_the_other_printer(cab_window, cab_env, gui_env):
    win = cab_window
    gui_env.configured = "VC-500W.local"
    win.set_label(Label(tape_mm=25), None)  # a VC-500W label
    assert win.backend.id == "vc500w"
    assert pump(lambda: win.printer.info is gui_env.query_result)
    assert win.printer.row.get_title() == "Brother VC-500W"
    win.set_label(Label(tape_mm=25), None)  # same printer: no new query
    assert win.printer.info is gui_env.query_result


def test_cab_gap_row_allows_what_files_may_have(cab_window):
    gap = one(cab_window.sidebar, Adw.SpinRow, title="Gap")
    assert gap.get_adjustment().get_upper() == 50


def test_cab_new_label_and_back_to_tape(cab_window):
    win = cab_window
    win.label.set_media_size(100, 150)
    win.dirty = False
    win.files.new()
    assert win.label.printer == "cab" and win.label.media_size() == (50, 25)
    win.set_backend("vc500w")
    assert win.label.orientation == "along" and win.label.auto_length
    assert win.label.tape_mm == 25  # the nearest tape to 25 mm (label space)
    assert find(win.sidebar, Adw.ComboRow, title="Tape width")


def test_switching_back_restores_the_size(window):
    win = window
    win.label.tape_mm, win.label.auto_length, win.label.length_mm = 19, False, 80.0
    win.set_backend("cab")
    win.label.set_media_size(76, 51)
    win.set_backend("vc500w")
    assert (win.label.tape_mm, win.label.auto_length, win.label.length_mm) == (19, False, 80.0)
    win.set_backend("cab")
    assert win.label.media_size() == (76, 51)
    win.set_label(Label(tape_mm=25), None)  # another label: nothing to restore
    win.set_backend("cab")
    assert win.label.media_size() == (50, 25)


@pytest.mark.parametrize(
    "code, start",
    [
        ("too_wide", "The label is too wide for this printer: at most 105.6 mm."),
        ("out_of_labels", "The printer is out of labels."),
        ("head_open", "The print head is open."),
        ("label_not_found", "The printer cannot find the next label."),
        ("ribbon", "There is a problem with the ribbon."),
        ("paused", "The printer is paused."),
    ],
)
def test_die_cut_error_texts(code, start):
    assert texts.code_text(code, MILLIMETRES, 105.6).startswith(start)


def test_choosing_the_same_model_changes_nothing(window, mono):
    window.set_backend("vc500w")
    assert not window.dirty


def test_settings_are_robust(window):
    window.settings.data["printers"] = "junk"
    assert window.printer_settings.get("host") is None
    assert window.settings.data["printers"] == {"vc500w": {}}
    window.settings.data["printers"]["vc500w"] = 5
    assert window.printer_settings.get("host") is None
    window.printer_settings.update(safe_mm="wide")
    assert window.printer_settings.safe_mm == 1.5


def test_docs_url_follows_the_interface_language():
    class Catalog:
        def __init__(self, language):
            self._language = language

        def info(self):
            return {"language": self._language}

    assert i18n.docs_url(Catalog("de")) == "https://etiketto.joshuahirsig.xyz/de/"
    assert i18n.docs_url(Catalog("de_CH")) == "https://etiketto.joshuahirsig.xyz/de/"
    # no documentation in that language yet: English
    assert i18n.docs_url(Catalog("ja")) == "https://etiketto.joshuahirsig.xyz/en/"
    assert i18n.docs_url(object()) == "https://etiketto.joshuahirsig.xyz/en/"


def test_month_year_is_one_translatable_pattern():
    assert i18n.month_year(5, 2018) == "May 2018"


def test_help_action_opens_the_documentation(make_window, monkeypatch):
    win = make_window()
    opened = []
    monkeypatch.setattr(win, "open_url", opened.append)
    win.activate_action("win.help", None)
    assert opened == [i18n.docs_url()]


def test_too_long_fixed_length_from_a_file_is_clamped_consistently(make_window):
    win = make_window()
    win.set_label(Label(auto_length=False, length_mm=1000), None)
    # the row shows the printer's maximum, and the label really uses it
    assert win.label.length_mm == pytest.approx(431, abs=1)


def test_system_printer_has_a_translated_name_and_paper_texts():
    from etiketto.backends import get_backend

    assert texts.backend_name(get_backend("system")) == "System printer (CUPS, IPP)"
    assert texts.backend_name(get_backend("cab")) == get_backend("cab").name
    for code, start in (
        ("out_of_paper", "The printer is out of paper."),
        ("media_jam", "The paper is jammed."),
        ("supplies", "The printer is out of toner or ink."),
        ("job_held", "The print job was held in the queue"),
        ("job_canceled", "The print job was canceled at the printer or in the queue."),
    ):
        assert texts.code_text(code, MILLIMETRES).startswith(start)


def test_a_partly_printed_job_says_how_many_labels_came_out():
    error = PrinterError("aborted", printed=2, copies=5)
    assert texts.printer_error_text(error, MILLIMETRES) == (
        "The printer reported a problem. Switch it off and on again, then try once more. "
        "Labels already printed: 2 of 5."
    )
    assert "already printed" not in texts.printer_error_text(PrinterError("x"), MILLIMETRES)
    sheets = PrinterError("aborted", printed=10, copies=23, pages=True)
    assert texts.printer_error_text(sheets, MILLIMETRES).endswith(
        "Sheets already printed: 10 of 23."
    )


def test_a_busy_printer_with_canceled_queued_labels():
    error = PrinterBusy("0x0507", printed=1, copies=4, queue_canceled=True)
    assert texts.printer_error_text(error, MILLIMETRES) == (
        "The printer is busy. Wait a moment and try again. Labels already printed: 1 of 4. "
        "The labels still waiting in the queue were canceled."
    )


def test_canceled_queued_sheets_are_called_sheets():
    error = PrinterBusy("0x0507", printed=1, copies=4, pages=True, queue_canceled=True)
    assert texts.printer_error_text(error, MILLIMETRES) == (
        "The printer is busy. Wait a moment and try again. Sheets already printed: 1 of 4. "
        "The sheets still waiting in the queue were canceled."
    )


def test_switching_to_a_page_printer_turns_the_content_across(window):
    window.set_backend("cab")
    window.label.set_orientation("along")  # chosen on the cab: content along the roll
    size = window.label.media_size()
    window.set_backend("system")
    assert window.label.orientation == "across" and window.label.media_size() == size


def test_page_printers_have_no_roll(window):
    window.set_backend("system")
    assert not find(window.sidebar, Adw.SpinRow, title="Gap")
    assert not find(window.sidebar, Adw.ComboRow, title="Content direction")
    width = one(window.sidebar, Adw.SpinRow, title="Width")
    assert width.get_subtitle() == "mm"  # not "across the roll"
    assert width.get_adjustment().get_upper() == 330  # A3+
    margin = one(window.sidebar, Adw.SpinRow, title="Safety margin")
    assert margin.get_adjustment().get_upper() == 10  # office printers leave 4 to 5 mm
    window.set_backend("cab")
    assert one(window.sidebar, Adw.SpinRow, title="Gap")


def test_usb_permission_text_says_what_to_do():
    text = texts.code_text("usb_permission", MILLIMETRES)
    assert "“lp”" in text and "usermod -aG lp" in text


def test_a_usb_printer_not_found_gets_usb_advice():
    text = texts.printer_error_text(UsbNotFound("no USB printer"), MILLIMETRES)
    assert "cable" in text and "IP address" not in text
    assert "usb_not_found" in texts.CONNECTION_ERRORS  # "Printer not reachable"


def test_a_printer_that_stopped_taking_data_needs_attention():
    from etiketto.backends.base import PrinterStalled

    text = texts.printer_error_text(PrinterStalled("stopped taking data"), MILLIMETRES)
    assert text.startswith("The printer stopped taking data.")
    assert text != texts.code_text("timeout", MILLIMETRES)  # not "does not answer"
    assert "stalled" not in texts.CONNECTION_ERRORS  # reachable: "Printer needs attention"


# --- variables -------------------------------------------------------------------------


def variable_popover(row):
    button = one(row, Gtk.MenuButton, tooltip="Insert variable")
    return button.get_popover()


def choose_variable(popover, title):
    rows = find(popover.get_child(), Adw.ActionRow)
    row = next(r for r in rows if r.get_title() == title)
    row.get_parent().emit("row-activated", row)


def test_insert_variable_into_a_text(window, launched):
    view = one(window.sidebar, Gtk.TextView)
    buffer = view.get_buffer()
    buffer.set_text("Packed ")
    buffer.place_cursor(buffer.get_end_iter())
    popover = variable_popover(window.elements_group)
    titles = [r.get_title() for r in find(popover.get_child(), Adw.ActionRow)]
    assert titles[:3] == ["Date", "Best before (in 30 days)", "Time"] and "UUID" in titles
    assert find(popover.get_child(), Adw.ActionRow)[0].get_subtitle() == "⁨{=date}⁩"
    choose_variable(popover, "Date")
    assert window.label.elements[0].text == "Packed {=date}"
    help_button = one(popover.get_child(), Gtk.Button, tooltip=None)
    assert help_button.get_label() == "More about variables"
    help_button.emit("clicked")
    assert launched == [i18n.docs_url() + "use/#variables"]


def test_insert_variable_into_a_code(window):
    window.add_element(CodeElement(data="https://x/"))
    entry = one(window.sidebar, Adw.EntryRow, title="Content (link, text, number)")
    entry.set_position(len("https://"))
    expander = find(window.sidebar, Adw.ExpanderRow)[1]
    choose_variable(variable_popover(expander), "Counter with leading zeros")
    assert window.label.elements[1].data == "https://{=counter:0001}x/"
    assert entry.get_position() == len("https://{=counter:0001}")


def test_the_preview_shows_the_values_of_the_next_print(window):
    window.label.elements[0].text = "{=counter:000} {=uuid} {=nope}"
    window.label.counter_next = 7
    first, _image = window.render_image()
    again, _image = window.render_image()
    assert first.image.tobytes() == again.image.tobytes()  # the UUID stays while editing
    assert first.warnings == [render_warning("unknown_variable", {"n": 1, "name": "{=nope}"})]
    assert texts.warning_text(first.warnings[0]) == "Element 1: {=nope} is not a known variable"
    values = window.preview_values()
    assert (values.counter, values.copy, values.copies) == (7, 1, 1)


def render_warning(key, params):
    from etiketto.render import RenderWarning

    return RenderWarning(key, params)


def test_counter_group_follows_the_label(window):
    group = window.counter_group
    assert not group.get_visible()
    one(window.sidebar, Gtk.TextView).get_buffer().set_text("No. {=counter}")
    assert group.get_visible()
    next_row = one(group, Adw.SpinRow, title="Next number")
    step_row = one(group, Adw.SpinRow, title="Step")
    window.dirty = False
    next_row.set_value(41)
    step_row.set_value(5)
    assert (window.label.counter_next, window.label.counter_step) == (41, 5)
    assert window.dirty
    window.set_counter_next(100)
    assert next_row.get_value() == 100
    one(window.sidebar, Gtk.TextView).get_buffer().set_text("plain")
    assert not group.get_visible()


def test_element_names_for_the_command_line(window):
    window.add_element(CodeElement(data="x"))
    rows = find(window.sidebar, Adw.EntryRow, title="Name for the command line")
    assert len(rows) == 2
    rows[0].set_text(" title ")
    rows[1].set_text("link")
    assert [e.name for e in window.label.elements] == ["title", "link"]


def test_printing_moves_the_counter_on(ready, gui_env):
    ready.label.elements[0].text = "No. {=counter} {=copy}/{=copies}"
    ready.label.counter_next = 5
    ready.dirty = False
    ready.print_flow.start(PrintOptions(copies=3))
    assert pump(lambda: not ready.print_flow.busy)
    assert len(gui_env.prints) == 3  # a render per label
    assert [options.copies for _image, _host, options in gui_env.prints] == [1, 1, 1]
    assert ready.label.counter_next == 8
    assert ready.dirty  # the document remembers the next number
    assert one(ready.counter_group, Adw.SpinRow, title="Next number").get_value() == 8
    assert toast_titles(ready)[-1] == "Printed"


def test_a_failed_print_keeps_the_numbers_already_printed(ready, gui_env):
    ready.label.elements[0].text = "{=counter}"
    ready.label.counter_next = 1
    calls = []
    original = gui_env.backend.print_label

    def second_fails(image, host, options, progress=None):
        calls.append(1)
        if len(calls) == 2:
            raise PaperJam("jam")
        return original(image, host, options, progress)

    gui_env.backend.print_label = second_fails
    ready.print_flow.start(PrintOptions(copies=3))
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.label.counter_next == 2
    respond(ready.get_visible_dialog(), "close")


def test_a_label_opened_while_printing_keeps_its_counter(ready, gui_env):
    ready.label.elements[0].text = "{=counter}"
    ready.label.counter_next = 1
    ready.print_flow.start(PrintOptions(copies=2))
    other = Label(tape_mm=25, elements=[TextElement(text="{=counter}")], counter_next=50)
    ready.set_label(other, None)
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.label.counter_next == 50


def test_printing_goes_on_from_a_counter_the_command_line_moved_on(ready, gui_env, tmp_path):
    path = tmp_path / "serial.etiketto"
    ready.label.elements[0].text = "{=counter}"
    ready.label.counter_next = 5
    ready.files.write(path)
    Label.store_counter(path, 20)  # `etiketto print` printed 5 to 19 meanwhile
    ready.print_flow.start(PrintOptions(copies=2))
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.label.counter_next == 22
    ready.files.write(path)
    assert Label.load(path).counter_next == 22


def test_a_counter_set_back_on_purpose_stays(ready, gui_env, tmp_path):
    path = tmp_path / "serial.etiketto"
    ready.label.elements[0].text = "{=counter}"
    ready.label.counter_next = 50
    ready.files.write(path)
    ready.set_counter_next(1)  # the file did not change: the window's value counts
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.label.counter_next == 2
    ready.files.write(path)
    assert Label.load(path).counter_next == 2


def test_saving_keeps_a_counter_the_command_line_moved_on(window, tmp_path):
    path = tmp_path / "serial.etiketto"
    window.label.elements[0].text = "{=counter}"
    window.files.write(path)
    Label.store_counter(path, 9)
    window.change(margin_mm=3.0)
    window.files.write(path)
    assert Label.load(path).counter_next == 9 and window.label.counter_next == 9
    other = tmp_path / "copy.etiketto"
    Label.store_counter(path, 30)
    window.files.write(other)  # another file: nothing to go on from
    assert Label.load(other).counter_next == 9
    window.set_counter_next(10**20)
    assert window.label.counter_next == model.MAX_COUNTER


def test_the_preview_counts_the_copies_of_the_print_dialog(ready):
    ready.label.elements[0].text = "{=copy}/{=copies}"
    ready.print_flow.on_print()
    spin = one(ready.last_dialog, Adw.SpinRow, title="Copies")
    spin.set_value(4)
    assert ready.planned_copies == 4 and ready.preview_values().copies == 4
    respond(ready.last_dialog, "cancel")
    assert ready.planned_copies == 1


def test_a_next_number_typed_in_wins_over_the_file(ready, gui_env, tmp_path, monkeypatch):
    path = tmp_path / "serial.etiketto"
    ready.label.elements[0].text = "{=counter}"
    ready.label.counter_next = 5
    ready.files.write(path)
    one(ready.counter_group, Adw.SpinRow, title="Next number").set_value(3)
    assert ready.counter_edited
    Label.store_counter(path, 20)  # the command line printed meanwhile
    ready.print_flow.start(PrintOptions())
    assert pump(lambda: not ready.print_flow.busy)
    assert ready.label.counter_next == 4  # printed 3, as typed in
    ready.files.write(path)
    assert not ready.counter_edited and Label.load(path).counter_next == 4


def test_the_file_is_read_only_when_it_changed(window, tmp_path, monkeypatch):
    path = tmp_path / "serial.etiketto"
    window.label.elements[0].text = "{=counter}"
    window.files.write(path)
    reads = []
    real = Label.saved_counter
    monkeypatch.setattr(Label, "saved_counter", staticmethod(lambda p: reads.append(p) or real(p)))
    window.sync_counter()
    assert reads == []  # unchanged: not read
    Label.store_counter(path, 12)
    window.sync_counter()
    window.sync_counter()
    assert len(reads) == 1 and window.label.counter_next == 12
    path.unlink()
    window.sync_counter()  # gone: nothing to go on from
    assert window.label.counter_next == 12


def test_a_read_only_label_is_not_saved_over(window, tmp_path):
    path = tmp_path / "locked.etiketto"
    window.files.write(path)
    path.chmod(0o444)
    try:
        window.change(margin_mm=3.0)
        window.files.write(path)
        assert window.dirty
        assert toast_titles(window)[-1].startswith("Cannot save locked.etiketto")
        assert Label.load(path).margin_mm == 2.0
        window.files.write(tmp_path / "copy.etiketto")  # Save as: works
        assert not window.dirty and window.path == tmp_path / "copy.etiketto"
    finally:
        path.chmod(0o644)


# --- serial labels ----------------------------------------------------------------


@pytest.fixture
def rows_csv(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("Name;Code\nScrews;B-1\nWashers;B-2\nFuses;C-1\n", encoding="utf-8")
    return path


def loaded(win) -> bool:
    return pump(lambda: win.data.state != "loading")


def attach(win, path):
    win.data.attach(path)
    assert loaded(win)
    render_now(win)


def row_titles(win) -> list[str]:
    group = win._groups[-1]
    return [row.get_title() for row in find(group, Adw.PreferencesRow) if row.get_title()]


def preview_text(win) -> str:
    return win.data.current_label().elements[0].text


def test_no_data_file(window):
    assert window.data.state == "none" and not window.data.active
    assert row_titles(window) == ["No data file"]
    assert not window.preview.rows.get_visible()
    assert not window.lookup_action("row-next").get_enabled()
    assert window.data.current_label() is window.label
    window.data.load()  # nothing linked: nothing to read
    assert window.data.state == "none"


def test_choose_data_file(window, file_dialog, rows_csv):
    file_dialog.result = cancelled()
    window.activate_action("win.choose-data", None)
    file_dialog.result = "https://example.org/rows.csv"
    window.activate_action("win.choose-data", None)
    assert toast_titles(window) == ["Only local files can be used"]
    assert window.data.path is None and not window.dirty
    filters = file_dialog.instances[-1].kwargs["filters"]
    assert filters.get_item(0).get_name() == "Tables (CSV, ODS, XLSX)"

    file_dialog.result = rows_csv
    button = [b for b in find(window._groups[-1], Gtk.Button) if b.get_label() == "Choose…"][0]
    button.emit("clicked")
    assert window.data.state == "loading" and window.dirty
    assert one(window._groups[-1], Adw.ActionRow, title="rows.csv").get_subtitle() == "Reading…"
    assert loaded(window)
    assert window.label.data_file == str(rows_csv)
    assert row_titles(window) == ["rows.csv", "Columns"]
    file_row = one(window._groups[-1], Adw.ActionRow, title="rows.csv")
    assert file_row.get_subtitle() == "Rows: 3 · Columns: 2"
    assert one(window._groups[-1], Adw.ActionRow, title="Columns").get_subtitle() == "Name, Code"


def test_stepping_through_the_rows(window, rows_csv):
    window.label.elements[0].text = "{Name}"
    attach(window, rows_csv)
    preview = window.preview
    assert preview.rows.get_visible() and preview.row_label.get_text() == "Row 1 of 3"
    assert preview_text(window) == "Screws"
    enabled = {s: window.lookup_action("row-" + s).get_enabled() for s in ("first", "next")}
    assert enabled == {"first": False, "next": True}
    window.activate_action("win.row-next", None)
    assert preview.row_label.get_text() == "Row 2 of 3" and preview_text(window) == "Washers"
    window.activate_action("win.row-last", None)
    assert preview_text(window) == "Fuses"
    assert not window.lookup_action("row-last").get_enabled()
    assert window.lookup_action("row-previous").get_enabled()
    window.activate_action("win.row-previous", None)
    window.activate_action("win.row-first", None)
    assert preview_text(window) == "Screws"
    assert window.label.elements[0].text == "{Name}"  # the label keeps its placeholder
    render_now(window)
    assert window.rendered is not None


def test_unknown_columns_are_named_in_the_status(window, rows_csv):
    window.label.elements[0].text = "{Name} {Price} {Price}"
    attach(window, rows_csv)
    assert window.preview.status.get_text().endswith(" · No such column: {Price}")


def test_insert_a_column_into_a_text(window, rows_csv):
    assert not find(window.sidebar, Gtk.MenuButton, tooltip="Insert column")
    attach(window, rows_csv)
    button = one(window.sidebar, Gtk.MenuButton, tooltip="Insert column")
    menu = button.get_menu_model()
    assert [menu.get_item_attribute_value(i, "label").unpack() for i in range(2)] == [
        "Name",
        "Code",
    ]
    view = one(window.sidebar, Gtk.TextView)
    buffer = view.get_buffer()
    buffer.place_cursor(buffer.get_end_iter())
    button.activate_action("data.insert", GLib.Variant.new_string("Name"))
    assert window.label.elements[0].text == "Label{Name}"
    render_now(window)
    assert preview_text(window) == "LabelScrews"


def test_insert_a_column_into_a_code(window, rows_csv):
    window.add_element(CodeElement(data="https://x/"))
    attach(window, rows_csv)
    entry = one(window.sidebar, Adw.EntryRow, title="Content (link, text, number)")
    entry.set_position(-1)
    button = one(entry, Gtk.MenuButton, tooltip="Insert column")
    button.activate_action("data.insert", GLib.Variant.new_string("Code"))
    assert window.label.elements[1].data == "https://x/{Code}"
    assert window.data.current_label().elements[1].data == "https://x/B-1"


def test_columns_with_braces_cannot_be_inserted(window, tmp_path):
    path = tmp_path / "b.csv"
    path.write_text("a{b},Ok\n1,2\n")
    attach(window, path)
    menu = one(window.sidebar, Gtk.MenuButton, tooltip="Insert column").get_menu_model()
    assert menu.get_n_items() == 1


def test_many_columns_are_shortened_in_the_group(window, tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text(",".join("C%d" % i for i in range(15)) + "\n" + ",".join("x" * 15) + "\n")
    attach(window, path)
    columns = one(window._groups[-1], Adw.ActionRow, title="Columns").get_subtitle()
    assert columns.startswith("C0, C1,") and columns.endswith("C11, …")


def make_book(tmp_path):
    from test_merge import make_ods, ods_table, row, text

    return make_ods(
        tmp_path,
        ods_table("Mai", row(text("Name")), row(text("Anna")), row(text("Ben"))),
        ods_table("Juni", row(text("Name")), row(text("Cleo"))),
        name="book.ods",
    )


def test_sheets_to_choose_from(window, tmp_path):
    window.label.elements[0].text = "{Name}"
    attach(window, make_book(tmp_path))
    sheet = one(window._groups[-1], Adw.ComboRow, title="Sheet")
    assert sheet.get_selected() == 0
    window.activate_action("win.row-next", None)
    assert preview_text(window) == "Ben"
    sheet.set_selected(1)
    assert window.label.data_sheet == "Juni"
    assert loaded(window)
    assert preview_text(window) == "Cleo" and window.data.cursor.number == 1
    one(window._groups[-1], Adw.ComboRow, title="Sheet").set_selected(1)  # the same: nothing
    assert window.data.state == "ready"
    window.data.choose_sheet("Juni")
    assert window.data.state == "ready"


def test_read_again_keeps_the_row(window, rows_csv):
    window.label.elements[0].text = "{Name}"
    attach(window, rows_csv)
    window.activate_action("win.row-last", None)
    rows_csv.write_text("Name\nA\nB\nC\nD\n")
    one(window._groups[-1], Gtk.Button, tooltip="Read again").emit("clicked")
    assert loaded(window)
    assert window.data.cursor.number == 3 and preview_text(window) == "C"
    assert window.preview.row_label.get_text() == "Row 3 of 4"


def test_remove_the_data_file(window, rows_csv):
    attach(window, rows_csv)
    window.dirty = False
    one(window._groups[-1], Gtk.Button, tooltip="Remove data file").emit("clicked")
    assert window.label.data_file == window.label.data_sheet == ""
    assert window.data.state == "none" and window.dirty
    assert row_titles(window) == ["No data file"]
    assert not window.preview.rows.get_visible()


def test_answers_of_an_older_read_are_dropped(window, rows_csv, tmp_path):
    attach(window, rows_csv)
    book = window.data.book
    window.data.load()
    window.data.load()
    stale = window.data._generation - 1
    window.data._loaded(stale, book)
    window.data._failed(stale, OSError("old"))
    assert window.data.state == "loading"
    assert loaded(window) and window.data.state == "ready"
    window.data.remove()  # a read still running is of no use now
    window.data._loaded(window.data._generation - 1, book)
    assert window.data.state == "none"


@pytest.mark.parametrize(
    "content, text, report",
    [
        (b"\x00\x01\x02", "bad.csv is not a table. Use a CSV, ODS or XLSX file.", False),
        (b"", "bad.csv contains no data.", False),
        (b"PK\x03\x04" + b"\x00" * 30, "The file is damaged: bad.csv", True),
    ],
)
def test_data_files_that_cannot_be_used(window, tmp_path, content, text, report):
    (tmp_path / "bad.csv").write_bytes(content)
    attach(window, tmp_path / "bad.csv")
    assert window.data.state == "failed" and not window.data.active
    toast = window.toast_log[-1]
    assert toast.get_title() == text
    assert (toast.get_button_label() == "Report") is report
    file_row = one(window._groups[-1], Adw.ActionRow, title="bad.csv")
    assert file_row.get_subtitle() == text and file_row.has_css_class("error")
    assert not window.preview.rows.get_visible()


def test_too_large_data_file(window, rows_csv, monkeypatch):
    from etiketto.merge import limits

    monkeypatch.setattr(limits, "MAX_ROWS", 2)
    attach(window, rows_csv)
    assert toast_titles(window)[-1].startswith("rows.csv is too large for serial labels.")


def test_data_file_is_saved_with_the_label_and_read_on_open(window, rows_csv, tmp_path):
    window.label.elements[0].text = "{Code}"
    attach(window, rows_csv)
    window.files.write(tmp_path / "shelf.etiketto")
    stored = json.loads((tmp_path / "shelf.etiketto").read_text(encoding="utf-8"))
    assert stored["data_file"] == "rows.csv"
    assert "Screws" not in (tmp_path / "shelf.etiketto").read_text()  # never the data

    window.files.new()
    assert window.data.state == "none" and window.data.path is None
    window.files.open_file(tmp_path / "shelf.etiketto")
    assert loaded(window)
    assert window.data.state == "ready" and preview_text(window) == "B-1"
    assert not window.dirty


def test_missing_data_file_on_open_is_said_gently(window, rows_csv, tmp_path):
    attach(window, rows_csv)
    window.files.write(tmp_path / "shelf.etiketto")
    rows_csv.unlink()
    window.files.open_file(tmp_path / "shelf.etiketto")
    assert loaded(window)
    toast = window.toast_log[-1]
    assert toast.get_title() == (
        "The data file rows.csv is not there any more. Choose it again or remove it."
    )
    assert not toast.get_button_label()  # nothing to report
    assert window.label.data_file == str(rows_csv)  # the link stays until removed
    assert one(window._groups[-1], Gtk.Button, tooltip="Choose another file")


def test_drop_a_data_file(window, rows_csv):
    assert drop(window, Gio.File.new_for_path(str(rows_csv))) is True
    assert loaded(window) and window.data.state == "ready"


# --- printing serial labels --------------------------------------------------------------


@pytest.fixture
def series(ready, rows_csv):
    ready.label.elements[0].text = "{Name}"
    attach(ready, rows_csv)
    return ready


def printed_names(gui_env) -> list[int]:
    """The rows printed, told apart by their width (each name has its own)."""
    return [image.width for image, _host, _options in gui_env.prints]


def test_print_serial_labels(series, gui_env):
    series.print_flow.on_print()
    dialog = series.last_dialog
    assert dialog.get_heading() == "Print serial labels"
    box = dialog.get_extra_child()
    copies, first, last = (box.get_row_at_index(i) for i in range(3))
    assert copies.get_title() == "Copies of each label"
    assert (first.get_title(), first.get_value()) == ("From row", 1)
    assert (last.get_title(), last.get_value()) == ("To row", 3)
    copies.set_value(2)
    first.set_value(2)
    dialog.emit("response", "print")
    assert series.banner.get_revealed()
    assert series.banner.get_button_label() == "Stop"
    assert pump(lambda: not series.print_flow.busy)
    assert len(gui_env.prints) == 2
    assert {options.copies for _image, _host, options in gui_env.prints} == {2}
    assert toast_titles(series)[-1] == "Printed rows 2 to 3"
    series.print_flow.start_series(PrintOptions(), range(2, 3))
    assert pump(lambda: not series.print_flow.busy)
    assert toast_titles(series)[-1] == "Printed row 3"
    assert not series.banner.get_revealed() and not series.banner.get_button_label()
    assert series.print_button.get_label() == "Print"


def test_row_range_ends_follow_each_other(series):
    series.print_flow.on_print()
    box = series.last_dialog.get_extra_child()
    first, last = box.get_row_at_index(1), box.get_row_at_index(2)
    last.set_value(1)
    first.set_value(3)
    assert last.get_value() == 3
    last.set_value(2)
    assert first.get_value() == 2
    respond(series.last_dialog, "cancel")


def test_each_row_is_printed_with_its_values(series, gui_env, monkeypatch):
    prepared = []
    prepare = gui_env.backend.prepare
    monkeypatch.setattr(
        gui_env.backend,
        "prepare",
        lambda label, options, safe_mm: (
            prepared.append(label.elements[0].text) or prepare(label, options, safe_mm)
        ),
    )
    series.print_flow.start_series(PrintOptions(), range(3))
    assert pump(lambda: not series.print_flow.busy)
    assert prepared == ["Screws", "Washers", "Fuses"]


def test_stop_a_series(series, gui_env, monkeypatch):
    print_label = gui_env.backend.print_label

    def slow(image, host, options, progress=None):
        series.print_flow._stop.set()  # what the Stop button does, from the worker
        return print_label(image, host, options, progress)

    monkeypatch.setattr(gui_env.backend, "print_label", slow)
    series.print_flow.start_series(PrintOptions(), range(3))
    assert pump(lambda: not series.print_flow.busy)
    assert len(gui_env.prints) == 1
    assert toast_titles(series)[-1] == "Stopped after row 1"


def test_stop_button(series):
    banner = series.banner
    series.print_flow.stop()  # not printing: nothing to stop
    assert not series.print_flow._stop.is_set()
    series.print_flow.busy = True
    banner.set_button_label("Stop")
    banner.emit("button-clicked")
    assert series.print_flow._stop.is_set()
    assert banner.get_title() == "Stopping after this label…"
    assert not banner.get_button_label()
    series.print_flow.series_progress(SeriesProgress(2, 3, 2, Progress(1, 1, "PREHEATING")))
    assert banner.get_title() == "Stopping after this label…"  # stays until the end
    assert series.print_button.get_label() == "Printing 2/3…"


def test_series_progress_shows_printer_stages(series):
    series.print_flow.busy = True
    series.print_flow.series_progress(SeriesProgress(1, 3, 1, Progress(1, 1, "PREHEATING")))
    assert series.banner.get_title() == "Warming up…"
    series.print_flow.series_progress(SeriesProgress(2, 3, 2, Progress(1, 1, "")))
    assert series.banner.get_title() == "Printing serial labels"
    series.print_flow.busy = False
    series.print_flow.series_progress(SeriesProgress(3, 3, 3, Progress(1, 1, "")))
    assert series.print_button.get_label() == "Printing 2/3…"  # late event: ignored


@pytest.mark.parametrize("fail_at", [1, 2])
def test_series_printer_problem(series, gui_env, monkeypatch, fail_at):
    print_label = gui_env.backend.print_label

    def failing(image, host, options, progress=None):
        if len(gui_env.prints) + 1 == fail_at:
            raise PaperJam("JAM")
        return print_label(image, host, options, progress)

    monkeypatch.setattr(gui_env.backend, "print_label", failing)
    series.print_flow.start_series(PrintOptions(), range(3))
    assert pump(lambda: not series.print_flow.busy)
    dialog = series.get_visible_dialog()
    assert dialog.get_heading() == "Printing failed"
    assert dialog.get_body() == (
        "The label is jammed. Open the cover, remove it and try again.\n\nPrinting stopped "
        "at row %d. To continue, print again from row %d." % (fail_at, fail_at)
    )
    respond(dialog, "close")


def test_series_label_that_cannot_be_prepared(series, gui_env, monkeypatch):
    def broken(label, options, safe_mm):
        raise RuntimeError("boom")

    monkeypatch.setattr(gui_env.backend, "prepare", broken)
    series.print_flow.start_series(PrintOptions(), range(3))
    assert pump(lambda: not series.print_flow.busy)
    dialog = series.get_visible_dialog()
    assert dialog.get_body().startswith(
        "The label cannot be prepared for printing.\n\nPrinting stopped at row 1."
    )
    respond(dialog, "close")


def test_series_with_another_mode_is_reported(series, gui_env):
    gui_env.print_result = PrintResult(mode="color")
    series.print_flow.start_series(PrintOptions(mode="vivid"), range(2))
    assert pump(lambda: not series.print_flow.busy)
    assert toast_titles(series)[-1].startswith("Colour mode “Vivid” is not available")


def test_series_without_printer(window, rows_csv, gui_env):
    attach(window, rows_csv)
    window.print_flow.start_series(PrintOptions(), range(3))
    assert toast_titles(window)[-1] == "No printer yet – search for one or enter its address"
    assert not window.print_flow.busy and gui_env.prints == []


def test_no_printing_while_the_data_file_cannot_be_used(ready, rows_csv, tmp_path):
    ready.data.attach(rows_csv)
    assert ready.data.state == "loading"
    ready.print_flow.on_print()
    assert toast_titles(ready)[-1] == "The data file is still being read. Try again in a moment."
    assert loaded(ready)
    (tmp_path / "header.csv").write_text("Name\n")
    attach(ready, tmp_path / "header.csv")
    ready.print_flow.on_print()
    assert toast_titles(ready)[-1] == "header.csv contains no data."
    (tmp_path / "header.csv").unlink()
    ready.data.load()
    assert loaded(ready)
    ready.print_flow.on_print()
    assert toast_titles(ready)[-1].startswith("The data file header.csv is not there any more.")
    assert ready.get_visible_dialog() is None


def test_data_error_texts():
    from etiketto.merge import MergeError

    assert texts.data_error_text(PermissionError("no"), "a.csv") == "Cannot open a.csv"
    assert texts.data_error_text(MergeError("other", ""), "a.csv") == "The file is damaged: a.csv"


# --- review fixes: serial labels --------------------------------------------------------


def test_column_names_are_shown_as_text(window, tmp_path):
    """Markup and underscores in column names are text: no link, no bold,
    no mnemonic that swallows the underscore."""
    path = tmp_path / "m.csv"
    path.write_text("first_name,A&B,<b>x</b>\n1,2,3\n")
    attach(window, path)
    row = one(window._groups[-1], Adw.ActionRow, title="Columns")
    assert row.get_subtitle() == "first_name, A&amp;B, &lt;b&gt;x&lt;/b&gt;"
    menu = one(window.sidebar, Gtk.MenuButton, tooltip="Insert column").get_menu_model()
    labels = [menu.get_item_attribute_value(i, "label").unpack() for i in range(3)]
    assert labels == ["first__name", "A&B", "<b>x</b>"]


def test_sheet_row_waits_while_reading(window, tmp_path):
    attach(window, make_book(tmp_path))
    window.data.load()
    assert not one(window._groups[-1], Adw.ComboRow, title="Sheet").get_sensitive()
    assert loaded(window)
    assert one(window._groups[-1], Adw.ComboRow, title="Sheet").get_sensitive()


def test_a_new_read_cancels_the_running_one(window, rows_csv):
    attach(window, rows_csv)
    window.data.load()
    first = window.data._cancel
    window.data.load()
    assert first.is_set() and not window.data._cancel.is_set()
    window.data.remove()
    assert window.data._cancel.is_set()


def foreign_label(tmp_path, data_file: str):
    folder = tmp_path / "share"
    folder.mkdir()
    data = Label(elements=[TextElement(text="{Name}")]).to_dict()
    data["data_file"] = data_file
    (folder / "x.etiketto").write_text(json.dumps(data))
    return folder / "x.etiketto"


def test_data_file_outside_the_label_folder_is_read_only_when_agreed(ready, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("Name\nhidden\n")
    ready.files.open_file(foreign_label(tmp_path, "../secret.csv"))
    assert ready.data.state == "unread" and not ready.data.active
    dialog = ready.last_dialog
    assert dialog.get_heading() == "Read the data file?"
    assert str(secret) in dialog.get_body()
    assert one(ready._groups[-1], Adw.ActionRow, title="secret.csv").get_subtitle() == (
        "Not read: outside the label&apos;s folder"
    )
    respond(dialog, "cancel")
    assert ready.data.state == "unread"
    ready.print_flow.on_print()
    assert toast_titles(ready)[-1] == "Not read: outside the label&apos;s folder"  # escaped
    respond(dialog, "read")
    assert loaded(ready) and ready.data.rows == [{"Name": "hidden"}]


def test_agreeing_late_does_not_read_for_another_label(window, tmp_path):
    (tmp_path / "secret.csv").write_text("Name\nhidden\n")
    window.files.open_file(foreign_label(tmp_path, "../secret.csv"))
    dialog = window.last_dialog
    window.files.new()
    respond(dialog, "read")
    assert window.data.state == "none"


def test_template_has_no_data_file(window, rows_csv, tmp_path):
    attach(window, rows_csv)
    window.files._save_template(tmp_path / "t.etiketto")
    stored = json.loads((tmp_path / "t.etiketto").read_text(encoding="utf-8"))
    assert (stored["data_file"], stored["data_sheet"]) == ("", "")
    assert window.label.data_file == str(rows_csv)  # the open label keeps it


@pytest.fixture
def ean_series(ready, tmp_path):
    path = tmp_path / "ean.csv"
    path.write_text("Name;EAN\nA;4006381333931\nB;12x\nC;\nD;400638133393\n")
    ready.add_element(CodeElement(data="{EAN}", symbology="ean13"))
    attach(ready, path)
    return ready


def test_rows_whose_codes_cannot_be_drawn_are_named_before_printing(ean_series, gui_env):
    ean_series.print_flow.start_series(PrintOptions(), range(4))
    assert pump(lambda: not ean_series.print_flow.busy)
    dialog = ean_series.get_visible_dialog()
    assert dialog.get_heading() == "Rows that cannot be printed"
    assert dialog.get_body().endswith("\n\nRows: 2, 3")
    assert gui_env.prints == []  # nothing printed
    respond(dialog, "skip")
    assert pump(lambda: not ean_series.print_flow.busy)
    assert len(gui_env.prints) == 2
    assert toast_titles(ean_series)[-1] == "Printed rows 1 to 4"


def test_only_unprintable_rows_offer_no_skipping(ean_series, gui_env, monkeypatch):
    from etiketto.ui import printing

    monkeypatch.setattr(printing, "ROWS_LISTED", 1)
    ean_series.print_flow.start_series(PrintOptions(), range(1, 3))
    assert pump(lambda: not ean_series.print_flow.busy)
    dialog = ean_series.get_visible_dialog()
    assert dialog.get_body().endswith("Rows: 2, …")
    assert not dialog.has_response("skip")
    respond(dialog, "cancel")
    assert gui_env.prints == []


def test_an_empty_row_range_prints_nothing(series, gui_env):
    series.print_flow.start_series(PrintOptions(), range(0))
    assert toast_titles(series)[-1] == "No rows to print"
    assert not series.print_flow.busy and gui_env.prints == []
    from etiketto.serial import SeriesResult

    assert series.print_flow.series_done(SeriesResult([]), PrintOptions()) is False


def test_only_tables_in_visible_folders_are_read_without_asking(window, tmp_path):
    folder = tmp_path / "share"
    (folder / ".hidden").mkdir(parents=True)
    (folder / ".hidden" / "a.csv").write_text("Name\nA\n")
    (folder / "notes.md").write_text("Name\nA\n")
    (folder / "ok.txt").write_text("Name\nA\n")
    label = Label(elements=[TextElement(text="{Name}")])
    for name, state in (("ok.txt", "ready"), (".hidden/a.csv", "unread"), ("notes.md", "unread")):
        label.data_file = str(folder / name)
        label.save(folder / "x.etiketto")
        window.files.open_file(folder / "x.etiketto")
        assert loaded(window) and window.data.state == state, name


def test_a_link_shows_where_it_leads(window, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("Name\nhidden\n")
    folder = tmp_path / "share"
    folder.mkdir()
    (folder / "data.csv").symlink_to(secret)
    label = Label(elements=[TextElement(text="{Name}")], data_file=str(folder / "data.csv"))
    label.save(folder / "x.etiketto")
    window.files.open_file(folder / "x.etiketto")
    assert window.data.state == "unread"
    assert "%s\n→ %s" % (folder / "data.csv", secret) in window.last_dialog.get_body()


def test_agreed_data_files_are_not_asked_about_again(window, tmp_path):
    (tmp_path / "secret.csv").write_text("Name\nhidden\n")
    path = foreign_label(tmp_path, "../secret.csv")
    window.files.open_file(path)
    respond(window.last_dialog, "read")
    assert loaded(window) and window.data.state == "ready"
    stored = json.loads(config.SETTINGS_FILE.read_text())["trusted_data"]
    assert stored == [[str(path.resolve()), str((tmp_path / "secret.csv").resolve())]]
    dialog = window.last_dialog
    window.files.open_file(path)
    assert loaded(window) and window.data.state == "ready"
    assert window.last_dialog is dialog  # not asked again
    window.settings.data["trusted_data"] = "junk"
    assert not window.settings.trusts_data(path, tmp_path / "secret.csv")


def test_rows_that_are_gone_are_not_printed(series, gui_env):
    series.print_flow.start_series(PrintOptions(), [1, 2, 7, -1])
    assert pump(lambda: not series.print_flow.busy)
    assert len(gui_env.prints) == 2
    assert toast_titles(series)[-1] == "Printed rows 2 to 3"


def test_an_unexpected_error_ends_the_series(series, gui_env, monkeypatch):
    from etiketto.ui import printing

    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(printing, "print_series", broken)
    series.print_flow.start_series(PrintOptions(), range(3))
    assert pump(lambda: not series.print_flow.busy)
    assert series.get_visible_dialog().get_heading() == "Printing failed"
    assert series.print_button.get_sensitive()
    respond(series.get_visible_dialog(), "close")


def test_settings_are_only_for_this_user(window):
    window.settings.save()
    assert config.SETTINGS_FILE.stat().st_mode & 0o777 == 0o600


def test_forget_allowed_data_files(window, tmp_path):
    action = window.lookup_action("forget-data")
    assert not action.get_enabled()
    (tmp_path / "secret.csv").write_text("Name\nhidden\n")
    path = foreign_label(tmp_path, "../secret.csv")
    window.files.open_file(path)
    respond(window.last_dialog, "read")
    assert loaded(window) and action.get_enabled()
    window.activate_action("win.forget-data", None)
    assert not action.get_enabled()
    assert toast_titles(window)[-1].startswith("Etiketto asks again")
    assert "trusted_data" not in json.loads(config.SETTINGS_FILE.read_text())
    window.files.open_file(path)
    assert window.data.state == "unread"


def test_late_search_results_for_a_printer_model_that_is_gone(window):
    window.label.printer = "gone"  # e.g. a model a finished test registered
    assert window.printer.discovered([]) is False


def test_a_series_moves_the_counter_on(series, gui_env):
    series.label.elements[0].text = "{=counter} {Name}"
    series.label.counter_next = 4
    series.print_flow.start_series(PrintOptions(copies=2), range(3))
    assert pump(lambda: not series.print_flow.busy)
    assert len(gui_env.prints) == 6
    assert series.label.counter_next == 10


def test_preview_fills_row_and_variables_in_one_pass(window, tmp_path):
    path = tmp_path / "v.csv"
    path.write_text("Name\n{=counter}\n")
    window.label.elements[0].text = "{Name} {=counter} {{x}}"
    window.label.counter_next = 3
    attach(window, path)
    assert window.data.current_label().elements[0].text == "{=counter} 3 {x}"


def test_a_saved_sheet_that_is_gone_is_not_replaced(ready, tmp_path):
    ready.label.elements[0].text = "{Name}"
    ready.data.attach(make_book(tmp_path))
    assert loaded(ready)
    ready.label.data_sheet = "August"  # renamed in the spreadsheet since
    ready.data.load()
    assert loaded(ready)
    assert ready.data.state == "failed" and not ready.data.active
    assert toast_titles(ready)[-1] == "book.ods has no sheet named August"
    ready.print_flow.on_print()
    assert toast_titles(ready)[-1] == "book.ods has no sheet named August"
    sheet = one(ready._groups[-1], Adw.ComboRow, title="Sheet")
    assert sheet.get_selected() == 2  # "August", after "Mai" and "Juni"
    sheet.set_selected(2)  # the same: nothing to read
    assert ready.data.state == "failed"
    sheet.set_selected(0)  # the first one, chosen on purpose
    assert loaded(ready) and ready.data.state == "ready"
    assert ready.label.data_sheet == "Mai" and preview_text(ready) == "Anna"


def test_a_csv_file_with_a_saved_sheet_is_read(ready, rows_csv):
    """A saved sheet name (the data was a spreadsheet once) means nothing for
    a CSV file: it is read, and no sheet row offers only the missing one."""
    ready.label.elements[0].text = "{Name}"
    ready.label.data_sheet = "Mai"
    attach(ready, rows_csv)
    ready.label.data_sheet = "Mai"
    ready.data.load()
    assert loaded(ready) and ready.data.state == "ready" and ready.data.active
    assert not find(ready._groups[-1], Adw.ComboRow, title="Sheet")
    assert preview_text(ready) == "Screws"


def test_series_stop_text():
    assert texts.series_stop_text(3) == (
        "Printing stopped at row 3. To continue, print again from row 3."
    )
    assert texts.series_stop_text(3, 2).endswith(" Copies of row 3 already printed: 2.")


# --- raw label printers: their own settings --------------------------------------------------


@pytest.fixture
def raw_env(gui_env, monkeypatch):
    """ZPL and ESC/POS with faked query/print/discover."""
    from etiketto.backends import get_backend

    env = SimpleNamespace(
        info=PrinterInfo("ZD621-203dpi", None, None, None, "IDLE", firmware="V93"),
        prints=[],
        queries=[],
    )
    for backend_id in ("zpl", "escpos", "epl"):
        backend = get_backend(backend_id)

        def print_label(image, host, options, progress=None):
            env.prints.append((image, host, options))
            progress(Progress(1, options.copies))

        def query(host, backend_id=backend_id):
            env.queries.append((backend_id, host))
            return env.info

        monkeypatch.setattr(backend, "query", query)
        monkeypatch.setattr(backend, "print_label", print_label)
        monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    return env


def _raw_window(make_window, backend_id):
    win = make_window(
        {"printers": {"zpl": {"host": "zebra.local"}, "escpos": {"host": "pos.local"}}}
    )
    win.set_backend(backend_id)
    assert pump(lambda: win.printer.info is not None)
    return win


def test_raw_printer_settings_rows(make_window, raw_env):
    win = _raw_window(make_window, "zpl")
    resolution = one(win.sidebar, Adw.ComboRow, title="Resolution")
    assert _items(resolution) == ["As reported by the printer", "203 dpi", "300 dpi", "600 dpi"]
    sensor = one(win.sidebar, Adw.ComboRow, title="Label sensor")
    assert _items(sensor) == [
        "As set on the printer",
        "Gap between labels",
        "Black mark",
        "Continuous media",
    ]
    finish = one(win.sidebar, Adw.ComboRow, title="After printing")
    assert _items(finish) == ["As set on the printer", "Tear off", "Peel off", "Cut off"]
    darkness = one(win.sidebar, Adw.ComboRow, title="Darkness")
    assert _items(darkness)[:3] == ["As set on the printer", "0 %", "10 %"]
    speed = one(win.sidebar, Adw.ComboRow, title="Speed")
    assert _items(speed)[1] == "1 in/s" and len(_items(speed)) == 13
    sensor.set_selected(2)
    darkness.set_selected(6)  # 50 %
    stored = Settings.load().data["printers"]["zpl"]
    assert stored["media"] == "mark" and stored["darkness"] == 50
    darkness.set_selected(0)
    assert Settings.load().data["printers"]["zpl"]["darkness"] is None
    # the print takes them along
    darkness.set_selected(3)
    win.print_flow.on_print()
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    options = raw_env.prints[0][2]
    assert options.settings == {"media": "mark", "darkness": 20}
    upside_down = one(win.sidebar, Adw.SwitchRow, title="Print upside down")
    assert not upside_down.get_active()
    upside_down.set_active(True)
    assert Settings.load().data["printers"]["zpl"]["upside_down"] is True


def test_raw_printer_settings_are_shown_as_stored(make_window, raw_env):
    win = make_window(
        {
            "printers": {
                "zpl": {
                    "host": "zebra.local",
                    "dpi": "300",
                    "speed": 4,
                    "media": 7,
                    "darkness": 55,  # off the steps of 10 %: shown as it is
                    "upside_down": True,
                }
            }
        }
    )
    win.set_backend("zpl")
    assert _selected(one(win.sidebar, Adw.ComboRow, title="Resolution")) == "300 dpi"
    assert _selected(one(win.sidebar, Adw.ComboRow, title="Speed")) == "4 in/s"
    darkness = one(win.sidebar, Adw.ComboRow, title="Darkness")
    assert _selected(darkness) == "55 %" and "60 %" in _items(darkness)
    assert one(win.sidebar, Adw.SwitchRow, title="Print upside down").get_active()
    assert _selected(one(win.sidebar, Adw.ComboRow, title="Label sensor")) == (
        "As set on the printer"
    )
    win.set_backend("vc500w")
    assert not find(win.sidebar, Adw.ComboRow, title="Resolution")


def test_a_printer_without_status(make_window, raw_env):
    raw_env.info = PrinterInfo("", None, None, None, "UNKNOWN")
    win = _raw_window(make_window, "zpl")
    assert win.printer.row.get_title() == "Zebra ZPL"
    assert win.printer.row.get_subtitle() == "Reachable · the printer reports no status"


def test_receipt_printers_have_paper_not_cassettes(make_window, raw_env):
    raw_env.info = PrinterInfo("", None, None, None, "IDLE")
    win = _raw_window(make_window, "escpos")
    assert win.printer.row.get_subtitle() == "Ready"
    assert _items(one(win.sidebar, Adw.ComboRow, title="Tape width")) == ["58 mm", "80 mm"]
    assert win.printer.description() == "ESC/POS, 58 mm paper, IDLE"
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    assert "Receipt printer (ESC/POS)" in _items(model)
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    assert box.get_row_at_index(1).get_title() == "Cut"


def test_setting_names():
    assert texts.choice_name("203") == "203 dpi"
    assert texts.choice_name("odd") == "odd"
    assert texts.setting_title("odd") == "odd"
    assert texts.level_name("odd", 3) == "3"
    assert texts.level_name("darkness", None) == "As set on the printer"


def test_a_print_is_rendered_again_for_another_resolution(make_window, raw_env, monkeypatch):
    from etiketto.backends import get_backend
    from etiketto.backends.base import RenderAgain

    backend = get_backend("zpl")
    win = _raw_window(make_window, "zpl")
    calls = []
    prepares = []
    real_prepare = backend.prepare

    def print_label(image, host, options, progress=None):
        calls.append((image.size, options.host))
        if len(calls) == 1:
            backend.reported[host] = 12 * 25.4  # what the printer said now
            raise RenderAgain("rendered at 203.2 dpi, the printer has 304.8 dpi")
        progress(Progress(1, options.copies))

    def prepare(label, options, safe_mm=1.0):
        prepares.append(options.host)
        return real_prepare(label, options, safe_mm)

    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(backend, "prepare", prepare)
    monkeypatch.setattr(backend, "reported", {})
    win.print_flow.on_print()
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    assert prepares == ["zebra.local", "zebra.local"]
    (first, host), (second, _host) = calls
    assert host == "zebra.local" and second[0] == first[0] * 3 // 2  # 8 -> 12 dots per mm
    assert toast_titles(win)[-1] == "Printed"


# --- Brother QL: continuous rolls and die-cut labels ------------------------------------


@pytest.fixture
def ql_env(gui_env, monkeypatch):
    """Both QL backends with faked query/print/discover: a QL-820NWB with the
    red and black 62 mm roll."""
    from etiketto.backends import get_backend
    from etiketto.backends.brother_ql import BLACK, QUALITIES, RED

    env = SimpleNamespace(
        info=PrinterInfo(
            "QL-820NWB", 62, None, None, "IDLE", modes=(BLACK, RED), qualities=QUALITIES
        ),
        queries=[],
        prints=[],
    )
    for backend_id in ("brother_ql", "brother_ql_diecut"):
        backend = get_backend(backend_id)

        def query(host):
            env.queries.append(host)
            return env.info

        def print_label(image, host, options, progress=None):
            env.prints.append((image, host, options))
            progress(Progress(1, options.copies))

        monkeypatch.setattr(backend, "query", query)
        monkeypatch.setattr(backend, "print_label", print_label)
        monkeypatch.setattr(
            backend,
            "discover",
            lambda timeout=3.0: [FoundPrinter("Brother QL-820NWB", "QL820.local", "192.0.2.7")],
        )
    return env


@pytest.fixture
def ql_window(make_window, ql_env):
    win = make_window()
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    names = _items(model)
    assert "Brother QL, continuous rolls" in names and "Brother QL, die-cut labels" in names
    model.set_selected(names.index("Brother QL, continuous rolls"))
    assert pump(lambda: win.printer.info is ql_env.info)
    tape = win.label_group.tape_row
    tape.set_selected(_items(tape).index("62 mm"))  # the loaded roll: no warning
    render_now(win)
    return win


def test_ql_print_dialog_offers_cut_colours_and_quality(ql_window, ql_env):
    win = ql_window
    assert win.label.printer == "brother_ql" and win.label.tape_mm == 62
    assert _items(win.label_group.tape_row) == [
        "12 mm",
        "18 mm",
        "29 mm",
        "38 mm",
        "50 mm",
        "54 mm",
        "62 mm",
        "90 mm",
        "102 mm",
        "104 mm",
    ]
    assert win.printer.row.get_title() == "Brother QL-820NWB"
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    copies, cut, colours, quality = (box.get_row_at_index(i) for i in range(4))
    assert box.get_row_at_index(4) is None  # no colour adjustment
    assert _items(cut) == ["Full cut", "Cut after the last label", "No cut"]
    assert colours.get_title() == "Colours" and _items(colours) == ["Black", "Red and black"]
    assert quality.get_title() == "Quality"
    assert _items(quality) == ["Standard", "High resolution (600 dpi)"]
    cut.set_selected(1)
    colours.set_selected(1)
    quality.set_selected(1)
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    options = ql_env.prints[0][2]
    assert (options.cut, options.mode, options.quality) == ("end", "red", "high")
    saved = Settings.load().data["printers"]["brother_ql"]
    assert (saved["cut"], saved["mode"], saved["quality"]) == ("end", "red", "high")
    assert win.print_flow.choices()["quality"] == "high"


def test_ql_without_two_colours_or_high_resolution(ql_window, ql_env):
    from etiketto.backends.brother_ql import BLACK

    win = ql_window
    # what the backend reports for a QL-500 (no 600 dpi)
    ql_env.info = PrinterInfo(
        "QL-500", 62, None, None, "IDLE", modes=(BLACK,), qualities=("standard",)
    )
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    # no cutter on the QL-500: no cut row
    assert box.get_row_at_index(0).get_title() == "Copies"
    assert box.get_row_at_index(1) is None


def test_ql_that_does_not_report_its_state(ql_window, ql_env):
    win = ql_window
    ql_env.info = PrinterInfo("QL", None, None, None, "UNKNOWN")
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    assert win.printer.row.get_title() == "Brother QL"
    assert win.printer.row.get_subtitle() == "Reachable · the printer reports no status"


def test_ql_with_the_wrong_kind_of_roll(ql_window, ql_env):
    win = ql_window
    ql_env.info = PrinterInfo("QL-820NWB", None, None, None, "ERROR", error="wrong_media")
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    assert win.printer.row.get_title() == "Printer needs attention"
    assert win.printer.row.get_subtitle().startswith("The loaded roll does not match the label.")


def test_ql_die_cut_labels(ql_window, ql_env):
    win = ql_window
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    model.set_selected(_items(model).index("Brother QL, die-cut labels"))
    assert win.label.printer == "brother_ql_diecut"
    assert win.label.media_size() == (62, 29)
    size = one(win.sidebar, Adw.ComboRow, title="Label size")
    assert _items(size)[:3] == ["62 × 29 mm", "29 × 90 mm", "62 × 100 mm"]
    # one printer, one setup: the host found for continuous rolls is used
    assert win.printer_settings.host == "QL820.local"


def _plain(text: str) -> str:
    """Without the marks that isolate names and numbers in a sentence."""
    return text.replace("\u2068", "").replace("\u2069", "")


def test_ql_roll_texts(ql_window, ql_env):
    win = ql_window
    assert _plain(win.printer.row.get_subtitle()) == "62 mm roll"
    assert ("Roll", "62 mm") in [(t, _plain(v)) for t, v in win.printer.details()]
    ql_env.info = PrinterInfo("QL-820NWB", None, None, None, "IDLE")
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    assert win.printer.row.get_subtitle() == "No roll loaded"
    ql_env.info = PrinterInfo("QL-820NWB", 29, None, None, "IDLE")
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    win.print_flow.on_print()
    dialog = win.last_dialog
    assert dialog.get_heading() == "Wrong roll"
    assert _plain(dialog.get_body()) == (
        "The label is designed for a 62 mm roll, but a 29 mm roll is loaded. "
        "The label would be scaled to the roll."
    )


def test_ql_die_cut_labels_of_another_size(ql_window, ql_env):
    win = ql_window
    ql_env.info = PrinterInfo(
        "QL-820NWB", None, None, None, "IDLE", label_mm=(29, 90), pitch_mm=93
    )
    win.set_backend("brother_ql_diecut")
    assert pump(lambda: win.printer.info is ql_env.info)
    assert ("Roll", "29 × 90 mm") in [(t, _plain(v)) for t, v in win.printer.details()]
    win.print_flow.on_print()
    dialog = win.last_dialog
    assert dialog.get_heading() == "Different labels loaded"
    assert _plain(dialog.get_body()) == (
        "The label is 62 × 29 mm, but labels of 29 × 90 mm are loaded. Check the "
        "label size, or load the matching roll."
    )
    win.label.set_media_size(29, 90)  # the same size: no warning
    win.print_flow.on_print()
    assert win.last_dialog.get_heading() == "Print label"


def test_ql_offers_the_kind_of_the_loaded_roll(ql_window, ql_env):
    win = ql_window
    ql_env.info = PrinterInfo(
        "QL-820NWB",
        None,
        None,
        None,
        "ERROR",
        error="wrong_media",
        media_backend="brother_ql_diecut",
    )
    win.toast_log.clear()
    win.printer.refresh()
    assert pump(lambda: win.printer.info is ql_env.info)
    toast = win.toast_log[-1]
    assert toast.get_title() == "The loaded roll is for “Brother QL, die-cut labels”"
    assert toast.get_button_label() == "Switch"
    toast.emit("button-clicked")
    assert win.label.printer == "brother_ql_diecut"
    # an unknown entry or the current one: nothing to offer
    count = len(win.toast_log)
    win.printer._offer_other_media(replace(ql_env.info, media_backend="no-such"))
    win.printer._offer_other_media(replace(ql_env.info, media_backend="brother_ql_diecut"))
    assert len(win.toast_log) == count


def test_ql_says_when_the_roll_decided_the_colours(ql_window, ql_env, monkeypatch):
    win = ql_window
    backend = win.backend
    monkeypatch.setattr(
        backend,
        "print_label",
        lambda image, host, options, progress=None: PrintResult(mode="red", reason="roll"),
    )
    win.print_flow.start(PrintOptions(mode="black"))
    assert pump(lambda: not win.print_flow.busy)
    assert toast_titles(win)[-1] == "Printed with “Red and black”, as the loaded roll needs it"


# --- Brother P-touch: tape widths, extra print settings, tape type ---------------------------


@pytest.fixture
def ptouch_env(gui_env, monkeypatch):
    """The P-touch backend with faked query/print/discover: a PT-P710BT with
    heat-shrink tube (as if; that model takes none) for the details."""
    from etiketto.backends import get_backend

    backend = get_backend("ptouch")
    env = SimpleNamespace(
        backend=backend,
        info=PrinterInfo(
            "PT-P710BT",
            12,
            None,
            None,
            "IDLE",
            cassette=CassetteInfo(printable_mm=9.3, media="heat_shrink_2"),
        ),
        prints=[],
    )

    def print_label(image, host, options, progress=None):
        env.prints.append((image, host, options))
        progress(Progress(1, options.copies))

    monkeypatch.setattr(backend, "query", lambda host: env.info)
    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(
        backend,
        "discover",
        lambda timeout=3.0: [
            FoundPrinter("Brother PT-P710BT (USB)", "usb://Brother/PT-P710BT", "")
        ],
    )
    return env


def test_ptouch_printer(make_window, ptouch_env):
    win = make_window()
    model = one(win.sidebar, Adw.ComboRow, title="Printer model")
    model.set_selected(_items(model).index("Brother P-touch"))
    assert pump(lambda: win.printer.info is ptouch_env.info)
    assert win.label.printer == "ptouch"
    tape = win.label_group.tape_row
    assert _items(tape) == ["3.5 mm", "6 mm", "9 mm", "12 mm", "18 mm", "24 mm", "36 mm"]
    tape.set_selected(0)
    assert win.label.tape_mm == 3.5
    tape.set_selected(3)
    assert win.label.tape_mm == 12 and isinstance(win.label.tape_mm, int)
    assert ("Tape type", "Heat-shrink tube 2:1") in win.printer.details()
    assert ("Printable width", "9.3 mm") in win.printer.details()
    assert win.printer.row.get_title() == "Brother PT-P710BT"

    win.label.elements = [TextElement(text="P")]
    render_now(win)
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    rows = [box.get_row_at_index(i) for i in range(5)]
    assert [r.get_title() for r in rows] == [
        "Copies",
        "Cut",
        "Chain printing",
        "High resolution",
        "Tape feed",
    ]
    assert box.get_row_at_index(5) is None  # a single mode, no colour correction
    assert _items(rows[1]) == ["Full cut", "No cut"]  # no half cut on this model
    assert rows[4].get_adjustment().get_lower() == 2  # Brother: 2 mm on 128-pin models
    rows[2].set_active(True)
    rows[4].set_value(5)
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    _image, host, options = ptouch_env.prints[0]
    assert host == "usb://Brother/PT-P710BT"
    assert options.info is ptouch_env.info  # rendered for the model as asked
    assert (options.cut, options.chain, options.high_res, options.feed_mm) == (
        "full",
        True,
        False,
        5.0,
    )
    saved = Settings.load().data["printers"]["ptouch"]
    assert (saved["chain"], saved["high_res"], saved["feed_mm"]) == (True, False, 5.0)
    assert win.print_flow.choices() == {
        "copies": 1,
        "cut": "full",
        "mode": "standard",
        "quality": "",  # no choice of quality
        "chain": True,
        "high_res": False,
        "feed_mm": 5.0,
    }


def test_ptouch_feed_setting_is_checked(make_window, ptouch_env):
    win = make_window()
    win.set_backend("ptouch")
    win.printer.info = None
    for junk, expected in ((True, 2.0), ("x", 2.0), (500, 127.0), (0.5, 1.0), (3, 3.0)):
        win.printer_settings.update(feed_mm=junk, chain="yes")
        choices = win.print_flow.choices()
        assert choices["feed_mm"] == expected and choices["chain"] is False


def test_ptouch_texts():
    from etiketto.ui.texts import MEDIA_NAMES, code_text

    assert set(MEDIA_NAMES) >= {"laminated", "heat_shrink_2", "heat_shrink_3"}
    for code in ("battery", "overheat", "unsupported_model", "editor_lite"):
        assert code_text(code, MILLIMETRES) != code_text("generic", MILLIMETRES)


# --- NIIMBOT: density and label type per job ---------------------------------------------


@pytest.fixture
def niimbot_env(gui_env, monkeypatch):
    """The NIIMBOT backend with faked query/print; a D110 on USB serial."""
    from etiketto.backends import get_backend

    backend = get_backend("niimbot")
    env = SimpleNamespace(info=PrinterInfo("D110", None, None, None, "IDLE"), prints=[])

    def print_label(image, host, options, progress=None):
        env.prints.append((image, host, options))
        progress(Progress(1, options.copies))

    monkeypatch.setattr(backend, "query", lambda host: env.info)
    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    monkeypatch.setattr(backend, "configured_host", lambda: "/dev/ttyACM0")
    return env


@pytest.fixture
def niimbot_window(make_window, niimbot_env):
    win = make_window()
    win.set_backend("niimbot")
    assert pump(lambda: win.printer.info is niimbot_env.info)
    render_now(win)
    return win


def test_niimbot_print_dialog_has_density_and_label_type(niimbot_window, niimbot_env):
    win = niimbot_window
    assert win.printer.row.get_title() == "NIIMBOT D110"
    win.print_flow.on_print()
    box = win.last_dialog.get_extra_child()
    assert box.get_row_at_index(0).get_title() == "Copies"
    density = box.get_row_at_index(1)
    assert density.get_title() == "Print density"
    assert density.get_subtitle() == "Higher values print darker"
    adjustment = density.get_adjustment()
    assert (adjustment.get_lower(), adjustment.get_upper(), density.get_value()) == (1, 3, 2)
    media = box.get_row_at_index(2)
    assert media.get_title() == "Label type"
    assert _items(media) == ["Labels with gaps", "Transparent labels"]
    assert box.get_row_at_index(3) is None
    density.set_value(3)
    media.set_selected(1)
    win.last_dialog.emit("response", "print")
    assert pump(lambda: not win.print_flow.busy)
    options = niimbot_env.prints[0][2]
    assert (options.density, options.media) == (3, "transparent")
    saved = Settings.load().data["printers"]["niimbot"]
    assert (saved["density"], saved["media"]) == (3, "transparent")
    # remembered, and kept within what the connected model takes
    assert win.print_flow.choices()["density"] == 3
    niimbot_env.info = PrinterInfo("B1", None, None, None, "IDLE")
    win.printer.found(niimbot_env.info)
    assert win.print_flow.choices()["media"] == "transparent"
    win.printer_settings.update(density=9, media="tube")
    assert win.print_flow.choices()["density"] == 5
    assert win.print_flow.choices()["media"] == "gap"


def test_printers_without_density_have_no_such_rows(ready):
    choices = ready.print_flow.choices()
    assert "density" not in choices and "media" not in choices
    ready.print_flow.on_print()
    box = ready.last_dialog.get_extra_child()
    titles = []
    index = 0
    while (row := box.get_row_at_index(index)) is not None:
        titles.append(row.get_title())
        index += 1
    assert "Print density" not in titles and "Label type" not in titles
    respond(ready.last_dialog, "cancel")


def test_serial_port_and_battery_texts():
    text = texts.code_text("serial_permission", MILLIMETRES)
    assert "“dialout”" in text and "usermod -aG dialout" in text and "“uucp”" in text
    assert texts.code_text("battery", MILLIMETRES) == (
        "The printer's battery is low – charge it or connect the power adapter."
    )
    assert texts.MEDIA_TYPE_NAMES["black_mark"] == "Labels with black marks"


def test_third_party_notices_name_everything_the_about_dialog_credits():
    """THIRD_PARTY_NOTICES.md is the complete list: every legal section of
    the About dialog and every protocol source is in it."""
    from pathlib import Path

    notices = (Path(__file__).parent.parent / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for name, _holder, _license, _text in app.LEGAL:
        for part in name.split(", "):
            assert part in notices, part
    for source in app.PROTOCOL_SOURCES:
        assert "**%s**" % source in notices, source
    assert "labelburg" in notices


def test_the_preview_follows_a_resolution_the_printer_reports_later(
    make_window, raw_env, monkeypatch
):
    """A ZPL printer set to "as reported": the preview (and its code
    warnings) is first at the default resolution; once the printer has
    reported 300 dpi it is rendered again at that one."""
    from etiketto.backends import get_backend

    backend = get_backend("zpl")
    monkeypatch.setattr(backend, "reported", {})
    win = _raw_window(make_window, "zpl")
    render_now(win)
    assert win.rendered_dpi == pytest.approx(8 * 25.4)
    backend.reported["zebra.local"] = 12 * 25.4  # the query found out
    win.printer.found(raw_env.info)
    assert pump(lambda: win.rendered_dpi == pytest.approx(12 * 25.4))
    win.printer.found(raw_env.info)  # nothing new: no render
    assert win._render_source == 0
