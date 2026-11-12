"""Reading the XML parts of ODS and XLSX files, which are untrusted.

Each part is checked against its size limit before and while it is read.
Large parts are parsed as a stream: finished elements are dropped, only
the row being read is kept (events()). Nesting is limited too. Files with a
document type declaration are refused: spreadsheets never have one, and it
is what entity expansion attacks (billion laughs, external entities) need.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO
from xml.etree import ElementTree as ET

from tavletta.merge import limits
from tavletta.merge.table import check_cancelled, damaged, too_large

_ENCODING = re.compile(rb"(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']*)")
_UTF8 = (b"utf-8", b"utf8")


# text and complete ordinary tags (attribute values may hold ">"), as far as
# they go: the regular expression engine skips them, the tracker below only
# looks at what is left (a tag that goes on in the next read, a comment). A
# tag complete within one read is no longer than the read, which is far
# below MAX_TAG_BYTES; only tags that go on are measured.
# (possessive: a tag cut off at the end of the read is given up at once).
# Complete comments, CDATA sections and processing instructions are
# skipped too: a flood of tiny ones would otherwise run the slow path.
_SKIP = re.compile(
    rb"""(?:[^<]++"""
    rb"""|<(?![!?])[^<>"']*+(?:(?:"[^"]*+"|'[^']*+')[^<>"']*+)*+>"""
    rb"""|<!--(?:[^-]++|-(?!->))*+-->"""
    rb"""|<!\[CDATA\[(?:[^\]]++|\](?!\]>))*+\]\]>"""
    rb"""|<\?(?:[^?]++|\?(?!>))*+\?>)*+"""
)
_TAG_STOP = re.compile(rb"""["'>]""")
# markup whose end is not a ">" outside quotes. The parser holds a comment
# or processing instruction whole; CDATA it passes on in pieces, as text,
# so a long one is no more than a long text (bounded by the part's size).
_SPECIAL = ((b"<!--", b"-->"), (b"<![CDATA[", b"]]>"), (b"<?", b"?>"))
_CDATA_END = b"]]>"
_LOOKAHEAD = 9  # len(b"<![CDATA[")
_TEXT, _TAG, _QUOTE, _OTHER = range(4)


class _TagTracker:
    """Measures each start or end tag across reads: the parser holds a tag
    whole until its ">", and ">" is allowed in attribute values, so a tag
    ends at the first ">" outside quotes. Comments, CDATA sections and
    processing instructions end at their own marks and are measured too."""

    def __init__(self) -> None:
        self.state = _TEXT
        self.end = b""  # what ends a quote or other markup
        self.length = 0  # of the open tag or markup so far
        self.carry = b""  # undecided bytes from the end of the last read

    def feed(self, data: bytes) -> None:
        window, self.carry = self.carry + data, b""
        pos = 0
        while pos < len(window):
            pos = self._step(window, pos)

    def _step(self, window: bytes, pos: int) -> int:
        if self.state == _TEXT:
            return self._text(window, pos)
        if self.state == _OTHER:
            return self._other(window, pos)
        if self.state == _QUOTE:
            found = window.find(self.end, pos)
            stop = found + 1 if found >= 0 else len(window)
            self.state = _TAG if found >= 0 else _QUOTE
        else:
            match = _TAG_STOP.search(window, pos)
            stop = match.end() if match else len(window)
            if match:
                self.end = match.group()
                self.state = _TEXT if self.end == b">" else _QUOTE
        self._grow(stop - pos, "tag")
        return stop

    def _text(self, window: bytes, pos: int) -> int:
        pos = _SKIP.match(window, pos).end()  # type: ignore[union-attr]
        if pos >= len(window):
            return pos
        head = window[pos : pos + _LOOKAHEAD]
        for start, end in _SPECIAL:
            if head.startswith(start):
                self.state, self.end, self.length = _OTHER, end, 0
                return pos + len(start)
            if len(head) < len(start) and start.startswith(head):
                self.carry = window[pos:]  # "<!" at the end: which one is it?
                return len(window)
        self.state, self.length = _TAG, 0
        return pos + 1

    def _other(self, window: bytes, pos: int) -> int:
        found = window.find(self.end, pos)
        if found >= 0:
            self._grow(found - pos, "comment or processing instruction")
            self.state = _TEXT
            return found + len(self.end)
        # the end mark may be split: look at its first bytes again next time
        keep = min(len(self.end) - 1, len(window) - pos)
        self.carry = window[len(window) - keep :]
        self._grow(len(window) - pos - keep, "comment or processing instruction")
        return len(window)

    def _grow(self, count: int, what: str) -> None:
        if self.end == _CDATA_END and self.state == _OTHER:
            return  # streamed by the parser: not held whole
        self.length += count
        if self.length > limits.MAX_TAG_BYTES:
            raise too_large("XML %s longer than %d bytes" % (what, limits.MAX_TAG_BYTES))


class GuardedStream:
    """A file object for the XML parser that stops at `cap` bytes, whatever
    the zip directory claims, at a DOCTYPE or ENTITY declaration and at a
    tag (or comment) longer than MAX_TAG_BYTES: the parser builds an element
    only when its start tag is complete, so millions of attributes in one
    tag would fill memory before any other check could see them."""

    CHUNK = 64 * 1024
    _MARKS = (b"<!DOCTYPE", b"<!ENTITY")

    def __init__(self, raw: IO[bytes], cap: int):
        self._raw = raw
        self._cap = cap
        self._count = 0
        self._tail = b""  # a declaration may be split between two reads
        self._tags = _TagTracker()
        self._markups = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(self.CHUNK if size < 0 else min(size, self.CHUNK))
        if not self._count:
            _check_encoding(data)
        self._count += len(data)
        if self._count > self._cap:
            raise too_large("XML part larger than %d bytes" % self._cap)
        window = self._tail + data
        if any(mark in window for mark in self._MARKS):
            raise damaged("XML with a document type declaration")
        self._tail = window[-len(self._MARKS[0]) :]
        check_cancelled()  # also in a part that holds no elements, only markup
        # comments, CDATA sections and processing instructions: dropped by
        # the parser, but each costs time; a spreadsheet has hardly any
        fresh = window[-len(data) - 1 :]  # with the last byte before: "<" | "!--"
        self._markups += fresh.count(b"<!") + fresh.count(b"<?")
        if self._markups > limits.MAX_MARKUPS:
            raise too_large(
                "more than %d comments, CDATA sections or instructions" % limits.MAX_MARKUPS
            )
        self._tags.feed(data)
        return data


def _check_encoding(start: bytes) -> None:
    """Only UTF-8 (what ODS and XLSX use): in UTF-16 the check for
    declarations above would not see them."""
    if start[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in start[:4]:
        raise damaged("XML not in UTF-8")
    match = _ENCODING.match(start)
    if match and match.group(1).lower() not in _UTF8:
        raise damaged("XML in encoding %r" % match.group(1).decode("ascii", "replace"))


def local(tag: str) -> str:
    """The tag or attribute name without its namespace."""
    return tag.rpartition("}")[2]


@contextmanager
def member(zf: zipfile.ZipFile, name: str, cap: int) -> Iterator[GuardedStream]:
    info = zf.getinfo(name)
    if info.file_size > cap:
        raise too_large("%s larger than %d bytes" % (name, cap))
    with zf.open(info) as raw:
        yield GuardedStream(raw, cap)


def parse(zf: zipfile.ZipFile, name: str) -> ET.Element:
    """A small part (workbook, relations, styles) as a whole, at most
    MAX_PART_BYTES and MAX_DEPTH deep."""
    with member(zf, name, limits.MAX_PART_BYTES) as stream:
        bounds = _Bounds(name)
        root: ET.Element | None = None
        try:
            # not exploitable: GuardedStream refuses DOCTYPE/ENTITY before the parser sees them
            for event, elem in ET.iterparse(stream, events=("start", "end")):  # nosec B314
                root = elem if root is None else root
                if event == "start":
                    bounds.start(elem)
                else:
                    bounds.depth -= 1
        except ET.ParseError as e:
            raise damaged("%s: %s" % (name, e)) from e
    assert root is not None  # iterparse raises ParseError for a part without elements
    return root


_CANCEL_EVERY = 4096  # elements between two looks at the cancel flag


class _Bounds:
    """What the parser keeps besides the elements themselves, checked at
    each start tag: nesting, attributes per element and the distinct tag
    and attribute names (ElementTree caches every name it has seen)."""

    def __init__(self, part: str):
        self.part = part
        self.depth = 0
        self.names: set[str] = set()
        self.elements = 0
        self.kept = 0  # elements of the current outermost record
        self.other = 0  # elements outside records

    def start(self, elem: ET.Element) -> None:
        self.depth += 1
        self.elements += 1
        if not self.elements % _CANCEL_EVERY:
            check_cancelled()
        if self.depth > limits.MAX_DEPTH:
            raise too_large("%s nested deeper than %d" % (self.part, limits.MAX_DEPTH))
        if len(elem.attrib) > limits.MAX_ATTRIBUTES:
            raise too_large("more than %d attributes in an element" % limits.MAX_ATTRIBUTES)
        names = self.names
        if elem.tag not in names or (elem.attrib and not names.issuperset(elem.attrib)):
            names.add(elem.tag)  # a name not seen before: rare after the first rows
            names.update(elem.attrib)
            if len(names) > limits.MAX_NAMES:
                raise too_large("more than %d tag and attribute names" % limits.MAX_NAMES)
        if self.elements > limits.MAX_ELEMENTS:
            raise too_large("more than %d elements in %s" % (limits.MAX_ELEMENTS, self.part))

    def count(self, inside: bool, record: bool) -> None:
        """Elements kept in a record, and the others, in a streamed part."""
        self.kept = self.kept + 1 if inside else 0
        if self.kept > limits.MAX_ROW_ELEMENTS:
            raise too_large("more than %d elements in a row" % limits.MAX_ROW_ELEMENTS)
        self.other += not inside and not record
        if self.other > limits.MAX_OTHER_ELEMENTS:
            raise too_large("more than %d elements" % limits.MAX_OTHER_ELEMENTS)


def events(
    zf: zipfile.ZipFile, name: str, records: frozenset[str]
) -> Iterator[tuple[str, ET.Element]]:
    """("start" | "end", element) while a large part is parsed. Elements
    whose local name is in `records` (rows, shared strings) are kept whole
    until they end, at most MAX_ROW_ELEMENTS elements each; every other
    element is dropped as soon as it ends, and so is a record once the
    caller has seen its end; there are at most MAX_OTHER_ELEMENTS of them,
    MAX_ELEMENTS in all. Memory stays bounded whatever the part holds."""
    with member(zf, name, limits.MAX_MEMBER_BYTES) as stream:
        bounds = _Bounds(name)
        stack: list[ET.Element] = []
        inside = 0  # open records
        kinds: dict[str, bool] = {}  # tag: is it a record
        try:
            # not exploitable, as in parse()
            for event, elem in ET.iterparse(stream, events=("start", "end")):  # nosec B314
                record = kinds.get(elem.tag)
                if record is None:  # at most MAX_NAMES tags: _Bounds counts them
                    record = kinds[elem.tag] = local(elem.tag) in records
                if event == "start":
                    bounds.start(elem)
                    bounds.count(bool(inside), record)
                    inside += record
                    stack.append(elem)
                    yield event, elem
                    continue
                bounds.depth -= 1
                stack.pop()
                yield event, elem
                inside -= record
                if not inside and stack:  # finished and not part of a record
                    stack[-1].remove(elem)
        except ET.ParseError as e:
            raise damaged("%s: %s" % (name, e)) from e
