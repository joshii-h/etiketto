"""The "Content" group of the sidebar: one expander row per element."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from tavletta.history import typing_key  # noqa: E402
from tavletta.model import (  # noqa: E402
    ImageElement,
    PageElement,
    ShapeElement,
    SymbolElement,
    TextElement,
    element_id,
)
from tavletta.ui import freelayout, styles  # noqa: E402
from tavletta.ui.data import column_button  # noqa: E402
from tavletta.ui.i18n import _  # noqa: E402
from tavletta.ui.symbols import SymbolChooser, SymbolIcon  # noqa: E402
from tavletta.ui.texts import (  # noqa: E402
    FITS,
    ROTATIONS,
    SYMBOLOGY_GROUPS,
    SYMBOLOGY_HELP,
    TEXT_ALIGNS,
    element_icon,
    element_title,
)
from tavletta.ui.variables import (  # noqa: E402
    insert_into_entry,
    insert_into_text_view,
    variable_button,
)
from tavletta.ui.widgets import (  # noqa: E402
    color_button,
    combo_row,
    flat_button,
    grouped_combo_row,
    length_row,
    spin_row,
    toggle,
)

if TYPE_CHECKING:
    from tavletta.ui.window import Window


IMAGE_OPTIONS = (
    "crop",
    "rotation",
    "mirror",
    "flip",
    "brightness",
    "contrast",
    "gamma",
    "invert",
)


def _image_options(element: ImageElement) -> dict:
    """The options of the image that belong to its picture, set to their
    defaults: the changed ones (empty if none was changed)."""
    defaults = ImageElement()
    return {
        name: getattr(defaults, name)
        for name in IMAGE_OPTIONS
        if getattr(element, name) != getattr(defaults, name)
    }


class ElementsGroup(Adw.PreferencesGroup):
    """The "Content" group: one expander row per element."""

    def __init__(self, window: Window):
        super().__init__(title=_("Content"), description=freelayout.description(window))
        self.window = window
        # the rows open, the one opened last at the end: the open element
        self._open: list[int] = []
        self.rows: list[Adw.ExpanderRow] = []
        self._add_menu_button()
        self.add(freelayout.layout_row(window))

        if not window.label.elements:
            empty = Adw.ActionRow(
                title=_("No content yet"), subtitle=_("Add text, an image, a symbol or a code")
            )
            empty.add_css_class("dim-label")
            self.add(empty)
        for index, element in enumerate(window.label.elements):
            row = self._element_row(index, element)
            self.rows.append(row)
            self.add(row)

    def _add_menu_button(self) -> None:
        add_menu = Gio.Menu()
        add_menu.append(_("Text"), "win.add-text")
        add_menu.append(_("Image…"), "win.add-image")
        add_menu.append(_("Symbol…"), "win.add-symbol")
        add_menu.append(_("QR code / barcode"), "win.add-code")
        add_menu.append(_("Line or shape"), "win.add-shape")
        add = Gtk.MenuButton(
            icon_name="list-add-symbolic", menu_model=add_menu, tooltip_text=_("Add")
        )
        add.add_css_class("flat")
        self.set_header_suffix(add)

    def _element_row(self, index: int, element) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow()
        # a symbol shows itself
        prefix = SymbolIcon(element.path) if isinstance(element, SymbolElement) else None
        row.add_prefix(prefix or Gtk.Image(icon_name=element_icon(element)))
        if index == self.window.expand_index:
            row.set_expanded(True)
            self._open.append(index)
        row.connect("notify::expanded", lambda r, _p: self._on_expanded(index, r.get_expanded()))
        self._add_header_buttons(row, index)

        def retitle():
            row.set_title(GLib.markup_escape_text(element_title(element)))

        retitle()

        def set_attr(**kwargs):
            for key, value in kwargs.items():
                setattr(element, key, value)
            retitle()
            # typing into one field is one step of undo (History.record)
            self.window.mark_changed(typing_key("element:%d" % index, kwargs))

        self._kind_rows(row, element, set_attr, prefix)
        row.add_row(freelayout.position_row(self.window, element, set_attr))
        for widget, field in self._fields(row):  # by the element, wherever it moves
            widget.set_name("field:%d:%s" % (element_id(element), field))
        return row

    def _kind_rows(self, row, element, set_attr, prefix) -> None:
        """The rows of the element's own kind."""
        if isinstance(element, TextElement):
            self._text_rows(row, element, set_attr)
        elif isinstance(element, ImageElement):
            self._image_rows(row, element, set_attr)
        elif isinstance(element, SymbolElement):
            self._symbol_rows(row, element, set_attr, prefix)
        elif isinstance(element, PageElement):
            self._page_rows(row, element, set_attr)
        elif isinstance(element, ShapeElement):
            styles.shape_rows(self.window, row, element, set_attr)
        else:
            self._code_rows(row, element, set_attr)

    @staticmethod
    def _fields(widget) -> list:
        """The text fields of a row: the window's undo is the one that counts
        there too, and gives them the focus back (window._refocus)."""
        found = []
        child = widget.get_first_child()
        while child:
            if isinstance(child, Gtk.TextView):
                child.get_buffer().set_enable_undo(False)
                found.append((child, "text"))
            elif isinstance(child, Adw.EntryRow):
                child.set_enable_undo(False)
                found.append((child, child.get_title()))
            found += ElementsGroup._fields(child)
            child = child.get_next_sibling()
        return found

    def open_page(self, index: int, title: str) -> bool:
        """Open the page of options of that title of element index again
        (after the sidebar was rebuilt); False if there is none."""
        if not 0 <= index < len(self.rows):
            return False
        child = self.rows[index].get_first_child()
        stack = [child] if child else []
        while stack:
            widget = stack.pop()
            if isinstance(widget, Adw.ActionRow) and widget.get_title() == title:
                widget.emit("activated")
                return True
            stack.extend(n for n in (widget.get_next_sibling(), widget.get_first_child()) if n)
        return False

    def _on_expanded(self, index: int, expanded: bool) -> None:
        """The open element is the one the shortcuts (Ctrl+D, Delete,
        Alt+Up/Down) act on: the row opened last of those still open."""
        if index in self._open:
            self._open.remove(index)
        if expanded:
            self._open.append(index)
        self.window.expand_index = self._open[-1] if self._open else -1

    def _add_header_buttons(self, row: Adw.ExpanderRow, index: int) -> None:
        window = self.window
        # ExpanderRow packs suffixes from the end: add them right to left
        row.add_suffix(
            flat_button("user-trash-symbolic", _("Remove"), lambda: window.remove_element(index))
        )
        for icon, tip, delta in (
            ("go-down-symbolic", _("Move down"), 1),
            ("go-up-symbolic", _("Move up"), -1),
        ):
            target = index + delta
            button = flat_button(icon, tip, lambda t=target: window.move_element(index, t))
            button.set_sensitive(0 <= target < len(window.label.elements))
            row.add_suffix(button)

    def _text_rows(self, row: Adw.ExpanderRow, element: TextElement, set_attr) -> None:
        row.add_row(
            self._text_view_row(element, set_attr, self.window.open_url, self.window.data.columns)
        )
        weight = freelayout.WeightRow(element, set_attr)
        row.add_row(self._font_row(element, set_attr, weight))
        row.add_row(weight)
        if freelayout.is_free(self.window):
            size = freelayout.text_size_row(self.window, element, set_attr)
        else:
            size = spin_row(
                _("Size"),
                element.size_pt,
                0,
                300,
                1,
                0,
                lambda v: set_attr(size_pt=v),
                subtitle=_("pt, 0 = fill tape height"),
            )
        size.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
        row.add_row(size)
        row.add_row(
            combo_row(_("Line alignment"), TEXT_ALIGNS, element.align, lambda v: set_attr(align=v))
        )
        styles.text_rows(self.window, row, element, set_attr)
        row.add_row(self._name_row(element, set_attr))

    @staticmethod
    def _name_row(element, set_attr) -> Adw.EntryRow:
        """The name `tavletta print --set NAME=VALUE` refers to."""
        name = Adw.EntryRow(title=_("Name for the command line"))
        name.set_text(element.name)
        name.connect("changed", lambda e: set_attr(name=e.get_text().strip()))
        return name

    @staticmethod
    def _text_view_row(
        element: TextElement, set_attr, open_url, columns: list[str]
    ) -> Adw.PreferencesRow:
        """The text, a button to insert variables and, with a data file, one
        to insert its columns."""
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.get_buffer().set_text(element.text)
        view.get_buffer().connect(
            "changed",
            lambda buf: set_attr(
                text=buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            ),
        )
        frame = Gtk.Frame(child=view, hexpand=True)
        box = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=8, margin_bottom=8)
        box.append(frame)
        buttons = [variable_button(lambda p: insert_into_text_view(view, p), open_url)]
        if columns:
            buttons.append(column_button(columns, lambda p: insert_into_text_view(view, p)))
        for button in buttons:
            button.set_valign(Gtk.Align.START)
            box.append(button)
        return Adw.PreferencesRow(activatable=False, child=box)

    @staticmethod
    def _font_row(element: TextElement, set_attr, weight: freelayout.WeightRow) -> Adw.ActionRow:
        """The font, its weights in the weight row; bold, italic, underline
        and strikethrough. The bold button sets the weight back to bold or
        regular."""
        font_row = Adw.ActionRow(title=_("Font"))
        font_button = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER)
        font_button.set_level(Gtk.FontLevel.FAMILY)
        desc = Pango.FontDescription()
        desc.set_family(element.font or "Sans")  # "Source Sans 3": not size 3
        font_button.set_font_desc(desc)

        def font(button, _pspec) -> None:
            set_attr(font=button.get_font_desc().get_family() or "Sans")
            weight.set_family(element.font)

        def bold(active: bool) -> None:
            set_attr(bold=active, weight=0)
            weight.show_default()

        font_button.connect("notify::font-desc", font)
        font_row.add_suffix(font_button)
        font_row.add_suffix(toggle("format-text-bold-symbolic", _("Bold"), element.bold, bold))
        for icon, tip, attr in (
            ("format-text-italic-symbolic", _("Italic"), "italic"),
            ("format-text-underline-symbolic", _("Underline"), "underline"),
            ("format-text-strikethrough-symbolic", _("Strikethrough"), "strikethrough"),
        ):
            font_row.add_suffix(
                toggle(icon, tip, getattr(element, attr), lambda v, a=attr: set_attr(**{a: v}))
            )
        return font_row

    def _image_rows(self, row: Adw.ExpanderRow, element: ImageElement, set_attr) -> None:
        image_row = Adw.ActionRow(title=_("Image file"))
        image_row.set_subtitle(GLib.markup_escape_text(element.name or _("None")))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)

        def picked(new: ImageElement):
            options = _image_options(element)
            set_attr(data=new.data, name=new.name, **options)
            image_row.set_subtitle(GLib.markup_escape_text(new.name))
            if options:  # crop and turn were for the other picture
                self.window.rebuild_sidebar()
                self.window.toast(_("Crop, turn and adjustments reset for the new image"))

        choose.connect("clicked", lambda _b: self.window.files.choose_image(picked))
        image_row.add_suffix(choose)
        row.add_row(image_row)
        styles.image_rows(self.window, row, element, set_attr)

    def _page_rows(self, row: Adw.ExpanderRow, element: PageElement, set_attr) -> None:
        area = Adw.ActionRow(title=_("Area"))
        area.set_subtitle(GLib.markup_escape_text(element.name))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda _b: self.window.files.edit_area(element))
        area.add_suffix(choose)
        row.add_row(area)
        row.add_row(combo_row(_("Scale"), FITS, element.fit, lambda v: set_attr(fit=v)))

    def _symbol_rows(
        self, row: Adw.ExpanderRow, element: SymbolElement, set_attr, prefix: SymbolIcon
    ) -> None:
        icon = SymbolIcon(element.path, 24)
        symbol_row = Adw.ActionRow(title=_("Symbol"))
        symbol_row.add_prefix(icon)
        symbol_row.set_subtitle(GLib.markup_escape_text(element.name or _("None")))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)

        def picked(symbol):
            set_attr(name=symbol.name, path=symbol.path)
            symbol_row.set_subtitle(GLib.markup_escape_text(symbol.name))
            for widget in (icon, prefix):
                widget.show(symbol.path)

        choose.connect(
            "clicked",
            lambda _b: SymbolChooser(self.window, picked, element.name).present(),
        )
        symbol_row.add_suffix(choose)
        row.add_row(symbol_row)
        size = length_row(
            self.window.unit,
            _("Size"),
            element.size_mm,
            0,
            200,
            1,
            lambda v: set_attr(size_mm=v),
            subtitle=_("%(unit)s, 0 = fill tape height"),
        )
        color = color_button(element.color, lambda v: set_attr(color=v))
        color.set_tooltip_text(
            _(
                "Black-and-white printers print symbols in black, or in white if the colour is light"
            )
        )
        size.add_suffix(color)
        row.add_row(size)
        row.add_row(
            combo_row(
                _("Rotation"),
                [(str(k), text) for k, text in ROTATIONS],
                str(element.rotation),
                lambda v: set_attr(rotation=int(v)),
            )
        )

    def _code_rows(self, row: Adw.ExpanderRow, element, set_attr) -> None:
        def set_type(symbology: str) -> None:
            kind.set_subtitle(SYMBOLOGY_HELP.get(symbology, ""))
            error_correction.set_visible(symbology == "qr")
            set_attr(symbology=symbology)

        kind = grouped_combo_row(_("Type"), SYMBOLOGY_GROUPS, element.symbology, set_type)
        kind.set_subtitle(SYMBOLOGY_HELP.get(element.symbology, ""))
        row.add_row(kind)
        error_correction = freelayout.error_correction_row(element, set_attr)
        data = Adw.EntryRow(title=_("Content (link, text, number)"))
        data.set_text(element.data)
        data.connect("changed", lambda e: set_attr(data=e.get_text()))
        data.add_suffix(
            variable_button(lambda p: insert_into_entry(data, p), self.window.open_url)
        )
        if columns := self.window.data.columns:
            data.add_suffix(column_button(columns, lambda p: insert_into_entry(data, p)))
        row.add_row(data)
        row.add_row(error_correction)
        color = Adw.ActionRow(title=_("Colour"))
        color.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
        row.add_row(color)
        row.add_row(self._name_row(element, set_attr))
