"""The main window: owns the document state, delegates to the controllers."""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from PIL import Image  # noqa: E402

from etiketto import report  # noqa: E402
from etiketto.backends import get_backend  # noqa: E402
from etiketto.backends.base import Backend, PrintOptions, margins_for  # noqa: E402
from etiketto.job import PREVIEW_MAX_DPI, preview_image  # noqa: E402
from etiketto.model import (  # noqa: E402
    MAX_COUNTER,
    CodeElement,
    Label,
    SheetChoice,
    SymbolElement,
    TextElement,
)
from etiketto.render import Rendered  # noqa: E402
from etiketto.serial import unknown_columns  # noqa: E402
from etiketto.sheets import Sheet, find  # noqa: E402
from etiketto.sheets.layout import first_positions, in_margins  # noqa: E402
from etiketto.shipping.source import clear_caches, drop_stale_documents  # noqa: E402
from etiketto.ui.data import DATA_SUFFIXES, STEPS, DataGroup, DataSource  # noqa: E402
from etiketto.ui.elements import ElementsGroup  # noqa: E402
from etiketto.ui.files import Files  # noqa: E402
from etiketto.ui.i18n import _, docs_url  # noqa: E402
from etiketto.ui.label_group import LabelGroup  # noqa: E402
from etiketto.ui.preview import Preview  # noqa: E402
from etiketto.ui.printer import Printer  # noqa: E402
from etiketto.ui.printing import PrintFlow  # noqa: E402
from etiketto.ui.settings import PrinterSettings, Settings  # noqa: E402
from etiketto.ui.sheets import PositionsDialog, SheetChooser, margins_note  # noqa: E402
from etiketto.ui.symbols import SymbolChooser  # noqa: E402
from etiketto.ui.texts import error_details, image_from_path, warning_text  # noqa: E402
from etiketto.ui.variables import CounterGroup  # noqa: E402
from etiketto.units import Unit, unit_for  # noqa: E402
from etiketto.variables import Values, expand_label  # noqa: E402


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Etiketto")
        self.set_default_size(1200, 760)
        self.settings = Settings.load()
        self.label = Label(elements=[TextElement(text=_("Label"))])
        self.path: Path | None = None
        self.dirty = False
        # the start label and "New label" take the width of the inserted tape
        self.adopts_tape = True
        self._geometries: dict[str, dict] = {}  # label size per printer model
        self._last_sheet: SheetChoice | None = None  # back to it after a single label
        self.rendered: Rendered | None = None
        # the resolution the preview was last rendered at (print_dpi()):
        # its code warnings hold for that one
        self.rendered_dpi: float | None = None
        # what the preview was rendered for: (resolution, Raster: tape,
        # print head, reductions), see render_geometry()
        self.rendered_geometry: tuple | None = None
        self.expand_index = -1
        self.last_dialog = None
        self._render_source = 0
        # each render request counts up; one done shows if it is newer than
        # what the preview shows and than _render_floor (a label, printer
        # model or window that is gone)
        self._render_generation = 0
        self._shown_generation = -1  # the generation the preview shows
        self._render_floor = 0
        self._closed = False
        self.preview_worker = PreviewWorker(self._show_render)
        self._render_error: str | None = None
        self._groups: list[Gtk.Widget] = []
        # random variables ({=uuid}, {=id}) keep their preview value while editing
        self._preview_random = os.urandom(64)
        # {=copies} in the preview: the number in the print dialog while it is open
        self.planned_copies = 1
        # counter_next as saved in self.path when it was opened or saved, and
        # the file's stat() then: a different value there means the command
        # line printed meanwhile (see sync_counter)
        self.saved_counter: int | None = None
        self._saved_stamp: tuple | None = None
        self.counter_edited = False  # the user changed the next number since

        # the controllers come first: the layout and the sidebar use them
        self.printer = Printer(self)
        self.print_flow = PrintFlow(self)
        self.files = Files(self)
        self.data = DataSource(self)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        self.banner = Adw.Banner(revealed=False)  # printer stage while printing
        self.banner.connect("button-clicked", lambda _b: self.print_flow.stop())
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self._build_body())
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self._install_actions()
        self._install_drop_target()
        self.connect("close-request", self._on_close_request)
        # a search or query still running must not touch a closed window's
        # settings (they would overwrite those of a newer window)
        self.connect("destroy", self._on_destroy)

        self.rebuild_sidebar()
        self.update_title()
        self.update_rows()
        self.schedule_render()
        if not GLib.getenv("ETIKETTO_SCREENSHOT"):
            self.printer.refresh()

    # --- layout --------------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Etiketto")
        header.set_title_widget(self.window_title)

        open_button = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Open"))
        open_button.set_action_name("win.open")
        header.pack_start(open_button)

        self.template_menu = Gio.Menu()
        templates = Gtk.MenuButton(
            icon_name="view-list-bullet-symbolic",
            tooltip_text=_("Templates"),
            menu_model=self.template_menu,
        )
        templates.connect("notify::active", self._on_templates_menu)
        header.pack_start(templates)

        save_button = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save"))
        save_button.set_action_name("win.save")
        header.pack_start(save_button)

        header.pack_end(
            Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._main_menu())
        )

        self.print_button = Gtk.Button(label=_("Print"))
        self.print_button.add_css_class("suggested-action")
        self.print_button.set_action_name("win.print")
        header.pack_end(self.print_button)
        return header

    @staticmethod
    def _main_menu() -> Gio.Menu:
        menu = Gio.Menu()
        menu.append(_("New label"), "win.new")
        menu.append(_("Shipping label…"), "win.shipping")
        menu.append(_("Save as…"), "win.save-as")
        menu.append(_("Save as template…"), "win.save-template")
        menu.append(_("Data for serial labels…"), "win.choose-data")
        menu.append(_("Forget allowed data files"), "win.forget-data")
        menu.append(_("Export PNG…"), "win.export")
        menu.append(_("Help"), "win.help")
        menu.append(_("Report a problem…"), "win.report")
        menu.append(_("About Etiketto"), "app.about")
        return menu

    def _build_body(self) -> Gtk.Widget:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(720)

        self.preview = Preview()
        paned.set_start_child(self.preview)

        side_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroller.set_size_request(420, -1)
        self.sidebar = Adw.PreferencesPage()
        side_scroller.set_child(self.sidebar)
        paned.set_end_child(side_scroller)
        return paned

    def _on_templates_menu(self, button, _pspec) -> None:
        if button.get_active():
            self.files.fill_templates()

    def rebuild_sidebar(self) -> None:
        for group in self._groups:
            self.sidebar.remove(group)
        self.label_group = LabelGroup(self)
        self.elements_group = ElementsGroup(self)
        self.counter_group = CounterGroup(self.label, self.mark_changed, self._counter_edited)
        self._groups = [
            self.printer.group(),
            self.label_group,
            self.elements_group,
            self.counter_group,
            DataGroup(self),
        ]
        for group in self._groups:
            self.sidebar.add(group)

    # --- printer backend and units -------------------------------------------------

    @property
    def backend(self) -> Backend:
        # Label.load() refuses printers this version does not know
        return get_backend(self.label.printer)

    @property
    def printer_settings(self) -> PrinterSettings:
        """Settings of the current printer model."""
        return self.settings.printer(self.backend)

    @property
    def unit(self) -> Unit:
        return unit_for(self.settings.units)

    def set_backend(self, backend_id: str) -> None:
        if backend_id == self.label.printer:
            return
        # switching back restores the size the label had on that model
        self._geometries[self.label.printer] = self.label.geometry()
        geometry = self._geometries.get(backend_id)
        self.label.switch_printer(backend_id, geometry)
        self.printer.info = None
        self._render_floor = self._render_generation  # not drawn for the other model
        self._structure_changed()
        self.printer.refresh()

    def set_units(self, value: str) -> None:
        self.settings.set_units(value)
        self.rebuild_sidebar()
        self.schedule_render()
        self.printer.update_row()

    def size_text(self, length_mm: float | None = None) -> str:
        """ "50 × 25 mm" for the status line and the print dialog."""
        if self.backend.capabilities.continuous:
            length = self.label.length_mm if length_mm is None else length_mm
            return self.unit.label_size(self.label.tape_mm, length)
        return self.unit.size(*self.label.media_size())

    # --- model changes -----------------------------------------------------------

    def change(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self.label, key, value)
        self.mark_changed()

    def mark_changed(self) -> None:
        self.dirty = True
        self.update_title()
        self.counter_group.update()
        self.schedule_render()

    def set_counter_next(self, value: int) -> None:
        """After printing: the counter of the next label, a change of the document."""
        self.label.counter_next = min(value, MAX_COUNTER)
        self.mark_changed()

    def _counter_edited(self) -> None:
        self.counter_edited = True

    def remember_saved(self) -> None:
        """The label in self.path is as it is here: opened or just saved."""
        self.saved_counter = self.label.counter_next if self.path else None
        self._saved_stamp = _stamp(self.path) if self.path else None
        self.counter_edited = False

    def sync_counter(self) -> None:
        """Before printing and saving: if the file changed since it was
        opened or saved and its counter moved on (`etiketto print` printed
        from it), go on from there; numbers already printed are not printed
        again. A next number typed in here since then is kept as it is. The
        file is only read if its stat() changed: no reading on every print.
        The window does not lock the file (see cli.py); printing from both
        at the same moment is not covered."""
        if self.path is None:
            return
        stamp = _stamp(self.path)
        if stamp is None or stamp == self._saved_stamp:
            return
        self._saved_stamp = stamp
        saved = Label.saved_counter(self.path)
        if saved is None or saved == self.saved_counter:
            return
        self.saved_counter = saved
        if saved > self.label.counter_next and not self.counter_edited:
            self.set_counter_next(saved)

    def _structure_changed(self) -> None:
        """A change the sidebar has to show with other rows."""
        self.rebuild_sidebar()
        self.mark_changed()

    def add_element(self, element) -> None:
        self.label.elements.append(element)
        self.expand_index = len(self.label.elements) - 1
        self._structure_changed()

    def choose_symbol(self) -> None:
        """Pick a symbol from the library and add it."""
        SymbolChooser(
            self,
            lambda symbol: self.add_element(SymbolElement(name=symbol.name, path=symbol.path)),
        ).present()

    def move_element(self, index: int, target: int) -> None:
        items = self.label.elements
        items[index], items[target] = items[target], items[index]
        self.expand_index = target
        self._structure_changed()

    def remove_element(self, index: int) -> None:
        del self.label.elements[index]
        self.expand_index = -1
        self._structure_changed()

    def set_background_image(self, image) -> None:
        self.label.background_image = image
        self._structure_changed()

    def set_label(self, label: Label, path: Path | None, adopts_tape: bool = False) -> None:
        printer_changed = label.printer != self.label.printer
        self.label = label
        self._last_sheet = None  # another label (new, opened): not its sheet
        self._render_floor = self._render_generation  # renders of the label before
        clear_caches()  # decoded documents of the label before, also the
        self.preview_worker.drop_documents()  # preview thread's own
        self.adopts_tape = adopts_tape
        if printer_changed:  # a file or template for the other printer model
            self.printer.info = None
        self.path = path
        self.remember_saved()
        self.dirty = False
        self._geometries = {}
        self.expand_index = -1
        self.rebuild_sidebar()
        self.update_title()
        self.data.follow_label()
        self.schedule_render()
        if printer_changed:
            self.printer.refresh()

    # --- label sheets ----------------------------------------------------------------

    @property
    def sheet(self) -> Sheet | None:
        """The label sheet the label is printed on, if any (and known)."""
        choice = self.label.sheet
        if not choice or not self.backend.capabilities.pages:
            return None
        return find(choice.id)

    def set_sheet(self, choice: SheetChoice | None) -> None:
        """Print on this sheet (the label takes its label size), or None:
        a single label again, in the size it had on the sheet."""
        if self.label.sheet:
            self._last_sheet = self.label.sheet
        self.label.sheet = choice
        self.label.use_sheet_size()
        self._structure_changed()

    def choose_sheet(self) -> None:
        """The sheet chooser; back to the last sheet without asking when
        "Label sheet" is chosen again."""
        current = self.label.sheet
        if current is None and self._last_sheet and find(self._last_sheet.id):
            self.set_sheet(self._last_sheet)
            return
        chosen = []

        def then(sheet: Sheet) -> None:
            chosen.append(sheet)
            rotated = bool(current and current.rotated)
            same = current is not None and current.id == sheet.id
            positions = current.positions if same and current else list(range(sheet.count))
            self.set_sheet(SheetChoice(sheet.id, positions, rotated))

        def closed(_dialog) -> None:
            if not chosen and not self.label.sheet:
                self.rebuild_sidebar()  # "Print on" back to a single label

        chooser = SheetChooser(self, then, find(current.id) if current else None)
        chooser.dialog.connect("closed", closed)
        chooser.present()

    def choose_positions(self) -> None:
        sheet, choice = self.sheet, self.label.sheet
        if not sheet or not choice:
            return

        def then(positions: list[int]) -> None:
            choice.positions = positions
            self._structure_changed()

        margins = self.sheet_margins()
        dialog = PositionsDialog(
            self, sheet, choice.positions, margins.sides() if margins else None, then
        )
        dialog.present()

    def set_sheet_rotated(self, rotated: bool) -> None:
        if self.label.sheet and self.label.sheet.rotated != rotated:
            self.label.sheet.rotated = rotated
            self.label.use_sheet_size()
            self.mark_changed()

    def sheet_margins(self):
        """The printer's unprintable margins for the sheet's paper, if known."""
        sheet, info = self.sheet, self.printer.info
        if not sheet or not info:
            return None
        return margins_for(info.margins, sheet.paper)

    def positions_in_margins(self, positions=None) -> list[int]:
        """Of the positions (by default the chosen ones on the first sheet),
        those whose content the printer's margins would cut off."""
        sheet, margins, choice = self.sheet, self.sheet_margins(), self.label.sheet
        if not sheet or not margins or not choice:
            return []
        if positions is None:
            positions = first_positions(sheet, choice.positions)
        return in_margins(sheet, positions, margins.sides(), self.printer_settings.safe_mm)

    def data_changed(self, rebuild: bool = True) -> None:
        """The data file was read, failed or removed, or another row is shown."""
        if rebuild:  # the group shows the file, the elements the columns to insert
            self.rebuild_sidebar()
        self.update_rows()
        self.schedule_render()

    def update_rows(self) -> None:
        """The row buttons under the preview: only with rows to step through."""
        data = self.data
        cursor = data.cursor
        if data.active:
            self.preview.show_row(cursor.number, cursor.count)
        else:
            self.preview.hide_row()
        for step in STEPS:
            action = self.lookup_action("row-" + step)
            if action:
                back = step in ("first", "previous")
                action.set_enabled(
                    data.active and not (cursor.at_start if back else cursor.at_end)
                )

    def update_title(self) -> None:
        name = self.path.name if self.path else _("Unsaved label")
        self.window_title.set_subtitle(("• " if self.dirty else "") + name)

    # --- preview -------------------------------------------------------------------

    def render_options(self) -> PrintOptions:
        """The options the next print would have, as far as the render
        depends on them (mode, quality, high resolution, the printer's own
        settings and what it reported)."""
        return self.printer_settings.options(self.printer.info, host=self.printer.host or "")

    def print_dpi(self) -> float:
        """The resolution the label is printed at (Backend.render_dpi): the
        model's, what the printer settings choose (a ZPL printer at 300
        dpi, a QL at 600 dpi in high quality, a P-touch at high resolution)
        or what a page printer reported; for the preview and the export at
        most PREVIEW_MAX_DPI (an A4 page at 1200 dpi would be 140
        megapixels, rendered on every keystroke), a page only as fine as
        it fits (Backend.label_dpi)."""
        return self.backend.label_dpi(self.label, self.render_options(), PREVIEW_MAX_DPI)

    def render_image(self) -> tuple[Rendered, Image.Image]:
        """The label rendered for the current printer model and safety margin,
        at the resolution it is printed at (codes and thin lines differ),
        and the image of it as the printer would print it (preview, export)."""
        return self._render_work()()

    def _render_work(self) -> Callable[[], tuple[Rendered, Image.Image]]:
        """What a render needs, copied from the window now (the label filled
        in, the options, the sheet, what the backend's capabilities say),
        and a function that renders it from those copies alone: it runs in
        the preview's thread while the label is edited further."""
        backend = self.backend
        label, warnings = expand_label(self.label, self.preview_values(), self.data.row)
        label = copy.deepcopy(label)  # the window's elements change while it renders
        options = copy.deepcopy(self.render_options())
        self.rendered_geometry = self.render_geometry()
        self.rendered_dpi = backend.label_dpi(label, options, PREVIEW_MAX_DPI)
        safe_mm, choice = self.printer_settings.safe_mm, self.label.sheet
        sheet = self.sheet if choice else None  # round labels and CDs: their shape
        rotated = bool(choice and choice.rotated)

        def work() -> tuple[Rendered, Image.Image]:
            rendered, image, _dpi = preview_image(backend, label, options, safe_mm, sheet, rotated)
            rendered.warnings[:0] = warnings
            return rendered, image

        return work

    def render_geometry(self) -> tuple:
        """What a render for the next print depends on besides the label:
        the resolution and the raster (the tape or print area in pixels,
        the head's reductions), from the printer as last asked."""
        options = self.render_options()
        dpi = self.backend.label_dpi(self.label, options, PREVIEW_MAX_DPI)
        return dpi, self.backend.label_raster(self.label, options)

    def render_for(self, options: PrintOptions) -> Rendered:
        """The label (the next print's values) rendered for these options:
        the print dialog's choices of quality or high resolution change the
        resolution, so its code check renders anew."""
        label, warnings = expand_label(self.label, self.preview_values(), self.data.row)
        rendered = self.backend.render_label(
            label, options, self.printer_settings.safe_mm, PREVIEW_MAX_DPI, bleed=False
        )
        rendered.warnings[:0] = warnings
        return rendered

    def preview_values(self) -> Values:
        """The variables as the next print fills them in: now, the next counter."""
        return Values.for_label(
            self.label,
            datetime.now(),
            self.planned_copies,
            random=lambda n: self._preview_random[:n],
        )

    def schedule_render(self) -> None:
        """A render soon (edits come in bursts); a render still running for
        the label before this edit is shown when it is done (the preview
        stays live while typing goes on), the one for the edit follows."""
        if self._closed:
            return
        self._render_generation += 1
        if self._render_source:
            GLib.source_remove(self._render_source)
        self._render_source = GLib.timeout_add(120, self.render_preview)

    @property
    def preview_current(self) -> bool:
        """The preview (and `rendered`) shows the label as it is now."""
        return self.rendered is not None and self._shown_generation == self._render_generation

    def render_if_stale(self) -> None:
        """The preview rendered here and now if an edit or a render is still
        pending (the print dialog shows the length of what it prints)."""
        if not self.preview_current:
            if self._render_source:
                GLib.source_remove(self._render_source)
            self.render_preview(wait=True)

    def render_preview(self, wait: bool = False) -> bool:
        """Render the preview in the preview's thread (a large label at 600
        dpi takes a while; the window stays responsive), shown when it is
        done unless a newer one is shown already; `wait`: here and now."""
        self._render_source = 0
        if self._closed:
            return False
        self._render_generation += 1
        generation = self._render_generation
        try:
            work = self._render_work()
        except Exception as e:  # noqa: BLE001 - keep the app usable, offer a report
            self._render_failed(e)
            return False
        if wait:
            self._show_render(generation, *_run(work))
        else:
            self.preview_worker.submit(generation, work)
        return False

    def _show_render(self, generation: int, result, error: BaseException | None) -> bool:
        """A render done: shown if it is newer than what the preview shows.
        One overtaken by an edit is shown too, so that the preview follows
        continuous typing on a slow label; its failure is not (the newer
        render that follows shows it, if it still fails)."""
        if self._closed or generation <= max(self._shown_generation, self._render_floor):
            return False
        if error is not None and generation != self._render_generation:
            return False
        self._shown_generation = generation
        if error is not None:
            self._render_failed(error)
            return False
        self.rendered, image = result
        self._render_error = None
        self.preview.show_image(
            image, self.rendered.length_mm, self.label.tape_mm, self._status_text(self.rendered)
        )
        return False

    def _status_text(self, rendered: Rendered) -> str:
        parts = [self.size_text(rendered.length_mm)]
        if rendered.overflow:
            parts.append(_("content scaled down to fit"))
        parts.extend(self._limit_texts(rendered))
        if rendered.warnings:
            problems = "; ".join(warning_text(w) for w in rendered.warnings)
            parts.append(_("Problem: %s") % problems)
        if unknown := unknown_columns(self.label, self.data.columns) if self.data.active else []:
            names = ", ".join("{%s}" % name for name in unknown)
            parts.append(_("No such column: %s") % names)
        return " · ".join(parts) + self._sheet_status()

    def _sheet_status(self) -> str:
        """ " · Avery L7160", and the labels the printer's margins cut off."""
        if not self.label.sheet:
            return ""
        cut = self.positions_in_margins()
        return " · " + self.label.sheet.id + (" · " + margins_note(cut) if cut else "")

    def _limit_texts(self, rendered: Rendered) -> list[str]:
        """What exceeds the printer's limits."""
        caps = self.backend.capabilities
        texts = []
        if caps.max_length_mm and caps.too_long(rendered.length_mm):
            limit = self.unit.format(caps.max_length_mm)
            texts.append(_("Too long for this printer: at most %s") % limit)
        width = self.label.media_size()[0]
        if not caps.continuous and caps.max_width_mm and caps.too_wide(width):
            limit = self.unit.format(caps.max_width_mm)
            texts.append(_("Too wide for this printer: at most %s") % limit)
        return texts

    def _render_failed(self, error: BaseException) -> None:
        self.rendered = None
        message = _("The preview cannot be drawn")
        self.preview.clear(message)
        details = error_details(error)
        if details != self._render_error:  # once, not on every keystroke
            self._render_error = details
            self.error(message, details)

    # --- dialogs, toasts, links ----------------------------------------------------

    def present_dialog(self, dialog) -> None:
        self.last_dialog = dialog
        dialog.present(self)

    def confirm(self, heading: str, body: str, confirm_id: str, label: str, then) -> None:
        """Ask before something that cannot be undone; then() on confirmation."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response(confirm_id, label)
        dialog.set_response_appearance(confirm_id, Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: response == confirm_id and then())
        self.present_dialog(dialog)

    def _add_toast(self, text: str, timeout: int, button: str = "", on_click=None) -> None:
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if button:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda _t: on_click())
        self.toasts.add_toast(toast)

    def toast(self, text: str) -> None:
        self._add_toast(text, 4)

    def offer(self, text: str, button: str, then) -> None:
        """A toast with an action the user may take."""
        self._add_toast(text, 8, button, then)

    def error(self, text: str, details: str | None = None) -> None:
        """A toast for problems, with a button to report them; the details
        (original error text) only go into the report."""
        problem = text + ("\n\n" + details if details else "")
        self._add_toast(text, 8, _("Report"), lambda: self.report_problem(problem))

    def report_problem(self, problem: str | None = None) -> None:
        toolkit = "GTK %d.%d, libadwaita %d.%d" % (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Adw.get_major_version(),
            Adw.get_minor_version(),
        )
        self.open_url(report.issue_url(problem, self.printer.description(), toolkit))

    def open_url(self, url: str) -> None:
        """Open a web page; if no browser can be started, offer the link."""

        def launched(launcher, result):
            try:
                ok = launcher.launch_finish(result)
            except GLib.Error:
                ok = False
            if not ok:
                self._offer_link(url)

        Gtk.UriLauncher.new(url).launch(self, None, launched)

    def _offer_link(self, url: str) -> None:
        """No browser could be opened: let the user copy the address."""
        self._add_toast(
            _("The browser cannot be opened"),
            0,
            _("Copy link"),
            lambda: self.get_clipboard().set(url),
        )

    # --- actions, drag and drop, closing --------------------------------------------

    def _install_actions(self) -> None:
        actions = {
            "open": self.files.open,
            "save": self.files.save,
            "save-as": self.files.save_as,
            "save-template": self.files.save_template,
            "export": self.files.export,
            "report": lambda *_a: self.report_problem(),
            "help": lambda *_a: self.open_url(docs_url()),
            "print": self.print_flow.on_print,
            "new": self.files.new,
            "shipping": self.files.open_shipping,
            "add-text": lambda *_a: self.add_element(TextElement(text=_("Text"))),
            "add-image": lambda *_a: self.files.choose_image(self.add_element),
            "add-code": lambda *_a: self.add_element(CodeElement()),
            "add-symbol": lambda *_a: self.choose_symbol(),
            "choose-data": self.data.choose,
            "forget-data": self.data.forget_allowed,
        }
        for step in STEPS:
            actions["row-" + step] = lambda *_a, s=step: self.data.step(s)
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.data.update_forget_action()
        template = Gio.SimpleAction.new("open-template", GLib.VariantType.new("s"))
        template.connect("activate", self.files.open_template)
        self.add_action(template)

        app = self.get_application()
        app.set_accels_for_action("win.open", ["<Control>o"])
        app.set_accels_for_action("win.save", ["<Control>s"])
        app.set_accels_for_action("win.save-as", ["<Control><Shift>s"])
        app.set_accels_for_action("win.print", ["<Control>p"])
        app.set_accels_for_action("win.help", ["F1"])
        app.set_accels_for_action("win.new", ["<Control>n"])
        app.set_accels_for_action("win.row-first", ["<Alt>Home"])
        app.set_accels_for_action("win.row-previous", ["<Alt>Page_Up"])
        app.set_accels_for_action("win.row-next", ["<Alt>Page_Down"])
        app.set_accels_for_action("win.row-last", ["<Alt>End"])

    def _install_drop_target(self) -> None:
        target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _on_drop(self, _target, file, _x, _y) -> bool:
        if not file.get_path():
            self.toast(_("Only local files can be dropped"))
            return False
        path = Path(file.get_path())
        if path.suffix == ".etiketto":
            self.files.confirm_discard(lambda: self.files.open_file(path))
            return True
        if path.suffix.lower() == ".pdf":  # a carrier's label
            self.files.confirm_discard(lambda: self.files.shipping_from(path))
            return True
        if path.suffix.lower() in DATA_SUFFIXES:
            self.data.attach(path)
            return True
        try:
            self.add_element(image_from_path(path))
        except ValueError as e:
            self.toast(str(e))
            return False
        return True

    def _on_destroy(self, _window) -> None:
        self.printer._next_generation()
        self._closed = True  # a render still running is not shown, none starts
        self._render_generation += 1
        if self._render_source:
            GLib.source_remove(self._render_source)
            self._render_source = 0
        self.preview_worker.stop(timeout=0)  # it ends after the render it is in

    def _on_close_request(self, _window) -> bool:
        if self.print_flow.busy:
            self.toast(_("Please wait until printing has finished"))
            return True
        if not self.dirty:
            self.preview_worker.stop(timeout=0)  # closes: it ends after its render
            return False

        def close():
            self.dirty = False
            self.close()

        self.files.confirm_discard(close)
        return True


def _stamp(path: Path) -> tuple | None:
    """What changes when the file is written: replaced (inode), size, time."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _run(work: Callable[[], tuple[Rendered, Image.Image]]) -> tuple:
    """(result, None), or (None, the exception): shown in the main loop."""
    try:
        return work(), None
    except Exception as e:  # noqa: BLE001 - shown in the main loop
        return None, e


class PreviewWorker:
    """One long-lived thread for a window's preview renders. It keeps what
    a thread builds once, Pango's font map and Poppler's documents (see
    shipping/source.py), from render to render, and takes only the latest
    request: one overtaken while another renders is dropped unstarted.
    done(generation, result, error) is called in the main loop. A request
    after stop(), or after the thread ended on a BaseException, starts a
    new thread (the one stopping ends after its render)."""

    def __init__(self, done: Callable[..., bool]):
        self._done = done
        self._wake = threading.Condition()
        self._pending: tuple[int, Callable] | None = None
        self._stop = threading.Event()  # the current thread's
        self._drop = False  # drop_documents() asked: wake up for it
        self.thread: threading.Thread | None = None

    def submit(self, generation: int, work: Callable) -> None:
        with self._wake:
            self._pending = (generation, work)  # the older request, if any, is dropped
            thread = self.thread
            if thread is None or not thread.is_alive() or self._stop.is_set():
                self._stop = threading.Event()
                self.thread = threading.Thread(
                    target=self._loop, args=(self._stop,), name="etiketto-preview", daemon=True
                )
                self.thread.start()
            self._wake.notify_all()

    def drop_documents(self) -> None:
        """The thread forgets the documents it decoded before clear_caches()
        now, not only at its next render (shipping/source.py)."""
        with self._wake:
            self._drop = True
            self._wake.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        """End the thread (after the render it is in, if any)."""
        with self._wake:
            self._stop.set()
            self._pending = None
            self._wake.notify_all()
        if self.thread is not None:
            self.thread.join(timeout)

    def _loop(self, stop: threading.Event) -> None:
        while True:
            with self._wake:
                while self._pending is None and not stop.is_set() and not self._drop:
                    self._wake.wait()
                if stop.is_set():
                    return
                self._drop = False
                request, self._pending = self._pending, None
            drop_stale_documents()  # of the label before (clear_caches)
            if request is None:
                continue
            generation, work = request
            try:
                outcome = _run(work)
            except BaseException as e:  # noqa: BLE001 - SystemExit, …: the thread ends
                GLib.idle_add(self._done, generation, None, e)
                return
            GLib.idle_add(self._done, generation, *outcome)
