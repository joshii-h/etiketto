"""Placeholders in label texts: "{Name}" is replaced by the row's value.

The rules are those of variables.expand(), which fills columns and
variables in one pass; these are its column-only forms for code that has
no variable values at hand (checking codes, naming unknown columns).
"""

from __future__ import annotations

from collections.abc import Mapping

from etiketto.variables import column_names, expand

# "{=date}" and the like are variables, not columns: never filled from a row,
# whatever the columns are called
VARIABLE = "="


def fill(text: str, row: Mapping[str, str]) -> str:
    """`text` with each "{column}" replaced by the row's value; variables
    and unknown names stay as they are."""
    return expand(text, None, row)[0]


def placeholders(text: str) -> list[str]:
    """The column names `text` refers to, each once, in order; variables
    ("{=date}") are none."""
    return column_names(text)
