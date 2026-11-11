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
import struct
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypeVar
from xml.etree import ElementTree as ET

from etiketto.merge import csvfile, limits, ods, xlsx
from etiketto.merge.fill import fill, placeholders
from etiketto.merge.table import MergeError, Table, damaged, too_large

__all__ = [
    "MergeError",
    "Table",
    "fill",
    "limits",
    "placeholders",
    "read_table",
    "sheet_names",
]

_ZIP = (b"PK\x03\x04", b"PK\x05\x06")  # a file, or an empty archive
# the end of central directory record: 22 bytes and a comment of up to 64 KiB
_END_RECORD, _END_SEARCH = b"PK\x05\x06", 22 + 0xFFFF
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


def read_table(path: str | Path, sheet: str | int | None = None) -> Table:
    """The data rows of a CSV, ODS or XLSX file, told apart by content.
    `sheet` (name or index, default the first) applies to ODS and XLSX."""
    data = _read(path)
    return _safely(lambda: _table(data, sheet))


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
    return csvfile.read(data)


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
    # 0xFFFF and 0xFFFFFFFF: the real values are in a ZIP64 record
    if entries != 0xFFFF and entries > limits.MAX_ZIP_ENTRIES:
        raise too_large("%d entries in the archive" % entries)
    if size != 0xFFFFFFFF and size > limits.MAX_ZIP_ENTRIES * _ENTRY_BYTES:
        raise too_large("central directory of %d bytes" % size)


def _read(path: str | Path) -> bytes:
    with open(path, "rb") as file:  # no more than the limit, however large the file is
        data = file.read(limits.MAX_FILE_BYTES + 1)
    if len(data) > limits.MAX_FILE_BYTES:
        raise too_large("file larger than %d MB" % (limits.MAX_FILE_BYTES // 2**20))
    return data


def _safely(action: Callable[[], T]) -> T:
    try:
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
