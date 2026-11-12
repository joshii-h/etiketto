"""What the server can do, for HTTP and MCP alike.

Labels come from the labels folder or as content in the request; values,
data rows and checks work as on the command line (cli.py), whose pieces
are used here: a label prints the same from the window, the command line
and the server. Messages are translated in the language of the request
(i18n.language()), errors are ApiError.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fnmatch
import io
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from etiketto import __version__
from etiketto.backends import backends, get_backend
from etiketto.backends.base import Backend, PrinterError, PrinterInfo, PrintOptions
from etiketto.cli import (
    EXIT_COUNTER,
    EXIT_FILE,
    EXIT_LABEL,
    REFUSED,
    CliError,
    _check_rows,
    _options,
    apply_assignments,
)
from etiketto.job import Job, PrepareFailed
from etiketto.merge import MergeError, read_book
from etiketto.model import (
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    LockTimeout,
    TextElement,
    data_inside,
    locked,
    writable,
)
from etiketto.render import RenderWarning, render
from etiketto.serial import (
    Series,
    SeriesError,
    SeriesProgress,
    SeriesResult,
    print_series,
    row_span,
    unknown_columns,
    used_columns,
)
from etiketto.server.errors import ApiError, bad_request, invalid_field, not_found
from etiketto.server.jobs import Failure, JobQueue, PrintJob, QueueFull
from etiketto.server.paths import LabelsDir
from etiketto.server.requests import LabelRequest, PreviewRequest, PrintRequest
from etiketto.ui.i18n import _
from etiketto.ui.settings import PrinterSettings, Settings
from etiketto.ui.texts import (
    backend_name,
    code_text,
    data_error_text,
    label_error_text,
    printed_remark,
    printer_error_text,
    save_error_text,
    series_stop_text,
    warning_text,
)
from etiketto.units import unit_for
from etiketto.variables import BUILTIN, Values, label_names, uses_counter

log = logging.getLogger(__name__)
DISCOVERY_SECONDS = 30.0  # printers found are remembered this long
MAX_HOST = 512
RENDERS = 2  # previews rendered at once: a large label takes much memory


@dataclass
class Prepared:
    """A label filled in and checked, ready to be previewed or printed."""

    label: Label
    path: Path | None  # the label file; None for a label sent as content
    backend: Backend
    settings: Settings
    printer: PrinterSettings
    options: PrintOptions
    values: Values
    rows: list[dict[str, str]]  # data rows; [] for a label without
    indexes: list[int]  # the rows to print
    fixed_time: bool  # "date" was set: the time is not that of printing
    fixed_counter: bool  # counter_start was given

    def job(self, values: Values | None = None, index: int | None = None) -> Job:
        columns = self.rows[self.indexes[0] if index is None else index] if self.rows else None
        return Job(
            self.backend,
            self.label,
            self.options,
            self.printer.safe_mm,
            values or self.values,
            columns,
        )


@dataclass
class Preview:
    png: bytes
    width: int
    height: int
    warnings: list[dict[str, str]]

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "warnings": self.warnings,
            "png": base64.b64encode(self.png).decode(),
        }


def _warning(key: str, message: str) -> dict[str, str]:
    return {"code": key, "message": message}


def _cli_error(error: CliError) -> ApiError:
    """The command line's errors (already translated) as API errors."""
    status, code = {
        EXIT_LABEL: (422, "label_unprintable"),
        EXIT_FILE: (422, "file_problem"),
        EXIT_COUNTER: (409, "counter_not_saved"),
    }.get(error.code, (400, "bad_request"))
    return ApiError(status, code, str(error))


def host_ok(host: str) -> bool:
    """An address as a printer has one: host name, IP address, printer URI
    or CUPS queue name; printable, no spaces, not absurdly long."""
    return 0 < len(host) <= MAX_HOST and all(33 <= ord(c) < 127 for c in host)


class Service:
    def __init__(
        self,
        labels_dir: str | Path,
        allowed_hosts: tuple[str, ...] = (),
        settings: Callable[[], Settings] = Settings.load,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.labels = LabelsDir(labels_dir)
        self.allowed_hosts = allowed_hosts  # fnmatch patterns; () allows any
        self._settings = settings
        self._clock = clock
        self.jobs = JobQueue(self._failed)
        self._found: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._found_lock = threading.Lock()
        self._label_locks: dict[str, threading.Lock] = {}
        self._renders = threading.BoundedSemaphore(RENDERS)

    def close(self, timeout: float = 30.0) -> None:
        """Cancel the waiting jobs; printing ones stop after their label."""
        self.jobs.close(timeout)

    # --- printers -------------------------------------------------------------

    def health(self) -> dict:
        return {"status": "ok", "version": __version__}

    def printers(self, discover: bool = True) -> list[dict]:
        chosen = backends()
        settings = self._settings()
        found: dict[str, list[dict[str, str]]] = {}
        if discover:
            with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                found = dict(
                    zip((b.id for b in chosen), pool.map(self._discover, chosen), strict=True)
                )
        return [self._printer(b, settings, found.get(b.id, [])) for b in chosen]

    def _printer(self, backend: Backend, settings: Settings, found: list[dict]) -> dict:
        caps = backend.capabilities
        kind = "tape" if caps.continuous else ("pages" if caps.pages else "labels")
        return {
            "id": backend.id,
            "name": backend_name(backend),
            "kind": kind,
            "dpi": caps.dpi,
            "color_model": caps.color_model,
            "tape_widths_mm": list(caps.tape_widths_mm),
            "label_sizes_mm": [[s.width_mm, s.height_mm] for s in caps.label_sizes],
            "configured_host": settings.printer(backend).host,
            "found": found,
        }

    def _discover(self, backend: Backend) -> list[dict[str, str]]:
        with self._found_lock:
            cached = self._found.get(backend.id)
        if cached and time.monotonic() - cached[0] < DISCOVERY_SECONDS:
            return cached[1]
        try:
            found = [
                {"name": p.name, "host": p.host, "address": p.address} for p in backend.discover()
            ]
        except Exception:  # noqa: BLE001 - a broken plugin must not break the list
            found = []
        with self._found_lock:
            self._found[backend.id] = (time.monotonic(), found)
        return found

    def _backend(self, backend_id: object) -> Backend:
        if not isinstance(backend_id, str):
            raise invalid_field("printer")
        try:
            return get_backend(backend_id)
        except KeyError:
            raise not_found(
                "printer_not_found", _("Unknown printer model: %s") % backend_id
            ) from None

    def status(self, backend_id: object, host: object = None) -> dict:
        backend = self._backend(backend_id)
        if host is not None and not isinstance(host, str):
            raise invalid_field("host")
        settings = self._settings()
        host = self._host(host or settings.printer(backend).host)
        try:
            info = backend.query(host)
        except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
            unit = unit_for(settings.units)
            code = e.code if isinstance(e, PrinterError) else "generic"
            raise ApiError(502, "printer_" + code, printer_error_text(e, unit)) from None
        return self._info(backend, host, info, settings)

    def _info(self, backend: Backend, host: str, info: PrinterInfo, settings: Settings) -> dict:
        unit = unit_for(settings.units)
        cassette = info.cassette
        return {
            "printer": backend.id,
            "host": host,
            "name": backend.display_name(info),
            "model": info.model,
            "state": info.state,
            "error": (
                {"code": info.error, "message": code_text(info.error, unit)}
                if info.error
                else None
            ),
            "dpi": backend.capabilities.dpi,
            "firmware": info.firmware or None,
            "modes": [m.id for m in backend.modes(info)],
            "media": {
                "tape_mm": info.tape_mm,
                "remaining_mm": info.remaining_mm,
                "total_mm": info.total_mm,
                "pitch_mm": info.pitch_mm,
                "cassette": (
                    {
                        "lot": cassette.lot,
                        "serial": cassette.serial,
                        "made": cassette.made.isoformat() if cassette.made else None,
                        "printable_mm": cassette.printable_mm,
                    }
                    if cassette
                    else None
                ),
            },
        }

    def _host(self, host: str | None) -> str:
        if not host:
            raise bad_request(_("No printer address: give it as host"), "no_host")
        if not host_ok(host):
            raise invalid_field("host")
        if self.allowed_hosts and not any(
            fnmatch.fnmatchcase(host, p) for p in self.allowed_hosts
        ):
            raise ApiError(
                403, "host_not_allowed", _("This printer address is not allowed: %s") % host
            )
        return host

    def _print_host(self, request: PrintRequest, prepared: Prepared) -> str:
        """The address asked for, the one set in the app, or the only
        printer found."""
        host = request.host or prepared.printer.host
        if not host:
            found = self._discover(prepared.backend)
            host = found[0]["host"] if len(found) == 1 else None
        return self._host(host)

    # --- labels ---------------------------------------------------------------

    def list_labels(self) -> list[dict]:
        result = []
        for path in self.labels.labels():
            with contextlib.suppress(OSError):
                info = path.stat()
                result.append(
                    {
                        "name": self.labels.name_of(path),
                        "bytes": info.st_size,
                        "modified": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(
                            timespec="seconds"
                        ),
                    }
                )
        return result

    def describe(self, name: object) -> dict:
        path = self.labels.label(name)
        label = self._load(path)
        names = label_names(label)
        width, height = label.media_size()
        data_file = label.data_file
        own_data = (
            bool(data_file) and self.labels.contains(data_file) and data_inside(data_file, path)
        )
        return {
            "name": self.labels.name_of(path),
            "printer": {"id": label.printer, "name": backend_name(self._backend(label.printer))},
            "size": {
                "tape_mm": label.tape_mm,
                "length_mm": None if label.auto_length else label.length_mm,
                "auto_length": label.auto_length,
                "orientation": label.orientation,
                "width_mm": width,
                "height_mm": height,
                "gap_mm": label.gap_mm,
            },
            "elements": [_element(n, e) for n, e in enumerate(label.elements, 1)],
            "variables": sorted(names - BUILTIN),
            "builtin_variables": sorted(names & BUILTIN),
            "columns": used_columns(label),
            "data_file": self.labels.name_of(data_file) if own_data else None,
            "data_file_unusable": bool(data_file) and not own_data,
            "data_sheet": label.data_sheet or None,
            "counter": {
                "used": uses_counter(label),
                "next": label.counter_next,
                "step": label.counter_step,
            },
            "features": label.features(),
        }

    def _load(self, path: Path) -> Label:
        name = self.labels.name_of(path)
        try:
            return Label.load(path)
        except OSError:
            raise ApiError(422, "file_problem", _("Cannot open %s") % name) from None
        except LabelError as e:
            raise ApiError(422, "label_" + e.code, label_error_text(e, name)) from None

    def _content(self, content: dict) -> Label:
        try:
            label = Label.from_dict(content)
        except LabelError as e:
            raise ApiError(422, "label_" + e.code, label_error_text(e, "label_content")) from None
        except (TypeError, ValueError, RecursionError):  # e.g. a field of the wrong kind
            raise ApiError(
                422, "label_damaged", _("The file is damaged: %s") % "label_content"
            ) from None
        # a label sent along may name any file: its data come with the request
        label.data_file, label.data_sheet = "", ""
        return label

    # --- filling in -----------------------------------------------------------

    def prepare(self, request: LabelRequest, copies: int = 1, rows=None) -> Prepared:
        """The label of a request with its values and data rows, checked
        as the command line checks before printing."""
        path = self.labels.label(request.label) if request.label is not None else None
        label = self._load(path) if path else self._content(request.content or {})
        if request.printer and request.printer != label.printer:
            self._backend(request.printer)
            label.switch_printer(request.printer)
        backend = self._backend(label.printer)
        settings = self._settings()
        printer = settings.printer(backend)
        if request.counter_start is not None:
            label.counter_next = request.counter_start
        table, indexes = self._rows(request, label, path, rows)
        try:
            overrides = apply_assignments(label, request.assignments, bool(table))
        except CliError as e:
            raise _cli_error(e) from None
        options = _options(backend, printer, copies, label.gap_mm)
        values = Values.for_label(
            label, overrides.now or self._clock(), copies, custom=overrides.custom
        )
        return Prepared(
            label,
            path,
            backend,
            settings,
            printer,
            options,
            values,
            table,
            indexes,
            overrides.now is not None,
            request.counter_start is not None,
        )

    def _rows(self, request: LabelRequest, label: Label, path: Path | None, span):
        """The data rows: given inline, a data file of the labels folder,
        or the label's own data file if it lies beside it in the folder."""
        if request.data_rows is not None:
            table = request.data_rows
        else:
            data = self._data_path(request, label, path)
            if data is None:
                return [], []
            own = bool(label.data_file) and Path(label.data_file).resolve() == data.resolve()
            table = self._read(data, request.sheet or (label.data_sheet if own else ""))
        first, last = span or (1, len(table))
        indexes = list(row_span(first, last, len(table)))
        if not indexes:
            raise bad_request(_("No rows to print"), "no_rows")
        return table, indexes

    def _data_path(self, request: LabelRequest, label: Label, path: Path | None) -> Path | None:
        if request.data_file is not None:
            return self.labels.data(request.data_file)
        if not label.data_file or path is None:
            if request.sheet:
                raise bad_request(_("A sheet needs a data file"), "no_data")
            return None
        if not (self.labels.contains(label.data_file) and data_inside(label.data_file, path)):
            raise ApiError(
                422,
                "data_outside",
                _("The label uses a data file outside the labels folder: send its rows along"),
            )
        return Path(label.data_file)

    def _read(self, path: Path, sheet: str) -> list[dict[str, str]]:
        name = path.name
        try:
            book = read_book(path, sheet)
        except (OSError, MergeError) as e:
            raise ApiError(422, "data_problem", data_error_text(e, name)) from None
        if sheet and book.sheet != sheet:
            error = MergeError("no_sheet", sheet)
            raise ApiError(422, "data_problem", data_error_text(error, name))
        return book.table.rows

    # --- preview --------------------------------------------------------------

    def preview(self, request: PreviewRequest) -> Preview:
        prepared = self.prepare(request)
        index = request.row - 1
        if prepared.rows and index >= len(prepared.rows):
            raise invalid_field("row")
        job = prepared.job(index=index if prepared.rows else None)
        with self._renders:
            label, warnings = job.expanded(1)
            if request.view == "printer":
                image = self._printer_image(job, label)
            else:
                caps = prepared.backend.capabilities
                rendered = render(
                    label, caps.dpi, safe_mm=job.safe_mm, min_length_mm=caps.min_length_mm
                )
                warnings = warnings + rendered.warnings
                image = prepared.backend.preview(rendered.image, prepared.options.mode)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        texts = [_warning(w.key, warning_text(w)) for w in warnings]
        if prepared.rows and (unknown := unknown_columns(prepared.label, list(prepared.rows[0]))):
            names = ", ".join("{%s}" % name for name in unknown)
            texts.append(_warning("unknown_column", _("No such column: %s") % names))
        return Preview(buffer.getvalue(), image.width, image.height, texts)

    @staticmethod
    def _printer_image(job: Job, label: Label):
        try:
            return job.image(label)
        except PrepareFailed:
            raise ApiError(
                422, "prepare_failed", _("The label cannot be prepared for printing.")
            ) from None

    # --- printing -------------------------------------------------------------

    def submit(self, request: PrintRequest) -> PrintJob:
        """Check the request and queue the job; ApiError if it cannot print."""
        prepared = self.prepare(request, request.copies, request.rows)
        warnings = self._check(prepared)
        save = prepared.path is not None and not request.no_save_counter
        if save and prepared.path and prepared.job().next_counter() is not None:
            self._check_writable(prepared.path)
        host = self._print_host(request, prepared)
        total = request.copies * (len(prepared.indexes) if prepared.rows else 1)
        name = self.labels.name_of(prepared.path) if prepared.path else ""

        def run(job: PrintJob):
            return self._print(job, prepared, save)

        job = PrintJob(prepared.backend.id, host, name, total, run, warnings=warnings)
        try:
            return self.jobs.submit(job)
        except QueueFull:
            raise ApiError(
                429, "queue_full", _("Too many print jobs are waiting – try again later")
            ) from None

    def _check(self, prepared: Prepared) -> list[RenderWarning]:
        label = prepared.label
        if not label.elements and not label.background_image:
            raise ApiError(422, "label_unprintable", _("The label is empty"))
        warnings = prepared.job().check()
        for warning in warnings:
            if warning.key in REFUSED:
                raise ApiError(422, "label_unprintable", warning_text(warning))
        if prepared.rows:
            series = Series(
                label, prepared.rows, prepared.indexes, prepared.options, values=prepared.values
            )
            try:
                _check_rows(series)
            except CliError as e:
                raise _cli_error(e) from None
        return warnings

    @staticmethod
    def _check_writable(path: Path) -> None:
        if not writable(path):
            error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            raise ApiError(
                409,
                "counter_not_saved",
                save_error_text(error, path.name)
                + " – "
                + _("nothing was printed: the next counter value could not be saved"),
            )

    @contextlib.contextmanager
    def _exclusive(self, path: Path | None) -> Iterator[None]:
        """The label file for one job at a time, from reading its counter
        until the next one is saved: other jobs of this server wait here,
        other programs (the command line) through the file lock."""
        if path is None:
            yield
            return
        key = os.path.realpath(path)
        with self._found_lock:
            lock = self._label_locks.setdefault(key, threading.Lock())
        with lock, locked(path):
            yield

    def _print(self, job: PrintJob, prepared: Prepared, save: bool):
        with self._exclusive(prepared.path if save else None):
            values = self._fresh_values(prepared, save)
            if prepared.rows:
                return self._print_series(job, prepared, values, save)
            return self._print_one(job, prepared, values, save)

    def _fresh_values(self, prepared: Prepared, save: bool) -> Values:
        """The values when the job prints: the time of printing (unless
        set) and the counter as saved now, after the jobs before it."""
        values = prepared.values
        if not prepared.fixed_time:
            values = replace(values, now=self._clock())
        if save and prepared.path and not prepared.fixed_counter:
            saved = Label.saved_counter(prepared.path)
            if saved is not None:
                values = replace(values, counter=saved)
        return values

    def _print_one(self, job: PrintJob, prepared: Prepared, values: Values, save: bool):
        print_job = prepared.job(values)

        def progress(_event) -> None:
            job.printed = print_job.printed

        printed = False
        try:
            result = print_job.run(job.host, progress, job.stop)
            printed = True
        finally:
            job.printed = print_job.printed
            if save:
                counter = print_job.next_counter()
                self._save_counter(prepared, counter, print_job.printed, printed)
        return lambda: printed_remark(prepared.options, result)

    def _print_series(self, job: PrintJob, prepared: Prepared, values: Values, save: bool):
        outcome = SeriesResult()
        series = Series(
            prepared.label,
            prepared.rows,
            prepared.indexes,
            prepared.options,
            prepared.printer.safe_mm,
            values,
        )

        def progress(_event: SeriesProgress) -> None:
            job.printed = outcome.labels
            job.rows_printed = len(outcome.printed)

        printed = False
        try:
            print_series(
                prepared.backend, job.host, series, progress, job.stop, outcome, check=False
            )
            printed = True
        finally:
            job.printed = outcome.labels
            job.rows_printed = len(outcome.printed)
            if save:
                self._save_counter(prepared, outcome.counter, outcome.labels, printed)
        return lambda: printed_remark(prepared.options, outcome.result)

    @staticmethod
    def _save_counter(prepared: Prepared, counter: int | None, labels: int, fail: bool) -> None:
        """Save the next counter value, also after a printer problem: the
        labels printed used their numbers. A problem saving it fails the
        job, unless the job failed already (that problem is told then)."""
        if counter is None or not labels or prepared.path is None:
            return
        try:
            Label.store_counter(prepared.path, counter)
        except (OSError, ValueError) as e:
            log.warning("cannot save the counter of %s: %s", prepared.path.name, e)
            if fail:
                raise CounterNotSaved(prepared.path.name, e) from e

    def _failed(self, job: PrintJob, error: Exception) -> None:
        """What a failed job tells: kept as a function, translated when the
        job is asked for."""
        units = self._settings().units
        if isinstance(error, CounterNotSaved):
            job.failure = Failure("counter_not_saved", error.text)
        elif isinstance(error, LockTimeout):
            job.failure = Failure(
                "label_locked",
                lambda: _("%s is still locked by another print – try again later") % job.label,
            )
        elif isinstance(error, SeriesError):
            job.failure = Failure(*_series_failure(error, units))
        elif isinstance(error, PrepareFailed):
            job.failure = Failure(
                "prepare_failed", lambda: _("The label cannot be prepared for printing.")
            )
        else:
            code = error.code if isinstance(error, PrinterError) else "generic"
            job.failure = Failure(
                "printer_" + code, lambda: printer_error_text(error, unit_for(units))
            )

    def job(self, job_id: object) -> PrintJob:
        job = self.jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise not_found("job_not_found", _("No such print job: %s") % job_id)
        return job

    @staticmethod
    def job_dict(job: PrintJob) -> dict:
        data = job.to_dict()
        data["warnings"] = [_warning(w.key, warning_text(w)) for w in job.warnings]
        return data

    def cancel(self, job_id: object) -> PrintJob:
        job = self.job(job_id)
        if job.done.is_set():
            raise ApiError(409, "job_finished", _("The print job has already finished"))
        self.jobs.cancel(job)
        return job


class CounterNotSaved(Exception):
    def __init__(self, name: str, error: Exception):
        super().__init__(str(error))
        self.name = name
        self.error = error

    def text(self) -> str:
        error = self.error
        reason = (
            save_error_text(error, self.name)
            if isinstance(error, OSError)
            else _("Cannot save %s") % self.name
        )
        return (
            reason + " – " + _("the labels were printed, but the next counter value was not saved")
        )


def _series_failure(error: SeriesError, units: str | None) -> tuple[str, Callable[[], str]]:
    def message() -> str:
        if error.preparing:
            text = _("The label cannot be prepared for printing.")
        else:
            text = printer_error_text(error.error, unit_for(units))
        return text + " " + series_stop_text(error.row, error.copies_printed)

    if error.preparing:
        return "prepare_failed", message
    code = error.error.code if isinstance(error.error, PrinterError) else "generic"
    return "printer_" + code, message


def _element(index: int, element) -> dict:
    """An element as the API shows it; `name` is what "set" fills in."""
    data: dict = {"index": index, "kind": element.kind}
    if isinstance(element, TextElement):
        data.update(name=element.name or None, text=element.text)
    elif isinstance(element, CodeElement):
        data.update(name=element.name or None, data=element.data, symbology=element.symbology)
    elif isinstance(element, ImageElement):
        data["file"] = element.name or None  # the image's file name, for display only
    return data
