"""Variables in label texts and code contents, filled in when printing.

    {=date}              today, in the format of the system language
    {=date:%d.%m.%Y}     today with a strftime format
    {=date+30d}          in 30 days ("best before"); d, w, m, y and h, also
                         combined ("+1m-1d") and with a format
    {=time}              now, {=time:%H:%M}
    {=counter}           the label's number: goes up by the step with every
                         printed label, also from copy to copy
    {=counter:0001}      with leading zeros, as wide as the pattern
    {=copy}, {=copies}   number of this copy and of all copies in the job
    {=uuid}              a random UUID (version 4)
    {=id}, {=id:12}      a short random ID (Crockford base 32), 8 characters
    {=NAME}              any other name (letters of any language, digits,
                         "_"): a value given on the command line

Dates and times are in the system's time zone: "+1d" keeps the time of day
(also across a change to or from summer time), "+2h" counts real hours;
%z and %Z give the offset and the zone of that moment.

All variables of one label have the same values: a counter, an ID or a UUID
used twice (text and QR code) shows the same value on that label; the next
label gets new ones.

Why "{=" and not "{date}": serial labels fill "{Column}" from data files,
whose column names someone else chose ("Date", "Counter" and "ID" are
common headings). With plain names a column would silently change what a
variable means, or a variable would shadow a column, depending on the data
file. The "=" marks a computed value, as in a spreadsheet formula, and keeps
both apart for good. Both share one syntax: "{{" and "}}" are literal braces.

Columns and variables are filled in one pass (expand(text, values,
columns)), never one after the other: a column pass first would turn
"{{=date}}" into "{=date}" and the variable pass would then fill it in.

A text without "{=" (and without columns to fill) stays exactly as it is,
"{{" included: labels saved before variables existed print as before.

Deliberately not str.format(): texts come from label files, and format()
would evaluate "{0.__class__}" or pad to "{:>99999999}". A format here is
only ever a strftime pattern or a number of digits.
"""

from __future__ import annotations

import calendar
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo

from etiketto.model import CodeElement, Element, Label, TextElement
from etiketto.render import RenderWarning

# "{{" and "}}" are literal braces; "{=spec}" is a variable, "{spec}" a column
_TOKEN = re.compile(r"\{\{|\}\}|\{(=?)([^{}]*)\}")
# name, offsets ("+30d-1w"), format after the first colon (may contain colons)
_SPEC = re.compile(r"\s*([^\W\d]\w*)\s*((?:[+-]\s*\d+\s*[A-Za-z]+\s*)*)(?::(.*))?\Z", re.DOTALL)
_OFFSET = re.compile(r"([+-])\s*(\d+)\s*([A-Za-z]+)")

DATE_FORMAT = "%x"  # the date as the system language writes it
TIME_FORMAT = "%X"
_WALL_UNITS = {"d": "days", "w": "weeks"}  # calendar days: the time of day stays
ID_LENGTH = 8
MAX_ID_LENGTH = 64
MAX_DIGITS = 30  # leading zeros: "{=counter:0001}" is 4 digits wide
# Crockford's base 32: no I, L, O and U, nothing to mistake when typed in again
ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# names the program fills in; others come from the command line (--set)
BUILTIN = frozenset({"date", "time", "counter", "copy", "copies", "uuid", "id"})
# names whose value differs from one printed label to the next
PER_COPY = frozenset({"counter", "copy", "uuid", "id"})


@dataclass
class Values:
    """What the variables of one label are filled with. `random` returns n
    random bytes for UUID and ID (os.urandom; the preview passes a fixed
    source so the values do not change with every keystroke)."""

    now: datetime
    counter: int = 1  # the counter of the first label of the job
    step: int = 1
    copy: int = 1
    copies: int = 1
    custom: Mapping[str, str] = field(default_factory=dict)
    random: Callable[[int], bytes] = os.urandom
    # one UUID and one ID per label, however often they are used on it
    _cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def for_label(cls, label: Label, now: datetime, copies: int = 1, **kwargs) -> Values:
        """The values of the first label of a job, the counter from the label."""
        return cls(now, label.counter_next, label.counter_step, copies=copies, **kwargs)

    def for_copy(self, copy: int) -> Values:
        """The values of the given copy (1-based) of the same job."""
        return replace(self, copy=copy)

    @property
    def number(self) -> int:
        """The counter of this label."""
        return self.counter + (self.copy - 1) * self.step

    def cached(self, name: str, make: Callable[[], str]) -> str:
        if name not in self._cache:
            self._cache[name] = make()
        return self._cache[name]


# --- one variable -----------------------------------------------------------


def _add_months(moment: datetime, months: int) -> datetime:
    """Calendar months; the 31st becomes the last day of a shorter month."""
    index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(index, 12)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def _aware(wall: datetime, zone: tzinfo | None) -> datetime:
    """A wall clock time in `zone`, or in the system's zone (None)."""
    return wall.replace(tzinfo=zone) if zone else wall.astimezone()


def shift(moment: datetime, offsets: str) -> datetime:
    """Apply "+30d", "-1w", "+1m", "+1y", "+2h" (in this order of writing).
    Days, weeks, months and years move the calendar date and keep the time
    of day; hours are real hours, across a change to or from summer time.
    The result knows its time zone (the system's, if `moment` has none)."""
    zone = moment.tzinfo
    wall = moment.replace(tzinfo=None)
    for sign, amount, unit in _OFFSET.findall(offsets):
        n = int(amount) * (-1 if sign == "-" else 1)
        unit = unit.lower()
        if unit in ("m", "y"):
            wall = _add_months(wall, n * (12 if unit == "y" else 1))
        elif unit in _WALL_UNITS:
            wall += timedelta(**{_WALL_UNITS[unit]: n})
        elif unit == "h":
            later = _aware(wall, zone) + timedelta(hours=n)
            wall = later.astimezone(zone).replace(tzinfo=None)
        else:
            raise ValueError("unknown unit %r" % unit)
    return _aware(wall, zone)


def _plain(offsets: str, fmt: str | None) -> None:
    """Variables that take neither an offset nor a format."""
    if offsets or fmt is not None:
        raise ValueError("no offset or format allowed")


def _digits(fmt: str | None) -> int:
    """A width written as a pattern of digits ("0001": 4) or none."""
    if fmt is None:
        return 1
    fmt = fmt.strip()
    if not fmt.isdigit() or not 0 < len(fmt) <= MAX_DIGITS:
        raise ValueError("not a width: %r" % fmt)
    return len(fmt)


def _number(value: int, offsets: str, fmt: str | None) -> str:
    if offsets:
        raise ValueError("a number takes no offset")
    return "%0*d" % (_digits(fmt), value)


def _moment(values: Values, offsets: str, fmt: str | None, default: str) -> str:
    text = shift(values.now, offsets).strftime(default if fmt is None else fmt)
    if not text and fmt and fmt.strip():  # a format the C library does not know
        raise ValueError("empty date for %r" % fmt)
    return text


def _short_id(values: Values, offsets: str, fmt: str | None) -> str:
    if offsets:
        raise ValueError("an ID takes no offset")
    length = ID_LENGTH if fmt is None else int(fmt.strip() or 0)
    if not 0 < length <= MAX_ID_LENGTH:
        raise ValueError("ID length %d" % length)

    def make() -> str:
        # 256 is a multiple of 32: every character is equally likely
        return "".join(ID_ALPHABET[b % 32] for b in values.random(MAX_ID_LENGTH))

    return values.cached("id", make)[:length]


def _uuid(values: Values, offsets: str, fmt: str | None) -> str:
    _plain(offsets, fmt)
    return values.cached("uuid", lambda: str(uuid.UUID(bytes=values.random(16), version=4)))


_HANDLERS: dict[str, Callable[[Values, str, str | None], str]] = {
    "date": lambda v, o, f: _moment(v, o, f, DATE_FORMAT),
    "time": lambda v, o, f: _moment(v, o, f, TIME_FORMAT),
    "counter": lambda v, o, f: _number(v.number, o, f),
    "copy": lambda v, o, f: _number(v.copy, o, f),
    "copies": lambda v, o, f: _number(v.copies, o, f),
    "uuid": _uuid,
    "id": _short_id,
}


def _custom(values: Values, name: str, offsets: str, fmt: str | None) -> str | None:
    if name not in values.custom:
        return None
    _plain(offsets, fmt)
    return values.custom[name]


def resolve(spec: str, values: Values) -> str | None:
    """The value of one variable ("date+30d:%d.%m.%Y", without "{=" and "}");
    None if there is no such variable or the offset or format is invalid."""
    match = _SPEC.match(spec)
    if not match:
        return None
    name, offsets, fmt = match.groups()
    handler = _HANDLERS.get(name)
    try:
        if handler is None:
            return _custom(values, name, offsets, fmt)
        return handler(values, offsets, fmt)
    except (ValueError, OverflowError, OSError):  # bad offset, format or width; a year
        return None  # past 9999 (OSError: a moment the C library cannot convert)


def variable_name(spec: str) -> str | None:
    """The name of a valid variable: one that could be filled in (a custom
    name takes neither offset nor format); None for anything else."""
    match = _SPEC.match(spec)
    if not match:
        return None
    name, offsets, fmt = match.groups()
    if name not in _HANDLERS:
        return name if not offsets and fmt is None else None
    return name if resolve(spec, _PROBE) is not None else None


# a valid date format gives text on any day; random bytes are not needed
_PROBE = Values(datetime(2000, 1, 1, 12), random=lambda n: bytes(n))


# --- texts ------------------------------------------------------------------


def expand(
    text: str, values: Values, columns: Mapping[str, str] | None = None
) -> tuple[str, list[str]]:
    """`text` with its variables (and, if given, the "{Column}" placeholders
    of serial labels) filled in, and the placeholders that could not be:
    they stay in the text as they are, so the label shows them."""
    if columns is None and "{=" not in text:
        return text, []
    unknown: list[str] = []

    def replace_token(match: re.Match[str]) -> str:
        whole, marker, spec = match.group(0), match.group(1), match.group(2)
        if marker is None:  # "{{" or "}}"
            return whole[0]
        if marker:
            value = resolve(spec, values)
        else:
            value = None if columns is None else columns.get(spec.strip())
            if value is None and columns is None:
                return whole  # plain braces, no serial labels: not a placeholder
        if value is None:
            unknown.append(whole)
            return whole
        return value

    result = _TOKEN.sub(replace_token, text)
    return result, unknown + _unclosed(text)


def _unclosed(text: str) -> list[str]:
    """ "{=" outside any placeholder: one not closed ("{=date"), or one with a
    brace inside ("{={=date}}"). The text stays as it is; the label shows it."""
    found = []
    position = 0
    for match in [*_TOKEN.finditer(text), None]:
        end = match.start() if match else len(text)
        gap = text[position:end]
        start = gap.find("{=")
        while start >= 0:
            found.append(_FRAGMENT.match(gap, start).group(0))  # type: ignore[union-attr]
            start = gap.find("{=", start + 2)
        position = match.end() if match else end
    return found


_FRAGMENT = re.compile(r"\{=[^{}\s]{0,20}")


def names(text: str) -> set[str]:
    """The names of the variables `text` uses."""
    if "{=" not in text:
        return set()
    found = set()
    for match in _TOKEN.finditer(text):
        if match.group(1):  # only valid uses: "{=counter+1d}" is no counter
            name = variable_name(match.group(2))
            if name:
                found.add(name)
    return found


def _content(element: Element) -> str | None:
    if isinstance(element, TextElement):
        return element.text
    if isinstance(element, CodeElement):
        return element.data
    return None


def _with_content(element: Element, content: str) -> Element:
    if isinstance(element, TextElement):
        return replace(element, text=content)
    assert isinstance(element, CodeElement)  # only they have a content (_content)
    return replace(element, data=content)


def label_names(label: Label) -> set[str]:
    """The names of all variables the label uses."""
    found: set[str] = set()
    for element in label.elements:
        content = _content(element)
        if content:
            found |= names(content)
    return found


def uses_counter(label: Label) -> bool:
    return "counter" in label_names(label)


def varies_per_copy(label: Label) -> bool:
    """True if each printed label needs a render of its own."""
    return not PER_COPY.isdisjoint(label_names(label))


def expand_label(label: Label, values: Values) -> tuple[Label, list[RenderWarning]]:
    """A copy of the label with its variables filled in (the label stays as
    it is), and a warning for each variable that could not be."""
    warnings: list[RenderWarning] = []
    elements: list[Element] = []
    for index, element in enumerate(label.elements, 1):
        content = _content(element)
        if content is not None and "{=" in content:
            content, unknown = expand(content, values)
            warnings += [
                # a placeholder ends with "}", a variable not closed does not
                RenderWarning(
                    "unknown_variable" if u.endswith("}") else "unclosed_variable",
                    {"n": index, "name": u},
                )
                for u in unknown
            ]
            element = _with_content(element, content)
        elements.append(element)
    return replace(label, elements=elements), warnings
