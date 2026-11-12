"""Opening, saving, exporting, templates and new labels."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, Gtk

from tavletta import imageformats
from tavletta.model import Label, LabelError, PageElement, TextElement
from tavletta.shipping.source import SourceError, page_info
from tavletta.shipping.targets import Target, make_label
from tavletta.ui import settings as config
from tavletta.ui.i18n import _
from tavletta.ui.shipping import ShippingDialog
from tavletta.ui.texts import (
    error_details,
    image_from_path,
    label_error_text,
    save_error_text,
    source_error_text,
)

if TYPE_CHECKING:
    from tavletta.ui.window import Window


# shipping labels: PDF and the image formats a label may embed
SHIPPING_TYPES = ("application/pdf", *imageformats.MIME_TYPES)
SHIPPING_SUFFIXES = ("pdf", *imageformats.SUFFIXES)


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
        flt = Gtk.FileFilter(name=_("Tavletta labels"))
        flt.add_pattern("*.tavletta")
        return _filters(flt)

    def choose_image(self, callback) -> None:
        flt = Gtk.FileFilter(name=_("Images"))
        for mime in imageformats.MIME_TYPES:
            flt.add_mime_type(mime)
        for suffix in imageformats.SUFFIXES:
            flt.add_suffix(suffix)
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

    # --- shipping labels ---------------------------------------------------------

    def open_shipping(self, *_args) -> None:
        flt = Gtk.FileFilter(name=_("PDF documents and images"))
        for mime in SHIPPING_TYPES:
            flt.add_mime_type(mime)
        for suffix in SHIPPING_SUFFIXES:
            flt.add_suffix(suffix)
        dialog = Gtk.FileDialog(title=_("Open shipping label"), filters=_filters(flt))

        def done(d, result):
            try:
                path = d.open_finish(result).get_path()
            except GLib.Error:  # cancelled
                return
            if path:
                self.shipping_from(Path(path))
            else:
                self.window.toast(_("Only local files can be used"))

        self.confirm_discard(lambda: dialog.open(self.window, None, done))

    def shipping_from(self, path: Path) -> None:
        """Choose the area of a carrier's PDF or image, then make a new label of it."""
        window = self.window
        try:
            element = PageElement.from_file(path)
            page_info(element.data)  # a PDF or an image at all?
        except (OSError, LabelError) as e:
            window.error(_("Cannot open %s") % path.name, str(e))
            return
        except SourceError as e:
            window.error(source_error_text(e, path.name), str(e))
            return
        window.present_dialog(
            ShippingDialog(window, element, self._new_shipping_label, self._use_shipping_label)
        )

    def _new_shipping_label(self, element: PageElement, target: Target | None) -> Label:
        window = self.window
        assert target is not None
        return make_label(window.backend, element, target, window.printer_settings.safe_mm)

    def _use_shipping_label(self, element: PageElement, target: Target | None) -> None:
        self.window.set_label(self._new_shipping_label(element, target), None)
        self.window.dirty = self.window.dirty_outside = True  # nothing saved yet
        self.window.update_title()

    def edit_area(self, element: PageElement) -> None:
        """Choose the area of a page already on the label again."""
        window = self.window

        def preview(new: PageElement, _target) -> Label:
            elements = [new if e is element else e for e in window.label.elements]
            return replace(window.label, elements=elements)

        def apply(new: PageElement, _target) -> None:
            element.data, element.page, element.crop = new.data, new.page, new.crop
            element.rotation, element.fit = new.rotation, new.fit
            window.rebuild_sidebar()
            window.mark_changed()

        try:
            dialog = ShippingDialog(window, element, preview, apply, edit=True)
        except SourceError as e:
            window.error(source_error_text(e, element.name), str(e))
            return
        window.present_dialog(dialog)

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
        self._pick_save_path(_("Save label"), ".tavletta", self.write, self._label_filter())

    def save_template(self, *_args) -> None:
        """Store a copy as template; the open file and its state stay as they
        are. A template keeps the placeholders but not the data file: each
        use chooses its own, and a template passed on names no path of
        this computer (the home folder, a user name)."""
        self._pick_save_path(
            _("Save as template…"),
            ".tavletta",
            self._save_template,
            self._label_filter(),
            folder=config.TEMPLATE_DIR,
        )

    def _save_template(self, path: Path) -> bool:
        label = dataclasses.replace(self.window.label, data_file="", data_sheet="")
        return self._save(label.save, path, _("Saved %s") % path.name)

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
            window.remember_saved()
            window.dirty = False
            window.update_title()

    def export(self, *_args) -> None:
        self._pick_save_path(_("Export PNG"), ".png", self.export_png)

    def export_png(self, path: Path) -> None:
        window = self.window
        dpi = window.print_dpi()
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
        files = sorted(directory.glob("*.tavletta")) if directory.is_dir() else []
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
    """On a file system that ignores case, "X.TAVLETTA" is "X.tavletta"."""
    try:
        return path.samefile(other)
    except OSError:  # the chosen name does not exist yet
        return False


def _with_suffix(path: Path, suffix: str) -> Path:
    """The path with the suffix appended, not replaced: "Shelf 1.2" stays
    "Shelf 1.2.tavletta". An image may end in ".PNG"; a label gets exactly
    ".tavletta", which the template menu and the open dialog look for."""
    if path.suffix.lower() != suffix:
        return path.with_name(path.name + suffix)
    return path.with_suffix(suffix) if suffix == ".tavletta" else path
