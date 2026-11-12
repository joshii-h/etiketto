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

from etiketto.backends.base import Backend, PrintOptions, PrintResult, Progress
from etiketto.job import Job, PrepareFailed
from etiketto.merge.fill import VARIABLE
from etiketto.model import CodeElement, Label, TextElement
from etiketto.render import DEFAULT_SAFE_MM, check_code
from etiketto.variables import Values, column_names, expand, expand_label, uses_counter

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
    ):
        super().__init__(str(error))
        self.row = row
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
    `values` (those of the first label are enough to tell)."""
    codes = [e for e in label.elements if isinstance(e, CodeElement) and column_names(e.data)]
    if not codes:
        return []
    values = values or Values(datetime.now())

    def fails(code: CodeElement, row: Mapping[str, str]) -> bool:
        return _code_fails(replace(code, data=expand(code.data, values, row)[0]))

    return [index + 1 for index in indexes if any(fails(code, rows[index]) for code in codes)]


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

    def first_values(self) -> Values:
        return self.values or Values.for_label(self.label, datetime.now(), self.options.copies)


def print_series(
    backend: Backend,
    host: str,
    series: Series,
    progress: Callable[[SeriesProgress], None] | None = None,
    stop: threading.Event | None = None,
    outcome: SeriesResult | None = None,
) -> SeriesResult:
    """Print one label per row, `options.copies` of each, as a print job per
    row. `stop` is looked at between two rows: the row being printed is
    always finished, the first one too. Raises UnprintableRows before the
    first label if any row's codes cannot be drawn, SeriesError when a
    label fails. `outcome` (returned) is kept up to date while printing."""
    values = series.first_values()
    if bad := unprintable_rows(series.label, series.rows, series.indexes, values):
        raise UnprintableRows(bad)
    outcome = outcome if outcome is not None else SeriesResult()
    counting = uses_counter(series.label)
    outcome.counter = values.counter if counting else None
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
        try:
            outcome.result = job.run(host, report) or outcome.result
        except PrepareFailed as e:
            cause = e.__cause__ if isinstance(e.__cause__, Exception) else e
            raise SeriesError(row, list(outcome.printed), cause, True, outcome) from e
        except Exception as e:  # noqa: BLE001 - the interface maps PrinterError codes
            raise SeriesError(row, list(outcome.printed), e, False, outcome) from e
        finally:  # also after Ctrl+C: the labels sent used their numbers
            outcome.labels += job.printed
            if counting:
                outcome.counter = values.counter + values.step * outcome.labels
        outcome.printed.append(row)
    return outcome
