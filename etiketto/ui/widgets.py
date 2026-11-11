"""Small GTK helpers shared by the interface modules."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from etiketto.ui.i18n import _  # noqa: E402


def rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "#%02x%02x%02x" % (
        round(rgba.red * 255),
        round(rgba.green * 255),
        round(rgba.blue * 255),
    )


def hex_to_rgba(value: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not rgba.parse(value):
        rgba.parse("#000000")
    return rgba


def string_list(items) -> Gtk.StringList:
    return Gtk.StringList.new([label for _key, label in items])


def combo_row(title: str, items, current: str, on_change) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, model=string_list(items))
    keys = [key for key, _label in items]
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
    return row


def color_button(value: str, on_change) -> Gtk.ColorDialogButton:
    button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
    button.set_rgba(hex_to_rgba(value))
    button.set_valign(Gtk.Align.CENTER)
    button.connect("notify::rgba", lambda b, _p: on_change(rgba_to_hex(b.get_rgba())))
    return button


def spin_row(title, value, lower, upper, step, digits, on_change, subtitle=None) -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lower, upper, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_digits(digits)
    row.set_value(value)
    row.connect("notify::value", lambda r, _p: on_change(r.get_value()))
    return row


def length_row(
    unit, title, mm, lower_mm, upper_mm, step_mm, on_change_mm, subtitle=None
) -> Adw.SpinRow:
    """A spin row that shows a millimetre value in the chosen unit."""
    digits = unit.digits if step_mm < 1 or unit.key == "in" else 0
    return spin_row(
        title,
        unit.to_display(mm, digits),
        unit.to_display(lower_mm, digits),
        unit.to_display(upper_mm, digits),
        unit.step(step_mm),
        digits,
        lambda v: on_change_mm(round(unit.from_display(v), 3)),
        subtitle=(subtitle % {"unit": unit.symbol}) if subtitle else unit.symbol,
    )


def flat_button(icon_name: str, tooltip: str, on_click) -> Gtk.Button:
    """A flat icon button, vertically centred in its row."""
    button = Gtk.Button(icon_name=icon_name, tooltip_text=tooltip)
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.connect("clicked", lambda _b: on_click())
    return button


def boxed_list(rows) -> Gtk.ListBox:
    """A list box in the boxed-list style (for dialogs), without selection."""
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    return box


def confirm_dialog(
    heading: str,
    body: str,
    confirm_id: str,
    confirm_label: str,
    on_confirm,
    appearance=Adw.ResponseAppearance.DESTRUCTIVE,
) -> Adw.AlertDialog:
    """A Cancel / confirm dialog; on_confirm() runs only for the confirm response.
    The caller presents it."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response(confirm_id, confirm_label)
    dialog.set_response_appearance(confirm_id, appearance)
    dialog.connect("response", lambda _d, response: response == confirm_id and on_confirm())
    return dialog
