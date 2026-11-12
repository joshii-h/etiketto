"""The printer connection: status row, search, details and the printer settings group."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from etiketto.backends import backends, get_backend  # noqa: E402
from etiketto.backends.base import Choice, PrinterInfo, Setting, Toggle  # noqa: E402
from etiketto.ui.i18n import _, isolate, month_year  # noqa: E402
from etiketto.ui.texts import (  # noqa: E402
    CONNECTION_ERRORS,
    MEDIA_NAMES,
    backend_name,
    cassette_age_hint,
    choice_name,
    code_text,
    level_name,
    printer_error_text,
    setting_title,
)
from etiketto.ui.widgets import (  # noqa: E402
    combo_row,
    flat_button,
    length_row,
    list_dialog,
)
from etiketto.units import default_unit  # noqa: E402

if TYPE_CHECKING:
    from etiketto.ui.window import Window

# a measured label distance this far from the label's own is a different roll
PITCH_TOLERANCE_MM = 1.0


class Printer:
    """Connection to the printer of the window's label: query, search, status row."""

    # widgets of the current sidebar, set by group() before any status update
    row: Adw.ActionRow
    details_button: Gtk.Button
    hint: Adw.ActionRow
    _host_row: Adw.EntryRow

    def __init__(self, window: Window):
        self.window = window
        self.info: PrinterInfo | None = None
        self.generation = 0  # answers of older printer queries are dropped
        self.chooser: Adw.AlertDialog | None = None  # the open "Choose a printer" dialog

    @property
    def host(self) -> str | None:
        return self.window.printer_settings.host

    # --- sidebar group ---------------------------------------------------------------

    def group(self) -> Adw.PreferencesGroup:
        window = self.window
        group = Adw.PreferencesGroup(title=_("Printer"))
        if len(backends()) > 1:
            group.add(self._backend_row())
        self._add_status_rows(group)

        self._host_row = Adw.EntryRow(title=_("Host name or IP address"))
        self._host_row.set_text(self.host or "")
        self._host_row.set_show_apply_button(True)
        self._host_row.connect("apply", self._on_host_apply)
        group.add(self._host_row)
        for setting in window.backend.capabilities.settings:
            group.add(self._setting_row(setting))

        group.add(
            length_row(
                window.unit,
                _("Safety margin"),
                window.printer_settings.safe_mm,
                0,
                10,  # office printers leave 4 to 5 mm of a page blank
                0.1,
                self._on_safe_margin,
                _(
                    "%(unit)s kept free for content and border; the background is printed edge to edge"
                ),
            )
        )

        auto_name = _("Millimetres") if default_unit() == "mm" else _("Inches")
        group.add(
            combo_row(
                _("Units"),
                [
                    ("auto", _("Automatic (%s)") % auto_name),
                    ("mm", _("Millimetres")),
                    ("in", _("Inches")),
                ],
                window.settings.units or "auto",
                window.set_units,
            )
        )
        return group

    def _backend_row(self) -> Adw.ComboRow:
        items = [(b.id, backend_name(b)) for b in backends()]
        return combo_row(
            _("Printer model"), items, self.window.backend.id, self.window.set_backend
        )

    def _setting_row(self, setting: Setting) -> Adw.PreferencesRow:
        """A row for one of the backend's own settings, kept in settings.json."""
        key, stored = setting.key, self.window.printer_settings.get(setting.key)
        if isinstance(setting, Toggle):
            row = Adw.SwitchRow(title=setting_title(key), active=setting.value(stored))
            row.connect("notify::active", lambda r, _p: self._on_setting(key, r.get_active()))
            return row
        if isinstance(setting, Choice):
            items = [(value, choice_name(value)) for value in setting.choices]
            current = setting.value(stored)
            return combo_row(
                setting_title(key), items, current, lambda v: self._on_setting(key, v)
            )
        level = setting.value(stored)
        values = sorted(set(setting.steps()) | ({level} if level is not None else set()))
        # a value off the steps (settings.json edited by hand) is shown as it is
        steps = [("", level_name(key, None))] + [(str(v), level_name(key, v)) for v in values]
        return combo_row(
            setting_title(key),
            steps,
            "" if level is None else str(level),
            lambda v: self._on_setting(key, int(v) if v else None),
        )

    def _on_setting(self, key: str, value) -> None:
        # a printer setting, not part of the label: no dirty flag
        self.window.printer_settings.update(**{key: value})
        if key == "dpi":  # the preview at the resolution the label is printed at
            self.window.schedule_render()

    def _add_status_rows(self, group: Adw.PreferencesGroup) -> None:
        self.row = Adw.ActionRow(title=_("Searching…"))
        self.row.set_subtitle_lines(3)
        self.details_button = flat_button(
            "dialog-information-symbolic", _("Printer details"), self.show_details
        )
        self.row.add_suffix(self.details_button)
        self.row.add_suffix(flat_button("view-refresh-symbolic", _("Refresh"), self.refresh))
        self.row.add_suffix(
            flat_button(
                "system-search-symbolic",
                _("Search for printers"),
                self.discover,
            )
        )
        group.add(self.row)
        self.hint = Adw.ActionRow(visible=False, activatable=False)
        self.hint.set_title_lines(0)
        self.hint.add_css_class("dim-label")
        self.hint.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        group.add(self.hint)
        self.update_row()

    def _on_host_apply(self, row: Adw.EntryRow) -> None:
        self.window.printer_settings.update(host=row.get_text().strip())
        self.refresh()

    def _on_safe_margin(self, value: float) -> None:
        # a printer setting, not part of the label: no dirty flag
        self.window.printer_settings.update(safe_mm=round(value, 1))
        self.window.schedule_render()

    # --- search and connection ---------------------------------------------------------

    def discover(self) -> None:
        """Look for printers of the current model on the network."""
        generation = self._next_generation()
        self.update_row(_("Searching for printers…"), "")
        backend = self.window.backend

        def work():
            found = backend.discover()
            GLib.idle_add(self.discovered, found, generation)

        threading.Thread(target=work, name="etiketto-discover", daemon=True).start()

    def discovered(self, found, generation: int | None = None) -> bool:
        if self._outdated(generation) or not self._known_printer():
            return False
        if not found:
            self.update_row(
                _("No printer found"),
                _(
                    "Make sure the printer is switched on and in the same network, or enter its address below"
                ),
            )
            return False
        if len(found) == 1:  # several: let the user choose, never guess
            self._use(found[0].host)
            self.window.toast(_("Found %s") % found[0].name)
            return False
        self._choose(found)
        if self.host:
            self.refresh()
        else:
            # refresh() would search again and take the first printer,
            # overriding the choice the user is about to make
            self.update_row(_("Choose a printer"), "")
        return False

    def _choose(self, found) -> None:
        if self.chooser is not None:  # never two choosers at once
            self.chooser.force_close()
        rows = [
            Adw.ActionRow(
                title=GLib.markup_escape_text(printer.name),
                subtitle="%s · %s" % (isolate(printer.host), isolate(printer.address)),
                activatable=True,
            )
            for printer in found
        ]
        dialog = self.chooser = list_dialog(_("Choose a printer"), rows)
        dialog.connect("closed", self.chooser_closed)
        for row, printer in zip(rows, found, strict=True):
            row.connect("activated", self._on_chosen, dialog, printer.host)
        dialog.add_response("cancel", _("Cancel"))
        self.window.present_dialog(dialog)

    def _on_chosen(self, _row, dialog, host: str) -> None:
        dialog.close()
        self._use(host)

    def chooser_closed(self, dialog) -> None:
        if self.chooser is dialog:
            self.chooser = None

    def _use(self, host: str) -> None:
        self.window.printer_settings.update(host=host)
        self._host_row.set_text(host)  # not a rebuild: open rows and input stay
        self.refresh()

    def refresh(self) -> None:
        host = self.host
        if not host:
            # nothing configured yet: look for one on the network
            self.discover()
            return
        generation = self._next_generation()
        backend = self.window.backend
        self.update_row(_("Connecting…"), isolate(host))

        def work():
            try:
                info = backend.query(host)
            except Exception as e:  # noqa: BLE001 - shown in the status row
                GLib.idle_add(self.failed, e, generation)
            else:
                GLib.idle_add(self.found, info, generation)

        threading.Thread(target=work, name="etiketto-query", daemon=True).start()

    def _next_generation(self) -> int:
        self.generation += 1  # a pending status answer or search is outdated now
        return self.generation

    def _known_printer(self) -> bool:
        """False for a window whose printer model is gone (a late answer to
        a window of a test that registered a model of its own)."""
        return self.window.label.printer in {backend.id for backend in backends()}

    def _outdated(self, generation: int | None) -> bool:
        return generation is not None and generation != self.generation

    def found(self, info: PrinterInfo, generation: int | None = None) -> bool:
        if self._outdated(generation):
            return False
        self.info = info
        self.update_row()
        window = self.window
        # the printer's margins (labels that reach into them), its resolution
        # (reported now, or by a raw printer's query): the preview and its
        # code warnings at the resolution the label is printed at
        if window.sheet or window.print_dpi() != window.rendered_dpi:
            window.schedule_render()
        widths = window.backend.capabilities.tape_widths_mm
        fresh = window.adopts_tape and window.path is None and not window.dirty
        tape = self.other_tape()
        if fresh and tape in widths:
            # take over the inserted tape; the row's handler renders, but
            # this is no edit by the user
            window.label_group.tape_row.set_selected(widths.index(tape))
            window.dirty = False
            window.update_title()
        self._offer_other_media(info)
        return False

    def _offer_other_media(self, info: PrinterInfo) -> None:
        """The loaded media fits another printer model entry (die-cut labels
        instead of a continuous roll): offer to switch to it."""
        other = info.media_backend
        if not other or other == self.window.backend.id:
            return
        try:
            backend = get_backend(other)
        except KeyError:
            return
        self.window.offer(
            _("The loaded roll is for “%s”") % backend_name(backend),
            _("Switch"),
            lambda: self.window.set_backend(other),
        )

    # --- does the loaded material fit the label? ----------------------------------------

    def other_tape(self) -> float | None:
        """Tape printers: the inserted tape width if the label has another one."""
        info, label = self.info, self.window.label
        if not self.window.backend.capabilities.continuous or not info or not info.tape_mm:
            return None
        return info.tape_mm if info.tape_mm != label.tape_mm else None

    def other_label_size(self) -> tuple[float, float] | None:
        """Die-cut printers: the loaded label size if it is not the label's."""
        info, label = self.info, self.window.label
        if self.window.backend.capabilities.continuous or not info or not info.label_mm:
            return None
        size = label.media_size()
        differs = any(
            abs(a - b) > PITCH_TOLERANCE_MM for a, b in zip(info.label_mm, size, strict=True)
        )
        return info.label_mm if differs else None

    def other_pitch(self) -> float | None:
        """Die-cut printers: the measured label distance if it is not the label's."""
        info, label = self.info, self.window.label
        if self.window.backend.capabilities.continuous or not info or not info.pitch_mm:
            return None
        differs = abs(info.pitch_mm - label.pitch_mm()) > PITCH_TOLERANCE_MM
        return info.pitch_mm if differs else None

    def failed(self, error: Exception, generation: int | None = None) -> bool:
        if self._outdated(generation):
            return False
        self.info = None
        code = getattr(error, "code", "generic")
        title = (
            _("Printer not reachable")
            if code in CONNECTION_ERRORS
            else _("Printer needs attention")
        )
        self.update_row(title, printer_error_text(error, self.window.unit))
        return False

    # --- status row --------------------------------------------------------------------

    def update_row(self, title: str | None = None, subtitle: str | None = None) -> None:
        info = self.info
        connected = False
        if title is None and info is not None:
            # the printer itself reports a codename (the VC-500W says "Wedge")
            title, subtitle = self.window.backend.display_name(info), self._summary(info)
            if info.error:  # reachable, but it reports a problem
                title = _("Printer needs attention")
            connected = True
        self.row.set_title(GLib.markup_escape_text(title or _("Searching…")))
        self.row.set_subtitle(GLib.markup_escape_text(subtitle or ""))
        self.details_button.set_sensitive(connected)
        hint = cassette_age_hint(info) if connected else None
        self.hint.set_title(GLib.markup_escape_text(hint or ""))
        self.hint.set_visible(hint is not None)

    def _remaining_text(self, info: PrinterInfo) -> str | None:
        if not self.window.backend.capabilities.reports_remaining:
            return None
        if info.remaining_mm is None or not info.total_mm:
            return None
        unit = self.window.unit
        return _("%(left)s of %(total)s left") % {
            "left": unit.format_long(info.remaining_mm),
            "total": unit.format_long(info.total_mm),
        }

    def _summary(self, info: PrinterInfo) -> str:
        unit = self.window.unit
        caps = self.window.backend.capabilities
        if info.error:
            return code_text(info.error, unit)
        if info.state == "UNKNOWN":
            return _("Reachable · the printer reports no status")
        if not caps.continuous or not caps.cassettes:
            if info.state == "BUSY":
                return _("Printing")
            if info.pitch_mm:
                return _("Ready · a label every %s") % unit.format(info.pitch_mm)
            return _("Ready")
        roll = self.window.backend.capabilities.media == "roll"
        if not info.tape_mm:
            return _("No roll loaded") if roll else _("No cassette inserted")
        name = isolate(unit.tape_name(info.tape_mm))
        parts = [_("%s roll") % name if roll else _("%s cassette") % name]
        remaining = self._remaining_text(info)
        if remaining:
            parts.append(remaining)
        return " · ".join(parts)

    # --- details -----------------------------------------------------------------------

    def details(self) -> list[tuple[str, str]]:
        """(title, value) pairs for the details dialog; empty values left out."""
        info = self.info
        if info is None:
            return []
        rows = self._printer_rows(info) + self._cassette_rows(info)
        return [(title, value) for title, value in rows if value]

    def _printer_rows(self, info: PrinterInfo) -> list[tuple[str, str]]:
        unit = self.window.unit
        return [
            (_("Model"), self.window.backend.display_name(info)),
            (_("Address"), isolate(self.host) if self.host else ""),
            (_("Firmware"), info.firmware),
            self._media_row(info),
            (_("Label distance"), unit.format(info.pitch_mm) if info.pitch_mm else ""),
            (_("Remaining tape"), self._remaining_text(info) or ""),
        ]

    def _media_row(self, info: PrinterInfo) -> tuple[str, str]:
        """The loaded cassette or roll; die-cut labels by their size."""
        unit = self.window.unit
        if info.label_mm:
            return _("Roll"), isolate(unit.size(*info.label_mm))
        value = isolate(unit.tape_name(info.tape_mm)) if info.tape_mm else ""
        if self.window.backend.capabilities.media == "roll":
            return _("Roll"), value
        return _("Cassette"), value

    def _cassette_rows(self, info: PrinterInfo) -> list[tuple[str, str]]:
        cassette = info.cassette
        if not cassette:
            return []
        printable = self.window.unit.format(cassette.printable_mm) if cassette.printable_mm else ""
        made = cassette.made
        return [
            (_("Tape type"), MEDIA_NAMES.get(cassette.media, "")),
            (_("Printable width"), printable),
            (_("Lot number"), isolate(cassette.lot) if cassette.lot else ""),
            (_("Serial number"), isolate(cassette.serial) if cassette.serial else ""),
            # the production date is read from the serial number: a guess
            (_("Probably made"), month_year(made.month, made.year) if made else ""),
        ]

    def show_details(self) -> None:
        details = self.details()
        if not details:
            return
        rows = []
        for title, value in details:
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value))
            row.add_css_class("property")
            row.set_subtitle_selectable(True)
            rows.append(row)
        dialog = list_dialog(_("Printer details"), rows, cassette_age_hint(self.info) or "")
        dialog.add_response("close", _("Close"))
        self.window.present_dialog(dialog)

    def description(self) -> str | None:
        """For bug reports: model, cassette, state and firmware (in English)."""
        info = self.info
        if not info:
            return None
        caps = self.window.backend.capabilities
        if caps.continuous and not caps.cassettes:
            media = "%g mm paper" % self.window.label.tape_mm
        elif caps.continuous:
            media = "%s cassette" % ("%g mm" % info.tape_mm if info.tape_mm else "no")
        else:
            media = "label every %s mm" % ("%g" % info.pitch_mm if info.pitch_mm else "?")
        parts = [self.window.backend.display_name(info), media, info.state]
        if info.firmware:
            parts.append("firmware " + info.firmware)
        return ", ".join(parts)
