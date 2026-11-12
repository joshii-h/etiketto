"""Small GTK helpers shared by the interface modules."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402


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


def combo_row(title: str, items, current: str, on_change) -> Adw.ComboRow:
    """A choice of (key, label) items; on_change(key) when another is chosen."""
    keys = [key for key, _label in items]
    model = Gtk.StringList.new([label for _key, label in items])
    row = Adw.ComboRow(title=title, model=model)
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
    return row


def grouped_combo_row(title: str, groups, current: str, on_change) -> Adw.ComboRow:
    """A choice of (key, label) items in groups of (heading, items), each
    group under its heading; on_change(key) when another is chosen."""
    keys = [key for _heading, items in groups for key, _label in items]
    sections = Gio.ListStore(item_type=Gio.ListModel)
    for _heading, items in groups:
        sections.append(Gtk.StringList.new([label for _key, label in items]))
    starts, position = {}, 0
    for heading, items in groups:
        starts[position] = heading
        position += len(items)

    def bind(_factory, header: Gtk.ListHeader):
        header.get_child().set_label(starts.get(header.get_start(), ""))

    headers = Gtk.SignalListItemFactory()
    headers.connect(
        "setup",
        lambda _f, header: header.set_child(
            Gtk.Label(xalign=0, css_classes=["heading"], margin_top=6, margin_bottom=3)
        ),
    )
    headers.connect("bind", bind)
    row = Adw.ComboRow(title=title, model=Gtk.FlattenListModel(model=sections))
    row.set_header_factory(headers)
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


def list_dialog(heading: str, rows, body: str = "") -> Adw.AlertDialog:
    """A dialog with a boxed list of rows; the caller adds the responses."""
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.set_extra_child(box)
    return dialog
