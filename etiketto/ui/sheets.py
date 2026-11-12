"""Label sheets in the interface: the sheet drawn as a grid, the chooser
and the choice of positions on a sheet that is partly used already."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from etiketto.sheets import COMMON_PAPERS, Sheet, search  # noqa: E402
from etiketto.ui.i18n import _, isolate  # noqa: E402
from etiketto.ui.widgets import spin_row  # noqa: E402
from etiketto.units import Unit  # noqa: E402

if TYPE_CHECKING:
    from etiketto.ui.window import Window

MAX_RESULTS = 200  # rows in the chooser; more: refine the search
VIEW_PADDING = 8  # pixels around the page
# the page is paper: drawn white with dark lines in light and dark mode alike
INK = (0.2, 0.2, 0.2)


def paper_text(sheet: Sheet, unit: Unit) -> str:
    return sheet.paper_name or unit.size(*sheet.paper)


def sheet_summary(sheet: Sheet, unit: Unit) -> str:
    """ "63.5 × 38.1 mm · 21 per sheet · A4" """
    return _("%(size)s · %(count)d per sheet · %(paper)s") % {
        "size": unit.size(sheet.width, sheet.height),
        "count": sheet.count,
        "paper": paper_text(sheet, unit),
    }


def positions_text(positions: Iterable[int], count: int) -> str:
    """How the positions of the first sheet are chosen, in a few words."""
    chosen = sorted(p for p in set(positions) if 0 <= p < count)
    if len(chosen) == count:
        return _("All (%d)") % count
    if chosen and chosen == list(range(chosen[0], count)):
        return _("From position %(n)d of %(total)d") % {"n": chosen[0] + 1, "total": count}
    return _("%(n)d of %(total)d positions") % {"n": len(chosen), "total": count}


def numbers_text(positions: Iterable[int]) -> str:
    """Positions for people: "1, 2, 5" (counted from 1)."""
    return ", ".join(str(p + 1) for p in sorted(positions))


# --- the sheet as a picture --------------------------------------------------------


class SheetView(Gtk.DrawingArea):
    """A sheet drawn to scale: the page, its labels (chosen ones in the
    accent colour, numbered), the printer's unprintable margins shaded.
    With on_toggle, a click on a label calls on_toggle(position)."""

    def __init__(self, on_toggle: Callable[[int], None] | None = None):
        super().__init__(hexpand=True, vexpand=True)
        self.set_size_request(240, 320)
        self.sheet: Sheet | None = None
        self.chosen: set[int] = set()
        self.margins: tuple[float, float, float, float] | None = None
        self.on_toggle = on_toggle
        self.set_draw_func(self._draw)
        if on_toggle:
            click = Gtk.GestureClick()
            click.connect("released", self._on_click)
            self.add_controller(click)

    def show(self, sheet: Sheet | None, chosen: Iterable[int] = (), margins=None) -> None:
        self.sheet, self.chosen, self.margins = sheet, set(chosen), margins
        self.queue_draw()

    def transform(self, width: int, height: int) -> tuple[float, float, float]:
        """Pixels per mm and the page's top left corner in the widget."""
        assert self.sheet
        paper_w, paper_h = self.sheet.paper
        scale = max(
            0.01, min((width - 2 * VIEW_PADDING) / paper_w, (height - 2 * VIEW_PADDING) / paper_h)
        )
        return scale, (width - paper_w * scale) / 2, (height - paper_h * scale) / 2

    def position_at(self, x: float, y: float) -> int | None:
        if not self.sheet:
            return None
        scale, left, top = self.transform(self.get_width(), self.get_height())
        return self.sheet.position_at((x - left) / scale, (y - top) / scale)

    def _on_click(self, _gesture, _n, x: float, y: float) -> None:
        position = self.position_at(x, y)
        if position is not None and self.on_toggle:
            self.on_toggle(position)

    def _draw(self, _area, cr, width: int, height: int) -> None:
        sheet = self.sheet
        if not sheet:
            return
        scale, left, top = self.transform(width, height)
        cr.translate(left, top)
        cr.scale(scale, scale)
        paper_w, paper_h = sheet.paper
        cr.rectangle(0, 0, paper_w, paper_h)
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_source_rgba(*INK, 0.5)
        cr.set_line_width(1 / scale)
        cr.stroke()
        self._draw_margins(cr)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        for index in range(sheet.count):
            self._draw_label(cr, index, scale, accent)

    def _draw_margins(self, cr) -> None:
        assert self.sheet
        if not self.margins:
            return
        top, right, bottom, left = self.margins
        paper_w, paper_h = self.sheet.paper
        cr.rectangle(0, 0, paper_w, paper_h)
        cr.rectangle(left, top, paper_w - left - right, paper_h - top - bottom)
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)  # the band between the two
        cr.set_source_rgba(*INK, 0.15)
        cr.fill()
        cr.set_fill_rule(cairo.FillRule.WINDING)

    def _draw_label(self, cr, index: int, scale: float, accent) -> None:
        sheet = self.sheet
        assert sheet
        x0, y0, x1, y1 = sheet.rect(index)
        _label_path(cr, sheet, x0, y0, x1, y1)
        cr.set_line_width(1 / scale)
        if index in self.chosen:
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.45)
            cr.fill_preserve()
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        else:
            cr.set_source_rgba(*INK, 0.6)
        cr.stroke()
        size = min(x1 - x0, y1 - y0) * scale
        if size >= 14:  # room for a number
            text = str(index + 1)
            cr.set_font_size(min(12, size / 2) / scale)
            extents = cr.text_extents(text)
            cr.move_to(
                (x0 + x1) / 2 - extents.width / 2 - extents.x_bearing,
                (y0 + y1) / 2 - extents.height / 2 - extents.y_bearing,
            )
            cr.set_source_rgba(*INK, 0.9)
            cr.show_text(text)


def _label_path(cr, sheet: Sheet, x0: float, y0: float, x1: float, y1: float) -> None:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if sheet.shape == "rectangle":
        r = min(sheet.corner, (x1 - x0) / 2, (y1 - y0) / 2)
        cr.new_sub_path()
        cr.arc(x1 - r, y0 + r, r, -math.pi / 2, 0)
        cr.arc(x1 - r, y1 - r, r, 0, math.pi / 2)
        cr.arc(x0 + r, y1 - r, r, math.pi / 2, math.pi)
        cr.arc(x0 + r, y0 + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        return
    if sheet.shape == "cd":
        cr.new_sub_path()
        cr.arc(cx, cy, sheet.disc / 2, 0, 2 * math.pi)
        if sheet.hole:
            cr.new_sub_path()
            cr.arc_negative(cx, cy, sheet.hole / 2, 0, -2 * math.pi)
        return
    cr.save()
    cr.translate(cx, cy)
    cr.scale((x1 - x0) / 2, (y1 - y0) / 2)
    cr.new_sub_path()
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


# --- choosing a sheet ----------------------------------------------------------------


class SheetChooser:
    """A dialog to find a sheet by brand, part number, label size or paper;
    then(sheet) once one is chosen."""

    PAPERS = ("", *COMMON_PAPERS, "other")

    def __init__(self, window: Window, then: Callable[[Sheet], None], current: Sheet | None):
        self.window, self.then = window, then
        self.found: list[Sheet] = []
        self.chosen: Sheet | None = current
        self.dialog = Adw.Dialog(title=_("Label sheets"), content_width=760, content_height=560)
        self.use_button = Gtk.Button(label=_("Use sheet"), sensitive=current is not None)
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
        if current:
            self.entry.set_text(current.id)
        self.refresh()

    def _body(self) -> Gtk.Widget:
        self.entry = Gtk.SearchEntry(
            hexpand=True, placeholder_text=_("Brand, part number or size, e.g. 70x37")
        )
        self.entry.connect("search-changed", lambda _e: self.refresh())
        self.entry.connect("activate", lambda _e: self.use())
        names = [_("All paper sizes"), *COMMON_PAPERS, _("Other paper sizes")]
        self.paper = Gtk.DropDown.new_from_strings(names)
        self.paper.connect("notify::selected", lambda _d, _p: self.refresh())
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        bar.append(self.entry)
        bar.append(self.paper)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-selected", self._on_selected)
        self.list.connect("row-activated", lambda _l, _r: self.use())
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_size_request(380, -1)
        scroller.set_child(self.list)

        self.view = SheetView()
        self.details = Gtk.Label(wrap=True, xalign=0, selectable=True)
        self.details.add_css_class("dim-label")
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_end=12)
        side.append(self.view)
        side.append(self.details)

        panes = Gtk.Box(spacing=12, margin_bottom=12, margin_start=6)
        panes.append(scroller)
        panes.append(side)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(bar)
        box.append(panes)
        return box

    def present(self) -> None:
        self.window.present_dialog(self.dialog)

    def refresh(self) -> None:
        paper = self.PAPERS[self.paper.get_selected()]
        self.found = search(self.entry.get_text(), paper)
        chosen = self.chosen  # emptying the list unselects it
        self.list.remove_all()
        unit = self.window.unit
        for sheet in self.found[:MAX_RESULTS]:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(sheet.id),
                subtitle=GLib.markup_escape_text(sheet_summary(sheet, unit)),
                activatable=True,
            )
            self.list.append(row)
        if len(self.found) > MAX_RESULTS:
            more = _("%(shown)d of %(total)d sheets shown – refine the search") % {
                "shown": MAX_RESULTS,
                "total": len(self.found),
            }
            self.list.append(Adw.ActionRow(title=more, activatable=False, selectable=False))
        elif not self.found:
            self.list.append(
                Adw.ActionRow(title=_("No sheet found"), activatable=False, selectable=False)
            )
        # the sheet chosen so far stays chosen while the search finds it
        shown = self.found[:MAX_RESULTS]
        if chosen is not None and chosen in shown:
            self.list.select_row(self.list.get_row_at_index(shown.index(chosen)))
        elif chosen is not None and chosen in self.found:  # further down: not listed
            self._show(chosen)
        elif self.found:
            self.list.select_row(self.list.get_row_at_index(0))
        else:
            self._show(None)

    def _on_selected(self, _list, row) -> None:
        index = row.get_index() if row is not None else -1
        self._show(self.found[index] if 0 <= index < min(len(self.found), MAX_RESULTS) else None)

    def _show(self, sheet: Sheet | None) -> None:
        self.chosen = sheet
        self.use_button.set_sensitive(sheet is not None)
        self.view.show(sheet, range(sheet.count) if sheet else ())
        if sheet is None:
            self.details.set_text("")
            return
        lines = [sheet.id, sheet_summary(sheet, self.window.unit)]
        if sheet.description:
            lines.append(sheet.description)
        self.details.set_text("\n".join(lines))

    def use(self) -> None:
        if self.chosen is None:
            return
        self.dialog.close()
        self.then(self.chosen)


# --- positions on a partly used sheet --------------------------------------------------


class PositionsDialog:
    """Which positions of the first sheet to print on: click a label to
    leave it out or take it again, or start at a position; then(positions)."""

    def __init__(
        self,
        window: Window,
        sheet: Sheet,
        positions: Iterable[int],
        margins: tuple[float, float, float, float] | None,
        then: Callable[[list[int]], None],
    ):
        self.window, self.sheet, self.then = window, sheet, then
        self.chosen = {p for p in positions if 0 <= p < sheet.count}
        self.view = SheetView(self.toggle)
        self.view.set_size_request(280, 360)
        self.view.show(sheet, self.chosen, margins)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")

        self.start = spin_row(
            _("Start at position"), self._start(), 1, sheet.count, 1, 0, self._start_at
        )
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        rows.append(self.start)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        for title, positions in (
            (_("Select all"), range(sheet.count)),
            (_("Select none"), ()),
        ):
            button = Gtk.Button(label=title)
            button.connect("clicked", lambda _b, p=positions: self.choose(p))
            buttons.append(button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for widget in (self.view, self.status, rows, buttons):
            box.append(widget)
        self.dialog = Adw.AlertDialog(
            heading=_("Positions"),
            body=_(
                "Click a label to leave it out or to print on it again. Only the first "
                "sheet is affected: the sheets after it are filled completely."
            ),
        )
        self.dialog.set_extra_child(box)
        self.dialog.add_response("cancel", _("Cancel"))
        self.dialog.add_response("apply", _("Apply"))
        self.dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        self.dialog.set_default_response("apply")
        self.dialog.connect("response", self._on_response)
        self._update()

    def present(self) -> None:
        self.window.present_dialog(self.dialog)

    def _start(self) -> int:
        """The first position if the chosen ones run from it to the end, else 1."""
        chosen = sorted(self.chosen)
        if chosen and chosen == list(range(chosen[0], self.sheet.count)):
            return chosen[0] + 1
        return 1

    def toggle(self, position: int) -> None:
        self.chosen ^= {position}
        self._update()

    def _start_at(self, value: float) -> None:
        self.choose(range(int(value) - 1, self.sheet.count))

    def choose(self, positions: Iterable[int]) -> None:
        self.chosen = set(positions)
        self._update()

    def _update(self) -> None:
        self.view.show(self.sheet, self.chosen, self.view.margins)
        self.status.set_text(positions_text(self.chosen, self.sheet.count))
        self.dialog.set_response_enabled("apply", bool(self.chosen))

    def _on_response(self, _dialog, response: str) -> None:
        if response == "apply" and self.chosen:
            self.then(sorted(self.chosen))


def margins_note(positions: list[int]) -> str:
    # translators: %s is a list of position numbers on the sheet, e.g. "1, 2, 3"
    return _("Labels %s reach into the margin the printer cannot print on") % isolate(
        numbers_text(positions)
    )
