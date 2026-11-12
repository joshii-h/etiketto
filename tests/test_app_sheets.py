"""Label sheets in the window: choosing a sheet and the positions, the
preview, printing pages. The system printer is faked (query, print)."""

from __future__ import annotations

from types import SimpleNamespace

import cairo
import pytest
from conftest import gtk_available, pump, render_now, toast_titles

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402
from test_app import find, one, respond, walk  # noqa: E402

from etiketto.backends import get_backend  # noqa: E402
from etiketto.backends.base import Margins, PrinterInfo, PrintOptions, Progress  # noqa: E402
from etiketto.model import Label, SheetChoice, TextElement  # noqa: E402
from etiketto.sheets import find as find_sheet  # noqa: E402
from etiketto.sheets import search  # noqa: E402
from etiketto.ui import sheets as ui_sheets  # noqa: E402
from etiketto.ui.settings import Settings  # noqa: E402
from etiketto.units import MILLIMETRES  # noqa: E402

A4_MARGINS = Margins(6.35, 3.4, 6.35, 3.4, (210, 297))


@pytest.fixture
def system_env(gui_env, monkeypatch):
    """The system printer: an office printer with the margins of
    ippeveprinter for A4; pages and labels it was sent are recorded."""
    backend = get_backend("system")
    env = SimpleNamespace(
        info=PrinterInfo("Office", None, None, None, "IDLE", margins=(A4_MARGINS,)),
        pages=[],
        labels=[],
        queries=[],
    )

    def query(host):
        env.queries.append(host)
        return env.info

    def print_pages(pages, host, options, progress=None):
        env.pages.append(([page.copy() for page in pages], host, options))
        progress(Progress(1, len(pages)))

    def print_label(image, host, options, progress=None):
        env.labels.append((image, host, options))

    monkeypatch.setattr(backend, "query", query)
    monkeypatch.setattr(backend, "print_pages", print_pages)
    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    return env


@pytest.fixture
def office(make_window, system_env):
    """A window with the system printer, connected."""
    Settings({"printers": {"system": {"host": "HP_Office"}}}).save()
    win = make_window(settle=False)
    win.set_backend("system")
    assert pump(lambda: win.printer.info is system_env.info)
    win.dirty = False
    render_now(win)
    return win


def _button(root, label):
    return next(w for w in walk(root) if isinstance(w, Gtk.Button) and w.get_label() == label)


def _chooser(win) -> Adw.Dialog:
    dialog = win.last_dialog
    assert isinstance(dialog, Adw.Dialog) and dialog.get_title() == "Label sheets"
    return dialog


def _entry(dialog):
    return next(
        w
        for w in walk(dialog)
        if isinstance(w, Gtk.SearchEntry) and w.get_placeholder_text().startswith("Brand")
    )


def _search(dialog, text):
    entry = _entry(dialog)
    entry.set_text(text)
    entry.emit("search-changed")
    pump()


def _rows(dialog):
    listbox = next(w for w in walk(dialog) if isinstance(w, Gtk.ListBox))
    rows, index = [], 0
    while (row := listbox.get_row_at_index(index)) is not None:
        rows.append(row)
        index += 1
    return listbox, rows


def _use_sheet(win, sheet_id):
    """Print on a label sheet, chosen in the chooser."""
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(1)
    assert pump(lambda: isinstance(win.last_dialog, Adw.Dialog))
    dialog = _chooser(win)
    _search(dialog, sheet_id)
    _button(dialog, "Use sheet").emit("clicked")
    pump()


# --- choosing a sheet ------------------------------------------------------------------


def test_print_on_a_label_sheet(office):
    win = office
    combo = one(win.sidebar, Adw.ComboRow, title="Print on")
    assert combo.get_selected() == 0  # a single label, as before
    assert one(win.sidebar, Adw.ComboRow, title="Label size")
    _use_sheet(win, "Avery L7160")
    assert win.label.sheet == SheetChoice("Avery L7160", list(range(21)))
    assert win.label.media_size() == (63.994, 38.1)  # the label size follows the sheet
    assert win.dirty and win.sheet is find_sheet("Avery L7160")
    row = one(win.sidebar, Adw.ActionRow, title="Avery L7160")
    assert row.get_subtitle() == "64 × 38.1 mm · 21 per sheet · A4"
    assert one(win.sidebar, Adw.ActionRow, title="Positions").get_subtitle() == "All (21)"
    assert not find(win.sidebar, Adw.ComboRow, title="Label size")  # the sheet decides
    assert not find(win.sidebar, Adw.SpinRow, title="Width")
    render_now(win)
    assert win.preview.status.get_text().endswith("Avery L7160")


def test_the_chooser_finds_sheets(office):
    win = office
    win.choose_sheet()
    dialog = _chooser(win)
    use = _button(dialog, "Use sheet")
    _, rows = _rows(dialog)
    assert len(rows) == ui_sheets.MAX_RESULTS + 1  # and a note to refine the search
    assert rows[-1].get_title().startswith("200 of ")
    _search(dialog, "zweckform 70x37")
    listbox, rows = _rows(dialog)
    assert [r.get_title() for r in rows] == [s.id for s in search("zweckform 70x37")]
    assert listbox.get_selected_row() is rows[0] and use.get_sensitive()
    details = next(w for w in walk(dialog) if isinstance(w, Gtk.Label) and "\n" in w.get_text())
    assert details.get_text().startswith(rows[0].get_title())
    _search(dialog, "no such sheet")
    _, rows = _rows(dialog)
    assert [r.get_title() for r in rows] == ["No sheet found"] and not use.get_sensitive()
    _search(dialog, "")
    paper = one(dialog, Gtk.DropDown)
    paper.set_selected(2)  # US Letter
    _, rows = _rows(dialog)
    assert find_sheet(rows[0].get_title()).paper_name == "US Letter"
    paper.set_selected(3)  # other paper sizes
    listbox, rows = _rows(dialog)
    assert find_sheet(rows[0].get_title()).paper_name not in ("A4", "US Letter")
    listbox.emit("row-activated", rows[0])  # a double click uses it
    pump()
    assert win.label.sheet.id == rows[0].get_title()


def test_cancelling_the_chooser_keeps_a_single_label(office):
    win = office
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(1)
    assert pump(lambda: isinstance(win.last_dialog, Adw.Dialog))
    _button(_chooser(win), "Cancel").emit("clicked")
    pump()
    assert win.label.sheet is None and not win.dirty
    assert one(win.sidebar, Adw.ComboRow, title="Print on").get_selected() == 0


def test_back_to_a_single_label_and_to_the_sheet_again(office):
    win = office
    _use_sheet(win, "Zweckform 3475")
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(0)
    assert win.label.sheet is None
    assert win.label.media_size() == (70.051, 36.026)  # the size stays
    assert one(win.sidebar, Adw.ComboRow, title="Label size")
    win.last_dialog = None
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(1)
    pump()
    assert win.last_dialog is None  # no chooser: the last sheet again
    assert win.label.sheet.id == "Zweckform 3475"


def test_a_new_label_does_not_go_back_to_the_last_sheet(office):
    win = office
    _use_sheet(win, "Zweckform 3475")
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(0)
    win.dirty = False
    win.files.new()
    pump()
    win.last_dialog = None
    one(win.sidebar, Adw.ComboRow, title="Print on").set_selected(1)
    assert pump(lambda: win.last_dialog is not None)  # the chooser, not the old sheet
    assert win.label.sheet is None
    _button(_chooser(win), "Cancel").emit("clicked")
    pump()


def test_another_sheet_from_the_sheet_row(office):
    win = office
    _use_sheet(win, "Avery L7160")
    win.label.sheet.positions = [3, 4]
    one(win.sidebar, Adw.ActionRow, title="Avery L7160").emit("activated")
    dialog = _chooser(win)
    assert _entry(dialog).get_text() == "Avery L7160"  # the current one
    _button(dialog, "Use sheet").emit("clicked")
    pump()
    assert win.label.sheet.positions == [3, 4]  # the same sheet: positions stay
    win.choose_sheet()
    _search(_chooser(win), "Zweckform 3475")
    _button(_chooser(win), "Use sheet").emit("clicked")
    pump()
    assert win.label.sheet == SheetChoice("Zweckform 3475", list(range(24)))


def test_the_chooser_keeps_the_current_sheet_chosen(office):
    win = office
    win.choose_sheet()
    dialog = _chooser(win)
    _search(dialog, "Hisago GB861")  # lists "Hisago FSCGB861" first
    listbox, rows = _rows(dialog)
    listbox.select_row(rows[1])
    _button(dialog, "Use sheet").emit("clicked")
    pump()
    assert win.label.sheet.id == "Hisago GB861"
    win.choose_sheet()
    dialog = _chooser(win)
    listbox, rows = _rows(dialog)
    assert [r.get_title() for r in rows] == ["Hisago FSCGB861", "Hisago GB861"]
    assert listbox.get_selected_row() is rows[1]  # the current one, not the first
    _search(dialog, "hisago gb86")  # still in the list: still chosen
    listbox, rows = _rows(dialog)
    assert listbox.get_selected_row().get_title() == "Hisago GB861"
    _button(dialog, "Use sheet").emit("clicked")
    pump()
    assert win.label.sheet.id == "Hisago GB861"


def test_turning_the_content(office):
    win = office
    _use_sheet(win, "Zweckform 3475")
    win.dirty = False
    turned = one(win.sidebar, Adw.SwitchRow, title="Turn the content")
    turned.set_active(True)
    assert win.label.sheet.rotated and win.dirty
    assert win.label.media_size() == (36.026, 70.051)
    win.set_sheet_rotated(True)  # no change
    turned.set_active(False)
    assert win.label.media_size() == (70.051, 36.026)


# --- positions ---------------------------------------------------------------------


def test_choosing_positions_on_a_used_sheet(office):
    win = office
    _use_sheet(win, "Avery L7160")
    one(win.sidebar, Adw.ActionRow, title="Positions").emit("activated")
    dialog = win.last_dialog
    assert dialog.get_heading() == "Positions"
    status = next(
        w for w in walk(dialog) if isinstance(w, Gtk.Label) and w.get_text() == "All (21)"
    )
    start = one(dialog, Adw.SpinRow, title="Start at position")
    assert start.get_value() == 1
    start.set_value(5)
    assert status.get_text() == "From position 5 of 21"
    view = one(dialog, ui_sheets.SheetView)
    view.toggle = None
    view.on_toggle(20)  # a click on the last label: left out
    assert status.get_text() == "16 of 21 positions"
    _button(dialog, "Select none").emit("clicked")
    assert not dialog.get_response_enabled("apply")
    _button(dialog, "Select all").emit("clicked")
    view.on_toggle(0)
    view.on_toggle(1)
    respond(dialog, "apply")
    assert win.label.sheet.positions == list(range(2, 21)) and win.dirty
    row = one(win.sidebar, Adw.ActionRow, title="Positions")
    assert row.get_subtitle() == "From position 3 of 21"
    row.emit("activated")
    assert one(win.last_dialog, Adw.SpinRow, title="Start at position").get_value() == 3
    respond(win.last_dialog, "cancel")
    assert win.label.sheet.positions == list(range(2, 21))


def test_positions_texts():
    assert ui_sheets.positions_text(range(10), 10) == "All (10)"
    assert ui_sheets.positions_text([7, 8, 9], 10) == "From position 8 of 10"
    assert ui_sheets.positions_text([1, 5], 10) == "2 of 10 positions"
    assert ui_sheets.positions_text([], 10) == "0 of 10 positions"
    assert ui_sheets.numbers_text([4, 0, 2]) == "1, 3, 5"


def test_a_click_on_the_sheet_finds_the_label(office):
    clicked = []
    view = ui_sheets.SheetView(clicked.append)
    assert view.position_at(10, 10) is None  # nothing shown yet
    sheet = find_sheet("Avery L7160")
    view.show(sheet, [0])
    view.get_width = lambda: 226  # 1 px per mm, the page at 8, 8
    view.get_height = lambda: 313
    assert view.transform(226, 313) == (1.0, 8.0, 8.0)
    x, y = sheet.positions()[4]
    view._on_click(None, 1, 8 + x + 5, 8 + y + 5)
    view._on_click(None, 1, 1, 1)  # beside the page
    assert clicked == [4]


@pytest.mark.parametrize("shape", ["rectangle", "round", "ellipse", "cd"])
def test_the_sheet_is_drawn(office, shape):
    """Every shape, numbers, the chosen labels and the printer's margins."""
    sheet = next(s for s in search("") if s.shape == shape and s.hole >= 0)
    view = ui_sheets.SheetView()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 400)
    view._draw(None, cairo.Context(surface), 300, 400)  # nothing to draw yet
    view.show(sheet, [0], A4_MARGINS.sides())
    view._draw(None, cairo.Context(surface), 300, 400)
    surface.flush()
    data = bytes(surface.get_data())
    assert any(data[i] not in (0, 255) for i in range(0, len(data), 4))  # more than blank


# --- preview and margins -------------------------------------------------------------


def test_a_round_label_is_previewed_round(office):
    win = office
    round_sheet = next(s for s in search("", "A4") if s.shape == "round")
    win.label.background = "#000000"
    win.set_sheet(SheetChoice(round_sheet.id, [0]))
    _rendered, image = win.render_image()
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((image.width // 2, image.height // 2)) == (0, 0, 0)


def test_labels_in_the_printers_margin_are_shown(office, system_env):
    win = office
    _use_sheet(win, "Zweckform 3475")  # 3 × 8 from the very edge of the paper
    render_now(win)
    status = win.preview.status.get_text()
    assert "Labels \u20681, 2, 3, 4, 6, 7, 9," in status
    assert "reach into the margin the printer cannot print on" in status
    win.printer.info = None  # not connected: unknown
    assert win.positions_in_margins() == []
    win.printer.found(system_env.info)  # the answer shows them again
    assert pump(lambda: win._render_source == 0) and "Labels" in win.preview.status.get_text()


# --- printing -------------------------------------------------------------------


def test_printing_a_sheet(office, system_env):
    win = office
    _use_sheet(win, "Avery L7160")
    win.label.sheet.positions = list(range(4, 21))
    render_now(win)
    win.print_flow.on_print()
    dialog = win.last_dialog
    assert dialog.get_heading() == "Print label"
    assert dialog.get_body() == "64 × 38.1 mm · Avery L7160 · Sheets: 1"
    labels = one(dialog, Adw.SpinRow, title="Labels")
    assert labels.get_value() == 17  # the chosen positions
    labels.set_value(30)
    assert dialog.get_body().endswith("Sheets: 2")
    one(dialog, Adw.SwitchRow, title="Print outlines").set_active(True)
    respond(dialog, "print")
    assert pump(lambda: not win.print_flow.busy)
    pages, host, options = system_env.pages[0]
    assert host == "HP_Office" and options.copies == 30
    assert [p.size for p in pages] == [(2480, 3508)] * 2 and pages[0].info["size_mm"] == (210, 297)
    assert toast_titles(win)[-1] == "Printed"
    assert Settings.load().data["printers"]["system"]["outlines"] is True
    win.print_flow.on_print()
    assert one(win.last_dialog, Adw.SwitchRow, title="Print outlines").get_active()
    respond(win.last_dialog, "cancel")
    assert win.print_flow.open_dialog is None


@pytest.mark.parametrize("answer, prints", [("cancel", 0), ("print", 1)])
def test_printing_into_the_margin_asks_first(office, system_env, answer, prints):
    win = office
    _use_sheet(win, "Zweckform 3475")
    render_now(win)
    win.print_flow.on_print()
    respond(win.last_dialog, "print")
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Labels in the printer's margin"
    assert "labels \u20681, 2, 3, 4, 6, 7, 9," in dialog.get_body()
    respond(dialog, answer)
    assert pump(lambda: not win.print_flow.busy)
    assert len(system_env.pages) == prints


def test_a_single_label_still_prints_as_one_page(office, system_env):
    win = office
    win.print_flow.on_print()
    assert not find(win.last_dialog, Adw.SwitchRow, title="Print outlines")
    respond(win.last_dialog, "print")
    assert pump(lambda: not win.print_flow.busy)
    assert len(system_env.labels) == 1 and system_env.pages == []


def test_a_sheet_this_version_does_not_know(office, system_env):
    win = office
    label = Label(printer="system", orientation="across", elements=[TextElement(text="A")])
    label.sheet = SheetChoice("Future 1", [0])
    win.set_label(label, None)
    render_now(win)
    row = one(win.sidebar, Adw.ActionRow, title="Future 1")
    assert row.get_subtitle() == "Not in the list of sheets of this version"
    assert not find(win.sidebar, Adw.ActionRow, title="Positions")
    win.choose_positions()  # nothing to choose from
    win.print_flow.on_print()
    assert toast_titles(win)[-1] == (
        "This label sheet is unknown to this version – choose another one"
    )
    assert win.print_flow.sheet_job({"copies": 1}) is None


def test_sheet_summary_in_inches():
    sheet = find_sheet("Avery 5160")
    assert ui_sheets.sheet_summary(sheet, MILLIMETRES).endswith("30 per sheet · US Letter")
    other = next(s for s in search("", "other") if not s.paper_name)  # A5 has a name
    assert ui_sheets.paper_text(other, MILLIMETRES).endswith(" mm")


def test_print_options_of_a_sheet_reach_the_backend(office, system_env):
    win = office
    _use_sheet(win, "Avery L7160")
    win.print_flow.start(PrintOptions(copies=2), win.print_flow.sheet_job({"copies": 2}))
    assert pump(lambda: not win.print_flow.busy)
    pages, _host, options = system_env.pages[0]
    assert len(pages) == 1 and options.copies == 2


def test_odd_cases_of_the_dialogs(office):
    win = office
    win.label_group._on_print_on("page")  # already a single label: nothing happens
    assert not win.dirty
    win.choose_sheet()
    dialog = _chooser(win)
    _search(dialog, "no such sheet")
    _entry(dialog).emit("activate")  # Enter without a sheet: nothing to use
    assert win.label.sheet is None
    _search(dialog, "Avery L7160")
    _entry(dialog).emit("activate")
    pump()
    assert win.label.sheet.id == "Avery L7160"
    win.label.sheet.positions = [1, 5]  # not a run to the end: start at 1
    win.choose_positions()
    assert one(win.last_dialog, Adw.SpinRow, title="Start at position").get_value() == 1
    view = ui_sheets.SheetView()
    view.show(win.sheet, [], None)  # margins unknown: none drawn
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 140)
    view._draw(None, cairo.Context(surface), 100, 140)


# --- codes at the printer's resolution ------------------------------------------------------


def _code_window(make_window, system_env, dpi, length_mm=60.0):
    from dataclasses import replace

    from etiketto.model import CodeElement

    system_env.info = replace(system_env.info, dpi=dpi)
    Settings({"printers": {"system": {"host": "HP_Office"}}}).save()
    win = make_window(settle=False)
    win.set_backend("system")
    assert pump(lambda: win.printer.info is system_env.info)
    label = Label(printer="system", orientation="across", auto_length=False)
    label.elements = [CodeElement("ABC-123ABC-123", symbology="code128")]
    label.set_media_size(length_mm, 14)
    win.set_label(label, None)
    render_now(win)
    return win


def test_the_preview_is_rendered_at_the_printers_resolution(make_window, system_env):
    win = _code_window(make_window, system_env, 203)
    assert win.print_dpi() == 203
    assert win.rendered.image.width == round(60 / 25.4 * 203)
    assert "only one printer dot wide" in win.preview.status.get_text()  # not at 300 dpi


@pytest.mark.parametrize("answer, prints", [("cancel", 0), ("print", 1)])
def test_codes_that_may_not_scan_ask_first(make_window, system_env, answer, prints):
    win = _code_window(make_window, system_env, 203)
    win.print_flow.on_print()
    respond(win.last_dialog, "print")
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Codes may not scan"
    assert dialog.get_body() == (
        "Element 1: the code's modules are only one printer dot wide and may not scan – "
        "make the code larger"
    )
    respond(dialog, answer)
    assert pump(lambda: not win.print_flow.busy)
    assert len(system_env.labels) == prints


def test_a_code_that_does_not_fit_is_not_printed(make_window, system_env):
    win = _code_window(make_window, system_env, 203, length_mm=27)
    win.print_flow.on_print()
    respond(win.last_dialog, "print")
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Printing failed"
    assert dialog.get_body().startswith("A code does not fit on the label")
    assert system_env.labels == []


def test_a_code_the_printer_cannot_fit_says_so(office, system_env, monkeypatch):
    from etiketto.backends.base import CodeTooLarge

    def prepare(label, options, safe_mm):
        raise CodeTooLarge("element 1", n=1)

    monkeypatch.setattr(office.backend, "prepare", prepare)
    office.print_flow.on_print()
    respond(office.last_dialog, "print")
    assert pump(lambda: not office.print_flow.busy)
    dialog = office.get_visible_dialog()
    assert dialog.get_body().startswith("A code does not fit on the label")


def test_the_preview_is_never_finer_than_600_dpi(make_window, system_env):
    win = _code_window(make_window, system_env, 1200)
    assert win.print_dpi() == 600
    assert win.rendered.image.width == round(60 / 25.4 * 600)


def test_serial_labels_are_not_printed_on_sheets_yet(office, system_env, tmp_path):
    win = office
    label = Label(printer="system", orientation="across", elements=[TextElement(text="{Name}")])
    label.sheet = SheetChoice("Avery L7160", list(range(21)))
    win.set_label(label, None)
    rows = tmp_path / "rows.csv"
    rows.write_text("Name\nA\nB\n", encoding="utf-8")
    win.data.attach(rows)
    assert pump(lambda: win.data.state != "loading") and win.data.active
    render_now(win)
    win.print_flow.on_print()
    assert toast_titles(win)[-1] == "Serial labels cannot be printed on label sheets yet"
    assert not win.print_flow.busy and system_env.pages == [] and system_env.labels == []
