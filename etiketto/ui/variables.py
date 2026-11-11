"""The "Insert variable" button of the text and code editors, and the counter rows."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from etiketto.model import MAX_COUNTER, MAX_COUNTER_STEP, Label  # noqa: E402
from etiketto.ui.i18n import _, docs_url, isolate  # noqa: E402
from etiketto.ui.widgets import spin_row  # noqa: E402
from etiketto.variables import uses_counter  # noqa: E402


def variable_items() -> list[tuple[str, str]]:
    """(name, placeholder) of the variables the menu offers; the help page
    lists the formats and offsets."""
    return [
        (_("Date"), "{=date}"),
        (_("Best before (in 30 days)"), "{=date+30d}"),
        (_("Time"), "{=time}"),
        (_("Counter"), "{=counter}"),
        (_("Counter with leading zeros"), "{=counter:0001}"),
        (_("Copy number"), "{=copy}"),
        (_("Number of copies"), "{=copies}"),
        (_("Random ID"), "{=id}"),
        ("UUID", "{=uuid}"),
    ]


def variable_button(insert: Callable[[str], None], open_url: Callable[[str], None]):
    """A menu button that offers the variables; insert(placeholder) puts one
    at the cursor, open_url(url) opens the help."""
    button = Gtk.MenuButton(
        icon_name="insert-text-symbolic",
        tooltip_text=_("Insert variable"),
        valign=Gtk.Align.CENTER,
    )
    button.add_css_class("flat")
    popover = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    rows.add_css_class("boxed-list")
    items = variable_items()
    for title, placeholder in items:
        rows.append(Adw.ActionRow(title=title, subtitle=isolate(placeholder), activatable=True))

    def chosen(_list, row) -> None:
        popover.popdown()
        insert(items[row.get_index()][1])

    rows.connect("row-activated", chosen)
    box.append(rows)

    help_text = Gtk.Label(
        label=_(
            "Variables are filled in when printing. Every printed label gets the next "
            "counter value, also from copy to copy. The preview shows the values of the "
            "next print."
        ),
        wrap=True,
        max_width_chars=40,
        xalign=0,
    )
    help_text.add_css_class("dim-label")
    box.append(help_text)
    more = Gtk.Button(label=_("More about variables"))
    more.add_css_class("flat")

    def help_page(_button) -> None:
        popover.popdown()
        open_url(docs_url() + "use/#variables")

    more.connect("clicked", help_page)
    box.append(more)

    popover.set_child(box)
    button.set_popover(popover)
    return button


def insert_into_text_view(view: Gtk.TextView, placeholder: str) -> None:
    view.get_buffer().insert_at_cursor(placeholder)
    view.grab_focus()


def insert_into_entry(entry: Gtk.Editable, placeholder: str) -> None:
    text, position = entry.get_text(), entry.get_position()
    entry.set_text(text[:position] + placeholder + text[position:])
    entry.set_position(position + len(placeholder))


class CounterGroup(Adw.PreferencesGroup):
    """Next number and step of {=counter}; shown while the label uses it."""

    def __init__(self, label: Label, changed: Callable[[], None]):
        super().__init__(
            title=_("Counter"), description=_("Goes up with every printed label, by the step")
        )
        self.label = label
        self._changed = changed
        self.next_row = spin_row(
            _("Next number"), label.counter_next, 0, MAX_COUNTER, 1, 0, self._set_next
        )
        self.step_row = spin_row(
            _("Step"), label.counter_step, 1, MAX_COUNTER_STEP, 1, 0, self._set_step
        )
        self.add(self.next_row)
        self.add(self.step_row)
        self.update()

    def _set_next(self, value: float) -> None:
        if int(value) != self.label.counter_next:
            self.label.counter_next = int(value)
            self._changed()

    def _set_step(self, value: float) -> None:
        if int(value) != self.label.counter_step:
            self.label.counter_step = int(value)
            self._changed()

    def update(self) -> None:
        """Shown while the label uses {=counter}, with the label's values
        (printing moves the counter on)."""
        self.set_visible(uses_counter(self.label))
        self.next_row.set_value(self.label.counter_next)
