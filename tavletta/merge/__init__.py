"""Data rows for serial labels (mail merge) from CSV, ODS and XLSX files.

Data files are untrusted input, read like label files in model.py: within
the limits in limits.py, and every malformed file raises MergeError with a
code, never another exception (OSError only if the file cannot be read).
ODS and XLSX are read with the standard library (zipfile, ElementTree);
the module contains no third-party code.
"""

from __future__ import annotations

import csv
import io
import os
import re
import stat
import struct
import threading
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TypeVar
from xml.etree import ElementTree as ET

from tavletta.merge import csvfile, limits, ods, xlsx
from tavletta.merge.fill import fill, placeholders
from tavletta.merge.table import MergeError, Table, cancellable, damaged, too_large

__all__ = [
    "Book",
    "MergeError",
    "Table",
    "fill",
    "limits",
    "placeholders",
    "read_book",
    "read_table",
    "sheet_names",
]

_ZIP = (b"PK\x03\x04", b"PK\x05\x06")  # a file, or an empty archive
# the end of central directory record: 22 bytes and a comment of up to 64 KiB
_END_RECORD, _END_SEARCH = b"PK\x05\x06", 22 + 0xFFFF
# ZIP64: a locator right before the end record points to a record with the
# real counts, which zipfile takes over those of the end record
_LOCATOR, _LOCATOR_SIZE = b"PK\x06\x07", 20
_ZIP64_RECORD, _ZIP64_SIZE = b"PK\x06\x06", 56
_ENTRY_BYTES = 1024  # a directory entry with a long name and extra fields
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # old Excel (.xls) and other Office 97 files
_ODF_TYPE = b"application/vnd.oasis.opendocument"
# what the standard library raises for broken archives, XML and text
_BROKEN = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    struct.error,
    csv.Error,
    ET.ParseError,
    EOFError,
    KeyError,
    IndexError,
    ValueError,  # also UnicodeError
    OverflowError,
    NotImplementedError,  # a compression method zipfile does not have
    RuntimeError,  # an encrypted member; RecursionError
    OSError,  # the file is in memory by now: from the content
)
T = TypeVar("T")


def read_table(
    path: str | Path, sheet: str | int | None = None, cancel: threading.Event | None = None
) -> Table:
    """The data rows of a CSV, ODS or XLSX file, told apart by content.
    `sheet` (name or index, default the first) applies to ODS and XLSX.
    Setting `cancel` (from another thread) stops reading with
    MergeError("cancelled")."""
    data = _read(path)
    return _safely(lambda: _table(data, sheet), cancel)


@dataclass
class Book:
    """What the interface needs of a data file: the rows of one sheet and
    the names of all sheets to choose from."""

    table: Table
    sheets: list[str] = field(default_factory=list)  # [] for CSV
    sheet: str = ""  # the sheet the rows are from, "" for CSV


def read_book(path: str | Path, sheet: str = "", cancel: threading.Event | None = None) -> Book:
    """The rows of the sheet named `sheet`, or of the first one if it is ""
    or there is no such sheet (any more), and the names of all sheets. ODS
    files are parsed only once for both. `cancel` as in read_table()."""
    data = _read(path)

    def book() -> Book:
        if not data.startswith(_ZIP):
            return Book(_table(data, None))
        with _archive(data) as zf:
            names, table = _reader(zf).read_book(zf, sheet or None)
        return Book(table, names, sheet if sheet in names else names[0])

    return _safely(book, cancel)


def sheet_names(path: str | Path) -> list[str]:
    """The sheets of an ODS or XLSX file, [] for CSV."""
    data = _read(path)
    if not data.startswith(_ZIP):
        return []

    def names() -> list[str]:
        with _archive(data) as zf:
            return _reader(zf).sheet_names(zf)

    return _safely(names)


def _table(data: bytes, sheet: str | int | None) -> Table:
    if data.startswith(_ZIP):
        with _archive(data) as zf:
            return _reader(zf).read(zf, sheet)
    if data.startswith(_OLE):
        raise MergeError("not_table", "Office 97 file (e.g. .xls)")
    if kind := _binary_kind(data):
        raise MergeError("not_table", kind)
    return csvfile.read(data)


# files people may pick by mistake; none of them is text. Signatures that
# are printable come with the bytes that follow them in such files, so a
# CSV whose header starts with "GIF8" or "RIFF" is still read.
_MAGIC = [
    (re.compile(rb"%PDF-\d\.\d"), "PDF"),
    (re.compile(rb"%!PS-Adobe-"), "PostScript"),
    (re.compile(rb"\x89PNG\r\n\x1a\n"), "PNG"),
    (re.compile(rb"\xff\xd8\xff"), "JPEG"),
    (re.compile(rb"GIF8[79]a"), "GIF"),
    (re.compile(rb"RIFF.{4}(?:WEBP|WAVE|AVI )", re.DOTALL), "RIFF (WebP, WAV, AVI)"),
    (re.compile(rb"\x1f\x8b"), "gzip"),
    (re.compile(rb"BZh[1-9]1AY&SY"), "bzip2"),
    (re.compile(rb"\xfd7zXZ\x00"), "xz"),
    (re.compile(rb"7z\xbc\xaf\x27\x1c"), "7-Zip"),
    (re.compile(rb"Rar!\x1a\x07"), "RAR"),
    (re.compile(rb"\x7fELF"), "ELF program"),
    (re.compile(rb"\{\\rtf1"), "RTF"),
    (re.compile(rb"<\?xml[ \t\r\n]"), "XML (e.g. a flat ODS file)"),
]
_SNIFF = 64 * 1024


def _binary_kind(data: bytes) -> str:
    """What a file that is no text is ("" for text): a known signature, or
    NUL bytes, which no text file has unless it is UTF-16 (with a BOM)."""
    start = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:16]
    for magic, kind in _MAGIC:
        if magic.match(start):
            return kind
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return ""
    return "binary data" if b"\x00" in data[:_SNIFF] else ""


def _archive(data: bytes) -> zipfile.ZipFile:
    """The archive, once it is known to have no more than MAX_ZIP_ENTRIES
    entries: the end record is checked before zipfile reads the directory
    (whose size it trusts, so this bounds the memory), the directory itself
    before any member is opened."""
    _check_end_record(data)
    zf = zipfile.ZipFile(io.BytesIO(data))
    if len(zf.infolist()) > limits.MAX_ZIP_ENTRIES:
        zf.close()
        raise too_large("more than %d entries in the archive" % limits.MAX_ZIP_ENTRIES)
    return zf


def _check_end_record(data: bytes) -> None:
    end = data.rfind(_END_RECORD, max(0, len(data) - _END_SEARCH))
    if end < 0 or len(data) < end + 16:
        return  # zipfile reports a broken archive
    entries, size = struct.unpack("<HI", data[end + 10 : end + 16])
    locator = end - _LOCATOR_SIZE
    if locator >= 0 and data[locator : locator + 4] == _LOCATOR:
        entries, size = _zip64_counts(data, locator)
    _check_counts(entries, size)


def _zip64_counts(data: bytes, locator: int) -> tuple[int, int]:
    """Entries and directory size from the ZIP64 record, which zipfile uses
    instead of the end record. A spreadsheet never needs ZIP64, but some
    programs write it anyway."""
    (offset,) = struct.unpack("<Q", data[locator + 8 : locator + 16])
    # zipfile reads the record at the offset, or right before the locator
    # if something was put before the archive: whichever it takes, the
    # larger counts are checked
    counts = [
        struct.unpack("<QQ", data[start + 32 : start + 48])
        for start in (offset, locator - _ZIP64_SIZE)
        if 0 <= start <= len(data) - _ZIP64_SIZE and data[start : start + 4] == _ZIP64_RECORD
    ]
    if not counts:
        raise damaged("ZIP64 locator without a ZIP64 record")
    return max(entries for entries, _size in counts), max(size for _entries, size in counts)


def _check_counts(entries: int, size: int) -> None:
    if entries > limits.MAX_ZIP_ENTRIES:
        raise too_large("%d entries in the archive" % entries)
    if size > limits.MAX_ZIP_ENTRIES * _ENTRY_BYTES:
        raise too_large("central directory of %d bytes" % size)


def _read(path: str | Path) -> bytes:
    # a named pipe or a device would block or never end: only plain files
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise MergeError("not_table", "not a regular file")
    with open(path, "rb") as file:  # no more than the limit, however large the file is
        data = file.read(limits.MAX_FILE_BYTES + 1)
    if len(data) > limits.MAX_FILE_BYTES:
        raise too_large("file larger than %d MB" % (limits.MAX_FILE_BYTES // 2**20))
    return data


def _safely(action: Callable[[], T], cancel: threading.Event | None = None) -> T:
    try:
        with cancellable(cancel):
            return action()
    except MergeError:
        raise
    except _BROKEN as e:
        raise damaged("%s: %s" % (type(e).__name__, e)) from e


def _reader(zf: zipfile.ZipFile) -> ModuleType:
    present = set(zf.namelist())
    if xlsx.WORKBOOK in present:
        return xlsx
    if "content.xml" in present or _mimetype(zf, present).startswith(_ODF_TYPE):
        return ods
    raise MergeError("not_table", "archive without a spreadsheet")


def _mimetype(zf: zipfile.ZipFile, present: set[str]) -> bytes:
    if "mimetype" not in present or zf.getinfo("mimetype").file_size > 100:
        return b""
    return zf.read("mimetype")
