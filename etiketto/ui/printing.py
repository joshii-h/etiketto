"""From the Print button to the result: checks, dialog, job, progress."""

from __future__ import annotations

import copy
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from etiketto.backends.base import (
    ColorAdjust,
    PrintMode,
    PrintOptions,
    PrintResult,
    Progress,
)
from etiketto.job import Job, PrepareFailed
from etiketto.merge import MergeError
from etiketto.model import Label
from etiketto.serial import (
    Series,
    SeriesError,
    SeriesProgress,
    SeriesResult,
    UnprintableRows,
    print_series,
    row_span,
)
from etiketto.ui.i18n import _, isolate
from etiketto.ui.texts import (
    COLOR_ADJUST_ROWS,
    CUT_MODES,
    MODE_NAMES,
    data_error_text,
    error_details,
    printed_text,
    printer_error_text,
    series_stop_text,
    stage_text,
)
from etiketto.ui.widgets import combo_row, list_dialog, spin_row
from etiketto.variables import Values

if TYPE_CHECKING:
    from etiketto.ui.window import Window

ROWS_LISTED = 20  # rows named in a message; more are "…"


class PrintFlow:
    """From the Print button to the result: checks, dialog, job, progress."""

    def __init__(self, window: Window):
        self.window = window
        self.busy = False
        self._printing: Label | None = None
        self._stop = threading.Event()  # serial labels: stop after the current one

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
        if problem := self._data_problem():
            window.toast(problem)
            return
        if tape := window.printer.other_tape():
            self._wrong_cassette_dialog(tape)
        elif pitch := window.printer.other_pitch():
            self._wrong_labels_dialog(pitch)
        else:
            self.dialog()

    def _data_problem(self) -> str | None:
        """Why a linked data file cannot be printed from: a label with its
        placeholders unfilled is never what was wanted."""
        data = self.window.data
        if data.path is None or data.active:
            return None
        if data.state == "loading":
            return _("The data file is still being read. Try again in a moment.")
        if data.state == "unread":
            return data.status()
        error = data.error or MergeError("empty", "no data rows")
        return data_error_text(error, data.path.name)

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
        """Copies, cut and mode for the print dialog, from the last print;
        with a data file the rows to print, all of them."""
        self._plan_copies(1)
        choices = {"copies": 1, "cut": self._cut(), "mode": self.mode}
        if self.window.data.active:
            choices.update(first=1, last=len(self.window.data.rows))
        return choices

    def _plan_copies(self, copies: int) -> None:
        """{=copies} in the preview: as many as the dialog would print."""
        changed = self.window.planned_copies != copies
        self.window.planned_copies = copies
        if changed:
            self.window.schedule_render()

    def _set_copies(self, choices: dict, copies: int) -> None:
        choices.update(copies=copies)
        self._plan_copies(copies)

    def _rows(self, choices: dict) -> list[Gtk.Widget]:
        """Only what the printer can do: no cut row for printers without a
        cutter, no mode row with a single mode, colour correction if supported."""
        caps = self.window.backend.capabilities
        copies = _("Copies of each label") if self.window.data.active else _("Copies")
        rows: list[Gtk.Widget] = [
            spin_row(copies, 1, 1, 99, 1, 0, lambda v: self._set_copies(choices, int(v)))
        ]
        if self.window.data.active:
            rows.extend(self._row_range(choices))
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

    @staticmethod
    def _row_range(choices: dict) -> list[Adw.SpinRow]:
        """From and to which row of the data file; the other end follows."""
        count = choices["last"]
        first = spin_row(_("From row"), 1, 1, count, 1, 0, lambda v: None)
        last = spin_row(_("To row"), count, 1, count, 1, 0, lambda v: None)

        def changed(_row, _pspec) -> None:
            choices.update(first=int(first.get_value()), last=int(last.get_value()))

        def follow(row, _pspec, other, later: bool) -> None:
            value = row.get_value()
            if (other.get_value() < value) if later else (other.get_value() > value):
                other.set_value(value)

        first.connect("notify::value", follow, last, True)
        last.connect("notify::value", follow, first, False)
        for row in (first, last):
            row.connect("notify::value", changed)
        return [first, last]

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
        body = window.size_text(window.rendered.length_mm)
        heading = _("Print serial labels") if window.data.active else _("Print label")
        dialog = list_dialog(heading, self._rows(choices), body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("print", _("Print"))
        dialog.set_response_appearance("print", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("print")
        dialog.connect("response", lambda _d, result: self._on_response(result, choices))
        window.present_dialog(dialog)

    def _on_response(self, result: str, choices: dict) -> None:
        self._plan_copies(1)  # the preview shows a single label again
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
        if window.data.active:
            count = len(window.data.rows)
            self.start_series(options, row_span(choices["first"], choices["last"], count))
        else:
            self.start(options)

    # --- job ----------------------------------------------------------------------

    def _begin(self) -> str | None:
        """The printer's address, the interface switched to printing; None
        (and a hint) if there is no printer yet."""
        window = self.window
        host = window.printer.host
        if not host:
            window.toast(_("No printer yet – search for one or enter its address"))
            return None
        self.busy = True
        window.print_button.set_sensitive(False)
        window.print_button.set_label(_("Printing…"))
        return host

    def start(self, options: PrintOptions) -> None:
        window = self.window
        host = self._begin()
        if not host:
            return
        window.sync_counter()  # the command line may have printed from the file
        job = Job(
            window.backend,
            copy.deepcopy(window.label),
            options,
            window.printer_settings.safe_mm,
        )
        self._printing = window.label  # the counter moves on in this label only

        def progress(event: Progress):
            GLib.idle_add(self.progress, event)

        def work():
            # rendering a long label takes a moment: not in the main loop
            try:
                result = job.run(host, progress)
            except PrepareFailed as e:
                cause = e.__cause__ or e
                GLib.idle_add(
                    self.done, cause, _("The label cannot be prepared for printing."), job
                )
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.done, e, None, job)
            else:
                GLib.idle_add(self.done, None, None, job, result)

        threading.Thread(target=work, name="etiketto-print", daemon=True).start()

    def start_series(self, options: PrintOptions, indexes: Sequence[int]) -> None:
        """One label per row in `indexes`; a Stop button in the banner ends
        the series after the label being printed."""
        window = self.window
        # the file may have been read again, with fewer rows, since the dialog
        indexes = [index for index in indexes if 0 <= index < len(window.data.rows)]
        if not indexes:
            window.toast(_("No rows to print"))
            return
        host = self._begin()
        if not host:
            return
        backend = window.backend
        window.sync_counter()  # the command line may have printed from the file
        label = copy.deepcopy(window.label)
        series = Series(
            label,
            window.data.rows,
            indexes,
            options,
            window.printer_settings.safe_mm,
            Values.for_label(label, datetime.now(), options.copies),
        )
        self._printing = window.label  # the counter moves on in this label only
        self._stop.clear()
        window.banner.set_title(GLib.markup_escape_text(_("Printing serial labels")))
        window.banner.set_button_label(_("Stop"))
        window.banner.set_revealed(True)

        def progress(event: SeriesProgress):
            GLib.idle_add(self.series_progress, event)

        def work():
            try:
                outcome = print_series(backend, host, series, progress, self._stop)
            except UnprintableRows as e:
                GLib.idle_add(self.series_refused, e, options, series.indexes)
                return
            except SeriesError as e:
                GLib.idle_add(self.series_failed, e)
                return
            except Exception as e:  # noqa: BLE001 - a bug: end the job, offer a report
                GLib.idle_add(self.done, e)
                return
            GLib.idle_add(self.series_done, outcome, options)

        threading.Thread(target=work, name="etiketto-print", daemon=True).start()

    def stop(self) -> None:
        """Stop a series after the label being printed."""
        if not self.busy:
            return
        self._stop.set()
        banner = self.window.banner
        banner.set_title(GLib.markup_escape_text(_("Stopping after this label…")))
        banner.set_button_label(None)

    def series_progress(self, event: SeriesProgress) -> bool:
        if self.busy:
            window = self.window
            window.print_button.set_label(
                _("Printing %(n)d/%(total)d…") % {"n": event.label, "total": event.labels}
            )
            if not self._stop.is_set():
                text = stage_text(event.event.stage) or _("Printing serial labels")
                window.banner.set_title(GLib.markup_escape_text(text))
        return False

    def series_refused(
        self, error: UnprintableRows, options: PrintOptions, indexes: Sequence[int]
    ) -> bool:
        """Codes of some rows cannot be drawn: nothing printed yet. Print the
        other rows, or correct the data file first."""
        self._finish()
        bad = set(error.rows)
        shown = ", ".join(str(row) for row in error.rows[:ROWS_LISTED])
        if len(error.rows) > ROWS_LISTED:
            shown += ", …"
        body = (
            _(
                "The barcode or QR code of these rows cannot be made from their values, for "
                "example an EAN-13 with letters or an empty cell. Correct the data file and read "
                "it again, or print without these rows."
            )
            + "\n\n"
            + _("Rows: %s") % shown
        )
        others = [index for index in indexes if index + 1 not in bad]
        dialog = Adw.AlertDialog(heading=_("Rows that cannot be printed"), body=body)
        dialog.add_response("cancel", _("Cancel"))
        if others:
            dialog.add_response("skip", _("Print without these rows"))
            dialog.set_response_appearance("skip", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_dialog, result: str) -> None:
            if result == "skip":
                self.start_series(options, others)

        dialog.connect("response", response)
        self.window.present_dialog(dialog)
        return False

    def _count_series(self, outcome: SeriesResult) -> None:
        """The numbers of the labels printed are used up, also on failure."""
        if outcome.counter is not None and outcome.labels and self.window.label is self._printing:
            self.window.set_counter_next(outcome.counter)

    def series_done(self, outcome: SeriesResult, options: PrintOptions) -> bool:
        self._finish()
        self._count_series(outcome)
        if not outcome.printed:  # nothing to print: print_series() was given no rows
            return False
        first, last = outcome.printed[0], outcome.printed[-1]
        if outcome.stopped:
            text = _("Stopped after row %(row)d") % {"row": last}
        elif outcome.result and outcome.result.mode and outcome.result.mode != options.mode:
            text = printed_text(options, outcome.result)
        elif first == last:
            text = _("Printed row %(row)d") % {"row": first}
        else:
            text = _("Printed rows %(first)d to %(last)d") % {"first": first, "last": last}
        self.window.toast(text)
        return False

    def series_failed(self, error: SeriesError) -> bool:
        self._finish()
        self._count_series(error.outcome)
        if error.preparing:
            message = _("The label cannot be prepared for printing.")
        else:
            message = printer_error_text(error.error, self.window.unit)
        where = series_stop_text(error.row, error.copies_printed)
        self.failed(error.error, message + "\n\n" + where)
        return False

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
        job: Job | None = None,
        result: PrintResult | None = None,
    ) -> bool:
        window = self.window
        self._finish()
        counter = job.next_counter() if job and job.printed else None
        # the printed numbers are used up, also on failure; not in a label
        # opened while printing
        if counter is not None and window.label is self._printing:
            window.set_counter_next(counter)
        if error:
            self.failed(error, message or printer_error_text(error, window.unit))
        else:
            window.toast(printed_text(job.options if job else None, result))
        return False

    def _finish(self) -> None:
        """The interface back from printing; the printer's state afresh."""
        window = self.window
        self.busy = False
        window.print_button.set_sensitive(True)
        window.print_button.set_label(_("Print"))
        window.banner.set_revealed(False)
        window.banner.set_button_label(None)
        window.printer.refresh()

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
