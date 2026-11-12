"""The "Content" group of the sidebar: one expander row per element."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from etiketto.model import ImageElement, TextElement  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402
from etiketto.ui.texts import (  # noqa: E402
    SYMBOLOGY_GROUPS,
    SYMBOLOGY_HELP,
    TEXT_ALIGNS,
    element_icon,
    element_title,
)
from etiketto.ui.widgets import (  # noqa: E402
    color_button,
    combo_row,
    flat_button,
    grouped_combo_row,
    spin_row,
)

if TYPE_CHECKING:
    from etiketto.ui.window import Window


class ElementsGroup(Adw.PreferencesGroup):
    """The "Content" group: one expander row per element."""

    def __init__(self, window: Window):
        super().__init__(
            title=_("Content"),
            description=_("Elements are placed one after another along the tape"),
        )
        self.window = window
        self._add_menu_button()

        if not window.label.elements:
            empty = Adw.ActionRow(
                title=_("No content yet"), subtitle=_("Add text, an image or a code")
            )
            empty.add_css_class("dim-label")
            self.add(empty)
        for index, element in enumerate(window.label.elements):
            self.add(self._element_row(index, element))

    def _add_menu_button(self) -> None:
        add_menu = Gio.Menu()
        add_menu.append(_("Text"), "win.add-text")
        add_menu.append(_("Image…"), "win.add-image")
        add_menu.append(_("QR code / barcode"), "win.add-code")
        add = Gtk.MenuButton(
            icon_name="list-add-symbolic", menu_model=add_menu, tooltip_text=_("Add")
        )
        add.add_css_class("flat")
        self.set_header_suffix(add)

    def _element_row(self, index: int, element) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow()
        row.add_prefix(Gtk.Image(icon_name=element_icon(element)))
        row.set_expanded(index == self.window.expand_index)
        self._add_header_buttons(row, index)

        def retitle():
            row.set_title(GLib.markup_escape_text(element_title(element)))

        retitle()

        def set_attr(**kwargs):
            for key, value in kwargs.items():
                setattr(element, key, value)
            retitle()
            self.window.mark_changed()

        if isinstance(element, TextElement):
            self._text_rows(row, element, set_attr)
        elif isinstance(element, ImageElement):
            self._image_rows(row, element, set_attr)
        else:
            self._code_rows(row, element, set_attr)
        return row

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
        row.add_row(self._text_view_row(element, set_attr))
        row.add_row(self._font_row(element, set_attr))
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

    @staticmethod
    def _text_view_row(element: TextElement, set_attr) -> Adw.PreferencesRow:
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
        frame = Gtk.Frame(
            child=view, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8
        )
        return Adw.PreferencesRow(activatable=False, child=frame)

    @staticmethod
    def _font_row(element: TextElement, set_attr) -> Adw.ActionRow:
        font_row = Adw.ActionRow(title=_("Font"))
        font_button = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER)
        font_button.set_level(Gtk.FontLevel.FAMILY)
        font_button.set_font_desc(Pango.FontDescription.from_string(element.font))
        font_button.connect(
            "notify::font-desc",
            lambda b, _p: set_attr(font=b.get_font_desc().get_family() or "Sans"),
        )
        font_row.add_suffix(font_button)
        for icon, attr in (
            ("format-text-bold-symbolic", "bold"),
            ("format-text-italic-symbolic", "italic"),
        ):
            toggle = Gtk.ToggleButton(
                icon_name=icon, valign=Gtk.Align.CENTER, active=getattr(element, attr)
            )
            toggle.add_css_class("flat")
            toggle.connect("toggled", lambda t, a=attr: set_attr(**{a: t.get_active()}))
            font_row.add_suffix(toggle)
        return font_row

    def _image_rows(self, row: Adw.ExpanderRow, element: ImageElement, set_attr) -> None:
        image_row = Adw.ActionRow(title=_("Image file"))
        image_row.set_subtitle(GLib.markup_escape_text(element.name or _("None")))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)

        def picked(new: ImageElement):
            set_attr(data=new.data, name=new.name)
            image_row.set_subtitle(GLib.markup_escape_text(new.name))

        choose.connect("clicked", lambda _b: self.window.files.choose_image(picked))
        image_row.add_suffix(choose)
        row.add_row(image_row)

    def _code_rows(self, row: Adw.ExpanderRow, element, set_attr) -> None:
        def set_type(symbology: str) -> None:
            kind.set_subtitle(SYMBOLOGY_HELP.get(symbology, ""))
            set_attr(symbology=symbology)

        kind = grouped_combo_row(_("Type"), SYMBOLOGY_GROUPS, element.symbology, set_type)
        kind.set_subtitle(SYMBOLOGY_HELP.get(element.symbology, ""))
        row.add_row(kind)
        data = Adw.EntryRow(title=_("Content (link, text, number)"))
        data.set_text(element.data)
        data.connect("changed", lambda e: set_attr(data=e.get_text()))
        row.add_row(data)
        color = Adw.ActionRow(title=_("Colour"))
        color.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
        row.add_row(color)
