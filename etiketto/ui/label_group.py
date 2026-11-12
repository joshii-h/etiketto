"""The "Label" group of the sidebar: tape or die-cut size, spacing, background, border."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from etiketto.model import (  # noqa: E402
    MAX_GAP_MM,
    MAX_SIZE_MM,
    MIN_SIZE_MM,
    MIN_TAPE_LENGTH_MM,
)
from etiketto.sheets import find  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402
from etiketto.ui.sheets import positions_text, sheet_summary  # noqa: E402
from etiketto.ui.texts import ALIGNS, BORDER_STYLES, ORIENTATIONS, PRINT_ON  # noqa: E402
from etiketto.ui.widgets import (  # noqa: E402
    color_button,
    combo_row,
    expander,
    flat_button,
    length_row,
)

if TYPE_CHECKING:
    from etiketto.ui.window import Window

CUSTOM_SIZE = "custom"
# a size entered in inches still matches a preset in mm
SAME_SIZE_MM = 0.05
# the length and alignment rows are off while the length is automatic
INVERTED = GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN


def _tape_key(width_mm: float) -> str:
    """ "12" for 12 mm tape (also 12.0), "3.5" for 3.5 mm."""
    return "%g" % width_mm


def _tape_width(key: str) -> float:
    """12, not 12.0: tape widths in files and settings stay as they were."""
    width = float(key)
    return int(width) if width.is_integer() else width


def _size_key(width_mm: float, height_mm: float) -> str:
    return "%g×%g" % (width_mm, height_mm)


class LabelGroup(Adw.PreferencesGroup):
    """The "Label" group: tape or die-cut size, spacing, background, border."""

    tape_row: Adw.ComboRow  # continuous tape only
    _size_row: Adw.ComboRow  # the others: die-cut labels only
    _width_row: Adw.SpinRow
    _height_row: Adw.SpinRow

    def __init__(self, window: Window):
        super().__init__(title=_("Label"))
        self.window = window
        self._sizes: dict[str, tuple[float, float]] = {}  # preset key: (width, height)
        self._size_keys: list[str] = []  # the size row's entries
        self._syncing = False

        if window.backend.capabilities.continuous:
            self._add_tape_rows()
        else:
            self._add_size_rows()
        self._add_spacing_rows()
        self._add_background_rows()
        self._add_border_row()

    def _length_row(self, title, mm, lower_mm, upper_mm, step_mm, on_change_mm, subtitle=None):
        return length_row(
            self.window.unit, title, mm, lower_mm, upper_mm, step_mm, on_change_mm, subtitle
        )

    def _add_tape_rows(self) -> None:
        window = self.window
        widths = [
            (_tape_key(w), window.unit.tape_name(w))
            for w in window.backend.capabilities.tape_widths_mm
        ]
        self.tape_row = combo_row(
            _("Tape width"),
            widths,
            _tape_key(window.label.tape_mm),
            lambda v: window.change(tape_mm=_tape_width(v)),
        )
        self.add(self.tape_row)
        self._add_length_rows()

    def _add_spacing_rows(self) -> None:
        window, lab = self.window, self.window.label
        self.add(
            self._length_row(
                _("Spacing"),
                lab.margin_mm,
                0,
                20,
                0.5,
                lambda v: window.change(margin_mm=v),
                _("%(unit)s at the ends and between elements"),
            )
        )
        self.add(
            self._length_row(
                _("Edge spacing"),
                lab.edge_mm,
                0,
                10,
                0.5,
                lambda v: window.change(edge_mm=v),
                _("extra %(unit)s along the tape edges"),
            )
        )

    def _add_background_rows(self) -> None:
        window, lab = self.window, self.window.label
        bg = Adw.ActionRow(title=_("Background colour"))
        bg.add_suffix(color_button(lab.background, lambda v: window.change(background=v)))
        self.add(bg)

        bg_image = Adw.ActionRow(title=_("Background image"))
        name = lab.background_image.name if lab.background_image else _("None")
        bg_image.set_subtitle(GLib.markup_escape_text(name))
        bg_image.add_suffix(
            flat_button(
                "document-open-symbolic",
                _("Choose image"),
                lambda: window.files.choose_image(window.set_background_image),
            )
        )
        if lab.background_image:
            bg_image.add_suffix(
                flat_button(
                    "edit-clear-symbolic", _("Remove"), lambda: window.set_background_image(None)
                )
            )
        self.add(bg_image)

    def _add_border_row(self) -> None:
        window, lab = self.window, self.window.label

        def set_width(value: float) -> None:
            window.change(border_mm=value)
            self._show_border_style(shape)

        border = self._length_row(
            _("Border"),
            lab.border_mm,
            0,
            5,
            0.1,
            set_width,
            _("%(unit)s, 0 = none"),
        )
        border.add_suffix(color_button(lab.border_color, lambda v: window.change(border_color=v)))
        self.add(border)
        shape = expander(_("Border style"), _("Corners, line and distance from the edge"))
        shape.add_row(
            self._length_row(
                _("Corner radius"),
                lab.border_radius_mm,
                0,
                50,
                0.5,
                lambda v: window.change(border_radius_mm=v),
            )
        )
        shape.add_row(
            combo_row(
                _("Line style"),
                BORDER_STYLES,
                lab.border_style,
                lambda v: window.change(border_style=v),
            )
        )
        shape.add_row(
            self._length_row(
                _("Inset"),
                lab.border_inset_mm,
                0,
                10,
                0.5,
                lambda v: window.change(border_inset_mm=v),
                _("extra %(unit)s inside the safety margin"),
            )
        )
        self._show_border_style(shape)
        self.add(shape)

    def _show_border_style(self, shape: Adw.ExpanderRow) -> None:
        """The border's shape does nothing without a border: off, and why."""
        has_border = self.window.label.border_mm > 0
        shape.set_sensitive(has_border)
        shape.set_subtitle(
            _("Corners, line and distance from the edge")
            if has_border
            else _("Set a border width first")
        )

    def _add_length_rows(self) -> None:
        """Length, automatic length and alignment: only for endless tape."""
        window = self.window
        lab, caps = window.label, window.backend.capabilities
        auto = Adw.SwitchRow(title=_("Automatic length"), subtitle=_("As long as the content"))
        auto.set_active(lab.auto_length)
        self.add(auto)

        lower = max(MIN_TAPE_LENGTH_MM, caps.min_length_mm)
        upper = float(math.floor(min(MAX_SIZE_MM, caps.max_length_mm or MAX_SIZE_MM)))
        # a file made for another printer: show and keep the same value
        lab.length_mm = min(max(lab.length_mm, lower), upper)
        length = self._length_row(
            _("Length"), lab.length_mm, lower, upper, 1, lambda v: window.change(length_mm=v)
        )
        align = combo_row(_("Alignment"), ALIGNS, lab.align, lambda v: window.change(align=v))
        for row in (length, align):
            auto.bind_property("active", row, "sensitive", INVERTED)
            self.add(row)
        auto.connect("notify::active", lambda r, _p: window.change(auto_length=r.get_active()))

    def _size_key(self) -> str:
        """The preset matching the label, else CUSTOM_SIZE."""
        width, height = self.window.label.media_size()
        for size in self.window.backend.capabilities.label_sizes:
            if (
                abs(size.width_mm - width) < SAME_SIZE_MM
                and abs(size.height_mm - height) < SAME_SIZE_MM
            ):
                return _size_key(size.width_mm, size.height_mm)
        return CUSTOM_SIZE

    def _add_size_rows(self) -> None:
        """Die-cut labels: size (preset or by hand), gap, orientation, alignment;
        on a page printer a single label or a label sheet."""
        window = self.window
        lab, caps = window.label, window.backend.capabilities
        if caps.pages:
            current = "sheet" if lab.sheet else "page"
            self.add(combo_row(_("Print on"), PRINT_ON, current, self._on_print_on))
        if caps.pages and lab.sheet:
            self._add_sheet_rows()
        else:
            self._add_preset_rows(caps)
        self.add(combo_row(_("Alignment"), ALIGNS, lab.align, lambda v: window.change(align=v)))

    def _add_preset_rows(self, caps) -> None:
        unit = self.window.unit
        self._sizes = {
            _size_key(s.width_mm, s.height_mm): (s.width_mm, s.height_mm) for s in caps.label_sizes
        }
        items = [(key, unit.size(*size)) for key, size in self._sizes.items()]
        items.append((CUSTOM_SIZE, _("Custom size")))
        self._size_keys = [key for key, _name in items]
        self._size_row = combo_row(_("Label size"), items, self._size_key(), self._on_size_preset)
        self.add(self._size_row)

        self._add_width_height(caps)
        if not caps.pages:  # labels on a roll: gap and direction of the roll
            self._add_roll_rows()

    # --- label sheets ------------------------------------------------------------------

    def _on_print_on(self, key: str) -> None:
        window = self.window
        if (key == "sheet") == (window.label.sheet is not None):
            return
        if key == "page":
            window.set_sheet(None)
        else:
            # idle: the combo row's popover closes first; the sidebar is rebuilt
            GLib.idle_add(self._choose_sheet)

    def _choose_sheet(self) -> bool:
        self.window.choose_sheet()
        return False

    def _add_sheet_rows(self) -> None:
        window = self.window
        choice = window.label.sheet
        assert choice
        sheet = find(choice.id)
        row = Adw.ActionRow(title=GLib.markup_escape_text(choice.id), activatable=True)
        row.set_subtitle(
            GLib.markup_escape_text(sheet_summary(sheet, window.unit))
            if sheet
            else _("Not in the list of sheets of this version")
        )
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda _r: window.choose_sheet())
        self.sheet_row = row
        self.add(row)
        if not sheet:
            return
        positions = Adw.ActionRow(
            title=_("Positions"),
            subtitle=positions_text(choice.positions, sheet.count),
            activatable=True,
        )
        positions.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        positions.connect("activated", lambda _r: window.choose_positions())
        self.positions_row = positions
        self.add(positions)
        turned = Adw.SwitchRow(
            title=_("Turn the content"), subtitle=_("Read the label from its side")
        )
        turned.set_active(choice.rotated)
        turned.connect("notify::active", lambda r, _p: window.set_sheet_rotated(r.get_active()))
        self.add(turned)

    def _add_width_height(self, caps) -> None:
        width, height = self.window.label.media_size()
        max_width = caps.max_width_mm or MAX_SIZE_MM
        roll = not caps.pages  # a page has no roll to measure along
        self._width_row = self._length_row(
            _("Width"),
            min(width, max_width),
            MIN_SIZE_MM,
            max_width,
            0.5,
            lambda v: self._on_media_size(width=v),
            _("%(unit)s across the roll") if roll else None,
        )
        self._height_row = self._length_row(
            _("Height"),
            height,
            MIN_SIZE_MM,
            MAX_SIZE_MM,
            0.5,
            lambda v: self._on_media_size(height=v),
            _("%(unit)s along the roll") if roll else None,
        )
        self.add(self._width_row)
        self.add(self._height_row)

    def _add_roll_rows(self) -> None:
        lab, window = self.window.label, self.window
        self.add(
            self._length_row(
                _("Gap"),
                lab.gap_mm,
                0,
                MAX_GAP_MM,
                0.5,
                lambda v: window.change(gap_mm=v),
                _("%(unit)s between two labels on the roll"),
            )
        )
        self.add(
            combo_row(_("Content direction"), ORIENTATIONS, lab.orientation, self._on_orientation)
        )

    def _on_size_preset(self, key: str) -> None:
        if key == CUSTOM_SIZE or key == self._size_key():
            return
        self.window.label.set_media_size(*self._sizes[key])
        self._show_media_size()
        self.window.mark_changed()

    def _on_media_size(self, width: float | None = None, height: float | None = None) -> None:
        if self._syncing:
            return
        old_width, old_height = self.window.label.media_size()
        new = (old_width if width is None else width, old_height if height is None else height)
        self.window.label.set_media_size(*new)
        index = self._size_keys.index(self._size_key())
        if self._size_row.get_selected() != index:
            self._size_row.set_selected(index)
        self.window.mark_changed("label:media_size")  # spinning on: one step of undo

    def _show_media_size(self) -> None:
        """Width and height rows after the size was chosen from the list."""
        width, height = self.window.label.media_size()
        self._syncing = True  # rounding to the shown digits must not change the model
        try:
            for row, mm in ((self._width_row, width), (self._height_row, height)):
                row.set_value(self.window.unit.to_display(mm, row.get_digits()))
        finally:
            self._syncing = False

    def _on_orientation(self, orientation: str) -> None:
        self.window.label.set_orientation(orientation)
        self.window.mark_changed()
