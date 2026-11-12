"""Serial labels: one label per row of a data file.

The label keeps its {column} placeholders; each row fills them, together
with the variables, in one pass (variables.expand_label with the row as
`columns`), for the preview and for printing. Printing a series is a print
job (job.py) per row: no backend can take several different labels in one
job, and a loop works for every printer, can be stopped between two rows
and tells exactly which row a problem stopped at. The counter goes on from
row to row as from copy to copy.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from tavletta.backends.base import Backend, PrintOptions, PrintResult, Progress
from tavletta.job import Job, PrepareFailed, merge_results
from tavletta.merge.fill import VARIABLE
from tavletta.model import CodeElement, Label, TextElement
from tavletta.render import DEFAULT_SAFE_MM, check_code
from tavletta.sheets.layout import SheetJob
from tavletta.variables import (
    Values,
    column_names,
    expand,
    expand_label,
    uses_counter,
    varies_per_copy,
)

# what a QR code takes in any case: bytes, error correction M, the largest size
QR_BYTES = 2331


def filled(label: Label, row: Mapping[str, str], values: Values | None = None) -> Label:
    """A copy of the label with the row's values (and, with `values`, the
    variables) filled in; the label itself is not changed."""
    return expand_label(label, values, row)[0]


def _fillable(label: Label) -> list[str]:
    return [
        element.text if isinstance(element, TextElement) else element.data
        for element in label.elements
        if isinstance(element, (TextElement, CodeElement))
    ]


def used_columns(label: Label) -> list[str]:
    """The column names the label's texts and codes refer to, each once."""
    names: dict[str, None] = {}
    for text in _fillable(label):
        names.update(dict.fromkeys(column_names(text)))
    return list(names)


def unknown_columns(label: Label, columns: Sequence[str]) -> list[str]:
    """Placeholders without a column of that name: printed as they are."""
    return [name for name in used_columns(label) if name not in columns]


def insertable(columns: Sequence[str]) -> list[str]:
    """Columns a placeholder can name: braces cannot be part of one, and a
    name starting with "=" would be a variable ("{=date}")."""
    return [
        name
        for name in columns
        if "{" not in name and "}" not in name and not name.strip().startswith(VARIABLE)
    ]


def placeholder(column: str) -> str:
    return "{%s}" % column


class Cursor:
    """The row shown in the preview: index from 0, count rows."""

    def __init__(self, count: int = 0, index: int = 0):
        self.count = count
        self.index = 0
        self.go(index)

    def go(self, index: int) -> None:
        self.index = max(0, min(index, self.count - 1))

    def first(self) -> None:
        self.go(0)

    def previous(self) -> None:
        self.go(self.index - 1)

    def next(self) -> None:
        self.go(self.index + 1)

    def last(self) -> None:
        self.go(self.count - 1)

    @property
    def number(self) -> int:
        """The row as the interface counts: from 1."""
        return self.index + 1

    @property
    def at_start(self) -> bool:
        return self.index <= 0

    @property
    def at_end(self) -> bool:
        return self.index >= self.count - 1


def row_span(first: int, last: int, count: int) -> range:
    """Rows `first` to `last` (counted from 1, both included) as indexes,
    limited to the rows there are; empty if first is after last."""
    start = max(1, first)
    end = min(last, count)
    return range(start - 1, max(start - 1, end))


# --- printing a series --------------------------------------------------------------


@dataclass(frozen=True)
class SeriesProgress:
    """Which label of the series is printing, and the backend's progress."""

    label: int  # 1-based position in the series
    labels: int  # labels (rows) in the series
    row: int  # the row being printed, counted from 1
    event: Progress


@dataclass
class SeriesResult:
    """What a series printed so far: kept up to date while it prints, so a
    caller still has it after an error or an interruption."""

    printed: list[int] = field(default_factory=list)  # the rows printed, counted from 1
    stopped: bool = False  # stopped by the user before the last row
    result: PrintResult | None = None  # of the last label, if anything differed
    labels: int = 0  # labels printed, also those of a row that failed
    counter: int | None = None  # {=counter} of the next label; None without one


class SeriesError(Exception):
    """Printing stopped at `row` (counted from 1) because of `error`; the
    rows in `printed` were printed before."""

    def __init__(
        self,
        row: int,
        printed: list[int],
        error: Exception,
        preparing: bool,
        outcome: SeriesResult | None = None,
        copies_printed: int = 0,
        position: tuple[int, int] | None = None,
    ):
        super().__init__(str(error))
        self.row = row
        # on label sheets: where that row's label goes (position, sheet; from 1)
        self.position = position
        self.copies_printed = copies_printed  # of the failed row, before the problem
        self.printed = printed
        self.error = error
        self.preparing = preparing  # the label could not be rendered
        self.outcome = outcome or SeriesResult(list(printed))


class UnprintableRows(Exception):
    """Rows whose codes cannot be drawn: an EAN-13 with letters, an empty
    cell where a code needs content. Found before the first label is
    printed; `rows` counted from 1."""

    def __init__(self, rows: list[int]):
        super().__init__("codes that cannot be drawn in rows %s" % rows[:20])
        self.rows = rows


def _code_fails(element: CodeElement) -> bool:
    try:
        check_code(element)
        if element.symbology == "qr" and len(element.data.encode()) > QR_BYTES:
            import segno

            segno.make(element.data, error="m")
    except Exception:  # noqa: BLE001 - CodeError, segno's DataOverflowError
        return True
    return False


def unprintable_rows(
    label: Label,
    rows: Sequence[Mapping[str, str]],
    indexes: Sequence[int],
    values: Values | None = None,
) -> list[int]:
    """The rows (counted from 1) whose filled codes cannot be drawn: the
    renderer would print a crossed-out box instead. Only codes with column
    placeholders are checked; the others are the same for every row and
    the preview already shows their problem. Variables are filled in with
    `values` (those of the first label), the counter as it will be on each
    copy of each row: a counter can overflow a code's digits on a later
    copy (an EAN-13 at 99999 + 1). Without variables that change per copy
    one check per row does."""
    codes = [e for e in label.elements if isinstance(e, CodeElement) and column_names(e.data)]
    if not codes:
        return []
    first = values or Values(datetime.now())
    copies = range(1, first.copies + 1) if varies_per_copy(label) else range(1, 2)
    bad = []
    for position, index in enumerate(indexes):
        row_values = replace(first, counter=first.counter + first.step * first.copies * position)
        if any(
            _code_fails(
                replace(code, data=expand(code.data, row_values.for_copy(copy), rows[index])[0])
            )
            for copy in copies
            for code in codes
        ):
            bad.append(index + 1)
    return bad


@dataclass(frozen=True)
class Series:
    """What to print: the label, the rows and how. `values` are those of the
    first label (the counter to start from, the time, --set values);
    without them, now and the label's counter."""

    label: Label
    rows: Sequence[Mapping[str, str]]
    indexes: Sequence[int]  # which rows, see row_span()
    options: PrintOptions
    safe_mm: float = DEFAULT_SAFE_MM
    values: Values | None = None
    # page printers: on label sheets, each position one label (its count is
    # set from the rows and copies), see _print_on_sheets()
    sheet: SheetJob | None = None

    def first_values(self) -> Values:
        return self.values or Values.for_label(self.label, datetime.now(), self.options.copies)


def print_series(
    backend: Backend,
    host: str,
    series: Series,
    progress: Callable[[SeriesProgress], None] | None = None,
    stop: threading.Event | None = None,
    outcome: SeriesResult | None = None,
    check: bool = True,
) -> SeriesResult:
    """Print one label per row, `options.copies` of each, as a print job per
    row. `stop` is looked at between two rows: the row being printed is
    always finished, the first one too. Raises UnprintableRows before the
    first label if any row's codes cannot be drawn, SeriesError when a
    label fails. `outcome` (returned) is kept up to date while printing.
    `check` False: the caller has run unprintable_rows() already."""
    values = series.first_values()
    if check and (bad := unprintable_rows(series.label, series.rows, series.indexes, values)):
        raise UnprintableRows(bad)
    outcome = outcome if outcome is not None else SeriesResult()
    counting = uses_counter(series.label)
    outcome.counter = values.counter if counting else None
    if series.sheet is not None:
        return _print_on_sheets(backend, host, series, values, progress, outcome)
    total = len(series.indexes)
    for position, index in enumerate(series.indexes, start=1):
        if outcome.printed and stop is not None and stop.is_set():
            outcome.stopped = True
            return outcome
        row = index + 1

        def report(event: Progress, position=position, row=row) -> None:
            if progress:
                progress(SeriesProgress(position, total, row, event))

        # the counter goes on from the labels before; UUID and ID are new
        start = replace(values, counter=values.counter + values.step * outcome.labels)
        job = Job(backend, series.label, series.options, series.safe_mm, start, series.rows[index])
        _print_row(job, host, report, row, outcome, values if counting else None)
        outcome.printed.append(row)
    return outcome


def _print_row(
    job: Job, host: str, report, row: int, outcome: SeriesResult, values: Values | None
) -> None:
    """One row's job; a failure as SeriesError. The counter (`values`, None
    for a label without one) goes on by the labels printed, also after a
    failure or Ctrl+C: the labels sent used their numbers."""
    try:
        # queued, not confirmed or another mode on any row: the series says so
        outcome.result = merge_results([outcome.result, job.run(host, report)])
    except PrepareFailed as e:
        cause = e.__cause__ if isinstance(e.__cause__, Exception) else e
        raise SeriesError(row, list(outcome.printed), cause, True, outcome, job.printed) from e
    except Exception as e:  # noqa: BLE001 - the interface maps PrinterError codes
        raise SeriesError(row, list(outcome.printed), e, False, outcome, job.printed) from e
    finally:
        outcome.labels += job.printed
        if values is not None:
            outcome.counter = values.counter + values.step * outcome.labels


# --- serial labels on label sheets ---------------------------------------------------


def _print_on_sheets(
    backend: Backend,
    host: str,
    series: Series,
    values: Values,
    progress: Callable[[SeriesProgress], None] | None,
    outcome: SeriesResult,
) -> SeriesResult:
    """One job of label sheets (job.Job with a label for each position):
    the rows in order, each `copies` times on consecutive positions, from
    the chosen positions of the first sheet on, each position with its own
    counter, copy number, UUID and ID. It cannot be stopped between rows. A
    failure names the row to go on from, the copies of it printed and where
    on the sheets it goes; the labels that came out count (also on Ctrl+C,
    see Job.run)."""
    assert series.sheet is not None
    copies = max(1, series.options.copies)
    total = len(series.indexes) * copies
    sheet = replace(series.sheet, count=total)
    slots = sheet.slots()
    starts = [sum(len(page) for page in slots[:i]) for i in range(len(slots))]
    rows = [index + 1 for index in series.indexes]

    def position(n: int) -> tuple[Values, Mapping[str, str] | None]:
        row, copy = divmod(n, copies)
        start = replace(values, counter=values.counter + values.step * copies * row)
        return start.for_copy(copy + 1), series.rows[series.indexes[row]]

    def report(event: Progress) -> None:
        if progress:
            page = min(max(0, event.copy - 1), len(slots) - 1)
            row = rows[min(starts[page] // copies, len(rows) - 1)]
            progress(SeriesProgress(event.copy, event.copies, row, event))

    def count(done: int) -> None:
        outcome.labels += done
        outcome.printed = rows[: done // copies]
        if outcome.counter is not None:
            outcome.counter = values.counter + values.step * outcome.labels

    def stopped_at(done: int, error: Exception, preparing: bool) -> SeriesError:
        """The SeriesError after `done` labels came out (whole sheets)."""
        count(done)
        at = min(done // copies, len(rows) - 1)
        page = next((i for i, first in enumerate(starts) if first + len(slots[i]) > done), 0)
        where = (slots[page][done - starts[page]] + 1, page + 1)
        printed = list(outcome.printed)
        return SeriesError(rows[at], printed, error, preparing, outcome, done % copies, where)

    options = replace(series.options, copies=total)
    job = Job(backend, series.label, options, series.safe_mm, values, None, sheet, position)
    try:
        outcome.result = job.run(host, report)
    except PrepareFailed as e:
        cause = e.__cause__ if isinstance(e.__cause__, Exception) else e
        # a sheet is sent whole: a label that cannot be drawn stops its sheet
        before = max((first for first in starts if first <= (job.position or 0)), default=0)
        raise stopped_at(before, cause, True) from e
    except Exception as e:  # noqa: BLE001 - the interface maps PrinterError codes
        raise stopped_at(min(job.printed, total), e, False) from e
    except BaseException:  # Ctrl+C: the labels that came out used their numbers
        count(job.printed)
        raise
    count(total)
    return outcome
