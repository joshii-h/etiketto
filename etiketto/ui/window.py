"""The main window: owns the document state, delegates to the controllers."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from PIL import Image  # noqa: E402

from etiketto import report  # noqa: E402
from etiketto.backends import get_backend  # noqa: E402
from etiketto.backends.base import Backend  # noqa: E402
from etiketto.model import CodeElement, Label, TextElement  # noqa: E402
from etiketto.render import Rendered, render  # noqa: E402
from etiketto.ui.elements import ElementsGroup  # noqa: E402
from etiketto.ui.files import Files  # noqa: E402
from etiketto.ui.i18n import _, docs_url  # noqa: E402
from etiketto.ui.label_group import LabelGroup  # noqa: E402
from etiketto.ui.preview import Preview  # noqa: E402
from etiketto.ui.printer import Printer  # noqa: E402
from etiketto.ui.printing import PrintFlow  # noqa: E402
from etiketto.ui.settings import PrinterSettings, Settings  # noqa: E402
from etiketto.ui.texts import error_details, image_from_path, warning_text  # noqa: E402
from etiketto.units import Unit, unit_for  # noqa: E402


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
        self.rendered: Rendered | None = None
        self.expand_index = -1
        self.last_dialog = None
        self._render_source = 0
        self._render_error: str | None = None
        self._groups: list[Gtk.Widget] = []

        # the controllers come first: the layout and the sidebar use them
        self.printer = Printer(self)
        self.print_flow = PrintFlow(self)
        self.files = Files(self)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        self.banner = Adw.Banner(revealed=False)  # printer stage while printing
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self._build_body())
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self._install_actions()
        self._install_drop_target()
        self.connect("close-request", self._on_close_request)

        self.rebuild_sidebar()
        self.update_title()
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
        menu.append(_("Save as…"), "win.save-as")
        menu.append(_("Save as template…"), "win.save-template")
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
        self._groups = [self.printer.group(), self.label_group, self.elements_group]
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
        self.schedule_render()

    def _structure_changed(self) -> None:
        """A change the sidebar has to show with other rows."""
        self.rebuild_sidebar()
        self.mark_changed()

    def add_element(self, element) -> None:
        self.label.elements.append(element)
        self.expand_index = len(self.label.elements) - 1
        self._structure_changed()

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
        self.adopts_tape = adopts_tape
        if printer_changed:  # a file or template for the other printer model
            self.printer.info = None
        self.path = path
        self.dirty = False
        self._geometries = {}
        self.expand_index = -1
        self.rebuild_sidebar()
        self.update_title()
        self.schedule_render()
        if printer_changed:
            self.printer.refresh()

    def update_title(self) -> None:
        name = self.path.name if self.path else _("Unsaved label")
        self.window_title.set_subtitle(("• " if self.dirty else "") + name)

    # --- preview -------------------------------------------------------------------

    def render_image(self) -> tuple[Rendered, Image.Image]:
        """The label rendered for the current printer model and safety margin,
        and the image of it as the printer would print it (preview, export)."""
        backend = self.backend
        rendered = render(
            self.label,
            dpi=backend.capabilities.dpi,
            safe_mm=self.printer_settings.safe_mm,
            min_length_mm=backend.capabilities.min_length_mm,
        )
        return rendered, backend.preview(rendered.image, self.print_flow.mode)

    def schedule_render(self) -> None:
        if self._render_source:
            GLib.source_remove(self._render_source)
        self._render_source = GLib.timeout_add(120, self.render_preview)

    def render_preview(self) -> bool:
        self._render_source = 0
        try:
            self.rendered, image = self.render_image()
        except Exception as e:  # noqa: BLE001 - keep the app usable, offer a report
            self._render_failed(e)
            return False
        self._render_error = None
        self.preview.show_image(
            image, self.rendered.length_mm, self.label.tape_mm, self._status_text(self.rendered)
        )
        return False

    def _status_text(self, rendered: Rendered) -> str:
        caps = self.backend.capabilities
        info = self.size_text(rendered.length_mm)
        if rendered.overflow:
            info += " · " + _("content scaled down to fit")
        if caps.max_length_mm and caps.too_long(rendered.length_mm):
            info += " · " + _("Too long for this printer: at most %s") % self.unit.format(
                caps.max_length_mm
            )
        width = self.label.media_size()[0]
        if not caps.continuous and caps.max_width_mm and caps.too_wide(width):
            info += " · " + _("Too wide for this printer: at most %s") % self.unit.format(
                caps.max_width_mm
            )
        if rendered.warnings:
            problems = "; ".join(warning_text(w) for w in rendered.warnings)
            info += " · " + _("Problem: %s") % problems
        return info

    def _render_failed(self, error: Exception) -> None:
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
            "add-text": lambda *_a: self.add_element(TextElement(text=_("Text"))),
            "add-image": lambda *_a: self.files.choose_image(self.add_element),
            "add-code": lambda *_a: self.add_element(CodeElement()),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
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
        try:
            self.add_element(image_from_path(path))
        except ValueError as e:
            self.toast(str(e))
            return False
        return True

    def _on_close_request(self, _window) -> bool:
        if self.print_flow.busy:
            self.toast(_("Please wait until printing has finished"))
            return True
        if not self.dirty:
            return False

        def close():
            self.dirty = False
            self.close()

        self.files.confirm_discard(close)
        return True
