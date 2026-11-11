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

from etiketto import app  # noqa: E402
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
)
from etiketto.model import CodeElement, ImageElement, Label, TextElement  # noqa: E402
from etiketto.render import mm_to_px, render  # noqa: E402
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
        if isinstance(value, BaseException):
            raise value
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


# --- small helpers in app.py ------------------------------------------------------


def test_colour_conversion():
    rgba = app.hex_to_rgba("#ff8000")
    assert app.rgba_to_hex(rgba) == "#ff8000"
    assert app.rgba_to_hex(app.hex_to_rgba("not a colour")) == "#000000"


def test_element_titles_and_icons():
    assert app.element_title(TextElement(text="Erste Zeile\nzweite")) == "Erste Zeile"
    assert app.element_title(TextElement(text="x" * 60)) == "x" * 40
    assert app.element_title(TextElement(text="")) == "Text"
    assert app.element_title(ImageElement(name="foto.png")) == "foto.png"
    assert app.element_title(ImageElement()) == "Image"
    # the code content is isolated so it stays intact inside right-to-left text
    assert app.element_title(CodeElement(data="y" * 50)) == "QR code: \u2068" + "y" * 30 + "\u2069"
    assert (
        app.element_title(CodeElement(data="123", symbology="ean13")) == "EAN-13: \u2068123\u2069"
    )
    assert app.element_icon(TextElement()) == "format-text-bold-symbolic"
    assert app.element_icon(ImageElement()) == "image-x-generic-symbolic"
    assert app.element_icon(CodeElement()) == "view-grid-symbolic"


def test_image_from_path(tmp_path):
    element = app.image_from_path(str(png_file(tmp_path / "a.png")))
    assert element.name == "a.png" and element.raw()[:4] == b"\x89PNG"
    with pytest.raises(ValueError, match="Only local files"):
        app.image_from_path(None)
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(ValueError, match="Not an image: notes.txt"):
        app.image_from_path(str(tmp_path / "notes.txt"))


def test_settings_roundtrip(gui_env):
    assert app.load_settings() == {}
    settings = {"units": "in", "printers": {"vc500w": {"host": "p"}}}
    app.save_settings(settings)
    assert app.load_settings() == settings
    assert not app.SETTINGS_FILE.with_suffix(".tmp").exists()
    app.SETTINGS_FILE.write_text("{broken")
    assert app.load_settings() == {}
    app.SETTINGS_FILE.write_text("[1, 2]")
    assert app.load_settings() == {}


def test_settings_of_etiketto_0_1_are_migrated(gui_env):
    app.save_settings({"host": "p", "safe_mm": 1.0, "cut": "half", "mode": "normal", "units": "mm"})
    assert app.load_settings() == {
        "units": "mm",
        "printers": {"vc500w": {"host": "p", "safe_mm": 1.0, "cut": "half", "mode": "color"}},
    }


def test_migrate_settings_keeps_newer_values():
    settings = {"host": "old", "mode": "vivid", "printers": {"vc500w": {"host": "new"}}}
    assert app.migrate_settings(settings) == {
        "printers": {"vc500w": {"host": "new", "mode": "vivid"}}
    }
    assert app.migrate_settings({"host": "h", "printers": "junk"}) == {
        "printers": {"vc500w": {"host": "h"}}
    }
    assert app.migrate_settings({"units": "in"}) == {"units": "in"}


def test_locale_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "__file__", str(tmp_path / "pkg" / "app.py"))
    monkeypatch.setattr(app.sys, "prefix", str(tmp_path / "prefix"))
    assert app._locale_dir() == tmp_path / "pkg" / "locale"  # nothing found: package dir
    mo = tmp_path / "prefix" / "share" / "locale" / "de" / "LC_MESSAGES" / "etiketto.mo"
    mo.parent.mkdir(parents=True)
    mo.write_bytes(b"")
    assert app._locale_dir() == tmp_path / "prefix" / "share" / "locale"


# --- window state ----------------------------------------------------------------


def test_new_window(window, gui_env):
    assert subtitle(window) == "Unsaved label"
    assert not window.dirty
    assert window.label == Label(elements=[TextElement(text="Label")])
    assert window.rendered is not None
    assert window.picture.get_paintable() is not None
    assert window.size_label.get_text().startswith("50 × ")
    # no host configured: the printer is searched for on the network
    assert gui_env.discovers == 1
    assert window.printer_row.get_title() == "No printer found"


def test_dirty_tracking_and_title(window, tmp_path):
    window.change(margin_mm=3.0)
    assert window.label.margin_mm == 3.0
    assert window.dirty and subtitle(window) == "• Unsaved label"
    assert window._render_source
    path = tmp_path / "kabel.etiketto"
    window._write(path)
    assert not window.dirty and subtitle(window) == "kabel.etiketto"
    assert toast_titles(window) == ["Saved kabel.etiketto"]
    assert Label.load(path) == window.label
    window.mark_changed()
    assert subtitle(window) == "• kabel.etiketto"


def test_write_failure_keeps_dirty(window, tmp_path):
    window.change(tape_mm=12)
    window._write(tmp_path / "missing" / "x.etiketto")
    assert window.dirty and window.path is None
    assert toast_titles(window) == ["Cannot save x.etiketto"]  # no path, no errno
    assert window.toast_log[0].get_button_label() == "Report"


def test_element_list_operations(window):
    first = window.label.elements[0]
    code = CodeElement(data="abc")
    window.add_element(code)
    assert window.label.elements == [first, code] and window._expand_index == 1
    assert window.dirty
    window.move_element(1, 0)
    assert window.label.elements == [code, first] and window._expand_index == 0
    window.remove_element(0)
    assert window.label.elements == [first] and window._expand_index == -1
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
    expander_buttons[0].set_rgba(app.hex_to_rgba("#00ff00"))
    assert element.color == "#00ff00"


def test_code_element_widgets(window):
    window.add_element(CodeElement(data="x"))
    element = window.label.elements[1]
    one(window.sidebar, Adw.ComboRow, title="Type").set_selected(1)
    assert element.symbology == "code128"
    one(window.sidebar, Adw.EntryRow, title="Content (link, text, number)").set_text("ABC")
    assert element.data == "ABC"
    colour_row = one(window.sidebar, Adw.ActionRow, title="Colour")
    one(colour_row, Gtk.ColorDialogButton).set_rgba(app.hex_to_rgba("#0000ff"))
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
    one(border, Gtk.ColorDialogButton).set_rgba(app.hex_to_rgba("#ff0000"))
    bg = one(window.sidebar, Adw.ActionRow, title="Background colour")
    one(bg, Gtk.ColorDialogButton).set_rgba(app.hex_to_rgba("#ffff00"))
    window.tape_row.set_selected(0)
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
    window.choose_image(picked.append)
    assert picked == [] and window.toast_log == []
    (tmp_path / "x.txt").write_text("no")
    file_dialog.result = tmp_path / "x.txt"
    window.choose_image(picked.append)
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
    info = window.size_label.get_text()
    assert info.startswith("25 × 20 mm")
    assert "content scaled down to fit" in info
    assert "Problem: Element 2: the code is empty" in info


def test_render_failure_is_shown(window, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(app, "render", broken)
    render_now(window)
    assert window.rendered is None
    assert window.picture.get_paintable() is None
    assert window.size_label.get_text() == "The preview cannot be drawn"
    window._apply_zoom()  # nothing rendered: no crash, no size change
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
    window.preview_scroller.get_hadjustment().set_page_size(656)
    window.preview_scroller.get_vadjustment().set_page_size(356)
    px_per_mm = min(600 / length, 300 / 50, app.PREVIEW_PX_PER_MM * 4)
    assert window.zoom == pytest.approx(px_per_mm / app.PREVIEW_PX_PER_MM)
    assert window.picture.get_size_request() == (round(length * px_per_mm), round(50 * px_per_mm))
    window.set_zoom(2)
    assert not window.fit_zoom
    assert window.picture.get_size_request() == (round(length * 12), 600)
    window.preview_scroller.get_hadjustment().set_page_size(200)  # no refit when zoomed
    assert window.zoom == 2
    window.set_zoom(100)
    assert window.zoom == 8.0
    window.set_zoom(0.001)
    assert window.zoom == 0.1
    window.set_zoom(None)
    assert window.fit_zoom
    assert window.zoom == pytest.approx(min(144 / length, 6) / app.PREVIEW_PX_PER_MM)


def test_zoom_buttons(window):
    window.set_zoom(1.0)
    one(window, Gtk.Button, tooltip="Zoom in").emit("clicked")
    assert window.zoom == pytest.approx(1.25)
    one(window, Gtk.Button, tooltip="Zoom out").emit("clicked")
    one(window, Gtk.Button, tooltip="Zoom out").emit("clicked")
    assert window.zoom == pytest.approx(0.8)
    one(window, Gtk.Button, tooltip="Fit to window").emit("clicked")
    assert window.fit_zoom


# --- units and printer settings ------------------------------------------------------


def test_units_switch(window, gui_env):
    assert window.unit is MILLIMETRES
    assert window.tape_row.get_model().get_string(0) == "9 mm"
    units = one(window.sidebar, Adw.ComboRow, title="Units")
    assert units.get_model().get_string(0) == "Automatic (Millimetres)"
    units.set_selected(2)
    assert app.load_settings()["units"] == "in"
    assert window.unit is INCHES
    assert window.tape_row.get_model().get_string(0) == "3/8″"
    assert one(window.sidebar, Adw.SpinRow, title="Length").get_subtitle() == "in"
    render_now(window)
    assert "″" in window.size_label.get_text()


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
    row = win.length_row("Test", mm, 0, 1000, step, got.append, "%(unit)s here")
    assert row.get_value() == pytest.approx(value)
    assert row.get_digits() == digits
    assert row.get_subtitle() == ("%s here" % units)
    assert row.get_adjustment().get_step_increment() == win.unit.step(step)
    row.set_value(30.0)
    assert got == [back]
    assert win.length_row("Plain", 1, 0, 2, 1, got.append).get_subtitle() == units


def test_safety_margin_is_not_part_of_the_label(window):
    assert window.safe_mm == 1.5
    one(window.sidebar, Adw.SpinRow, title="Safety margin").set_value(0.04)
    assert window.safe_mm == 0.0
    assert app.load_settings()["printers"]["vc500w"]["safe_mm"] == 0.0
    assert not window.dirty
    render_now(window)
    assert window.rendered.image.tobytes() == render(window.label, safe_mm=0).image.tobytes()
    assert window.rendered.image.tobytes() != render(window.label).image.tobytes()


def test_host_entry(window, gui_env):
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == ""
    entry.set_text("  192.0.2.5 ")
    entry.emit("apply")
    assert app.load_settings()["printers"]["vc500w"]["host"] == "192.0.2.5"
    assert pump(lambda: window.printer_info is not None)
    assert gui_env.queries == ["192.0.2.5"]


def test_host_from_labelprinter_config(make_window, gui_env):
    gui_env.configured = "VC.local"
    win = make_window()
    assert gui_env.queries == ["VC.local"] and gui_env.discovers == 0
    assert one(win.sidebar, Adw.EntryRow, title="Host name or IP address").get_text() == "VC.local"
    app.save_settings({"host": "192.0.2.1"})
    win.settings = app.load_settings()
    assert win.host == "192.0.2.1"  # own setting wins


# --- printer status -----------------------------------------------------------------


def test_printer_found_adopts_tape_of_new_label(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    assert win.label.tape_mm == 25
    assert win.tape_row.get_selected() == 3
    assert not win.dirty and subtitle(win) == "Unsaved label"
    assert win.printer_row.get_title() == "Brother VC-500W"
    assert win.printer_row.get_subtitle() == "\u206825 mm\u2069 cassette · 4 m of 5 m left"


@pytest.mark.parametrize("state", ["dirty", "path"])
def test_printer_found_keeps_changed_or_saved_label(window, tmp_path, state):
    if state == "dirty":
        window.change(margin_mm=1.0)
    else:
        window.path = tmp_path / "x.etiketto"
    window._printer_found(PrinterInfo("Wedge", 12, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer_row.get_subtitle() == "\u206812 mm\u2069 cassette"


def test_printer_without_cassette(window):
    window._printer_found(PrinterInfo("Wedge", None, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer_row.get_subtitle() == "No cassette inserted"


def test_printer_status_in_inches(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window(settings={"units": "in"})
    assert win.printer_row.get_subtitle() == "\u20681″\u2069 cassette · 13.1 ft of 16.4 ft left"


def test_printer_failed(make_window, gui_env):
    gui_env.configured = "p"
    gui_env.query_error = PortClosed("Connection to 'p:9100' was refused.\nPossible solutions")
    win = make_window()
    assert win.printer_info is None
    assert win.printer_row.get_title() == "Printer not reachable"
    # what to do, not the host or labelprinter's English text
    # compare plain text: GLib/libadwaita versions escape the apostrophe differently
    assert html.unescape(win.printer_row.get_subtitle()) == (
        "The printer refuses the connection. Switch it off and on again. If its web interface "
        "offers a “Port 9100” option, turn it on."
    )
    assert not win.details_button.get_sensitive()


def test_refresh_button(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    one(win.sidebar, Gtk.Button, tooltip="Refresh").emit("clicked")
    assert win.printer_row.get_title() == "Connecting…"
    assert pump(lambda: len(gui_env.queries) == 2 and win.printer_info is not None)


# --- discovery ---------------------------------------------------------------------


def found(*names):
    return [
        FoundPrinter("Brother VC-500W (%s)" % n, "VC-%s.local" % n, "192.0.2.%d" % i)
        for i, n in enumerate(names, 10)
    ]


def test_discover_none(window, gui_env):
    window.discover_printers(choose=True)
    assert window.printer_row.get_title() == "Searching for printers…"
    assert pump(lambda: window.printer_row.get_title() == "No printer found")
    assert window.printer_row.get_subtitle().startswith("Make sure the printer is switched on")
    assert window.host is None


def test_discover_one_is_used(window, gui_env):
    gui_env.found = found("A")
    one(window.sidebar, Gtk.Button, tooltip="Search for printers").emit("clicked")
    assert pump(lambda: window.printer_info is not None)
    assert app.load_settings()["printers"]["vc500w"]["host"] == "VC-A.local"
    assert gui_env.queries == ["VC-A.local"]
    assert toast_titles(window) == ["Found Brother VC-500W (A)"]
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == "VC-A.local"


def test_discover_several_without_choice_takes_first(window, gui_env):
    gui_env.found = found("A", "B")
    window.discover_printers(choose=False)
    assert pump(lambda: window.printer_info is not None)
    assert window.host == "VC-A.local"
    assert window.get_visible_dialog() is None


def test_discover_several_lets_the_user_choose(window, gui_env):
    gui_env.found = found("A", "B")
    window.discover_printers(choose=True)
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
    assert window.host is None and gui_env.queries == []
    assert window.printer_row.get_title() == "Choose a printer"
    rows[1].emit("activated")
    assert window.host == "VC-B.local"
    assert pump(lambda: "VC-B.local" in gui_env.queries)
    assert window.get_visible_dialog() is None


def test_discover_outdated_results_are_dropped(window, gui_env):
    window.discover_printers(choose=True)
    first = window._query_generation
    window.discover_printers(choose=True)  # searched again before the answer came
    assert pump(lambda: window.printer_row.get_title() == "No printer found")
    assert window._printers_discovered(found("A", "B"), True, first) is False
    assert window.get_visible_dialog() is None and window.host is None


def test_discover_never_opens_two_choosers(window, gui_env):
    window._printers_discovered(found("A", "B"), True, window._query_generation)
    first = window.get_visible_dialog()
    window._printers_discovered(found("A", "B", "C"), True, window._query_generation)
    second = window.get_visible_dialog()
    assert second is not first and window._chooser is second
    assert len(find(second.get_extra_child(), Adw.ActionRow)) == 3
    window._on_chooser_closed(first)  # a late signal of the old one changes nothing
    assert window._chooser is second
    second.force_close()
    assert pump(lambda: window._chooser is None)


def test_discover_several_with_host_refreshes_current(make_window, gui_env):
    gui_env.configured = "VC-A.local"
    win = make_window()
    gui_env.found = found("A", "B")
    win.discover_printers(choose=True)
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
    ready.printing = True
    ready.on_print()
    assert ready.get_visible_dialog() is None
    ready.printing = False
    ready.rendered = None
    ready.on_print()
    assert ready.get_visible_dialog() is None
    ready.set_label(Label(tape_mm=25), None)
    render_now(ready)
    ready.on_print()
    assert toast_titles(ready) == ["The label is empty"]
    assert ready.get_visible_dialog() is None


def test_print_background_only_is_not_empty(ready, tmp_path):
    ready.set_label(
        Label(tape_mm=25, background_image=app.image_from_path(str(png_file(tmp_path / "b.png")))),
        None,
    )
    render_now(ready)
    ready.on_print()
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
        assert print_dialog is ready._last_dialog
        assert print_dialog.get_heading() == "Print label"
    else:
        assert print_dialog is None


def test_print_flow(ready, gui_env):
    ready.on_print()
    dialog = ready._last_dialog
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

    assert ready.printing
    assert not ready.print_button.get_sensitive()
    assert ready.print_button.get_label() == "Printing…"
    assert app.load_settings()["printers"]["vc500w"] == {"cut": "half", "mode": "color"}

    assert pump(lambda: not ready.printing)
    assert len(gui_env.prints) == 1
    image, host, options = gui_env.prints[0]
    assert host == "VC.local"
    assert options == PrintOptions(copies=3, cut="half", mode="color", data_format="rawrgb")
    # rendered in the worker for the printer: rawrgb, 25 mm at 320 dpi with bleed
    assert image.height == 333
    assert ready.print_button.get_sensitive()
    assert ready.print_button.get_label() == "Print"
    assert toast_titles(ready) == ["Printed"]
    assert pump(lambda: len(gui_env.queries) == 2)  # status refreshed after printing

    ready.on_print()  # last choices are the defaults now
    box = ready._last_dialog.get_extra_child()
    assert box.get_row_at_index(1).get_selected() == 1
    assert box.get_row_at_index(2).get_selected() == 1


def test_print_dialog_cancel(ready, gui_env):
    ready.on_print()
    respond(ready._last_dialog, "cancel")
    assert not ready.printing and gui_env.prints == []


def test_print_without_host(window, gui_env):
    window._start_print(PrintOptions())
    assert toast_titles(window) == ["No printer yet – search for one or enter its address"]
    assert not window.printing and gui_env.prints == []


def test_print_error_dialog_with_details_and_report(ready, gui_env, launched):
    gui_env.print_error = PaperJam("print_state=ERROR print_job_error=JAM")
    ready._start_print(PrintOptions(copies=2))
    assert pump(lambda: not ready.printing)
    assert ready.print_button.get_label() == "Print"
    assert ready.toast_log == []  # a dialog, not a toast
    dialog = ready.get_visible_dialog()
    assert dialog is ready._last_dialog
    assert dialog.get_heading() == "Printing failed"
    assert dialog.get_body() == "The label is jammed. Open the cover, remove it and try again."
    expander = dialog.get_extra_child()
    assert isinstance(expander, Gtk.Expander) and not expander.get_expanded()
    assert expander.get_child().get_label() == "print_state=ERROR print_job_error=JAM"
    respond(dialog, "close")
    assert launched == []
    ready._print_done(PaperJam("print_state=ERROR print_job_error=JAM"))
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
    window.printer_info = PrinterInfo("Wedge", None, None, None, "ERROR", firmware="2026031504")
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
    window.open_file(saved)
    assert window.label.tape_mm == 19 and window.path == saved
    assert not window.dirty and subtitle(window) == "saved.etiketto"
    assert window._expand_index == -1


def test_open_broken_file(window, tmp_path):
    for name, content in (("bad.etiketto", "{"), ("wide.etiketto", json.dumps({"tape_mm": 7}))):
        (tmp_path / name).write_text(content)
        window.open_file(tmp_path / name)
    (tmp_path / "new.etiketto").write_text(json.dumps({"version": 99}))
    window.open_file(tmp_path / "new.etiketto")
    window.open_file(tmp_path / "missing.etiketto")
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
    window.on_open()
    assert [d.method for d in file_dialog.instances] == [None]  # not shown yet
    dialog = window.get_visible_dialog()
    assert dialog.get_heading() == "Discard changes?"
    respond(dialog, "cancel")
    assert [d.method for d in file_dialog.instances] == [None] and window.dirty
    window.on_open()
    respond(window.get_visible_dialog(), "discard")
    assert window.path == saved and not window.dirty


def test_save_and_save_as(window, file_dialog, tmp_path):
    file_dialog.result = cancelled()
    window.activate_action("win.save", None)  # no path yet: save as
    assert file_dialog.instances[-1].initial_name == "label.etiketto"
    assert window.path is None
    file_dialog.result = tmp_path / "neu.txt"
    window.on_save()
    assert window.path == tmp_path / "neu.etiketto"
    assert (tmp_path / "neu.etiketto").exists() and not (tmp_path / "neu.txt").exists()
    window.change(tape_mm=9)
    window.on_save()  # known path: no dialog
    assert len(file_dialog.instances) == 2
    assert Label.load(tmp_path / "neu.etiketto").tape_mm == 9
    window.activate_action("win.save-as", None)
    assert file_dialog.instances[-1].initial_name == "neu.etiketto"
    folder = tmp_path / "new" / "folder"
    window.on_save_as(folder=folder)
    assert folder.is_dir() and file_dialog.instances[-1].initial_folder == str(folder)


def test_save_template(window, file_dialog, gui_env):
    window.change(tape_mm=12)
    file_dialog.result = cancelled()
    window.activate_action("win.save-template", None)
    assert app.TEMPLATE_DIR.is_dir()
    assert file_dialog.instances[-1].initial_folder == str(app.TEMPLATE_DIR)
    assert file_dialog.instances[-1].kwargs["title"] == "Save as template…"
    file_dialog.result = app.TEMPLATE_DIR / "Kabel"
    window.on_save_template()
    assert Label.load(app.TEMPLATE_DIR / "Kabel.etiketto").tape_mm == 12
    assert toast_titles(window) == ["Saved Kabel.etiketto"]
    assert window.path is None and window.dirty  # the open label is unchanged
    file_dialog.result = gui_env.tmp / "missing" / "x.etiketto"
    window.on_save_template()
    assert toast_titles(window)[-1] == "Cannot save x.etiketto"


def test_export_png(window, file_dialog, tmp_path):
    file_dialog.result = cancelled()
    window.activate_action("win.export", None)
    assert file_dialog.instances[-1].initial_name == "label.png"
    file_dialog.result = tmp_path / "out"
    window.on_export()
    from PIL import Image

    with Image.open(tmp_path / "out.png") as img:
        assert img.size == window.rendered.image.size
        assert round(img.info["dpi"][0]) == 317
    assert toast_titles(window) == ["Exported"]
    file_dialog.result = tmp_path / "missing" / "x.PNG"
    window.on_export()
    assert toast_titles(window)[-1] == "Cannot save x.PNG"


def test_export_png_render_failure(window, tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(app, "render", broken)
    window.export_png(tmp_path / "x.png")
    assert toast_titles(window) == ["The preview cannot be drawn"]
    assert not (tmp_path / "x.png").exists()


def test_templates_menu(window, gui_env):
    window._fill_templates()
    menu = window.template_menu
    assert menu.get_n_items() == 1
    label = menu.get_item_attribute_value(0, "label", GLib.VariantType("s")).get_string()
    assert label.startswith("No templates yet")
    app.TEMPLATE_DIR.mkdir(parents=True)
    for name in ("b", "a"):
        Label(tape_mm=9).save(app.TEMPLATE_DIR / ("%s.etiketto" % name))
    (app.TEMPLATE_DIR / "ignored.txt").write_text("")
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
    assert target == str(app.TEMPLATE_DIR / "b.etiketto")


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
    window.printer_info = PrinterInfo("Wedge", 19, None, None, "IDLE")
    window.on_new()
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
    window.printing = True
    assert window._on_close_request(window) is True
    assert toast_titles(window) == ["Please wait until printing has finished"]
    window.printing = False
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


def test_app_activate_reuses_window(application):
    application.do_activate()
    first = application.get_active_window()
    assert isinstance(first, app.Window)
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

    assert app.translation_is_rtl(Catalog("ar"))
    assert app.translation_is_rtl(Catalog("he_IL"))
    assert not app.translation_is_rtl(Catalog("de"))
    assert not app.translation_is_rtl(Catalog(""))
    assert not app.translation_is_rtl(object())  # NullTranslations without info()


# --- messages ---------------------------------------------------------------------------

I = "⁨%s⁩"  # noqa: E741 - isolated text, as in the interface


def test_warning_texts():
    from etiketto.render import RenderWarning

    texts = {
        "code_empty": "Element 2: the code is empty",
        "ean13_digits": "Element 2: EAN-13 needs exactly 12 digits",
        "ean13_check": "Element 2: the EAN-13 check digit is wrong",
        "code128_charset": "Element 2: Code 128 only accepts Latin letters, digits and punctuation",
        "image_unreadable": "Element 2: the image cannot be read",
        "element_failed": "Element 2 cannot be drawn",
    }
    for key, text in texts.items():
        assert app.warning_text(RenderWarning(key, {"n": 2})) == text
    assert app.warning_text(RenderWarning("no_room")) == (
        "Safety margin, border and edge spacing leave no room – reduce them"
    )
    assert app.warning_text(RenderWarning("fixed_too_short")) == (
        "The fixed length is shorter than the spacing – increase it"
    )
    assert app.warning_text(RenderWarning("background_unreadable")) == (
        "The background image cannot be read"
    )
    assert app.warning_text(RenderWarning("from_the_future")) == "from_the_future"


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
    text = app.printer_error_text(error, MILLIMETRES)
    assert text.startswith(start)
    assert "SEQUENCE" not in text and "anything" not in text  # details stay out


def test_printer_error_too_long_uses_the_unit():
    error = LabelTooLong("17.5 in", max_mm=431.8)
    assert app.printer_error_text(error, MILLIMETRES) == (
        "The label is too long for this printer: at most 431.8 mm."
    )
    assert app.printer_error_text(error, INCHES).endswith("at most 17.00 in.")


def test_stage_texts():
    assert app.stage_text("PREHEATING") == "Warming up…"
    assert app.stage_text("COOLDOWN") == "Cooling down…"
    assert app.stage_text("CLEANING") == "Cleaning…"
    assert app.stage_text("READY_FOR_CUT") == "Swipe the printer's panel to cut the label"
    assert app.stage_text("PRINTING") is None and app.stage_text("") is None


def test_label_and_save_error_texts():
    from etiketto.model import LabelError

    assert app.label_error_text(LabelError("not_label", "x"), "a.etiketto") == (
        "a.etiketto is not an Etiketto label"
    )
    assert app.label_error_text(LabelError("newer", "x"), "a.etiketto") == (
        "This label needs a newer version of Etiketto"
    )
    assert app.label_error_text(LabelError("damaged", "x"), "a.etiketto") == (
        "The file is damaged: a.etiketto"
    )
    for code in (errno.EACCES, errno.EPERM, errno.EROFS):
        assert app.save_error_text(OSError(code, "x"), "a") == (
            "Cannot save a: no permission to write there"
        )
    for code in (errno.ENOSPC, errno.EDQUOT):
        assert app.save_error_text(OSError(code, "x"), "a") == "Cannot save a: the disk is full"
    assert app.save_error_text(OSError(errno.EIO, "x"), "a") == "Cannot save a"


def _with_cassette(made):
    return PrinterInfo("Wedge", 25, None, None, "IDLE", cassette=CassetteInfo(made=made))


def test_cassette_age_hint():
    today = date(2026, 11, 5)
    assert app.cassette_age_hint(_with_cassette(date(2018, 5, 29)), today) == (
        "This cassette seems to be from May 2018. Old ZINK paper prints paler colours."
    )
    assert app.cassette_age_hint(_with_cassette(today - timedelta(days=731)), today)
    assert app.cassette_age_hint(_with_cassette(today - timedelta(days=730)), today) is None
    assert app.cassette_age_hint(_with_cassette(None), today) is None
    assert app.cassette_age_hint(PrinterInfo("Wedge", 25, None, None, "IDLE"), today) is None
    assert app.cassette_age_hint(None, today) is None
    assert [app.month_name(m) for m in (1, 12)] == ["January", "December"]


def test_month_names_have_a_context(monkeypatch):
    class Catalog:
        def pgettext(self, context, message):
            return "%s|%s" % (context, message)

    monkeypatch.setattr(app, "TRANSLATION", Catalog())
    assert app.month_name(5) == "month name|May"


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
    assert detailed.details_button.get_sensitive()
    one(detailed.sidebar, Gtk.Button, tooltip="Printer details").emit("clicked")
    dialog = detailed.get_visible_dialog()
    assert dialog is detailed._last_dialog and dialog.get_heading() == "Printer details"
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
    window.show_printer_details()  # not connected: nothing to show
    assert window.get_visible_dialog() is None
    window.printer_info = PrinterInfo("Wedge", None, None, None, "IDLE")
    assert window.printer_details() == [("Model", "Brother VC-500W")]
    window.show_printer_details()
    assert window.get_visible_dialog().get_body() == ""
    respond(window.get_visible_dialog(), "close")


def test_old_cassette_hint_row(detailed, gui_env):
    hint = detailed.cassette_hint
    assert hint.get_visible()
    assert hint.get_title() == (
        "This cassette seems to be from May 2018. Old ZINK paper prints paler colours."
    )
    gui_env.query_result = PrinterInfo("Wedge", 25, None, None, "IDLE")
    detailed.refresh_printer()
    assert pump(lambda: not hint.get_visible())
    detailed._printer_found(_with_cassette(date(2018, 1, 1)))
    assert hint.get_visible()
    detailed._printer_failed(PrinterTimeout())
    assert not hint.get_visible()


def test_old_printer_answers_are_dropped(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    current = win._query_generation
    win._printer_found(PrinterInfo("Wedge", 12, None, None, "IDLE"), current - 1)
    assert win.printer_info.tape_mm == 25
    win._printer_failed(PortClosed(), current - 1)
    assert win.printer_row.get_title() == "Brother VC-500W"
    win.discover_printers()  # a search makes a pending status answer outdated, too
    assert win._query_generation == current + 1
    win._printer_found(PrinterInfo("Wedge", 12, None, None, "IDLE"), current)
    assert win.printer_info.tape_mm == 25
    assert pump(lambda: not win.printer_row.get_title().startswith("Searching"))


# --- printing: options from the capabilities -------------------------------------------------


def test_print_dialog_offers_the_printers_modes(detailed, gui_env):
    detailed.on_print()
    box = detailed._last_dialog.get_extra_child()
    mode = box.get_row_at_index(2)
    assert [mode.get_model().get_string(i) for i in range(3)] == [
        "Vivid",
        "Normal",
        "Black and white (sharper)",
    ]
    mode.set_selected(2)
    detailed._last_dialog.emit("response", "print")
    assert pump(lambda: not detailed.printing)
    assert gui_env.prints[0][2].mode == "bw"


def test_saved_mode_the_printer_lacks_falls_back(ready):
    ready.save_printer_setting(mode="bw", cut="sideways")
    assert ready._print_choices() == {"copies": 1, "cut": "full", "mode": "vivid"}


def test_colour_adjustment(ready, gui_env):
    ready.on_print()
    colour = ready._last_dialog.get_extra_child().get_row_at_index(3)
    assert colour.get_subtitle() == "Not adjusted"
    reset = [b for b in find(colour, Gtk.Button) if b.get_label() == "Reset"][0]
    assert not reset.get_sensitive()  # nothing to reset
    spins = find(colour, Adw.SpinRow)
    assert [s.get_title() for s in spins] == ["Brightness", "Red", "Green", "Blue"]
    assert all(s.get_adjustment().get_lower() == -3 for s in spins)
    assert all(s.get_adjustment().get_upper() == 3 for s in spins)
    spins[0].set_value(2)
    spins[2].set_value(-3)
    assert app.load_settings()["printers"]["vc500w"]["color_adjust"] == {
        "brightness": 2,
        "red": 0,
        "green": -3,
        "blue": 0,
    }
    assert colour.get_subtitle() == "Adjusted" and reset.get_sensitive()
    assert not ready.dirty  # a printer setting, not part of the label
    ready._last_dialog.emit("response", "print")
    assert pump(lambda: not ready.printing)
    assert gui_env.prints[0][2].color == ColorAdjust(2, 0, -3, 0)

    ready.on_print()  # remembered; the reset button puts everything back
    colour = ready._last_dialog.get_extra_child().get_row_at_index(3)
    spins = find(colour, Adw.SpinRow)
    assert [s.get_value() for s in spins] == [2, 0, -3, 0]
    reset = [b for b in find(colour, Gtk.Button) if b.get_label() == "Reset"][0]
    assert reset.get_sensitive()
    reset.emit("clicked")
    assert [s.get_value() for s in spins] == [0, 0, 0, 0]
    assert ready.color_adjust.is_neutral() and colour.get_subtitle() == "Not adjusted"
    assert not reset.get_sensitive()
    respond(ready._last_dialog, "cancel")


@pytest.mark.parametrize(
    "setting, expected", [(None, "rawrgb"), ("png", "rawrgb"), ("jpeg", "jpeg")]
)
def test_data_format_setting(make_window, gui_env, setting, expected):
    gui_env.configured = "VC.local"
    printer = {} if setting is None else {"data_format": setting}
    win = make_window(settings={"printers": {"vc500w": printer}})
    win.on_print()
    win._last_dialog.emit("response", "print")
    assert pump(lambda: not win.printing)
    image, _host, options = gui_env.prints[0]
    assert options.data_format == expected
    # rawrgb: rendered with bleed for the exact width (25 mm at 320 dpi: 333 px)
    assert image.height == (333 if expected == "rawrgb" else mm_to_px(25))


def test_print_with_another_mode_is_reported(ready, gui_env):
    gui_env.print_result = PrintResult(mode="vivid")
    ready._start_print(PrintOptions(mode="bw"))
    assert pump(lambda: not ready.printing)
    assert toast_titles(ready) == [
        "Colour mode “Black and white (sharper)” is not available on this printer, "
        "printed with “Vivid”"
    ]


def test_print_result_with_the_requested_mode_is_just_printed(ready, gui_env):
    gui_env.print_result = PrintResult(mode="color")
    ready._start_print(PrintOptions(mode="color"))
    assert pump(lambda: not ready.printing)
    assert toast_titles(ready) == ["Printed"]


def test_bw_mode_shows_a_grey_preview(make_window, gui_env):
    from PIL import Image

    gui_env.configured = "VC.local"
    gui_env.query_result = PrinterInfo("Wedge", 25, 4000, 5000, "IDLE", modes=ALL_MODES)
    win = make_window(settings={"printers": {"vc500w": {"mode": "bw"}}})
    win.change(background="#ff0000")
    render_now(win)
    assert win.print_mode == "bw"
    png = win.picture.get_paintable().save_to_png_bytes().get_data()
    assert all(r == g == b for r, g, b, *_ in _colours(Image.open(io.BytesIO(png))))
    # choosing a colour mode in the print dialog shows colours again
    win.on_print()
    mode = win._last_dialog.get_extra_child().get_row_at_index(2)
    mode.set_selected(0)
    assert app.load_settings()["printers"]["vc500w"]["mode"] == "vivid"
    render_now(win)
    png = win.picture.get_paintable().save_to_png_bytes().get_data()
    assert (255, 0, 0) in {c[:3] for c in _colours(Image.open(io.BytesIO(png)))}
    respond(win._last_dialog, "cancel")


def test_length_row_is_limited_by_the_printer(window):
    length = one(window.sidebar, Adw.SpinRow, title="Length")
    adjustment = length.get_adjustment()
    assert (adjustment.get_lower(), adjustment.get_upper()) == (15, 431)


def test_too_long_label_is_reported(window):
    window.change(auto_length=False, length_mm=600)
    render_now(window)
    assert "Too long for this printer: at most 431.8 mm" in window.size_label.get_text()
    window.change(length_mm=400)
    render_now(window)
    assert "Too long" not in window.size_label.get_text()


def test_printer_stages_in_a_banner(ready, gui_env):
    banner = ready.banner
    ready.printing = True
    ready._print_progress(Progress(1, 2, "PREHEATING"))
    assert banner.get_revealed() and banner.get_title() == "Warming up…"
    assert ready.print_button.get_label() == "Printing 1/2…"
    ready._print_progress(Progress(2, 2, "PRINTING"))
    assert not banner.get_revealed()
    assert ready.print_button.get_label() == "Printing 2/2…"
    ready._print_progress(Progress(2, 2, "READY_FOR_CUT"))
    assert html.unescape(banner.get_title()) == ("Swipe the printer's panel to cut the label")
    ready._print_done(None)
    assert not banner.get_revealed()
    ready._print_progress(Progress(1, 1, "COOLDOWN"))  # late event after the end
    assert not banner.get_revealed() and ready.print_button.get_label() == "Print"

    gui_env.stages = ["COOLDOWN"]
    ready._start_print(PrintOptions())
    assert pump(lambda: not ready.printing)
    assert not banner.get_revealed()
    assert toast_titles(ready)[-1] == "Printed"


def test_print_prepare_failure(ready, gui_env, monkeypatch):
    def broken(label, options, safe_mm):
        raise RuntimeError("boom")

    monkeypatch.setattr(gui_env.backend, "prepare", broken)
    ready._start_print(PrintOptions())
    assert pump(lambda: not ready.printing)
    dialog = ready.get_visible_dialog()
    assert dialog.get_body() == "The label cannot be prepared for printing."
    assert dialog.get_extra_child().get_child().get_label() == "RuntimeError: boom"
    assert gui_env.prints == []
    respond(dialog, "close")


def test_print_too_long(ready, gui_env):
    gui_env.print_error = LabelTooLong("17.5 in", max_mm=431.8)
    ready._start_print(PrintOptions())
    assert pump(lambda: not ready.printing)
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
        row = window.length_row("Length", mm, 15, 1000, 1, got.append)
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
    assert [model.get_model().get_string(i) for i in range(2)] == [
        "Brother VC-500W",
        "Fake Mono 200",
    ]
    model.set_selected(1)
    assert win.label.printer == "fake-mono" and win.backend is mono
    assert win.label.tape_mm == 24 and win.dirty  # nearest width the printer has
    tape = win.tape_row.get_model()
    assert [tape.get_string(i) for i in range(tape.get_n_items())] == ["12 mm", "24 mm"]
    # no host for this printer yet: searched, found and remembered for it alone
    assert pump(lambda: win.printer_info is mono.info)
    assert app.load_settings()["printers"]["fake-mono"]["host"] == "mono.local"
    assert mono.queries == ["mono.local"]
    assert "host" not in app.load_settings()["printers"].get("vc500w", {})
    # no remaining tape: the printer does not report it
    assert win.printer_row.get_subtitle() == "⁨12 mm⁩ cassette"

    render_now(win)
    png = win.picture.get_paintable().save_to_png_bytes().get_data()
    assert _colours(Image.open(io.BytesIO(png))) <= {(0, 0, 0), (255, 255, 255)}

    win.change(tape_mm=12)
    render_now(win)
    win.on_print()
    box = win._last_dialog.get_extra_child()
    assert box.get_row_at_index(0).get_title() == "Copies"
    assert box.get_row_at_index(1) is None  # no cut, a single mode, no colour correction
    win._last_dialog.emit("response", "print")
    assert pump(lambda: not win.printing)
    image, host, options = mono.jobs[0]
    assert host == "mono.local"
    assert (options.cut, options.mode, options.data_format) == ("", "standard", "jpeg")
    assert _colours(image) <= {(0, 0, 0), (255, 255, 255)}
    assert image.height == mm_to_px(12, 203)

    win.dirty = False
    win.on_new()  # a new label stays with this printer
    assert win.label.printer == "fake-mono" and win.label.tape_mm == 12
    win.printer_info = None
    win.on_new()
    assert win.label.tape_mm == 24  # no 50 mm here: the widest tape


def test_no_length_rows_for_die_cut_printers(make_window, mono, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(mono, "capabilities", replace(mono.capabilities, continuous=False))
    win = make_window()
    assert find(win.sidebar, Adw.SpinRow, title="Length")
    win._on_backend("fake-mono")
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
    one(win.sidebar, Adw.ComboRow, title="Printer model").set_selected(1)
    assert pump(lambda: win.printer_info is cab_env.info)
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
    assert app.load_settings()["printers"]["cab"]["host"] == "printer04.local"
    assert win.printer_row.get_title() == "cab EOS2/300"
    assert win.printer_row.get_subtitle() == "Ready · a label every 28.0 mm"
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
    assert win.size_label.get_text().startswith("50 × 25 mm")
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
    win._on_units("in")
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
    assert win.size_label.get_text().startswith("50 × 25 mm")


def test_cab_print(cab_window, cab_env):
    win = cab_window
    win.on_print()
    dialog = win._last_dialog
    assert dialog.get_heading() == "Print label" and dialog.get_body() == "50 × 25 mm"
    box = dialog.get_extra_child()
    assert box.get_row_at_index(0).get_title() == "Copies"
    assert box.get_row_at_index(1) is None  # no cutter, one mode
    dialog.emit("response", "print")
    assert pump(lambda: not win.printing)
    image, host, options = cab_env.prints[0]
    assert host == "printer04.local" and options.gap_mm == 3
    assert image.size == (600, 300)  # printer orientation, 12 dots per mm
    assert toast_titles(win)[-1] == "Printed"


@pytest.mark.parametrize("answer, prints", [("cancel", False), ("print", True)])
def test_cab_different_labels_loaded(cab_window, cab_env, answer, prints):
    win = cab_window
    cab_env.info.pitch_mm = 20.0
    win.on_print()
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Different labels loaded"
    body = dialog.get_body()
    assert "50 × 25 mm" in body and "3.0 mm" in body and "28.0 mm" in body and "20.0 mm" in body
    respond(dialog, answer)
    following = win.get_visible_dialog()
    assert (following is not None and following.get_heading() == "Print label") == prints


def test_cab_close_enough_pitch_prints_directly(cab_window, cab_env):
    cab_env.info.pitch_mm = 28.8  # the sensor measures in whole mm
    cab_window.on_print()
    assert cab_window.get_visible_dialog().get_heading() == "Print label"


def test_cab_too_wide(cab_window):
    win = cab_window
    win.label.set_media_size(120, 25)
    render_now(win)
    assert "Too wide for this printer: at most 105.6 mm" in win.size_label.get_text()


def test_cab_printer_states(make_window, cab_env):
    from etiketto.backends.base import HeadOpen

    win = make_window()
    win._on_backend("cab")
    cab_env.info.error, cab_env.info.state = "out_of_labels", "ERROR"
    win.refresh_printer()
    assert pump(lambda: win.printer_info is cab_env.info)
    assert win.printer_row.get_title() == "Printer needs attention"
    assert win.printer_row.get_subtitle().startswith("The printer is out of labels")
    cab_env.info.error, cab_env.info.state = "", "BUSY"
    win._update_printer_row()
    assert win.printer_row.get_subtitle() == "Printing"
    cab_env.info.state, cab_env.info.pitch_mm = "IDLE", None
    win._update_printer_row()
    assert win.printer_row.get_subtitle() == "Ready"
    cab_env.error = HeadOpen("ESC s YD000000N")
    win.refresh_printer()
    assert pump(lambda: win.printer_row.get_title() == "Printer needs attention")
    assert win.printer_row.get_subtitle() == "The print head is open. Close it and try again."
    cab_env.error = PrinterTimeout("timed out")
    win.refresh_printer()
    assert pump(lambda: win.printer_row.get_title() == "Printer not reachable")


def test_cab_details_and_report(cab_window):
    win = cab_window
    details = dict(win.printer_details())
    assert details["Model"] == "cab EOS2/300"
    assert details["Label distance"] == "28.0 mm"
    assert "Cassette" not in details and "Remaining tape" not in details
    assert (
        win.printer_description()
        == "cab EOS2/300, label every 28 mm, IDLE, firmware 5.47.7 Jan 22, 2026"
    )
    win.printer_info.pitch_mm = None
    assert "label every ? mm" in win.printer_description()


def test_cab_back_to_tape_after_a_preset(cab_window):
    win = cab_window
    one(win.sidebar, Adw.ComboRow, title="Label size").set_selected(4)  # 100 × 50 mm
    win._on_backend("vc500w")
    assert win.label.tape_mm == 50 and isinstance(win.label.tape_mm, int)
    assert _selected(one(win.sidebar, Adw.ComboRow, title="Tape width")) == "50 mm"


def test_opening_a_label_for_the_other_printer(cab_window, cab_env, gui_env):
    win = cab_window
    gui_env.configured = "VC-500W.local"
    win.set_label(Label(tape_mm=25), None)  # a VC-500W label
    assert win.backend.id == "vc500w"
    assert pump(lambda: win.printer_info is gui_env.query_result)
    assert win.printer_row.get_title() == "Brother VC-500W"
    win.set_label(Label(tape_mm=25), None)  # same printer: no new query
    assert win.printer_info is gui_env.query_result


def test_cab_gap_row_allows_what_files_may_have(cab_window):
    gap = one(cab_window.sidebar, Adw.SpinRow, title="Gap")
    assert gap.get_adjustment().get_upper() == 50


def test_cab_new_label_and_back_to_tape(cab_window):
    win = cab_window
    win.label.set_media_size(100, 150)
    win.dirty = False
    win.on_new()
    assert win.label.printer == "cab" and win.label.media_size() == (50, 25)
    win._on_backend("vc500w")
    assert win.label.orientation == "along" and win.label.auto_length
    assert win.label.tape_mm == 25  # the nearest tape to 25 mm (label space)
    assert find(win.sidebar, Adw.ComboRow, title="Tape width")


def test_switching_back_restores_the_size(window):
    win = window
    win.label.tape_mm, win.label.auto_length, win.label.length_mm = 19, False, 80.0
    win._on_backend("cab")
    win.label.set_media_size(76, 51)
    win._on_backend("vc500w")
    assert (win.label.tape_mm, win.label.auto_length, win.label.length_mm) == (19, False, 80.0)
    win._on_backend("cab")
    assert win.label.media_size() == (76, 51)
    win.set_label(Label(tape_mm=25), None)  # another label: nothing to restore
    win._on_backend("cab")
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
    from etiketto.backends.base import LabelTooWide

    error = LabelTooWide("x", max_mm=105.6) if code == "too_wide" else app.error_from_code(code)
    assert app.printer_error_text(error, MILLIMETRES).startswith(start)


def test_choosing_the_same_model_changes_nothing(window, mono):
    window._on_backend("vc500w")
    assert not window.dirty


def test_backend_and_settings_are_robust(window):
    window.label.printer = "plugin-gone"
    assert window.backend.id == "vc500w"
    window.settings["printers"] = "junk"
    assert window.printer_settings == {}
    window.settings["printers"]["vc500w"] = 5
    assert window.printer_settings == {}
    window.printer_settings["safe_mm"] = "wide"
    assert window.safe_mm == 1.5


def test_docs_url_follows_the_interface_language():
    class Catalog:
        def __init__(self, language):
            self._language = language

        def info(self):
            return {"language": self._language}

    assert app.docs_url(Catalog("de")) == "https://etiketto.joshuahirsig.xyz/de/"
    assert app.docs_url(Catalog("de_CH")) == "https://etiketto.joshuahirsig.xyz/de/"
    # no documentation in that language yet: English
    assert app.docs_url(Catalog("ja")) == "https://etiketto.joshuahirsig.xyz/en/"
    assert app.docs_url(object()) == "https://etiketto.joshuahirsig.xyz/en/"


def test_month_year_is_one_translatable_pattern():
    assert app.month_year(5, 2018) == "May 2018"


def test_help_action_opens_the_documentation(make_window, monkeypatch):
    win = make_window()
    opened = []
    monkeypatch.setattr(win, "open_url", opened.append)
    win.activate_action("win.help", None)
    assert opened == [app.docs_url()]


def test_too_long_fixed_length_from_a_file_is_clamped_consistently(make_window):
    win = make_window()
    win.set_label(Label(auto_length=False, length_mm=1000), None)
    # the row shows the printer's maximum, and the label really uses it
    assert win.label.length_mm == pytest.approx(431, abs=1)
