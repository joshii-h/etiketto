"""The GTK window in the test process: real widgets, faked printer and dialogs.

device.query/print_label/discover, Gtk.FileDialog and Gtk.UriLauncher are
replaced (see conftest.gui_env and the fixtures below); config, settings and
templates live in tmp_path.
"""

from __future__ import annotations

import json
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

from etiketto import app, device  # noqa: E402
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
    app.save_settings({"host": "p", "units": "in"})
    assert app.load_settings() == {"host": "p", "units": "in"}
    assert not app.SETTINGS_FILE.with_suffix(".tmp").exists()
    app.SETTINGS_FILE.write_text("{broken")
    assert app.load_settings() == {}


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
    assert toast_titles(window)[0].startswith("Cannot save: ")
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
    assert "Problem: element 2: empty code" in info


def test_render_failure_is_shown(window, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(app, "render", broken)
    render_now(window)
    assert window.rendered is None
    assert window.picture.get_paintable() is None
    assert window.size_label.get_text() == "Problem: kaputt"
    window._apply_zoom()  # nothing rendered: no crash, no size change


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
        ("in", 1.5, 0.1, 0.059, 2, 762.0),
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
    assert app.load_settings()["safe_mm"] == 0.0
    assert not window.dirty
    render_now(window)
    assert window.rendered.image.tobytes() == render(window.label, safe_mm=0).image.tobytes()
    assert window.rendered.image.tobytes() != render(window.label).image.tobytes()


def test_host_entry(window, gui_env):
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == ""
    entry.set_text("  192.0.2.5 ")
    entry.emit("apply")
    assert app.load_settings()["host"] == "192.0.2.5"
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
    assert win.printer_row.get_subtitle() == "25 mm cassette · 400 mm of 500 mm left"


@pytest.mark.parametrize("state", ["dirty", "path"])
def test_printer_found_keeps_changed_or_saved_label(window, tmp_path, state):
    if state == "dirty":
        window.change(margin_mm=1.0)
    else:
        window.path = tmp_path / "x.etiketto"
    window._printer_found(device.PrinterInfo("Wedge", 12, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer_row.get_subtitle() == "12 mm cassette"


def test_printer_without_cassette(window):
    window._printer_found(device.PrinterInfo("Wedge", None, None, None, "IDLE"))
    assert window.label.tape_mm == 50
    assert window.printer_row.get_subtitle() == "No cassette inserted"


def test_printer_status_in_inches(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window(settings={"units": "in"})
    assert win.printer_row.get_subtitle() == "1″ cassette · 1.3 ft of 1.6 ft left"


def test_printer_failed(make_window, gui_env):
    gui_env.configured = "p"
    gui_env.query_error = OSError("connection refused\ndetails")
    win = make_window()
    assert win.printer_info is None
    assert win.printer_row.get_title() == "Printer not reachable"
    assert win.printer_row.get_subtitle() == "p: connection refused"


def test_refresh_button(make_window, gui_env):
    gui_env.configured = "p"
    win = make_window()
    one(win.sidebar, Gtk.Button, tooltip="Refresh").emit("clicked")
    assert win.printer_row.get_title() == "Connecting…"
    assert pump(lambda: len(gui_env.queries) == 2 and win.printer_info is not None)


# --- discovery ---------------------------------------------------------------------


def found(*names):
    return [
        device.FoundPrinter("Brother VC-500W (%s)" % n, "VC-%s.local" % n, "192.0.2.%d" % i)
        for i, n in enumerate(names, 10)
    ]


def test_discover_none(window, gui_env):
    window.discover_printers(choose=True)
    assert window.printer_row.get_title() == "Searching for printers…"
    assert pump(lambda: window.printer_row.get_title() == "No printer found")
    assert window.printer_row.get_subtitle().startswith("Make sure the printer is switched on")
    assert "host" not in window.settings


def test_discover_one_is_used(window, gui_env):
    gui_env.found = found("A")
    one(window.sidebar, Gtk.Button, tooltip="Search for printers").emit("clicked")
    assert pump(lambda: window.printer_info is not None)
    assert app.load_settings()["host"] == "VC-A.local"
    assert gui_env.queries == ["VC-A.local"]
    assert toast_titles(window) == ["Found Brother VC-500W (A)"]
    entry = one(window.sidebar, Adw.EntryRow, title="Host name or IP address")
    assert entry.get_text() == "VC-A.local"


def test_discover_several_without_choice_takes_first(window, gui_env):
    gui_env.found = found("A", "B")
    window.discover_printers(choose=False)
    assert pump(lambda: window.printer_info is not None)
    assert window.settings["host"] == "VC-A.local"
    assert window.get_visible_dialog() is None


def test_discover_several_lets_the_user_choose(window, gui_env):
    gui_env.found = found("A", "B")
    window.discover_printers(choose=True)
    assert pump(lambda: window.get_visible_dialog() is not None)
    dialog = window.get_visible_dialog()
    assert dialog.get_heading() == "Choose a printer"
    rows = find(dialog.get_extra_child(), Adw.ActionRow)
    assert [r.get_subtitle() for r in rows] == [
        "VC-A.local · 192.0.2.10",
        "VC-B.local · 192.0.2.11",
    ]
    # no second search that would take the first printer behind the dialog
    pump(timeout=0.2)
    assert "host" not in window.settings and gui_env.queries == []
    assert window.printer_row.get_title() == "Choose a printer"
    rows[1].emit("activated")
    assert window.settings["host"] == "VC-B.local"
    assert pump(lambda: "VC-B.local" in gui_env.queries)
    assert window.get_visible_dialog() is None


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
    copies, cut, mode = (box.get_row_at_index(i) for i in range(3))
    copies.set_value(3)
    cut.set_selected(1)
    mode.set_selected(1)
    dialog.emit("response", "print")

    assert ready.printing
    assert not ready.print_button.get_sensitive()
    assert ready.print_button.get_label() == "Printing…"
    assert app.load_settings() == {"cut": "half", "mode": "normal"}

    assert pump(lambda: not ready.printing)
    assert len(gui_env.prints) == 1
    image, host, kwargs = gui_env.prints[0]
    assert host == "VC.local"
    assert {k: kwargs[k] for k in ("mode", "cut", "copies")} == {
        "mode": "normal",
        "cut": "half",
        "copies": 3,
    }
    # printer orientation: width = tape width
    assert image.size == (mm_to_px(25), ready.rendered.image.width)
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
    window._start_print(1, "full", "vivid")
    assert toast_titles(window) == ["No printer configured"]
    assert not window.printing and gui_env.prints == []


def test_print_error_offers_report(ready, gui_env, launched):
    gui_env.print_error = OSError("paper jam")
    ready._start_print(2, "full", "vivid")
    assert pump(lambda: not ready.printing)
    toast = ready.toast_log[-1]
    assert toast.get_title() == "Printing failed: paper jam"
    assert toast.get_button_label() == "Report"
    assert ready.print_button.get_label() == "Print"
    toast.emit("button-clicked")
    query = parse_qs(urlparse(launched[0]).query)
    assert query["title"] == ["Problem: Printing failed: paper jam"]
    assert "- Printer: VC-500W, 25 mm cassette, IDLE" in query["body"][0]


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
    window.printer_info = device.PrinterInfo("Wedge", None, None, None, "ERROR")
    window.report_problem("boom")
    assert "VC-500W, no cassette, ERROR" in parse_qs(urlparse(launched[1]).query)["body"][0]


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
    window.open_file(tmp_path / "missing.etiketto")
    titles = toast_titles(window)
    assert [t.split(":")[0] for t in titles] == [
        "Cannot open bad.etiketto",
        "Cannot open wide.etiketto",
        "Cannot open missing.etiketto",
    ]
    assert "unsupported tape width 7 mm" in titles[1]
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
    assert toast_titles(window)[-1].startswith("Cannot save: ")


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
    assert toast_titles(window)[-1].startswith("Cannot save: ")


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
    assert toast_titles(window) and window.label.tape_mm == 9


def test_new_label(window):
    window.change(tape_mm=9)
    window.activate_action("win.new", None)
    respond(window.get_visible_dialog(), "discard")
    assert window.label == Label(tape_mm=50, elements=[TextElement(text="Label")])
    window.printer_info = device.PrinterInfo("Wedge", 19, None, None, "IDLE")
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
