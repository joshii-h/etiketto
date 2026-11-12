"""OpenDocument spreadsheets (LibreOffice): content.xml.

Cells hold what they show in <text:p>, so values are taken from there (a
price "CHF 3.50", a boolean "WAHR"), except dates, which are given as ISO
dates for every file type alike. LibreOffice writes the unused rest of a
sheet as cells and rows repeated up to a million times; empty ones are only
counted, never expanded.
"""

from __future__ import annotations

import zipfile
from collections import deque
from collections.abc import Callable, Iterator
from xml.etree import ElementTree as ET

from etiketto.merge import limits
from etiketto.merge.safexml import events
from etiketto.merge.table import Builder, MergeError, Table, iso_text, matches, too_large

_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

TABLE, ROW = _TABLE + "table", _TABLE + "table-row"
CELL, COVERED = _TABLE + "table-cell", _TABLE + "covered-table-cell"
PARAGRAPH = _TEXT + "p"
_RECORDS = frozenset({"table-row"})


class _Sheets:
    """Follows the sheets (top-level tables) while content.xml is parsed;
    tables inside cells are not sheets."""

    def __init__(self, wanted: str | int | None):
        self.wanted = wanted
        self.names: list[str] = []
        self.depth = 0
        self.selected = False
        self.found = False

    def table(self, event: str, elem: ET.Element) -> bool:
        """Track a table start or end; True when the wanted sheet is complete."""
        if event == "start":
            self.depth += 1
            if self.depth == 1:
                if len(self.names) >= limits.MAX_SHEETS:
                    raise too_large("more than %d sheets" % limits.MAX_SHEETS)
                self.names.append(elem.get(_TABLE + "name", ""))
                self.selected = matches(self.wanted, len(self.names) - 1, self.names[-1])
                self.found = self.found or self.selected
            return False
        self.depth -= 1
        return self.depth == 0 and self.selected


def sheet_names(zf: zipfile.ZipFile) -> list[str]:
    sheets = _Sheets(-1)  # no sheet has a negative index: read them all
    deque(_rows(zf, sheets), maxlen=0)
    return sheets.names


def read(zf: zipfile.ZipFile, wanted: str | int | None) -> Table:
    sheets = _Sheets(wanted)
    builder = Builder()
    for row in _rows(zf, sheets):
        builder.add(_cells(row), _count(row.get(_TABLE + "number-rows-repeated")))
    if not sheets.selected:
        raise MergeError("not_table", "no sheet %r among %r" % (wanted, sheets.names))
    return builder.table()


def read_book(zf: zipfile.ZipFile, wanted: str | None) -> tuple[list[str], Table]:
    """The names of all sheets and the table of the wanted one (the first
    if there is none of that name), in one pass over content.xml when the
    sheet exists."""
    sheets = _Sheets(wanted)
    builder = Builder()
    for row in _rows(zf, sheets, stop=False):
        builder.add(_cells(row), _count(row.get(_TABLE + "number-rows-repeated")))
    if sheets.found:
        return sheets.names, builder.table()
    if wanted is None:
        raise MergeError("not_table", "no sheets")
    return read_book(zf, None)


def _rows(zf: zipfile.ZipFile, sheets: _Sheets, stop: bool = True) -> Iterator[ET.Element]:
    """The rows of the selected sheet; with `stop` False the rest of the
    file is read too, for the names of the sheets after it."""
    tags = 0  # rows of the sheet, filled or not
    for event, elem in events(zf, "content.xml", _RECORDS):
        if elem.tag == TABLE:
            if sheets.table(event, elem) and stop:
                return
            tags = _sheet_start(sheets, event, tags)
        elif event == "end" and elem.tag == ROW:
            tags = _counted(tags)
            if sheets.selected and sheets.depth == 1:
                yield elem


def _sheet_start(sheets: _Sheets, event: str, tags: int) -> int:
    """The row count starts again with each sheet."""
    return 0 if sheets.depth == 1 and event == "start" else tags


def _counted(tags: int) -> int:
    tags += 1
    if tags > limits.MAX_ROW_TAGS:
        raise too_large("more than %d rows" % limits.MAX_ROW_TAGS)
    return tags


def _cells(row: ET.Element) -> list[str]:
    cells: list[str] = []
    pending = 0  # empty cells not added yet: only needed before a filled one
    for value, repeat in _entries(row):
        if not value:
            pending += repeat
            continue
        if len(cells) + pending + repeat > limits.MAX_COLUMNS:
            raise too_large("more than %d columns" % limits.MAX_COLUMNS)
        cells.extend([""] * pending + [value] * repeat)
        pending = 0
    return cells


def _entries(row: ET.Element) -> Iterator[tuple[str, int]]:
    """(value, how often) for each cell of a row; covered cells (hidden by a
    merged cell) are empty."""
    for cell in row:
        if cell.tag in (CELL, COVERED):
            value = _value(cell) if cell.tag == CELL else ""
            yield value, _count(cell.get(_TABLE + "number-columns-repeated"))


def _count(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


def _value(cell: ET.Element) -> str:
    date = cell.get(_OFFICE + "date-value")
    if date and cell.get(_OFFICE + "value-type") == "date":
        return iso_text(date)
    paragraphs = [_text(p) for p in cell if p.tag == PARAGRAPH]
    if paragraphs:
        return "\n".join(paragraphs)
    return _stored(cell)


def _stored(cell: ET.Element) -> str:
    """The value of a cell without displayed text."""
    if cell.get(_OFFICE + "value-type") == "boolean":
        return "TRUE" if cell.get(_OFFICE + "boolean-value") == "true" else "FALSE"
    return cell.get(_OFFICE + "value") or cell.get(_OFFICE + "string-value") or ""


def _spaces(elem: ET.Element) -> str:
    return " " * min(_count(elem.get(_TEXT + "c")), limits.MAX_SPACES)


_SPECIAL: dict[str, Callable[[ET.Element], str]] = {
    _TEXT + "s": _spaces,
    _TEXT + "tab": lambda _: "\t",
    _TEXT + "line-break": lambda _: "\n",
    _OFFICE + "annotation": lambda _: "",  # a comment on the cell
}


def _text(elem: ET.Element) -> str:
    """The text of a paragraph with its spans, links and spaces."""
    parts = [elem.text or ""]
    for child in elem:
        special = _SPECIAL.get(child.tag)
        parts.append(special(child) if special else _text(child))
        parts.append(child.tail or "")
    return "".join(parts)
