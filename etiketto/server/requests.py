"""The JSON body of a preview or print request, checked field by field.

Unknown fields are refused (a misspelt "copeis" must not print one label
instead of fifty). Data rows given inline are held to the limits of data
files (merge/limits.py).
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from etiketto.cli import ALL_ROWS, Assignment, _rows
from etiketto.merge import limits
from etiketto.model import MAX_COUNTER
from etiketto.server.errors import ApiError, bad_request, invalid_field
from etiketto.ui.i18n import _

MAX_COPIES = 9999
MAX_ASSIGNMENTS = 1000
MAX_VALUE = 100_000  # characters of one value or cell
MAX_WAIT = 300.0  # seconds a print request may wait for its job

COMMON = frozenset(
    {
        "label",
        "label_content",
        "printer",
        "set",
        "set_literal",
        "counter_start",
        "data_rows",
        "data_file",
        "sheet",
    }
)
PREVIEW = COMMON | {"row", "format", "view"}
PRINT = COMMON | {"host", "copies", "rows", "no_save_counter", "wait"}


@dataclass
class LabelRequest:
    """What both a preview and a print need: the label and its values."""

    label: str | None = None  # a name in the labels folder
    content: dict | None = None  # or the label itself, as in a .etiketto file
    printer: str | None = None
    assignments: list[Assignment] = field(default_factory=list)
    counter_start: int | None = None
    data_rows: list[dict[str, str]] | None = None
    data_file: str | None = None
    sheet: str = ""


@dataclass
class PreviewRequest(LabelRequest):
    row: int = 1  # which data row, counted from 1
    format: str = "png"  # or "json": the PNG in base64 and the warnings
    view: str = "preview"  # or "printer": the image as the printer gets it


@dataclass
class PrintRequest(LabelRequest):
    host: str | None = None
    copies: int = 1
    rows: tuple[int, int] | None = None  # from, to, counted from 1
    no_save_counter: bool = False
    wait: float = 0.0


def preview_request(body: object) -> PreviewRequest:
    data = _object(body, PREVIEW)
    request = PreviewRequest(**_common(data))
    request.row = _integer(data, "row", 1, limits.MAX_ROWS, 1)
    request.format = _choice(data, "format", ("png", "json"))
    request.view = _choice(data, "view", ("preview", "printer"))
    return request


def print_request(body: object) -> PrintRequest:
    data = _object(body, PRINT)
    request = PrintRequest(**_common(data))
    request.host = _text(data, "host")
    request.copies = _integer(data, "copies", 1, MAX_COPIES, 1)
    request.rows = _row_range(data.get("rows"))
    request.no_save_counter = _flag(data, "no_save_counter")
    request.wait = _number(data, "wait", 0.0, MAX_WAIT)
    return request


def _object(body: object, allowed: frozenset[str]) -> dict:
    if not isinstance(body, dict):
        raise bad_request(_("The request must be a JSON object"))
    if unknown := sorted(set(body) - allowed):
        raise bad_request(_("Unknown field: %s") % unknown[0], "unknown_field")
    return body


def _common(data: dict) -> dict:
    label, content = data.get("label"), data.get("label_content")
    if (label is None) == (content is None):
        raise bad_request(_("Give the label either by name or as content"), "no_label")
    if content is not None and not isinstance(content, dict):
        raise invalid_field("label_content")
    rows, data_file = data.get("data_rows"), data.get("data_file")
    if rows is not None and data_file is not None:
        raise bad_request(_("Give data rows or a data file, not both"), "data_conflict")
    return {
        "label": _text(data, "label"),
        "content": content,
        "printer": _text(data, "printer"),
        "assignments": _assignments(data, "set", False) + _assignments(data, "set_literal", True),
        "counter_start": _integer(data, "counter_start", 0, MAX_COUNTER, None),
        "data_rows": None if rows is None else data_rows(rows),
        "data_file": _text(data, "data_file"),
        "sheet": _text(data, "sheet") or "",
    }


def _text(data: Mapping, name: str) -> str | None:
    value = data.get(name)
    if value is not None and (not isinstance(value, str) or len(value) > MAX_VALUE):
        raise invalid_field(name)
    return value


def _flag(data: Mapping, name: str) -> bool:
    value = data.get(name, False)
    if not isinstance(value, bool):
        raise invalid_field(name)
    return value


def _integer(data: Mapping, name: str, lower: int, upper: int, default):
    value = data.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise invalid_field(name)
    return value


def _number(data: Mapping, name: str, lower: float, upper: float) -> float:
    value = data.get(name, lower)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_field(name)
    if not math.isfinite(value) or not lower <= value <= upper:
        raise invalid_field(name)
    return float(value)


def _choice(data: Mapping, name: str, allowed: tuple[str, ...]) -> str:
    value = data.get(name, allowed[0])
    if value not in allowed:
        raise invalid_field(name)
    return value


def _cell(value: object) -> str | None:
    """A value as text: text, a number or nothing (an empty cell)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return str(value)
    if isinstance(value, str) and len(value) <= MAX_VALUE:
        return value
    return None


def _assignments(data: Mapping, name: str, literal: bool) -> list[Assignment]:
    values = data.get(name)
    if values is None:
        return []
    if not isinstance(values, dict) or len(values) > MAX_ASSIGNMENTS:
        raise invalid_field(name)
    result = []
    for key, value in values.items():
        text = _cell(value)
        if not key.strip() or text is None:
            raise invalid_field(name)
        result.append(Assignment(key.strip(), text, literal))
    return result


def data_rows(rows: object) -> list[dict[str, str]]:
    """Inline data: a list of objects, one per row, column name -> value.
    Every row gets every column (an empty cell where one is missing), as
    rows of a data file have."""
    if not isinstance(rows, list) or not rows:
        raise invalid_field("data_rows")
    if len(rows) > limits.MAX_ROWS:
        raise ApiError(413, "too_large", _("Too many data rows: at most %d") % limits.MAX_ROWS)
    columns: dict[str, None] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise invalid_field("data_rows")
        columns.update(dict.fromkeys(row))
        if len(columns) > limits.MAX_COLUMNS:
            raise ApiError(
                413, "too_large", _("Too many columns: at most %d") % limits.MAX_COLUMNS
            )
    if len(rows) * len(columns) > limits.MAX_CELLS:
        raise ApiError(413, "too_large", _("Too many cells: at most %d") % limits.MAX_CELLS)
    if "" in columns:
        raise invalid_field("data_rows")
    return [_row(row, columns) for row in rows]


def _row(row: dict, columns: dict[str, None]) -> dict[str, str]:
    cells = {}
    for name in columns:
        text = _cell(row.get(name))
        if text is None:
            raise invalid_field("data_rows")
        cells[name] = text
    return cells


def _row_range(value: object) -> tuple[int, int] | None:
    """ "3-10", "5", "7-" (to the last row) or "-4", as on the command line."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise invalid_field("rows")
    try:
        first, last = _rows(value)
    except argparse.ArgumentTypeError as e:
        raise bad_request(str(e), "invalid_field") from None
    return first, min(last, ALL_ROWS)
