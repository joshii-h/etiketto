"""CSV and other delimited text (Excel's "Unicode text" is tab separated)."""

from __future__ import annotations

import codecs
import csv
import io
import re

from etiketto.merge.table import Builder, MergeError, Table

_DELIMITERS = ",;\t"
_SAMPLE = 16 * 1024  # enough lines to guess from; the sniffer is slow on long text
# Excel writes and reads "sep=;" as the first line to name the delimiter
_SEP_LINE = re.compile(r"sep=([^\"\r\n])[ \t]*(?:\r\n|\r|\n|\Z)")
# no text has it (read() refuses NUL): a single column is never split
_NO_DELIMITER = "\x00"


def read(data: bytes) -> Table:
    text = _decode(data)
    if "\x00" in text:
        raise MergeError("not_table", "binary data")
    if sep := _SEP_LINE.match(text):
        delimiter, text = sep.group(1), text[sep.end() :]
    else:
        delimiter = _delimiter(text[:_SAMPLE])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
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
    """The header decides: if it is a single field with every delimiter, it
    is a single column (the values may still contain commas); else the
    sniffer's choice if it splits the header, or the delimiter that splits
    it into the most fields. The header is the first filled record, so a
    quoted name may contain line breaks and delimiters."""
    fields = {delimiter: len(_header(sample, delimiter)) for delimiter in _DELIMITERS}
    best = max(_DELIMITERS, key=fields.__getitem__)
    if fields[best] < 2:
        return _NO_DELIMITER
    found = _sniffed(sample)
    return found if fields.get(found, 0) > 1 else best


def _header(sample: str, delimiter: str) -> list[str]:
    try:
        reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter)
        # the first record with a field or a delimiter: ";;" counts, "  " does not
        return next((cells for cells in reader if len(cells) > 1 or "".join(cells).strip()), [])
    except csv.Error:  # a field too long for the csv module: no header to split
        return []


def _sniffed(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        return ""
