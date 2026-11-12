"""Keyboard shortcuts of the label editor and the window that lists them.

The editing shortcuts (undo, the current element) are handled after the
widget with the focus had its turn (bubble phase): in a text field Ctrl+Z
undoes the typing there, Delete deletes a character; elsewhere they act
on the label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tavletta.ui.i18n import _  # noqa: E402

if TYPE_CHECKING:
    from tavletta.ui.window import Window

# action, accelerators (handled in the window's bubble phase)
EDITING = (
    ("win.undo", ("<Control>z",)),
    ("win.redo", ("<Control><Shift>z", "<Control>y")),
    ("win.add-text", ("<Control><Shift>t",)),
    ("win.add-image", ("<Control><Shift>g",)),
    ("win.add-symbol", ("<Control><Shift>y",)),
    ("win.add-code", ("<Control><Shift>b",)),
    ("win.add-shape", ("<Control><Shift>l",)),
    ("win.duplicate-element", ("<Control>d",)),
    ("win.remove-element", ("Delete",)),
    ("win.element-up", ("<Alt>Up",)),
    ("win.element-down", ("<Alt>Down",)),
)


def sections() -> list[tuple[str, list[tuple[str, str]]]]:
    """What the shortcuts window lists: (heading, [(title, accelerator)])."""
    return [
        (
            _("Label"),
            [
                (_("New label"), "<Control>n"),
                (_("Open"), "<Control>o"),
                (_("Save"), "<Control>s"),
                (_("Save as…"), "<Control><Shift>s"),
                (_("Print"), "<Control>p"),
                (_("Undo"), "<Control>z"),
                (_("Redo"), "<Control><Shift>z"),
            ],
        ),
        (
            _("Content"),
            [
                (_("Add text"), "<Control><Shift>t"),
                (_("Add an image"), "<Control><Shift>g"),
                (_("Add a symbol"), "<Control><Shift>y"),
                (_("Add a code"), "<Control><Shift>b"),
                (_("Add a line or shape"), "<Control><Shift>l"),
                (_("Duplicate the open element"), "<Control>d"),
                (_("Remove the open element"), "Delete"),
                (_("Move the open element up"), "<Alt>Up"),
                (_("Move the open element down"), "<Alt>Down"),
            ],
        ),
        (
            _("Serial labels"),
            [
                (_("First row"), "<Alt>Home"),
                (_("Previous row"), "<Alt>Page_Up"),
                (_("Next row"), "<Alt>Page_Down"),
                (_("Last row"), "<Alt>End"),
            ],
        ),
        (
            _("General"),
            [
                (_("Keyboard shortcuts"), "<Control>question"),
                (_("Help"), "F1"),
            ],
        ),
    ]


def install(window: Window) -> Gtk.ShortcutController:
    """The editing shortcuts on the window, in the bubble phase."""
    controller = Gtk.ShortcutController()
    controller.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
    for action, accels in EDITING:
        trigger = Gtk.ShortcutTrigger.parse_string("|".join(accels))
        controller.add_shortcut(Gtk.Shortcut.new(trigger, Gtk.NamedAction.new(action)))
    window.add_controller(controller)
    return controller


def dialog() -> Adw.Dialog:
    """The shortcuts window (libadwaita's, from version 1.8)."""
    shortcuts = Adw.ShortcutsDialog()
    for heading, items in sections():
        section = Adw.ShortcutsSection(title=heading)
        for title, accel in items:
            section.add(Adw.ShortcutsItem.new(title, accel))
        shortcuts.add(section)
    return shortcuts


def available() -> bool:
    """Older libadwaita has no shortcuts window: the menu leaves it out."""
    return hasattr(Adw, "ShortcutsDialog")
