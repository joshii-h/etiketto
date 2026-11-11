"""From the Print button to the result: checks, dialog, job, progress."""

from __future__ import annotations

import copy
import threading

from gi.repository import Adw, GLib, Gtk

from etiketto.backends.base import (
    ColorAdjust,
    PrinterInfo,
    PrintOptions,
    PrintResult,
    Progress,
)
from etiketto.ui.i18n import _, isolate
from etiketto.ui.texts import (
    COLOR_ADJUST_ROWS,
    CUT_MODES,
    MODE_NAMES,
    printer_error_text,
    stage_text,
)
from etiketto.ui.widgets import boxed_list, combo_row, confirm_dialog, spin_row

# a measured label distance this far from the label's own is a different roll
PITCH_TOLERANCE_MM = 1.0


class PrintFlow:
    """From the Print button to the result: checks, dialog, job, progress."""

    def __init__(self, window):
        self.window = window
        self.busy = False

    @property
    def mode(self) -> str:
        """The print mode of the last print (what the preview shows)."""
        return self.choices()["mode"]

    # --- checks ----------------------------------------------------------------

    def on_print(self, *_args) -> None:
        window = self.window
        if self.busy or not window.rendered:
            return
        if not window.label.elements and not window.label.background_image:
            window.toast(_("The label is empty"))
            return
        info = window.printer.info
        if self._wrong_cassette(info):
            self._wrong_cassette_dialog(info)
        elif self.pitch_differs():
            self._wrong_labels_dialog(info)
        else:
            self.dialog()

    def _wrong_cassette(self, info: PrinterInfo | None) -> bool:
        window = self.window
        return bool(
            window.backend.capabilities.continuous
            and info
            and info.tape_mm
            and info.tape_mm != window.label.tape_mm
        )

    def pitch_differs(self) -> bool:
        window = self.window
        info = window.printer.info
        if window.backend.capabilities.continuous or not info or not info.pitch_mm:
            return False
        return abs(info.pitch_mm - window.label.pitch_mm()) > PITCH_TOLERANCE_MM

    def _warn(self, heading: str, body: str) -> None:
        dialog = confirm_dialog(heading, body, "print", _("Print anyway"), self.dialog)
        self.window.present_dialog(dialog)

    def _wrong_cassette_dialog(self, info: PrinterInfo) -> None:
        unit, label = self.window.unit, self.window.label
        self._warn(
            _("Wrong cassette"),
            _(
                "The label is designed for %(label)s tape, but a %(tape)s cassette is "
                "inserted. The printer would scale it to the tape."
            )
            % {
                "label": isolate(unit.tape_name(label.tape_mm)),
                "tape": isolate(unit.tape_name(info.tape_mm)),
            },
        )

    def _wrong_labels_dialog(self, info: PrinterInfo) -> None:
        unit, label = self.window.unit, self.window.label
        self._warn(
            _("Different labels loaded"),
            _(
                "The label is %(size)s with a gap of %(gap)s, so a new one starts every "
                "%(pitch)s. The printer measured a label every %(measured)s. Check the "
                "label size and the gap."
            )
            % {
                "size": isolate(unit.size(*label.media_size())),
                "gap": isolate(unit.format(label.gap_mm)),
                "pitch": isolate(unit.format(label.pitch_mm())),
                "measured": isolate(unit.format(info.pitch_mm)),
            },
        )

    # --- print dialog ------------------------------------------------------------

    def _on_print_mode(self, choices: dict, mode: str) -> None:
        choices.update(mode=mode)
        self.window.printer_settings.update(mode=mode)
        self.window.schedule_render()  # black and white shows grey

    def choices(self) -> dict:
        """Copies, cut and mode for the print dialog, from the last print."""
        window = self.window
        caps = window.backend.capabilities
        cut = window.printer_settings.get("cut", "full")
        modes = [m.id for m in window.backend.modes(window.printer.info)]
        mode = window.printer_settings.get("mode")
        return {
            "copies": 1,
            "cut": cut if cut in caps.cut_modes else (caps.cut_modes or ("",))[0],
            "mode": mode if mode in modes else modes[0],
        }

    def rows(self, choices: dict) -> list[Gtk.Widget]:
        """Only what the printer can do: no cut row for printers without a
        cutter, no mode row with a single mode, colour correction if supported."""
        window = self.window
        caps = window.backend.capabilities
        rows: list[Gtk.Widget] = [
            spin_row(_("Copies"), 1, 1, 99, 1, 0, lambda v: choices.update(copies=int(v)))
        ]
        if caps.cut_modes:
            cuts = [(key, name) for key, name in CUT_MODES if key in caps.cut_modes]
            rows.append(combo_row(_("Cut"), cuts, choices["cut"], lambda v: choices.update(cut=v)))
        modes = [
            (m.id, MODE_NAMES.get(m.id, m.id)) for m in window.backend.modes(window.printer.info)
        ]
        if len(modes) > 1:
            rows.append(
                combo_row(
                    _("Colours"), modes, choices["mode"], lambda v: self._on_print_mode(choices, v)
                )
            )
        if caps.supports_color_adjust:
            rows.append(self._color_adjust_row())
        return rows

    def _color_adjust_row(self) -> Adw.ExpanderRow:
        """Brightness and colour balance, a printer setting kept in settings.json."""
        row = Adw.ExpanderRow(title=_("Colour adjustment"))
        adjust = self.window.printer_settings.color_adjust
        spins = []
        reset = Gtk.Button(label=_("Reset"), valign=Gtk.Align.CENTER)
        for name, title in COLOR_ADJUST_ROWS:
            spin = spin_row(
                title,
                getattr(adjust, name),
                -ColorAdjust.STEPS,
                ColorAdjust.STEPS,
                1,
                0,
                lambda v, n=name: self._set_color_adjust(row, reset, n, int(v)),
            )
            spins.append(spin)
            row.add_row(spin)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda _b: [spin.set_value(0) for spin in spins])
        row.add_suffix(reset)
        self._show_color_adjust(row, reset)
        return row

    def _show_color_adjust(self, row: Adw.ExpanderRow, reset: Gtk.Button) -> None:
        neutral = self.window.printer_settings.color_adjust.is_neutral()
        row.set_subtitle(_("Not adjusted") if neutral else _("Adjusted"))
        reset.set_sensitive(not neutral)

    def _set_color_adjust(self, row, reset, name: str, value: int) -> None:
        settings = self.window.printer_settings
        values = dict(settings.color_adjust.to_dict(), **{name: value})
        settings.update(color_adjust=ColorAdjust.from_dict(values).to_dict())
        self._show_color_adjust(row, reset)

    def dialog(self) -> None:
        window = self.window
        if window.rendered is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("Print label"),
            body=window.size_text(window.rendered.length_mm),
        )
        choices = self.choices()
        dialog.set_extra_child(boxed_list(self.rows(choices)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("print", _("Print"))
        dialog.set_response_appearance("print", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("print")
        dialog.connect("response", lambda _d, result: self._on_response(result, choices))
        window.present_dialog(dialog)

    def _on_response(self, result: str, choices: dict) -> None:
        if result != "print":
            return
        window = self.window
        settings = window.printer_settings
        settings.update(cut=choices["cut"], mode=choices["mode"])
        self.start(
            PrintOptions(
                copies=choices["copies"],
                cut=choices["cut"],
                mode=choices["mode"],
                color=settings.color_adjust,
                data_format=settings.data_format,
                gap_mm=window.label.gap_mm,
            )
        )

    # --- job ----------------------------------------------------------------------

    def start(self, options: PrintOptions) -> None:
        window = self.window
        host = window.printer.host
        if not host:
            window.toast(_("No printer yet – search for one or enter its address"))
            return
        backend = window.backend
        label, safe_mm = copy.deepcopy(window.label), window.printer_settings.safe_mm
        self.busy = True
        window.print_button.set_sensitive(False)
        window.print_button.set_label(_("Printing…"))

        def progress(event: Progress):
            GLib.idle_add(self.progress, event)

        def work():
            # rendering a long label takes a moment: not in the main loop
            try:
                image = backend.prepare(label, options, safe_mm)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.done, e, _("The label cannot be prepared for printing."))
                return
            try:
                result = backend.print_label(image, host, options, progress)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.done, e)
                return
            GLib.idle_add(self.done, None, None, options, result)

        threading.Thread(target=work, daemon=True).start()

    def progress(self, event: Progress) -> bool:
        if self.busy:
            window = self.window
            window.print_button.set_label(
                _("Printing %(n)d/%(total)d…") % {"n": event.copy, "total": event.copies}
            )
            text = stage_text(event.stage)
            window.banner.set_title(GLib.markup_escape_text(text or ""))
            window.banner.set_revealed(text is not None)
        return False

    def done(
        self,
        error: Exception | None,
        message: str | None = None,
        options: PrintOptions | None = None,
        result: PrintResult | None = None,
    ) -> bool:
        window = self.window
        self.busy = False
        window.print_button.set_sensitive(True)
        window.print_button.set_label(_("Print"))
        window.banner.set_revealed(False)
        if error:
            self.failed(error, message or printer_error_text(error, window.unit))
        elif options and result and result.mode and result.mode != options.mode:
            window.toast(
                _(
                    "Colour mode “%(wanted)s” is not available on this printer, printed with “%(used)s”"
                )
                % {
                    "wanted": MODE_NAMES.get(options.mode, options.mode),
                    "used": MODE_NAMES.get(result.mode, result.mode),
                }
            )
        else:
            window.toast(_("Printed"))
        window.printer.refresh()
        return False

    def failed(self, error: Exception, message: str) -> None:
        """A dialog: what happened and what to do, the original text on request."""
        details = getattr(error, "details", "") or "%s: %s" % (type(error).__name__, error)
        dialog = Adw.AlertDialog(heading=_("Printing failed"), body=message)
        expander = Gtk.Expander(label=_("Details"))
        expander.set_child(Gtk.Label(label=details, wrap=True, selectable=True, xalign=0))
        dialog.set_extra_child(expander)
        dialog.add_response("report", _("Report"))
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        problem = message + "\n\n" + details

        def response(_dialog, result):
            if result == "report":
                self.window.report_problem(problem)

        dialog.connect("response", response)
        self.window.present_dialog(dialog)
