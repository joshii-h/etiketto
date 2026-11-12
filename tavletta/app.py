"""GTK4 / libadwaita application: startup, activation, About, screenshot aids."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from tavletta import __version__  # noqa: E402
from tavletta.backends.base import PrinterInfo  # noqa: E402
from tavletta.ui.i18n import _, translation_is_rtl  # noqa: E402
from tavletta.ui.window import Window  # noqa: E402

APP_ID = "xyz.joshuahirsig.Tavletta"

# what Tavletta ships or derives from, for the About dialog: (name,
# copyright as the project states it, license, text of a custom license);
# THIRD_PARTY_NOTICES.md has the details and the license texts
LEGAL: tuple[tuple[str, str, Gtk.License, str | None], ...] = (
    ("labelprinter", "© 2021 Andrea Micheloni and contributors", Gtk.License.AGPL_3_0, None),
    ("gLabels templates", "© 2001-2026 Jaye Evins", Gtk.License.MIT_X11, None),
    (
        "Material Design Icons",
        "© Austin Andrews and contributors (Pictogrammers)",
        Gtk.License.APACHE_2_0,
        None,
    ),
    ("ThermalPrint", "© 2026 Alexis Rios-Negron", Gtk.License.MIT_X11, None),
    ("Zint", "© 2009-2025 Robin Stuart and contributors", Gtk.License.BSD_3, None),
    ("zint-bindings", "© 2024-2025 Anna Zhukova", Gtk.License.APACHE_2_0, None),
    ("segno", "© 2016-2025 Lars Heuer", Gtk.License.BSD_3, None),
    (
        "python-barcode",
        "© 2010-2013 Thorsten Weimann, © 2017-2023 Hugo Osvaldo Barrera et al.",
        Gtk.License.MIT_X11,
        None,
    ),
    (
        "Pillow",
        "© 1997-2011 Secret Labs AB, © 1995-2011 Fredrik Lundh and contributors, "
        "© 2010 Jeffrey 'Alex' Clark and contributors",
        Gtk.License.CUSTOM,
        "MIT-CMU (HPND)",
    ),
)
# bundled in the Flatpak only (elsewhere the distribution's): Xpdf's code
# GPL-2.0-only OR GPL-3.0-only, Poppler's additions GPL-2.0-or-later
FLATPAK_LEGAL: tuple[tuple[str, str, Gtk.License, str | None], ...] = (
    (
        "Poppler and poppler-data",
        "© The Poppler developers, © 1996-2011 Glyph &amp; Cog, LLC, © 1990-2019 Adobe",
        Gtk.License.CUSTOM,
        "GPL-2.0-only OR GPL-3.0-only; BSD-3-Clause (poppler-data CMaps)",
    ),
)


def legal_sections() -> tuple[tuple[str, str, Gtk.License, str | None], ...]:
    """What the About dialog credits: Poppler only where Tavletta ships it."""
    flatpak = bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()
    return LEGAL + FLATPAK_LEGAL if flatpak else LEGAL


PROTOCOL_SOURCES = (
    "LPrint",
    "libcups",
    "PAPPL",
    "libcupsfilters",
    "brother_ql",
    "brother-ql-next",
    "ptouch-print",
    "ptouch",
    "niimbluelib",
    "Thermalith",
    "barnguard",
    "m7i.org",
)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # the window icon in the title bar (layouts like "icon:minimize,maximize,close")
        Gtk.Window.set_default_icon_name(APP_ID)
        if translation_is_rtl():
            Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        css = Gtk.CssProvider()
        css.load_from_string(
            ".label-preview { border-radius: 0; box-shadow: 0 1px 4px alpha(black, 0.5); }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        window = self.get_active_window() or Window(self)
        window.present()
        self._maybe_screenshot(window)

    # development aid only, driven by tools/screenshots.sh: not part of the tests
    def _maybe_screenshot(self, window):  # pragma: no cover
        """Development aid for README screenshots:
        TAVLETTA_SCREENSHOT=out.png [TAVLETTA_SCREENSHOT_STATE=expand|print]"""
        shot = GLib.getenv("TAVLETTA_SCREENSHOT")
        if not shot:
            return
        state = GLib.getenv("TAVLETTA_SCREENSHOT_STATE") or ""
        delay = int(GLib.getenv("TAVLETTA_SCREENSHOT_DELAY") or "3000")
        # GNOME defaults, whatever the developer's desktop uses
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-decoration-layout", ":close")
        settings.set_property("gtk-font-name", "Sans 11")

        def prepare():
            # a believable printer instead of the network
            if window.backend.capabilities.continuous:
                tape = window.label.tape_mm
                window.printer.info = PrinterInfo("Wedge", tape, 4120, 5000, "IDLE")
            else:
                pitch = window.label.pitch_mm()
                window.printer.info = PrinterInfo(
                    "EOS2/300", None, None, None, "IDLE", pitch_mm=pitch
                )
            window.printer.update_row()
            if state in ("expand", "styles"):
                window.expand_index = len(window.label.elements) - 1
                window.rebuild_sidebar()
                if state == "styles":  # the last element's box and frame options open
                    self._open_page(window.elements_group, _("Box and frame"))

                def scroll_down():
                    child = window.sidebar.get_first_child()
                    while child and not isinstance(child, Gtk.ScrolledWindow):
                        child = child.get_first_child()
                    if child:
                        adj = child.get_vadjustment()
                        adj.set_value(adj.get_upper())
                    return False

                GLib.timeout_add(500, scroll_down)
            elif state == "print":
                window.print_flow.dialog()

                # once the dialog is mapped, clear the selected text in the copies field
                def unselect(widget=None):
                    widget = widget or window.last_dialog
                    child = widget.get_first_child() if widget else None
                    while child:
                        if isinstance(child, Gtk.Text):
                            child.select_region(0, 0)
                        unselect(child)
                        child = child.get_next_sibling()
                    return False

                GLib.timeout_add(400, unselect)
            return False

        GLib.timeout_add(max(500, delay - 1500), prepare)
        GLib.timeout_add(delay, self._screenshot, window, shot)

    def _open_page(self, widget, title):  # pragma: no cover - see _maybe_screenshot
        """Open the page of options of that title (of the last element)."""
        rows = []
        child_stack = [widget]
        while child_stack:
            node = child_stack.pop(0)
            child = node.get_first_child()
            while child:
                if isinstance(child, Adw.ActionRow) and child.get_title() == title:
                    rows.append(child)
                child_stack.append(child)
                child = child.get_next_sibling()
        if rows:
            rows[-1].emit("activated")

    def _screenshot(self, window, path):  # pragma: no cover - see _maybe_screenshot
        paintable = Gtk.WidgetPaintable.new(window)
        width, height = window.get_width(), window.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node:
            texture = window.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(path)
        self.quit()
        return False

    def do_open(self, files, _n_files, _hint):
        window = self.get_active_window() or Window(self)
        if files and files[0].get_path():
            path = Path(files[0].get_path())
            window.files.confirm_discard(lambda: window.files.open_file(path))
        window.present()
        self._maybe_screenshot(window)

    def on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name="Tavletta",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Joshua Hirsig",
            license_type=Gtk.License.AGPL_3_0,
            comments=_("Design and print labels")
            + "\n\n"
            + _(
                "Tavletta is Romansh for a small tablet (from tavla, table/board). Pronounced tav-LET-ta."
            ),
            website="https://git.joshuahirsig.xyz/joshii/tavletta",
            issue_url="https://git.joshuahirsig.xyz/joshii/tavletta/issues",
            copyright="© 2026 Joshua Hirsig",
            # translators: replace with your name to be credited in the About dialog
            translator_credits=_("translator-credits"),
        )
        for title, holder, license_type, text in legal_sections():
            about.add_legal_section(title, holder, license_type, text)
        about.add_legal_section(
            "labelburg",
            None,
            Gtk.License.CUSTOM,
            GLib.markup_escape_text(_("Ideas for the variables (counter, date, UUID), no code")),
        )
        about.add_legal_section(
            _("Protocol knowledge"),
            None,
            Gtk.License.CUSTOM,
            GLib.markup_escape_text(
                _(
                    "From %(projects)s and the manufacturers' manuals, written anew without "
                    "their code; see THIRD_PARTY_NOTICES.md"
                )
                % {"projects": ", ".join(PROTOCOL_SOURCES)}
            ),
        )
        about.present(self.get_active_window())


# entry points: running the real application is not part of the tests
def main() -> int:  # pragma: no cover
    return App().run(sys.argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
