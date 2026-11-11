"""Reading the XML parts of ODS and XLSX files, which are untrusted.

Each part is checked against the size limit before and while it is read,
and parsed as a stream so that finished rows can be dropped. Files with a
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

from etiketto.merge import limits
from etiketto.merge.table import damaged, too_large

_ENCODING = re.compile(rb"(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']*)")
_UTF8 = (b"utf-8", b"utf8")


class GuardedStream:
    """A file object for the XML parser that stops at `cap` bytes, whatever
    the zip directory claims, and at a DOCTYPE or ENTITY declaration."""

    CHUNK = 64 * 1024
    _MARKS = (b"<!DOCTYPE", b"<!ENTITY")

    def __init__(self, raw: IO[bytes], cap: int):
        self._raw = raw
        self._cap = cap
        self._count = 0
        self._tail = b""  # a declaration may be split between two reads

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
def member(zf: zipfile.ZipFile, name: str) -> Iterator[GuardedStream]:
    info = zf.getinfo(name)
    if info.file_size > limits.MAX_MEMBER_BYTES:
        raise too_large("%s larger than %d bytes" % (name, limits.MAX_MEMBER_BYTES))
    with zf.open(info) as raw:
        yield GuardedStream(raw, limits.MAX_MEMBER_BYTES)


def parse(zf: zipfile.ZipFile, name: str) -> ET.Element:
    """A small part (workbook, relations, styles) as a whole."""
    with member(zf, name) as stream:
        try:
            # not exploitable: GuardedStream refuses DOCTYPE/ENTITY before the parser sees them
            return ET.parse(stream).getroot()  # nosec B314
        except ET.ParseError as e:
            raise damaged("%s: %s" % (name, e)) from e


def events(zf: zipfile.ZipFile, name: str) -> Iterator[tuple[str, ET.Element, ET.Element | None]]:
    """("start" | "end", element, parent) while a large part is parsed; the
    caller drops what it has read with drop(element, parent)."""
    with member(zf, name) as stream:
        stack: list[ET.Element] = []
        try:
            # not exploitable, as in parse()
            for event, elem in ET.iterparse(stream, events=("start", "end")):  # nosec B314
                if event == "end":
                    stack.pop()
                yield event, elem, stack[-1] if stack else None
                if event == "start":
                    stack.append(elem)
        except ET.ParseError as e:
            raise damaged("%s: %s" % (name, e)) from e


def drop(elem: ET.Element, parent: ET.Element | None) -> None:
    if parent is not None:
        parent.remove(elem)
