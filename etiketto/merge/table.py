"""The result of reading a data file, and what all readers share."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from etiketto.merge import limits


class MergeError(ValueError):
    """A file that cannot be used as data for serial labels. code:
    "not_table" (no CSV, ODS or XLSX file, or no such sheet), "damaged" (one
    of them, but broken), "too_large" (over a limit in limits.py) or "empty"
    (no filled cell); the message is English detail for bug reports, the
    interface shows its own translated text."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def damaged(detail: str) -> MergeError:
    return MergeError("damaged", detail)


def too_large(detail: str) -> MergeError:
    return MergeError("too_large", detail)


@dataclass
class Table:
    """Column names from the first filled row, then one dict per data row
    with every column, values as the text the cell shows."""

    columns: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)


def matches(wanted: str | int | None, index: int, name: str) -> bool:
    """Is the sheet at `index` named `name` the one asked for: by name, by
    index, or the first one (None)."""
    if wanted is None:
        return index == 0
    if isinstance(wanted, str):
        return name == wanted
    return index == wanted


_ISO = re.compile(r"(-?\d{4,}-\d\d-\d\d)(?:T(\d\d:\d\d:\d\d(?:\.\d+)?))?")


def iso_text(value: str) -> str:
    """An ISO 8601 date (with time) as shown: "2026-11-11", the time only if
    there is one ("2026-11-11 14:30:00"); anything else unchanged."""
    match = _ISO.fullmatch(value)
    if not match:
        return value
    day, time = match.groups()
    return day if not time or time == "00:00:00" else "%s %s" % (day, time)


def column_letter(index: int) -> str:
    """0 -> "A", 25 -> "Z", 26 -> "AA", as spreadsheets name columns."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def column_names(header: list[str]) -> list[str]:
    """Unique names: stripped, the column letter for an empty one, " (2)" for
    a repeated one. A name written in the header is never given to another
    column as a suffixed or letter name."""
    explicit = [name.strip() for name in header]
    blocked = {name for name in explicit if name}
    used: set[str] = set()
    names = []
    for index, name in enumerate(explicit):
        unique = name if name and name not in used else _unique(index, name, blocked | used)
        used.add(unique)
        names.append(unique)
    return names


def _unique(index: int, name: str, taken: set[str]) -> str:
    base = name or column_letter(index)
    if base not in taken:
        return base
    number = 2
    while "%s (%d)" % (base, number) in taken:
        number += 1
    return "%s (%d)" % (base, number)


def _filled_length(cells: list[str]) -> int:
    """The length without the empty cells at the end."""
    end = len(cells)
    while end and not cells[end - 1]:
        end -= 1
    return end


class Builder:
    """Collects rows while a file is read and enforces the limits. Rows
    without any filled cell are left out (an empty label is of no use), so
    the empty rest of a sheet costs nothing."""

    def __init__(self) -> None:
        self._rows: list[tuple[list[str], int]] = []
        self._data_rows = -1  # the first row is the header
        self._cells = 0
        self._width = 0
        self._keys: dict[int, list[str]] = {}  # row number in the file: its stored cells
        self._sorted: list[int] = []
        self._blanked = 0

    def add(self, cells: list[str], repeat: int = 1, key: int | None = None) -> None:
        """A row (`repeat` times); `key` (its number in the file) lets
        blank() find it again."""
        end = _filled_length(cells)
        if not end:
            return
        if end > limits.MAX_COLUMNS:
            raise too_large("more than %d columns" % limits.MAX_COLUMNS)
        self._data_rows += repeat
        self._cells += end * repeat
        if self._data_rows > limits.MAX_ROWS or self._cells > limits.MAX_CELLS:
            raise too_large("more than %d rows or %d cells" % (limits.MAX_ROWS, limits.MAX_CELLS))
        stored = cells[:end]
        self._rows.append((stored, repeat))
        if key is not None:
            self._keys[key] = stored

    def blank(self, first_row: int, last_row: int, first: int, last: int) -> None:
        """Empty the cells of a merged range except its first one, which
        shows the value: hidden cells of a merged range count as empty, as
        covered cells in ODS do. The work is bounded by MAX_CELLS, whatever
        ranges a file claims."""
        if len(self._sorted) != len(self._keys):  # rows are all added by now
            self._sorted = sorted(self._keys)
        low = bisect.bisect_left(self._sorted, first_row)
        high = bisect.bisect_right(self._sorted, last_row)
        for key in self._sorted[low:high]:
            cells = self._keys[key]
            start = first + 1 if key == first_row else first
            end = min(last + 1, len(cells))
            self._blanked += max(1, end - start)
            if self._blanked > limits.MAX_CELLS:
                raise too_large("more than %d merged cells" % limits.MAX_CELLS)
            cells[start:end] = [""] * max(0, end - start)

    def table(self) -> Table:
        # rows may have lost cells to blank(): empty ones go, the width shrinks
        filled = [(cells[: _filled_length(cells)], count) for cells, count in self._rows]
        filled = [(cells, count) for cells, count in filled if cells]
        if not filled:
            raise MergeError("empty", "no filled cell")
        self._width = max(len(cells) for cells, _count in filled)
        (header, repeat), *rest = filled
        if repeat > 1:
            rest.insert(0, (header, repeat - 1))
        columns = column_names(self._padded(header))
        rows: list[dict[str, str]] = []
        for cells, count in rest:
            values = dict(zip(columns, self._padded(cells), strict=True))
            rows.extend(dict(values) for _ in range(count))
        return Table(columns, rows)

    def _padded(self, cells: list[str]) -> list[str]:
        return cells + [""] * (self._width - len(cells))
