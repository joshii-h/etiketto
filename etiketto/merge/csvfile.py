"""CSV and other delimited text (Excel's "Unicode text" is tab separated)."""

from __future__ import annotations

import codecs
import csv
import io

from etiketto.merge.table import Builder, MergeError, Table

_DELIMITERS = ",;\t"
_SAMPLE = 16 * 1024  # enough lines to guess from; the sniffer is slow on long text


def read(data: bytes) -> Table:
    text = _decode(data)
    if "\x00" in text:
        raise MergeError("not_table", "binary data")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text[:_SAMPLE]))
    builder = Builder()
    for cells in reader:
        builder.add(cells)
    return builder.table()


def _decode(data: bytes) -> str:
    """UTF-8 (with or without BOM) or UTF-16 with BOM; else what Excel on
    Windows writes, cp1252, and as a last resort latin-1, which takes any
    byte. UnicodeError for a broken UTF-8 or UTF-16 file with a BOM."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8")
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return data.decode("utf-16")
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def _delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:  # e.g. a single column: the most frequent one in the first line
        first = sample.split("\n", 1)[0]
        return max(_DELIMITERS, key=first.count)
