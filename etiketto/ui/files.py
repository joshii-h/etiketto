"""Opening, saving, exporting, templates and new labels."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, Gtk

from etiketto.model import Label, LabelError, TextElement
from etiketto.ui import settings as config
from etiketto.ui.i18n import _
from etiketto.ui.texts import error_details, image_from_path, label_error_text, save_error_text

if TYPE_CHECKING:
    from etiketto.ui.window import Window


def _filters(flt: Gtk.FileFilter) -> Gio.ListStore:
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(flt)
    return filters


class Files:
    """Opening, saving, exporting, templates and new labels."""

    def __init__(self, window: Window):
        self.window = window

    @staticmethod
    def _label_filter() -> Gio.ListStore:
        flt = Gtk.FileFilter(name=_("Etiketto labels"))
        flt.add_pattern("*.etiketto")
        return _filters(flt)

    def choose_image(self, callback) -> None:
        flt = Gtk.FileFilter(name=_("Images"))
        flt.add_pixbuf_formats()
        flt.add_suffix("webp")
        dialog = Gtk.FileDialog(title=_("Choose image"), filters=_filters(flt))

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

    def _load(self, path: Path, template: bool = False) -> Label | None:
        """The label in a file, or None after telling the user why not."""
        try:
            return Label.load(path)
        except (OSError, LabelError) as e:
            if template:
                text = _("Cannot open template %(name)s") % {"name": path.stem}
            elif isinstance(e, LabelError):
                text = label_error_text(e, path.name)
            else:
                text = _("Cannot open %s") % path.name
            self.window.error(text, str(e))
            return None

    def open_file(self, path: Path) -> None:
        label = self._load(path)
        if label:
            self.window.set_label(label, path)

    def open(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Open label"), filters=self._label_filter())

        def done(d, result):
            try:
                path = d.open_finish(result).get_path()
            except GLib.Error:  # cancelled
                return
            if path:
                self.open_file(Path(path))
            else:
                self.window.toast(_("Only local files can be used"))

        self.confirm_discard(lambda: dialog.open(self.window, None, done))

    # --- saving ------------------------------------------------------------------

    def _initial_name(self, suffix: str) -> str:
        path = self.window.path
        return (path.stem if path else _("label")) + suffix

    def _pick_save_path(
        self,
        title: str,
        suffix: str,
        callback,
        filters=None,
        folder: Path | None = None,
        name: str = "",
    ) -> None:
        """Ask for a file name, force the suffix, then call callback(path)."""
        dialog = Gtk.FileDialog(title=title)
        if filters is not None:
            dialog.set_filters(filters)
        dialog.set_initial_name(name or self._initial_name(suffix))
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

        def done(d, result):
            try:
                chosen = d.save_finish(result).get_path()
            except GLib.Error:  # cancelled
                return
            if not chosen:
                self.window.toast(_("Only local files can be used"))
                return
            path = _with_suffix(Path(chosen), suffix)
            if path.name != Path(chosen).name and path.exists() and not _same_file(path, chosen):
                # the dialog asked about overwriting another name: ask again
                self._pick_save_path(title, suffix, callback, filters, path.parent, path.name)
            else:
                callback(path)

        dialog.save(self.window, None, done)

    def save(self, *_args) -> None:
        if self.window.path:
            self.write(self.window.path)
        else:
            self.save_as()

    def save_as(self, *_args) -> None:
        self._pick_save_path(_("Save label"), ".etiketto", self.write, self._label_filter())

    def save_template(self, *_args) -> None:
        """Store a copy as template; the open file and its state stay as they are."""
        self._pick_save_path(
            _("Save as template…"),
            ".etiketto",
            self._save_label,
            self._label_filter(),
            folder=config.TEMPLATE_DIR,
        )

    def _save(self, save, path: Path, done: str) -> bool:
        """save(path), then the toast `done`; or tell the user why it failed."""
        try:
            save(path)
        except OSError as e:
            self.window.error(save_error_text(e, path.name), str(e))
            return False
        self.window.toast(done)
        return True

    def _save_label(self, path: Path) -> bool:
        return self._save(self.window.label.save, path, _("Saved %s") % path.name)

    def write(self, path: Path) -> None:
        window = self.window
        if window.path and _same_file(window.path, str(path)):
            window.sync_counter()  # the command line may have printed meanwhile
        if self._save_label(path):
            window.path = path
            window.saved_counter = window.label.counter_next
            window.dirty = False
            window.update_title()

    def export(self, *_args) -> None:
        self._pick_save_path(_("Export PNG"), ".png", self.export_png)

    def export_png(self, path: Path) -> None:
        window = self.window
        dpi = window.backend.capabilities.dpi
        try:
            _rendered, image = window.render_image()
        except Exception as e:  # noqa: BLE001
            window.error(_("The preview cannot be drawn"), error_details(e))
            return
        self._save(lambda p: image.save(p, format="PNG", dpi=(dpi, dpi)), path, _("Exported"))

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
        path = Path(value.get_string())

        def load():
            label = self._load(path, template=True)
            if label:  # a template starts a new, unsaved label
                self.window.set_label(label, None)

        self.confirm_discard(load)

    def new(self, *_args) -> None:
        window = self.window

        def reset():
            info = window.printer.info
            backend = window.backend
            inserted = info.tape_mm if info else None
            label = Label.new_for(backend.id, inserted)
            label.elements = [TextElement(text=_("Label"))]
            window.set_label(label, None, adopts_tape=True)

        self.confirm_discard(reset)

    def confirm_discard(self, then) -> None:
        if not self.window.dirty:
            then()
            return
        self.window.confirm(
            _("Discard changes?"),
            _("The current label has unsaved changes."),
            "discard",
            _("Discard"),
            then,
        )


def _same_file(path: Path, other: str) -> bool:
    """On a file system that ignores case, "X.ETIKETTO" is "X.etiketto"."""
    try:
        return path.samefile(other)
    except OSError:  # the chosen name does not exist yet
        return False


def _with_suffix(path: Path, suffix: str) -> Path:
    """The path with the suffix appended, not replaced: "Shelf 1.2" stays
    "Shelf 1.2.etiketto". An image may end in ".PNG"; a label gets exactly
    ".etiketto", which the template menu and the open dialog look for."""
    if path.suffix.lower() != suffix:
        return path.with_name(path.name + suffix)
    return path.with_suffix(suffix) if suffix == ".etiketto" else path
