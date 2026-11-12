"""Symbols in the window: the chooser (search, categories, recently used),
the element's rows in the sidebar and the symbol drawn as a widget."""

from __future__ import annotations

import cairo
import pytest
from conftest import gtk_available, pump, render_now

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402
from test_app import one, walk  # noqa: E402

from etiketto import symbols  # noqa: E402
from etiketto.model import SymbolElement  # noqa: E402
from etiketto.ui import symbols as ui_symbols  # noqa: E402
from etiketto.ui import texts  # noqa: E402
from etiketto.ui.settings import Settings  # noqa: E402


def _chooser(win) -> ui_symbols.SymbolChooser:
    assert pump(lambda: isinstance(win.last_dialog, Adw.Dialog))
    dialog = win.last_dialog
    assert dialog.get_title() == "Symbols"
    return dialog.chooser


def _search(chooser, text):
    chooser.entry.set_text(text)
    chooser.entry.emit("search-changed")
    pump()


def _shown(chooser) -> list[str]:
    model = chooser.names
    return [model.get_string(n) for n in range(model.get_n_items())]


def _button(root, label):
    return next(w for w in walk(root) if isinstance(w, Gtk.Button) and w.get_label() == label)


def test_add_a_symbol_from_the_chooser(window):
    win = window
    win.activate_action("win.add-symbol", None)
    chooser = _chooser(win)
    assert chooser.category.get_selected() == ui_symbols.ALL  # nothing used yet
    assert len(_shown(chooser)) == len(symbols.all_symbols())
    _search(chooser, "printer")
    assert _shown(chooser)[0] == "printer"
    assert chooser.chosen().name == "printer" and chooser.use_button.get_sensitive()
    assert "printer" in chooser.details.get_text()
    _button(win.last_dialog, "Use symbol").emit("clicked")
    pump()
    element = win.label.elements[-1]
    assert isinstance(element, SymbolElement)
    assert (element.name, element.path) == ("printer", symbols.find("printer").path)
    assert win.dirty and win.expand_index == len(win.label.elements) - 1
    assert Settings.load().data["recent_symbols"] == ["printer"]
    render_now(win)
    assert win.rendered.warnings == []


def test_the_chooser_offers_the_recently_used(make_window):
    win = make_window({"recent_symbols": ["star", "gone-in-a-newer-set", 7, "printer"]})
    win.choose_symbol()
    chooser = _chooser(win)
    assert chooser.category.get_selected() == ui_symbols.RECENT
    assert _shown(chooser) == ["star", "printer"]  # what this version knows
    _search(chooser, "print")
    assert _shown(chooser) == ["printer"]
    chooser.category.set_selected(ui_symbols.ALL)
    pump()
    assert len(_shown(chooser)) > 1
    chooser.category.set_selected(chooser.categories.index("Printer") + 2)
    pump()
    assert all("Printer" in symbols.find(n).categories for n in _shown(chooser))
    _search(chooser, "no such thing at all")
    assert _shown(chooser) == [] and chooser.stack.get_visible_child_name() == "empty"
    assert not chooser.use_button.get_sensitive() and chooser.details.get_text() == ""
    chooser.use()  # nothing chosen: nothing happens
    _button(win.last_dialog, "Cancel").emit("clicked")
    pump()
    assert not any(isinstance(e, SymbolElement) for e in win.label.elements)


def test_activating_a_symbol_uses_it(window):
    win = window
    win.choose_symbol()
    chooser = _chooser(win)
    _search(chooser, "star")
    chooser.grid.emit("activate", 0)
    pump()
    assert win.label.elements[-1].name == "star"


def test_the_symbol_rows(make_window):
    win = make_window()
    win.add_element(SymbolElement(name="star", path=symbols.find("star").path))
    row = one(win.sidebar, Adw.ExpanderRow, title="\u2068star\u2069")
    icons = [w for w in walk(row) if isinstance(w, ui_symbols.SymbolIcon)]
    assert len(icons) == 2 and all(i.path == symbols.find("star").path for i in icons)
    symbol_row = one(row, Adw.ActionRow, title="Symbol")
    assert symbol_row.get_subtitle() == "star"

    _button(symbol_row, "Choose…").emit("clicked")
    chooser = _chooser(win)
    assert chooser.entry.get_text() == "star"  # starts at the current symbol
    assert chooser.chosen().name == "star"
    _search(chooser, "printer")
    chooser.use()
    pump()
    element = win.label.elements[-1]
    assert element.name == "printer" and symbol_row.get_subtitle() == "printer"
    assert all(i.path == symbols.find("printer").path for i in icons)

    size = one(row, Adw.SpinRow, title="Size")
    assert size.get_subtitle() == "mm, 0 = fill tape height"
    size.set_value(12)
    assert element.size_mm == 12
    rotation = one(row, Adw.ComboRow, title="Rotation")
    rotation.set_selected(3)
    assert element.rotation == 270
    color = one(size, Gtk.ColorDialogButton)
    assert color.get_tooltip_text() == "Black-and-white printers print symbols in black"


def test_the_symbol_widget_draws_in_the_text_colour():
    icon = ui_symbols.SymbolIcon(symbols.find("square").path, 24)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
    icon._draw(icon, cairo.Context(surface), 24, 24)
    assert bytes(surface.get_data())[(12 * 24 + 12) * 4 + 3] == 255  # inked in the middle
    for path in ("", "L1,1"):  # nothing chosen, damaged: nothing drawn
        icon.show(path)
        blank = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
        icon._draw(icon, cairo.Context(blank), 24, 24)
        assert not any(bytes(blank.get_data()))


def test_texts_for_symbols(window):
    assert texts.element_title(SymbolElement(name="star")) == "⁨star⁩"
    assert texts.element_title(SymbolElement()) == "Symbol"
    assert texts.element_icon(SymbolElement()) == "starred-symbolic"
    warning = texts.RenderWarning("symbol_unknown", {"n": 2})
    assert texts.warning_text(warning) == "Element 2: this symbol is not in the symbol library"
    window.label.elements.clear()
    window.rebuild_sidebar()
    empty = one(window.sidebar, Adw.ActionRow, title="No content yet")
    assert empty.get_subtitle() == "Add text, an image, a symbol or a code"
