"""Serial labels in the interface: the data file, its sheet, the row the
preview shows, and the button that inserts a column into a text or code."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from tavletta.merge import Book, MergeError, read_book  # noqa: E402
from tavletta.model import Label, data_inside  # noqa: E402
from tavletta.serial import Cursor, filled, insertable, placeholder  # noqa: E402
from tavletta.ui.i18n import _  # noqa: E402
from tavletta.ui.texts import data_error_text  # noqa: E402
from tavletta.ui.widgets import flat_button  # noqa: E402

if TYPE_CHECKING:
    from tavletta.ui.window import Window

# what the open dialog offers and a drop takes as data; the content decides,
# not the suffix. A label may also name a ".txt" table (model.TABLE_SUFFIXES),
# but a dropped text file is not taken for data.
DATA_SUFFIXES = (".csv", ".tsv", ".ods", ".xlsx")
DATA_TYPES = (
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
COLUMNS_SHOWN = 12  # in the group's column row; the insert menu has all of them
STEPS = ("first", "previous", "next", "last")


class DataSource:
    """The data file linked to the label (label.data_file), read in a
    worker thread: a file of 100 000 rows takes seconds. state is "none"
    (no file linked), "unread" (outside the label's folder: read only when
    the user agrees), "loading", "ready" or "failed" (error says why)."""

    def __init__(self, window: Window):
        self.window = window
        self.state = "none"
        self.error: Exception | None = None
        self.book: Book | None = None
        self.cursor = Cursor()
        self._generation = 0  # answers of an older read are dropped
        self._cancel = threading.Event()  # stops the read in progress

    # --- what the rest of the interface asks -----------------------------------

    @property
    def path(self) -> Path | None:
        name = self.window.label.data_file
        return Path(name) if name else None

    @property
    def columns(self) -> list[str]:
        return self.book.table.columns if self.book and self.state == "ready" else []

    @property
    def rows(self) -> list[dict[str, str]]:
        return self.book.table.rows if self.book and self.state == "ready" else []

    @property
    def active(self) -> bool:
        """Rows to fill the label with: the preview and printing use them."""
        return bool(self.rows)

    @property
    def row(self) -> dict[str, str] | None:
        """The row the preview shows, None without rows."""
        return self.rows[self.cursor.index] if self.active else None

    def current_label(self) -> Label:
        """The label as the preview shows it: the current row and the
        variables of the next print filled in, in one pass."""
        label, row = self.window.label, self.row
        return label if row is None else filled(label, row, self.window.preview_values())

    # --- changes -------------------------------------------------------------------

    def choose(self, *_args) -> None:
        flt = Gtk.FileFilter(name=_("Tables (CSV, ODS, XLSX)"))
        for suffix in DATA_SUFFIXES:
            flt.add_suffix(suffix[1:])
        for mime in DATA_TYPES:
            flt.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog = Gtk.FileDialog(title=_("Choose data file"), filters=filters)

        def done(d, result):
            try:
                path = d.open_finish(result).get_path()
            except GLib.Error:  # cancelled
                return
            if path:
                self.attach(Path(path))
            else:
                self.window.toast(_("Only local files can be used"))

        dialog.open(self.window, None, done)

    def attach(self, path: Path) -> None:
        label = self.window.label
        label.data_file = str(path.absolute())
        label.data_sheet = ""
        self.cursor = Cursor()
        self.window.mark_changed()
        self.load()

    def _drop_read(self) -> None:
        """A read still running is of no use now: its answer is dropped, and
        it stops at the next row."""
        self._generation += 1
        self._cancel.set()

    def remove(self) -> None:
        label = self.window.label
        label.data_file = label.data_sheet = ""
        self._drop_read()
        self.state, self.error, self.book = "none", None, None
        self.cursor = Cursor()
        self.window.mark_changed()
        self.window.data_changed()

    def choose_sheet(self, name: str) -> None:
        if self.book is None or name == self.window.label.data_sheet:
            return
        self.window.label.data_sheet = name
        self.cursor = Cursor()
        self.window.mark_changed()
        self.load()

    def follow_label(self) -> None:
        """Another label was opened: read its data file, if it has one. A
        data file outside the label's folder is read only if the user agrees:
        the label may come from someone else and name any file."""
        self.cursor = Cursor()
        self.book = None
        path, label_file = self.path, self.window.path
        if path and (
            data_inside(str(path), label_file)
            or (label_file and self.window.settings.trusts_data(label_file, path))
        ):
            self.load()
            return
        self._drop_read()
        self.state, self.error = ("unread" if path else "none"), None
        self.window.data_changed()
        if path:
            self._ask_to_read(path)

    def forget_allowed(self, *_args) -> None:
        """Ask again for every data file outside its label's folder."""
        self.window.settings.forget_trusted_data()
        self.update_forget_action()
        self.window.toast(
            _("Tavletta asks again before reading data files outside a label's folder")
        )

    def update_forget_action(self) -> None:
        action = self.window.lookup_action("forget-data")
        if action:
            action.set_enabled(self.window.settings.has_trusted_data())

    def _ask_to_read(self, path: Path) -> None:
        target = path.resolve()
        shown = str(path) if target == path else "%s\n→ %s" % (path, target)  # a link
        dialog = Adw.AlertDialog(
            heading=_("Read the data file?"),
            body=_(
                "This label uses a data file outside its folder:\n\n%s\n\nRead it only if you "
                "know where the label comes from."
            )
            % shown,
        )
        dialog.add_response("cancel", _("Not now"))
        dialog.add_response("read", _("Read data file"))
        dialog.set_close_response("cancel")
        label = self.window.label

        def response(_dialog, result: str) -> None:
            # the same label still: not one opened after it
            if result == "read" and self.window.label is label and self.state == "unread":
                if self.window.path:  # not asked again for this label file
                    self.window.settings.trust_data(self.window.path, path)
                    self.update_forget_action()
                self.load()

        dialog.connect("response", response)
        self.window.present_dialog(dialog)

    def load(self, *_args) -> None:
        path, sheet = self.path, self.window.label.data_sheet
        if path is None:
            return
        self._drop_read()
        generation, cancel = self._generation, threading.Event()
        self._cancel = cancel
        self.state, self.error = "loading", None
        self.window.data_changed()

        def work():
            try:
                book = read_book(path, sheet, cancel)
            except Exception as e:  # noqa: BLE001 - OSError, MergeError; never stay "loading"
                GLib.idle_add(self._failed, generation, e)
            else:
                GLib.idle_add(self._loaded, generation, book)

        threading.Thread(target=work, name="tavletta-data", daemon=True).start()

    def _loaded(self, generation: int, book: Book) -> bool:
        if generation != self._generation:
            return False
        # a CSV file has no sheets: a saved sheet (the file was a
        # spreadsheet once) means nothing, the file is read as it is
        wanted = self.window.label.data_sheet if book.sheets else ""
        if wanted and book.sheet != wanted:
            # renamed or deleted since the label was saved: never print
            # another sheet instead; the sheet row offers those there are
            self._failed(generation, MergeError("no_sheet", wanted))
            self.book = book
            self.window.data_changed()
            return False
        self.book, self.state = book, "ready"
        self.cursor = Cursor(len(book.table.rows), self.cursor.index)
        self.window.data_changed()
        return False

    @property
    def sheet_missing(self) -> bool:
        return isinstance(self.error, MergeError) and self.error.code == "no_sheet"

    def _failed(self, generation: int, error: Exception) -> bool:
        if generation != self._generation:
            return False
        self.state, self.error, self.book = "failed", error, None
        name = self.path.name if self.path else ""
        text = data_error_text(error, name)
        if isinstance(error, MergeError) and error.code == "damaged":
            self.window.error(text, str(error))
        else:  # nothing to report: the file is missing, too large, …
            self.window.toast(text)
        self.window.data_changed()
        return False

    def step(self, how: str) -> None:
        """Show another row: "first", "previous", "next" or "last"."""
        getattr(self.cursor, how)()
        self.window.data_changed(rebuild=False)

    def status(self) -> str:
        """The data file's state in a few words, for the sidebar."""
        if self.state == "loading":
            return _("Reading…")
        if self.state == "unread":
            return _("Not read: outside the label's folder")
        if self.state == "failed" and self.error is not None:
            return data_error_text(self.error, self.path.name if self.path else "")
        return _("Rows: %(rows)d · Columns: %(columns)d") % {
            "rows": len(self.rows),
            "columns": len(self.columns),
        }


class DataGroup(Adw.PreferencesGroup):
    """The "Serial labels" group of the sidebar."""

    def __init__(self, window: Window):
        super().__init__(
            title=_("Serial labels"),
            description=_(
                "One label for each row of a table. Insert columns into texts and codes; "
                "each label shows the values of its row."
            ),
        )
        self.window = window
        data = window.data
        if data.path is None:
            empty = Adw.ActionRow(title=_("No data file"), subtitle=_("CSV, ODS or XLSX"))
            choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
            choose.connect("clicked", data.choose)
            empty.add_suffix(choose)
            self.add(empty)
            return
        self.add(self._file_row(data))
        if data.book and (len(data.book.sheets) > 1 or data.sheet_missing):
            self.add(self._sheet_row(data.book))
        if data.columns:
            columns = data.columns[:COLUMNS_SHOWN] + (
                ["…"] if len(data.columns) > COLUMNS_SHOWN else []
            )
            row = Adw.ActionRow(
                title=_("Columns"), subtitle=GLib.markup_escape_text(", ".join(columns))
            )
            row.set_subtitle_selectable(True)
            self.add(row)

    def _file_row(self, data: DataSource) -> Adw.ActionRow:
        assert data.path is not None
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(data.path.name),
            subtitle=GLib.markup_escape_text(data.status()),
        )
        row.set_tooltip_text(str(data.path))
        row.add_prefix(Gtk.Image(icon_name="x-office-spreadsheet-symbolic"))
        if data.state == "failed":
            row.add_css_class("error")
        # suffixes are packed from the start: left to right
        refresh = flat_button("view-refresh-symbolic", _("Read again"), data.load)
        refresh.set_sensitive(data.state != "loading")
        row.add_suffix(refresh)
        row.add_suffix(
            flat_button("document-open-symbolic", _("Choose another file"), data.choose)
        )
        row.add_suffix(flat_button("user-trash-symbolic", _("Remove data file"), data.remove))
        return row

    def _sheet_row(self, book: Book) -> Adw.ComboRow:
        data = self.window.data
        sheets = list(book.sheets)
        # a saved sheet that is gone stays selected (the file row says it is
        # missing): choosing any of the others, the first one too, reads it
        current = self.window.label.data_sheet if data.sheet_missing else book.sheet
        if current not in sheets:
            sheets.append(current)
        row = Adw.ComboRow(title=_("Sheet"), model=Gtk.StringList.new(sheets))
        row.set_selected(sheets.index(current))
        row.set_sensitive(data.state != "loading")
        row.connect(
            "notify::selected",
            lambda r, _p: self.window.data.choose_sheet(sheets[r.get_selected()]),
        )
        return row


def column_button(columns: list[str], insert: Callable[[str], None]) -> Gtk.MenuButton:
    """A menu of the columns; choosing one calls insert("{column}")."""
    menu = Gio.Menu()
    for name in insertable(columns):
        # menu labels take "_" as the mnemonic mark: "__" is an underscore
        item = Gio.MenuItem.new(name.replace("_", "__"), None)
        item.set_action_and_target_value("data.insert", GLib.Variant.new_string(name))
        menu.append_item(item)
    button = Gtk.MenuButton(
        icon_name="insert-text-symbolic",
        tooltip_text=_("Insert column"),
        menu_model=menu,
        valign=Gtk.Align.CENTER,
    )
    button.add_css_class("flat")
    action = Gio.SimpleAction.new("insert", GLib.VariantType.new("s"))
    action.connect("activate", lambda _a, value: insert(placeholder(value.get_string())))
    group = Gio.SimpleActionGroup()
    group.add_action(action)
    button.insert_action_group("data", group)
    return button
