"""Office Open XML workbooks (Excel): the workbook names the sheets, its
relations lead to their parts; strings may be shared, numbers get their
look from the cell's style. Tags are matched without their namespace, so
"Strict" files (other namespaces, same names) are read too.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from etiketto.merge import limits
from etiketto.merge.numfmt import BUILTIN, display
from etiketto.merge.safexml import drop, events, local, parse
from etiketto.merge.table import (
    Builder,
    MergeError,
    Table,
    damaged,
    iso_text,
    matches,
    too_large,
)

WORKBOOK = "xl/workbook.xml"
_RELATIONS = "xl/_rels/workbook.xml.rels"
_SHARED_STRINGS = "xl/sharedStrings.xml"
_STYLES = "xl/styles.xml"
_REFERENCE = re.compile(r"([A-Za-z]{1,3})([0-9]+)")
_EXCEL_COLUMNS = 16384  # A to XFD


@dataclass
class _Workbook:
    names: list[str] = field(default_factory=list)
    ids: list[str | None] = field(default_factory=list)  # relation of each sheet
    date1904: bool = False


def _workbook(zf: zipfile.ZipFile) -> _Workbook:
    book = _Workbook()
    for elem in parse(zf, WORKBOOK).iter():
        name = local(elem.tag)
        if name == "sheet":
            book.names.append(elem.get("name", ""))
            book.ids.append(_attribute(elem, "id"))  # r:id
        elif name == "workbookPr":
            book.date1904 = elem.get("date1904", "").lower() in ("1", "true")
    return book


def _attribute(elem: ET.Element, name: str) -> str | None:
    return next((value for key, value in elem.attrib.items() if local(key) == name), None)


def sheet_names(zf: zipfile.ZipFile) -> list[str]:
    return _workbook(zf).names


def read(zf: zipfile.ZipFile, wanted: str | int | None) -> Table:
    book = _workbook(zf)
    index = next((i for i, name in enumerate(book.names) if matches(wanted, i, name)), None)
    if index is None:
        raise MergeError("not_table", "no sheet %r among %r" % (wanted, book.names))
    part = _sheet_part(zf, book.ids[index])
    present = set(zf.namelist())
    cells = _Cells(_shared_strings(zf, present), _formats(zf, present), book.date1904)
    builder = Builder()
    for event, elem, parent in events(zf, part):
        if event == "end" and local(elem.tag) == "row":
            builder.add(cells.row(elem))
            drop(elem, parent)
    return builder.table()


def _sheet_part(zf: zipfile.ZipFile, relation: str | None) -> str:
    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in parse(zf, _RELATIONS)
        if local(rel.tag) == "Relationship"
    }
    if relation is None or relation not in targets:
        raise damaged("sheet relation %r missing" % relation)
    target = targets[relation]
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath("xl/" + target)


def _shared_strings(zf: zipfile.ZipFile, present: set[str]) -> list[str]:
    strings: list[str] = []
    if _SHARED_STRINGS not in present:
        return strings
    for event, elem, parent in events(zf, _SHARED_STRINGS):
        if event == "end" and local(elem.tag) == "si":
            if len(strings) >= limits.MAX_CELLS:
                raise too_large("more than %d shared strings" % limits.MAX_CELLS)
            strings.append(_rich_text(elem))
            drop(elem, parent)
    return strings


def _rich_text(elem: ET.Element) -> str:
    """The text of a shared or inline string: <t>, or runs <r><t>; phonetic
    guides (<rPh>) are not part of it."""
    runs = (_t_text(run) for run in elem if local(run.tag) == "r")
    return _t_text(elem) + "".join(runs)


def _t_text(elem: ET.Element) -> str:
    return "".join(t.text or "" for t in elem if local(t.tag) == "t")


def _formats(zf: zipfile.ZipFile, present: set[str]) -> list[str]:
    """The number format code of each cell style (cellXfs), "" = General."""
    if _STYLES not in present:
        return []
    root = parse(zf, _STYLES)
    custom = {
        elem.get("numFmtId"): elem.get("formatCode", "")
        for elem in root.iter()
        if local(elem.tag) == "numFmt"
    }
    styles = next((list(elem) for elem in root.iter() if local(elem.tag) == "cellXfs"), [])
    numbers = (xf.get("numFmtId", "0") for xf in styles)
    return [custom.get(number) or BUILTIN.get(number, "") for number in numbers]


class _Cells:
    def __init__(self, strings: list[str], formats: list[str], date1904: bool):
        self.strings = strings
        self.formats = formats
        self.date1904 = date1904

    def row(self, elem: ET.Element) -> list[str]:
        """The values of a row; cells name their column ("C7"), missing ones
        are empty, a cell without a name follows the one before."""
        cells: list[str] = []
        position = 0
        for cell in elem:
            if local(cell.tag) != "c":
                continue
            position = _column(cell.get("r"), position)
            value = self.value(cell)
            if value:
                _place(cells, position, value)
            position += 1
        return cells

    def value(self, cell: ET.Element) -> str:
        kind = cell.get("t", "n")
        if kind == "inlineStr":
            return _rich_text(_child(cell, "is"))
        raw = _child_text(cell, "v")
        if not raw:
            return ""
        if kind == "n":
            return display(raw, self._format(cell.get("s")), self.date1904)
        convert = {"s": self._shared, "b": _boolean, "d": iso_text}.get(kind)
        return convert(raw) if convert else raw  # "str" (formula), "e" (error)

    def _shared(self, raw: str) -> str:
        index = int(raw)
        if not 0 <= index < len(self.strings):
            raise damaged("no shared string %d" % index)
        return self.strings[index]

    def _format(self, style: str | None) -> str:
        try:
            index = int(style or 0)
        except ValueError:
            return ""
        return self.formats[index] if 0 <= index < len(self.formats) else ""


def _boolean(raw: str) -> str:
    return "TRUE" if raw.strip() == "1" else "FALSE"


def _child(elem: ET.Element, name: str) -> ET.Element:
    """The first child of that name, else an empty element (an element
    without children is false: test for None, never for truth)."""
    return next((child for child in elem if local(child.tag) == name), ET.Element(name))


def _child_text(elem: ET.Element, name: str) -> str:
    return _child(elem, name).text or ""


def _column(reference: str | None, default: int) -> int:
    if reference is None:
        return default
    match = _REFERENCE.fullmatch(reference)
    if not match:
        raise damaged("invalid cell reference %r" % reference)
    number = 0
    for letter in match.group(1).upper():
        number = number * 26 + ord(letter) - 64
    if number > _EXCEL_COLUMNS:
        raise damaged("invalid cell reference %r" % reference)
    return number - 1


def _place(cells: list[str], index: int, value: str) -> None:
    if index >= limits.MAX_COLUMNS:
        raise too_large("more than %d columns" % limits.MAX_COLUMNS)
    if index >= len(cells):
        cells.extend([""] * (index - len(cells) + 1))
    cells[index] = value
