"""Opening, saving, exporting, templates and new labels."""

from __future__ import annotations

from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from etiketto.model import Label, LabelError, TextElement
from etiketto.ui import settings as config
from etiketto.ui.i18n import _
from etiketto.ui.texts import image_from_path, label_error_text, save_error_text
from etiketto.ui.widgets import confirm_dialog


class Files:
    """Opening, saving, exporting, templates and new labels."""

    def __init__(self, window):
        self.window = window

    def label_filter(self) -> Gio.ListStore:
        filters = Gio.ListStore.new(Gtk.FileFilter)
        flt = Gtk.FileFilter(name=_("Etiketto labels"))
        flt.add_pattern("*.etiketto")
        filters.append(flt)
        return filters

    def choose_image(self, callback) -> None:
        dialog = Gtk.FileDialog(title=_("Choose image"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        flt = Gtk.FileFilter(name=_("Images"))
        flt.add_pixbuf_formats()
        flt.add_suffix("webp")
        filters.append(flt)
        dialog.set_filters(filters)

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                callback(image_from_path(file.get_path()))
            except ValueError as e:
                self.window.toast(str(e))

        dialog.open(self.window, None, done)

    def open_file(self, path: Path) -> None:
        window = self.window
        try:
            window.set_label(Label.load(path), path)
        except LabelError as e:
            window.error(label_error_text(e, path.name), str(e))
        except OSError as e:
            window.error(_("Cannot open %s") % path.name, str(e))

    def open(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Open label"), filters=self.label_filter())

        def done(d, result):
            try:
                self.open_file(Path(d.open_finish(result).get_path()))
            except GLib.Error:
                pass

        self.confirm_discard(lambda: dialog.open(self.window, None, done))

    # --- saving ------------------------------------------------------------------

    def _initial_name(self, suffix: str) -> str:
        path = self.window.path
        return (path.stem if path else _("label")) + suffix

    def _pick_save_path(
        self, title: str, suffix: str, callback, filters=None, folder: Path | None = None
    ) -> None:
        """Ask for a file name, force the suffix, then call callback(path)."""
        dialog = Gtk.FileDialog(title=title)
        if filters is not None:
            dialog.set_filters(filters)
        dialog.set_initial_name(self._initial_name(suffix))
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

        def done(d, result):
            try:
                path = Path(d.save_finish(result).get_path())
            except (GLib.Error, TypeError):
                return
            if path.suffix.lower() != suffix:
                path = path.with_suffix(suffix)
            callback(path)

        dialog.save(self.window, None, done)

    def save(self, *_args) -> None:
        if self.window.path:
            self.write(self.window.path)
        else:
            self.save_as()

    def save_as(self, *_args, folder: Path | None = None) -> None:
        self._pick_save_path(
            _("Save label"), ".etiketto", self.write, self.label_filter(), folder=folder
        )

    def save_template(self, *_args) -> None:
        """Store a copy as template; the open file and its state stay as they are."""
        self._pick_save_path(
            _("Save as template…"),
            ".etiketto",
            self._write_template,
            self.label_filter(),
            folder=config.TEMPLATE_DIR,
        )

    def _write_template(self, path: Path) -> None:
        window = self.window
        try:
            window.label.save(path)
        except OSError as e:
            window.error(save_error_text(e, path.name), str(e))
            return
        window.toast(_("Saved %s") % path.name)

    def write(self, path: Path) -> None:
        window = self.window
        try:
            window.label.save(path)
        except OSError as e:
            window.error(save_error_text(e, path.name), str(e))
            return
        window.path = path
        window.dirty = False
        window.update_title()
        window.toast(_("Saved %s") % path.name)

    def export(self, *_args) -> None:
        self._pick_save_path(_("Export PNG"), ".png", self.export_png)

    def export_png(self, path: Path) -> None:
        window = self.window
        dpi = window.backend.capabilities.dpi
        try:
            rendered = window.render_label(window.label)
            image = window.backend.preview(rendered.image, window.print_flow.mode)
        except Exception as e:  # noqa: BLE001
            window.error(_("The preview cannot be drawn"), "%s: %s" % (type(e).__name__, e))
            return
        try:
            image.save(path, format="PNG", dpi=(dpi, dpi))
        except OSError as e:
            window.error(save_error_text(e, path.name), str(e))
            return
        window.toast(_("Exported"))

    # --- templates and new labels ------------------------------------------------

    def fill_templates(self) -> None:
        menu = self.window.template_menu
        menu.remove_all()
        directory = config.TEMPLATE_DIR
        files = sorted(directory.glob("*.etiketto")) if directory.is_dir() else []
        if not files:
            menu.append(_("No templates yet – use “Save as template…”"), None)
            return
        for path in files:
            item = Gio.MenuItem.new(path.stem, None)
            item.set_action_and_target_value(
                "win.open-template", GLib.Variant.new_string(str(path))
            )
            menu.append_item(item)

    def open_template(self, _action, value) -> None:
        window = self.window
        path = Path(value.get_string())

        def load():
            try:
                label = Label.load(path)
            except (OSError, LabelError) as e:
                window.error(_("Cannot open template %(name)s") % {"name": path.stem}, str(e))
                return
            # a template starts a new, unsaved label
            window.set_label(label, None)

        self.confirm_discard(load)

    def new(self, *_args) -> None:
        window = self.window

        def reset():
            info = window.printer.info
            backend = window.backend
            inserted = info.tape_mm if info else None
            label = Label.new_for(backend.id, backend.capabilities, inserted)
            label.elements = [TextElement(text=_("Label"))]
            window.set_label(label, None)

        self.confirm_discard(reset)

    def confirm_discard(self, then) -> None:
        if not self.window.dirty:
            then()
            return
        dialog = confirm_dialog(
            _("Discard changes?"),
            _("The current label has unsaved changes."),
            "discard",
            _("Discard"),
            then,
        )
        self.window.present_dialog(dialog)
