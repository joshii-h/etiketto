"""From the Print button to the result: checks, dialog, job, progress."""

from __future__ import annotations

import copy
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk
from PIL import Image

from etiketto.backends.base import (
    CodeTooLarge,
    ColorAdjust,
    PrinterError,
    PrintMode,
    PrintOptions,
    PrintResult,
    Progress,
)
from etiketto.sheets.layout import (
    MAX_LABELS,
    Copies,
    LabelSource,
    Pages,
    SheetJob,
    first_positions,
)
from etiketto.ui.i18n import _, isolate
from etiketto.ui.sheets import numbers_text
from etiketto.ui.texts import (
    COLOR_ADJUST_ROWS,
    CUT_MODES,
    MODE_NAMES,
    error_details,
    printer_error_text,
    stage_text,
    warning_text,
)
from etiketto.ui.widgets import combo_row, list_dialog, spin_row

if TYPE_CHECKING:
    from etiketto.ui.window import Window

# render warnings about codes that may not scan, or do not fit
CODE_WARNINGS = ("code_too_big", "code_quiet", "code_thin")


def copies_of(image: Image.Image) -> LabelSource:
    """The same label at every position of a sheet. (A mail merge gives
    each position its own label instead: label_at(n) renders row n.)"""
    return Copies(image)


class PrintFlow:
    """From the Print button to the result: checks, dialog, job, progress."""

    def __init__(self, window: Window):
        self.window = window
        self.busy = False
        self.open_dialog: Adw.AlertDialog | None = None  # the print dialog, while open

    def _modes(self) -> tuple[PrintMode, ...]:
        return self.window.backend.modes(self.window.printer.info)

    @property
    def mode(self) -> str:
        """The print mode of the last print (what the preview shows)."""
        ids = [m.id for m in self._modes()]
        saved = self.window.printer_settings.get("mode")
        return saved if saved in ids else ids[0]

    def _cut(self) -> str:
        cut_modes = self.window.backend.capabilities.cut_modes
        saved = self.window.printer_settings.get("cut", "full")
        return saved if saved in cut_modes else (cut_modes or ("",))[0]

    # --- checks ----------------------------------------------------------------

    def on_print(self, *_args) -> None:
        window = self.window
        if self.busy or not window.rendered:
            return
        if not window.label.elements and not window.label.background_image:
            window.toast(_("The label is empty"))
            return
        if window.label.sheet and not window.sheet:
            window.toast(_("This label sheet is unknown to this version – choose another one"))
            return
        if tape := window.printer.other_tape():
            self._wrong_cassette_dialog(tape)
        elif pitch := window.printer.other_pitch():
            self._wrong_labels_dialog(pitch)
        else:
            self.dialog()

    def _warn(self, heading: str, body: str) -> None:
        self.window.confirm(heading, body, "print", _("Print anyway"), self.dialog)

    def _wrong_cassette_dialog(self, inserted_mm: float) -> None:
        unit, label = self.window.unit, self.window.label
        self._warn(
            _("Wrong cassette"),
            _(
                "The label is designed for %(label)s tape, but a %(tape)s cassette is "
                "inserted. The printer would scale it to the tape."
            )
            % {
                "label": isolate(unit.tape_name(label.tape_mm)),
                "tape": isolate(unit.tape_name(inserted_mm)),
            },
        )

    def _wrong_labels_dialog(self, measured_mm: float) -> None:
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
                "measured": isolate(unit.format(measured_mm)),
            },
        )

    # --- print dialog ------------------------------------------------------------

    def _on_print_mode(self, choices: dict, mode: str) -> None:
        choices.update(mode=mode)
        self.window.printer_settings.update(mode=mode)
        self.window.schedule_render()  # black and white shows grey

    def choices(self) -> dict:
        """Copies, cut and mode for the print dialog, from the last print; on
        a label sheet the number of labels (the chosen positions) and
        whether to print the outlines."""
        choices = {"copies": 1, "cut": self._cut(), "mode": self.mode}
        sheet, choice = self.window.sheet, self.window.label.sheet
        if sheet and choice:
            choices["copies"] = len(first_positions(sheet, choice.positions))
            choices["outlines"] = bool(self.window.printer_settings.get("outlines", False))
        return choices

    def _sheet_rows(self, choices: dict) -> list[Gtk.Widget]:
        labels = spin_row(
            _("Labels"),
            choices["copies"],
            1,
            MAX_LABELS,
            1,
            0,
            lambda v: self._on_labels(choices, int(v)),
        )
        outlines = Adw.SwitchRow(title=_("Print outlines"), subtitle=_("To test on plain paper"))
        outlines.set_active(choices["outlines"])
        outlines.connect("notify::active", lambda r, _p: choices.update(outlines=r.get_active()))
        return [labels, outlines]

    def _on_labels(self, choices: dict, count: int) -> None:
        choices.update(copies=count)
        if self.open_dialog:
            self.open_dialog.set_body(self._body(choices))

    def sheet_job(self, choices: dict) -> SheetJob | None:
        sheet, choice = self.window.sheet, self.window.label.sheet
        if not sheet or not choice:
            return None
        return SheetJob(
            sheet,
            choices["copies"],
            tuple(choice.positions),
            choice.rotated,
            choices.get("outlines", False),
        )

    def _body(self, choices: dict) -> str:
        window = self.window
        job = self.sheet_job(choices)
        if job is None:
            return window.size_text(window.rendered.length_mm if window.rendered else None)
        sheets = _("Sheets: %d") % len(job.slots())
        return " · ".join((window.size_text(), job.sheet.id, sheets))

    def _rows(self, choices: dict) -> list[Gtk.Widget]:
        """Only what the printer can do: no cut row for printers without a
        cutter, no mode row with a single mode, colour correction if supported;
        on a label sheet the number of labels and the outlines."""
        if self.window.sheet:
            return self._sheet_rows(choices)
        caps = self.window.backend.capabilities
        rows: list[Gtk.Widget] = [
            spin_row(_("Copies"), 1, 1, 99, 1, 0, lambda v: choices.update(copies=int(v)))
        ]
        if caps.cut_modes:
            cuts = [(key, name) for key, name in CUT_MODES if key in caps.cut_modes]
            rows.append(combo_row(_("Cut"), cuts, choices["cut"], lambda v: choices.update(cut=v)))
        modes = [(m.id, MODE_NAMES.get(m.id, m.id)) for m in self._modes()]
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

        def reset_all(_button) -> None:
            for spin in spins:
                spin.set_value(0)

        reset.connect("clicked", reset_all)
        row.add_suffix(reset)
        self._show_color_adjust(row, reset)
        return row

    def _show_color_adjust(self, row: Adw.ExpanderRow, reset: Gtk.Button) -> None:
        neutral = self.window.printer_settings.color_adjust.is_neutral()
        row.set_subtitle(_("Not adjusted") if neutral else _("Adjusted"))
        reset.set_sensitive(not neutral)

    def _set_color_adjust(self, row, reset, name: str, value: int) -> None:
        settings = self.window.printer_settings
        settings.update(color_adjust=replace(settings.color_adjust, **{name: value}).to_dict())
        self._show_color_adjust(row, reset)

    def dialog(self) -> None:
        window = self.window
        if window.rendered is None:
            return
        choices = self.choices()
        dialog = list_dialog(_("Print label"), self._rows(choices), self._body(choices))
        self.open_dialog = dialog
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("print", _("Print"))
        dialog.set_response_appearance("print", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("print")
        dialog.connect("response", lambda _d, result: self._on_response(result, choices))
        window.present_dialog(dialog)

    def _on_response(self, result: str, choices: dict) -> None:
        self.open_dialog = None
        if result != "print":
            return
        window = self.window
        settings = window.printer_settings
        settings.update(cut=choices["cut"], mode=choices["mode"])
        options = PrintOptions(
            copies=choices["copies"],
            cut=choices["cut"],
            mode=choices["mode"],
            color=settings.color_adjust,
            data_format=settings.data_format,
            gap_mm=window.label.gap_mm,
        )
        job = self.sheet_job(choices)
        self._codes_first(lambda: self._margins_first(options, job))

    def _codes_first(self, then) -> None:
        """Codes that may not scan at the printer's resolution (the preview
        is rendered at it): ask first; a code that does not fit: say so,
        nothing is printed."""
        rendered = self.window.rendered
        warnings = [w for w in rendered.warnings if w.key in CODE_WARNINGS] if rendered else []
        too_big = [w for w in warnings if w.key == "code_too_big"]
        if too_big:
            error = CodeTooLarge("element %s" % too_big[0].params.get("n"), **too_big[0].params)
            self.failed(error, printer_error_text(error, self.window.unit))
            return
        if not warnings:
            then()
            return
        self.window.confirm(
            _("Codes may not scan"),
            "\n".join(warning_text(w) for w in warnings),
            "print",
            _("Print anyway"),
            then,
        )

    def _margins_first(self, options: PrintOptions, job: SheetJob | None) -> None:
        """Labels on a sheet whose content reaches into the printer's
        margins: ask first."""
        window = self.window
        if job is None:
            self.start(options)
            return
        window.printer_settings.update(outlines=job.outlines)
        cut = window.positions_in_margins({p for page in job.slots() for p in page})
        if not cut:
            self.start(options, job)
            return
        window.confirm(
            _("Labels in the printer's margin"),
            _(
                "The printer cannot print near the edges of the paper. The content of "
                "labels %(positions)s reaches into that margin and would be cut off. "
                "Increase the safety margin, or leave these positions out."
            )
            % {"positions": isolate(numbers_text(cut))},
            "print",
            _("Print anyway"),
            lambda: self.start(options, job),
        )

    # --- job ----------------------------------------------------------------------

    def start(self, options: PrintOptions, job: SheetJob | None = None) -> None:
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
            except PrinterError as e:  # a code that does not fit: its own message
                GLib.idle_add(self.done, e)
                return
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.done, e, _("The label cannot be prepared for printing."))
                return
            try:
                if job is None:
                    result = backend.print_label(image, host, options, progress)
                else:
                    pages = Pages(job, copies_of(image), backend.capabilities.dpi)
                    result = backend.print_pages(pages, host, options, progress)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.done, e)
                return
            GLib.idle_add(self.done, None, None, options, result)

        threading.Thread(target=work, name="etiketto-print", daemon=True).start()

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
        else:
            window.toast(self._printed_text(options, result))
        window.printer.refresh()
        return False

    @staticmethod
    def _printed_text(options: PrintOptions | None, result: PrintResult | None) -> str:
        if result and result.queued:  # other jobs first: not printed yet, nothing wrong
            return _("Sent to the printer – it prints when its turn comes")
        if not (options and result and result.mode and result.mode != options.mode):
            return _("Printed")
        return _(
            "Colour mode “%(wanted)s” is not available on this printer, printed with “%(used)s”"
        ) % {
            "wanted": MODE_NAMES.get(options.mode, options.mode),
            "used": MODE_NAMES.get(result.mode, result.mode),
        }

    def failed(self, error: Exception, message: str) -> None:
        """A dialog: what happened and what to do, the original text on request."""
        details = getattr(error, "details", "") or error_details(error)
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
