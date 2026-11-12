"""The symbol library in the interface: a symbol drawn as a widget and the
chooser that finds one by name, alias or category (in English, as the icon
set names them), with the recently used ones first at hand."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from etiketto import symbols  # noqa: E402
from etiketto.symbols import Symbol  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402

if TYPE_CHECKING:
    from etiketto.ui.window import Window

CELL_PX = 40  # a symbol in the chooser's grid
ALL, RECENT = 0, 1  # the first entries of the category list


class SymbolIcon(Gtk.DrawingArea):
    """A symbol in the text colour of the theme, so it follows light and dark mode."""

    def __init__(self, path: str = "", size: int = 16):
        super().__init__(content_width=size, content_height=size, valign=Gtk.Align.CENTER)
        self.path = path
        self.set_draw_func(self._draw)

    def show(self, path: str) -> None:
        self.path = path
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if not self.path:
            return
        size = min(width, height)
        cr.translate((width - size) / 2, (height - size) / 2)
        cr.scale(size / symbols.VIEW_BOX, size / symbols.VIEW_BOX)
        color = self.get_color()
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        try:
            symbols.trace(cr, symbols.segments(self.path))
            cr.fill()
        except Exception:  # noqa: BLE001 - a damaged file must not break the window
            cr.new_path()
            _placeholder(cr)


def _placeholder(cr) -> None:
    """A crossed-out square in the 24 × 24 box: a symbol that cannot be
    drawn (the preview warns about the element)."""
    cr.set_line_width(2)
    cr.rectangle(3, 3, 18, 18)
    cr.move_to(3, 3)
    cr.line_to(21, 21)
    cr.move_to(21, 3)
    cr.line_to(3, 21)
    cr.stroke()


class SymbolChooser:
    """A dialog to find a symbol; then(symbol) once one is chosen."""

    def __init__(self, window: Window, then: Callable[[Symbol], None], current: str = ""):
        self.window, self.then = window, then
        self.found: list[Symbol] = []
        self.dialog = Adw.Dialog(title=_("Symbols"), content_width=640, content_height=560)
        self.dialog.chooser = self  # lives as long as the dialog; the tests find it there
        self.use_button = Gtk.Button(label=_("Use symbol"), sensitive=False)
        self.use_button.add_css_class("suggested-action")
        self.use_button.connect("clicked", lambda _b: self.use())
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda _b: self.dialog.close())
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self.use_button)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self._body())
        self.dialog.set_child(view)
        self.dialog.set_default_widget(self.use_button)
        self.dialog.set_focus(self.entry)
        recent = self.recent()
        self.category.set_selected(RECENT if recent and not current else ALL)
        self.entry.set_text(current)
        self.refresh()

    def _body(self) -> Gtk.Widget:
        self.entry = Gtk.SearchEntry(
            hexpand=True, placeholder_text=_("Search in English, e.g. arrow or recycle")
        )
        self.entry.connect("search-changed", lambda _e: self.refresh())
        self.entry.connect("activate", lambda _e: self.use())
        self.categories = list(symbols.categories())
        names = [_("All symbols"), _("Recently used"), *self.categories]
        self.category = Gtk.DropDown.new_from_strings(names)
        self.category.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.category.set_enable_search(True)
        self.category.connect("notify::selected", lambda _d, _p: self.refresh())
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        bar.append(self.entry)
        bar.append(self.category)

        self.names = Gtk.StringList()
        self.selection = Gtk.SingleSelection(model=self.names, autoselect=True)
        self.selection.connect("notify::selected", lambda _s, _p: self._on_selected())
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_cell)
        factory.connect("bind", self._bind_cell)
        self.grid = Gtk.GridView(model=self.selection, factory=factory, max_columns=30)
        self.grid.connect("activate", lambda _g, _n: self.use())
        # typing while the grid has the focus goes to the search
        self.entry.set_key_capture_widget(self.grid)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.grid)
        self.empty = Adw.StatusPage(title=_("No symbol found"), icon_name="edit-find-symbolic")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(scroller, "grid")
        self.stack.add_named(self.empty, "empty")

        self.details = Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END, margin_start=12, margin_end=12
        )
        self.details.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=12)
        box.append(bar)
        box.append(self.stack)
        box.append(self.details)
        return box

    @staticmethod
    def _setup_cell(_factory, item) -> None:
        icon = SymbolIcon(size=CELL_PX)
        icon.set_margin_top(6)
        icon.set_margin_bottom(6)
        item.set_child(icon)

    @staticmethod
    def _bind_cell(_factory, item) -> None:
        name = item.get_item().get_string()
        found = symbols.find(name)
        icon = item.get_child()
        icon.show(found.path if found else "")
        icon.set_tooltip_text(name)
        item.set_accessible_label(name)  # what a screen reader says for the cell

    def present(self) -> None:
        self.window.present_dialog(self.dialog)

    def recent(self) -> list[Symbol]:
        names = self.window.settings.data.get("recent_symbols")
        found = (
            (symbols.find(n) for n in names if isinstance(n, str))
            if isinstance(names, list)
            else ()
        )
        return [s for s in found if s is not None]

    def refresh(self) -> None:
        selected = self.category.get_selected()
        query = self.entry.get_text()
        if selected == RECENT:
            recent = self.recent()
            found = recent
            if query.strip():  # the recent ones that match, best first
                wanted = {s.name for s in recent}
                found = [s for s in symbols.search(query) if s.name in wanted]
        else:
            category = self.categories[selected - 2] if selected > RECENT else None
            found = symbols.search(query, category)
        self.found = found
        self.names.splice(0, self.names.get_n_items(), [s.name for s in found])
        self.stack.set_visible_child_name("grid" if found else "empty")
        self._on_selected()

    def chosen(self) -> Symbol | None:
        index = self.selection.get_selected()
        return self.found[index] if 0 <= index < len(self.found) else None

    def _on_selected(self) -> None:
        symbol = self.chosen()
        self.use_button.set_sensitive(symbol is not None)
        if symbol is None:
            self.details.set_text("")
            return
        words = [symbol.name, *symbol.categories]
        self.details.set_markup(
            "<b>%s</b>" % GLib.markup_escape_text(words[0])
            + "".join(" · " + GLib.markup_escape_text(w) for w in words[1:])
        )

    def use(self) -> None:
        symbol = self.chosen()
        if symbol is None:
            return
        settings = self.window.settings
        settings.data["recent_symbols"] = symbols.remember(
            settings.data.get("recent_symbols"), symbol.name
        )
        settings.save()
        self.dialog.close()
        self.then(symbol)
