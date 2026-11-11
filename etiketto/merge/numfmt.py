"""Numbers of XLSX cells as Excel shows them, as far as that is needed for
labels: dates as ISO dates, times, fixed decimals and percent. Thousands
separators, currency symbols, text in the format and fractions are not
reproduced; such numbers come out like Excel's "General" format.

Excel stores dates as days since 1900-01-00 (with the non-existent
1900-02-29 of Lotus 1-2-3 as day 60) or, in the 1904 date system, since
1904-01-01; only the number format of the cell's style tells a date.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

# built-in formats an XLSX file refers to by number only (ECMA-376 18.8.30)
BUILTIN = {
    "1": "0",
    "2": "0.00",
    "3": "#,##0",
    "4": "#,##0.00",
    "9": "0%",
    "10": "0.00%",
    "14": "yyyy-mm-dd",
    "15": "d-mmm-yy",
    "16": "d-mmm",
    "17": "mmm-yy",
    "18": "h:mm AM/PM",
    "19": "h:mm:ss AM/PM",
    "20": "h:mm",
    "21": "h:mm:ss",
    "22": "m/d/yy h:mm",
    "45": "mm:ss",
    "46": "[h]:mm:ss",
    "47": "mm:ss.0",
    "49": "@",
}

# text in quotes, escaped characters, [Red], [$-407], padding (_x) and fill (*x)
_LITERALS = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.')
_ELAPSED = re.compile(r"\[(h+|m+|s+)\]", re.IGNORECASE)  # [h]:mm: hours beyond 24
_AMPM = re.compile(r"am/pm|a/p")
_SECOND_DECIMALS = re.compile(r"s\.(0+)")
_DECIMALS = re.compile(r"\.([0#?]+)")
_LARGEST_GENERAL_INTEGER = 1e15  # beyond that Excel switches to 1E+15 too


@dataclass(frozen=True)
class _Shape:
    """What a date or time format shows."""

    date: bool
    hours: bool
    seconds: bool
    ampm: bool
    elapsed: bool
    decimals: int  # of the seconds

    @property
    def time(self) -> bool:
        return self.hours or self.seconds


def display(raw: str, code: str, date1904: bool) -> str:
    """The text of a number cell: `raw` is the stored value, `code` the
    format of the cell's style ("" = General)."""
    try:
        value = float(raw)
    except ValueError:
        return raw
    if not math.isfinite(value):
        return raw
    shape = _shape(code)
    if shape is None:
        return _number(value, code)
    return _date_time(value, shape, date1904) or general(value)


def general(value: float) -> str:
    if value.is_integer() and abs(value) < _LARGEST_GENERAL_INTEGER:
        return str(int(value))
    return ("%.15g" % value).replace("e", "E")


@lru_cache(maxsize=256)
def _shape(code: str) -> _Shape | None:
    """None for a format that is no date or time."""
    plain = _ELAPSED.sub(r"\1", code)
    lower = _LITERALS.sub("", plain).lower()
    ampm = bool(_AMPM.search(lower))
    lower = _AMPM.sub("", lower)
    hours, seconds = "h" in lower, "s" in lower
    is_date = _is_date(lower, hours or seconds)
    if not (is_date or hours or seconds):
        return None
    decimals = _length(_SECOND_DECIMALS, lower)
    return _Shape(is_date, hours, seconds, ampm, plain != code, decimals)


def _is_date(lower: str, time: bool) -> bool:
    # "m" is the month, or the minutes next to hours or seconds
    return "d" in lower or "y" in lower or ("m" in lower and not time)


def _length(pattern: re.Pattern[str], text: str) -> int:
    """Length of the first group of the pattern's first match, 0 without one."""
    match = pattern.search(text)
    return len(match.group(1)) if match else 0


def _date_time(value: float, shape: _Shape, date1904: bool) -> str | None:
    """None where Excel shows "#####" (before the first day, after 9999)."""
    if value < 0:
        return None
    unit = 10**shape.decimals
    total = round(value * 86400 * unit)
    days, part = divmod(total, 86400 * unit)
    parts = []
    if shape.date:
        # a date alone is the day the value falls on, not the nearest one
        day = _serial_date(days if shape.time else math.floor(value), date1904)
        if day is None:
            return None
        parts.append(day)
    if shape.time:
        parts.append(_clock(total if shape.elapsed else part, shape, unit))
    return " ".join(parts)


def _serial_date(days: int, date1904: bool) -> str | None:
    if date1904:
        start = date(1904, 1, 1)
    elif days == 60:
        return "1900-02-29"  # does not exist, but Excel shows it
    elif days < 1:
        return None
    else:
        start = date(1899, 12, 31) if days < 60 else date(1899, 12, 30)
    try:
        return (start + timedelta(days=days)).isoformat()
    except OverflowError:
        return None


def _clock(units: int, shape: _Shape, unit: int) -> str:
    whole, fraction = divmod(units, unit)
    hours, rest = divmod(whole, 3600)
    minutes, seconds = divmod(rest, 60)
    second = "%02d" % seconds
    if shape.decimals:
        second += ".%0*d" % (shape.decimals, fraction)
    if not shape.hours:  # mm:ss
        return "%02d:%s" % (minutes, second)
    if not shape.elapsed:
        hours %= 24
    tail = ":" + second if shape.seconds else ""
    if shape.ampm:
        return _twelve_hours(hours, minutes, tail)
    return "%02d:%02d%s" % (hours, minutes, tail)


def _twelve_hours(hours: int, minutes: int, tail: str) -> str:
    return "%d:%02d%s %s" % (hours % 12 or 12, minutes, tail, "PM" if hours >= 12 else "AM")


def _number(value: float, code: str) -> str:
    """Fixed decimals and percent from the first section of the format;
    everything else like General."""
    section = _LITERALS.sub("", code).split(";", 1)[0].lower()
    if _is_general(section, value):
        return general(value)
    percent = "%" in section
    # at most 10: 15 digits before and 10 after stay within Decimal's precision
    places = min(_length(_DECIMALS, section), 10)
    number = Decimal(repr(value)) * (100 if percent else 1)
    rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return "%s%s" % (rounded + 0, "%" if percent else "")  # + 0: no "-0.00"


def _is_general(section: str, value: float) -> bool:
    """General, text (@) and scientific formats, and numbers too large for
    fixed decimals."""
    has_digits = any(digit in section for digit in "0#?")
    return "e" in section or not has_digits or abs(value) >= _LARGEST_GENERAL_INTEGER
