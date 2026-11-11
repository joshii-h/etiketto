"""Placeholders in label texts: "{Name}" is replaced by the row's value.

Deliberately not str.format(): the texts come from label files and the
values from data files, and format() would evaluate "{0.__class__}" or
"{Name:>99999999}". Here a placeholder is only ever a column name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# "{{" and "}}" are literal braces, "{...}" without braces inside a placeholder
_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")


def fill(text: str, row: Mapping[str, str]) -> str:
    """`text` with each "{column}" replaced by the row's value; names are
    compared without surrounding spaces. Unknown names stay as they are, so
    the interface can point them out."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name is None:
            return match.group(0)[0]
        key = name.strip()
        if key and key in row:
            return str(row[key])
        return match.group(0)

    return _TOKEN.sub(replace, text)


def placeholders(text: str) -> list[str]:
    """The column names `text` refers to, each once, in order."""
    names: dict[str, None] = {}
    for match in _TOKEN.finditer(text):
        key = (match.group(1) or "").strip()
        if key:
            names[key] = None
    return list(names)
