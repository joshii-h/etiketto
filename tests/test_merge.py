"""Serial labels: reading data rows from CSV, ODS and XLSX, filling placeholders.

The spreadsheets are built here with zipfile, as small as the readers allow,
so every test shows the XML it relies on. Data files come from anywhere, so
the property tests feed random bytes and random archives: the result is a
Table or a MergeError, nothing else.
"""

from __future__ import annotations

import io
import os
import struct
import time
import zipfile

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tavletta.merge import (
    Book,
    MergeError,
    Table,
    fill,
    limits,
    ods,
    placeholders,
    read_book,
    read_table,
    sheet_names,
)
from tavletta.merge.safexml import GuardedStream

# --- building files -----------------------------------------------------------


def write(tmp_path, name, data: bytes | str):
    path = tmp_path / name
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def write_zip(tmp_path, name, members: dict[str, bytes | str]):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return path


ODS_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:calcext="urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"'
)


def ods_content(*tables: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content {ODS_NS} office:version="1.3">'
        f"<office:body><office:spreadsheet>{''.join(tables)}</office:spreadsheet></office:body>"
        "</office:document-content>"
    )


def ods_table(name: str, *rows: str) -> str:
    return f'<table:table table:name="{name}">{"".join(rows)}</table:table>'


def row(*cells: str, repeat: int = 1) -> str:
    attr = f' table:number-rows-repeated="{repeat}"' if repeat > 1 else ""
    return f"<table:table-row{attr}>{''.join(cells)}</table:table-row>"


def text(value: str, repeat: int = 1) -> str:
    attr = f' table:number-columns-repeated="{repeat}"' if repeat > 1 else ""
    return f'<table:table-cell office:value-type="string"{attr}><text:p>{value}</text:p></table:table-cell>'


def empty(repeat: int = 1) -> str:
    attr = f' table:number-columns-repeated="{repeat}"' if repeat > 1 else ""
    return f"<table:table-cell{attr}/>"


def make_ods(tmp_path, *tables: str, content: str | None = None, name="data.ods"):
    return write_zip(
        tmp_path,
        name,
        {
            "mimetype": "application/vnd.oasis.opendocument.spreadsheet",
            "content.xml": content if content is not None else ods_content(*tables),
        },
    )


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def make_xlsx(
    tmp_path,
    *sheets: str,
    names: tuple[str, ...] = (),
    shared: list[str] | None = None,
    styles: str | None = None,
    workbook_pr: str = "",
    targets: tuple[str, ...] = (),
    extra: dict[str, str] | None = None,
    name="data.xlsx",
):
    """sheets: the <sheetData> content of each sheet; shared: <si> contents."""
    names = names or tuple(f"Sheet{i + 1}" for i in range(len(sheets)))
    targets = targets or tuple(f"worksheets/sheet{i + 1}.xml" for i in range(len(sheets)))
    sheet_list = "".join(
        f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)
    )
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" Target="{t}"/>'
        for i, t in enumerate(targets)
    )
    members: dict[str, str] = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">{workbook_pr}'
        f"<sheets>{sheet_list}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG_REL}">{rels}</Relationships>',
    }
    for i, data in enumerate(sheets):
        members[f"xl/worksheets/sheet{i + 1}.xml"] = (
            f'<worksheet xmlns="{MAIN}"><sheetData>{data}</sheetData></worksheet>'
        )
    if shared is not None:
        items = "".join(f"<si>{s}</si>" for s in shared)
        members["xl/sharedStrings.xml"] = f'<sst xmlns="{MAIN}">{items}</sst>'
    if styles is not None:
        members["xl/styles.xml"] = f'<styleSheet xmlns="{MAIN}">{styles}</styleSheet>'
    members.update(extra or {})
    return write_zip(tmp_path, name, members)


def xrow(*cells: str) -> str:
    return f"<row>{''.join(cells)}</row>"


def inline(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def num(ref: str, value: str, style: int | None = None) -> str:
    s = f' s="{style}"' if style is not None else ""
    return f'<c r="{ref}"{s}><v>{value}</v></c>'


def date_styles(*formats: tuple[int, str]) -> str:
    """cellXfs: 0 = General, 1 = built-in 14, then one xf per custom format."""
    fmts = "".join(f'<numFmt numFmtId="{i}" formatCode="{c}"/>' for i, c in formats)
    xfs = '<xf numFmtId="0"/><xf numFmtId="14"/>' + "".join(
        f'<xf numFmtId="{i}"/>' for i, _ in formats
    )
    return f'<numFmts count="{len(formats)}">{fmts}</numFmts><cellXfs>{xfs}</cellXfs>'


def code_of(path, **kwargs) -> str:
    with pytest.raises(MergeError) as info:
        read_table(path, **kwargs)
    assert str(info.value)  # English detail for bug reports
    return info.value.code


# --- CSV ----------------------------------------------------------------------


@pytest.mark.parametrize("sep", [",", ";", "\t"])
def test_csv_delimiters_are_detected(tmp_path, sep):
    path = write(tmp_path, "a.csv", f"Name{sep}City\nAnna{sep}Bern\nBen{sep}Chur\n")
    table = read_table(path)
    assert table == Table(
        ["Name", "City"], [{"Name": "Anna", "City": "Bern"}, {"Name": "Ben", "City": "Chur"}]
    )


def test_csv_utf8_with_bom(tmp_path):
    path = write(tmp_path, "a.csv", b"\xef\xbb\xbfOrt;PLZ\nZ\xc3\xbcrich;8000\n")
    assert read_table(path).rows == [{"Ort": "Zürich", "PLZ": "8000"}]


def test_csv_from_windows_excel_is_cp1252(tmp_path):
    path = write(tmp_path, "a.csv", "Ort;Preis\nZürich;5 €\n".encode("cp1252"))
    assert read_table(path).rows == [{"Ort": "Zürich", "Preis": "5 €"}]


def test_csv_falls_back_to_latin1(tmp_path):
    # 0x81 is undefined in cp1252
    path = write(tmp_path, "a.csv", b"A;B\nx\x81;y\n")
    assert read_table(path).rows == [{"A": "x\x81", "B": "y"}]


def test_csv_utf16_unicode_text_from_excel(tmp_path):
    path = write(tmp_path, "a.txt", "﻿Name\tOrt\nAnna\tZürich\n".encode("utf-16-le"))
    assert read_table(path).rows == [{"Name": "Anna", "Ort": "Zürich"}]


def test_csv_quoted_fields(tmp_path):
    path = write(
        tmp_path, "a.csv", 'Name,Address\n"Muster, Anna","Weg 1\n3000 Bern"\n"Say ""hi""",x\n'
    )
    assert read_table(path).rows == [
        {"Name": "Muster, Anna", "Address": "Weg 1\n3000 Bern"},
        {"Name": 'Say "hi"', "Address": "x"},
    ]


def test_csv_single_column(tmp_path):
    path = write(tmp_path, "a.csv", "Name\nAnna\nBen\n")
    assert read_table(path) == Table(["Name"], [{"Name": "Anna"}, {"Name": "Ben"}])
    # a header without a delimiter: the commas of the values do not split them
    path = write(tmp_path, "b.csv", "Name\nAnna\nBert, Jr.\nCarl\n")
    assert [r["Name"] for r in read_table(path).rows] == ["Anna", "Bert, Jr.", "Carl"]


def test_csv_header_is_the_first_filled_record(tmp_path):
    """A quoted name may hold a line break or a delimiter; lines of spaces
    before the header are no header."""
    path = write(tmp_path, "a.csv", '"Name\nlang";Ort\nAnna;Bern\n')
    assert read_table(path).columns == ["Name\nlang", "Ort"]
    path = write(tmp_path, "b.csv", '"Name, Vorname";Ort\n"Muster, Anna";Bern\n')
    assert read_table(path).columns == ["Name, Vorname", "Ort"]
    path = write(tmp_path, "c.csv", "\n\n  \nName;Ort\nAnna;Bern\n")
    assert read_table(path) == Table(["Name", "Ort"], [{"Name": "Anna", "Ort": "Bern"}])


def test_csv_sep_line_from_excel(tmp_path):
    path = write(tmp_path, "a.csv", "sep=;\r\nName;Wert\r\nx,1;2\r\n")
    assert read_table(path) == Table(["Name", "Wert"], [{"Name": "x,1", "Wert": "2"}])
    path = write(tmp_path, "b.csv", "sep=|\nA|B\n1|2\n")
    assert read_table(path).rows == [{"A": "1", "B": "2"}]
    # only a line of its own: a header that starts so is a header
    path = write(tmp_path, "c.csv", "sep=1;Name\nx;Anna\n")
    assert read_table(path).columns == ["sep=1", "Name"]
    assert code_of(write(tmp_path, "d.csv", "sep=;")) == "empty"


def test_csv_sniffed_delimiter_must_be_in_the_header(tmp_path, monkeypatch):
    import csv

    class Comma:
        delimiter = ","

    monkeypatch.setattr(csv.Sniffer, "sniff", lambda *args, **kwargs: Comma)
    path = write(tmp_path, "a.csv", "A;B\n1,5;2\n")
    assert read_table(path).rows == [{"A": "1,5", "B": "2"}]


def test_csv_when_sniffing_fails_the_most_common_delimiter_wins(tmp_path, monkeypatch):
    import csv

    def refuse(*args, **kwargs):
        raise csv.Error("Could not determine delimiter")

    monkeypatch.setattr(csv.Sniffer, "sniff", refuse)
    path = write(tmp_path, "a.csv", "A;B;C\n1,5;2;3\n")
    assert read_table(path).rows == [{"A": "1,5", "B": "2", "C": "3"}]


def test_csv_rows_of_other_length(tmp_path):
    path = write(tmp_path, "a.csv", "A,B,C\n1\n\n1,2,3,4\n,,\n")
    table = read_table(path)
    # empty rows are left out, a longer row adds a column named by its letter
    assert table.columns == ["A", "B", "C", "D"]
    assert table.rows == [
        {"A": "1", "B": "", "C": "", "D": ""},
        {"A": "1", "B": "2", "C": "3", "D": "4"},
    ]


def test_csv_header_names(tmp_path):
    path = write(tmp_path, "a.csv", " Name ,,Name,Name (2),Name, \nx,x,x,x,x,x\n")
    assert read_table(path).columns == ["Name", "B", "Name (3)", "Name (2)", "Name (4)", "F"]


def test_csv_header_only(tmp_path):
    assert read_table(write(tmp_path, "a.csv", "A;B\n")) == Table(["A", "B"], [])


@pytest.mark.parametrize("data", [b"", b"\n\n", b";;\n;;\n", b"\xef\xbb\xbf"])
def test_csv_without_content_is_empty(tmp_path, data):
    assert code_of(write(tmp_path, "a.csv", data)) == "empty"


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 100,  # old .xls (OLE2)
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n",
        b"\xff\xd8\xff\xe0 JFIF",
        b"\x1f\x8b\x08\x00",
        b'<?xml version="1.0"?><office:document><a>1,2</a></office:document>',
        b"GIF89a\x01\x00",
        b"RIFF\x24\x00\x00\x00WAVEfmt ",
        b"BZh91AY&SY\x00",
        b"Rar!\x1a\x07\x00",
        b"a,b\n1,\x00\n",  # a NUL byte: no text
        "a,b\n1,2\n".encode("utf-16-le"),  # UTF-16 without a BOM
    ],
)
def test_binary_files_are_not_tables(tmp_path, data):
    assert code_of(write(tmp_path, "a.csv", data)) == "not_table"


@pytest.mark.parametrize("start", ["GIF8", "RIFF", "BZh", "Rar!", "%PDF-Nr"])
def test_headers_that_start_like_a_signature_are_read(tmp_path, start):
    path = write(tmp_path, "a.csv", "%s;Name\n1;Anna\n" % start)
    assert read_table(path).columns == [start, "Name"]


def test_csv_with_an_absurdly_long_field_is_damaged(tmp_path):
    path = write(tmp_path, "a.csv", 'A\n"' + "x" * 200_000 + '"\n')
    assert code_of(path) == "damaged"


def test_csv_utf16_with_bad_bytes_is_damaged(tmp_path):
    path = write(tmp_path, "a.csv", b"\xff\xfeA\x00\x00\xd8")  # lone surrogate
    assert code_of(path) == "damaged"


def test_files_over_the_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 10)
    assert code_of(write(tmp_path, "a.csv", "A\n" + "x\n" * 10)) == "too_large"


def test_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        read_table(tmp_path / "missing.csv")
    with pytest.raises(OSError):
        sheet_names(tmp_path / "missing.ods")


def test_csv_has_no_sheets(tmp_path):
    assert sheet_names(write(tmp_path, "a.csv", "A\n1\n")) == []


def test_row_column_and_cell_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_ROWS", 2)
    assert read_table(write(tmp_path, "a.csv", "A\n1\n2\n")).rows == [{"A": "1"}, {"A": "2"}]
    assert code_of(write(tmp_path, "b.csv", "A\n1\n2\n3\n")) == "too_large"
    monkeypatch.setattr(limits, "MAX_COLUMNS", 3)
    assert code_of(write(tmp_path, "c.csv", "A,B,C,D\n")) == "too_large"
    monkeypatch.setattr(limits, "MAX_CELLS", 5)
    assert code_of(write(tmp_path, "d.csv", "A,B\n1,2\n3,4\n5\n")) == "too_large"
    # the header row is not counted: 2 rows of 2 cells fit 4, as 4 rows fit MAX_ROWS
    monkeypatch.setattr(limits, "MAX_CELLS", 4)
    assert len(read_table(write(tmp_path, "e.csv", "A,B\n1,2\n3,4\n")).rows) == 2


def test_cells_are_rows_times_columns(tmp_path, monkeypatch):
    """Every row gets every column: one wide row makes the table that large,
    however few cells are filled."""
    monkeypatch.setattr(limits, "MAX_CELLS", 10)
    assert len(read_table(write(tmp_path, "a.csv", "A,B\n" + "x\n" * 5)).rows) == 5
    assert code_of(write(tmp_path, "b.csv", "A,B,C\n" + "x\n" * 4)) == "too_large"


def test_reading_can_be_cancelled(tmp_path):
    import threading

    cancel = threading.Event()
    path = write(tmp_path, "a.csv", "A\n1\n")
    assert read_table(path, cancel=cancel).rows == [{"A": "1"}]
    cancel.set()
    with pytest.raises(MergeError) as info:
        read_table(path, cancel=cancel)
    assert info.value.code == "cancelled"
    ods_path = make_ods(tmp_path, ods_table("T", row(text("A")), row(text("1"))))
    with pytest.raises(MergeError, match="cancelled"):
        read_book(ods_path, cancel=cancel)
    assert read_book(ods_path).table.rows == [{"A": "1"}]  # only that read


# --- ODS ----------------------------------------------------------------------


def test_ods_values(tmp_path):
    cells = [
        '<table:table-cell office:value-type="float" office:value="42" calcext:value-type="float">'
        "<text:p>42</text:p></table:table-cell>",
        # what the user sees, not the stored value
        '<table:table-cell office:value-type="currency" office:value="3.5" office:currency="CHF">'
        "<text:p>CHF 3.50</text:p></table:table-cell>",
        '<table:table-cell office:value-type="date" office:date-value="2026-11-11">'
        "<text:p>11.11.26</text:p></table:table-cell>",
        '<table:table-cell office:value-type="date" office:date-value="2026-11-11T14:30:00">'
        "<text:p>11.11.26 14:30</text:p></table:table-cell>",
        '<table:table-cell office:value-type="boolean" office:boolean-value="true">'
        "<text:p>WAHR</text:p></table:table-cell>",
        '<table:table-cell office:value-type="boolean" office:boolean-value="false"/>',
        '<table:table-cell office:value-type="float" office:value="3.5"/>',
        '<table:table-cell office:value-type="string" office:string-value="Hidden"/>',
    ]
    header = row(*(text(c) for c in "ABCDEFGH"))
    path = make_ods(tmp_path, ods_table("Data", header, row(*cells)))
    assert read_table(path).rows == [
        {
            "A": "42",
            "B": "CHF 3.50",
            "C": "2026-11-11",
            "D": "2026-11-11 14:30:00",
            "E": "WAHR",
            "F": "FALSE",
            "G": "3.5",
            "H": "Hidden",
        }
    ]


def test_ods_date_value_that_is_not_iso_is_shown_as_is(tmp_path):
    cell = '<table:table-cell office:value-type="date" office:date-value="sometime"/>'
    path = make_ods(tmp_path, ods_table("T", row(text("D")), row(cell)))
    assert read_table(path).rows == [{"D": "sometime"}]


def test_ods_text_structure(tmp_path):
    cell = (
        '<table:table-cell office:value-type="string">'
        '<text:p>Line <text:span>one</text:span><text:s/>x<text:s text:c="3"/>y</text:p>'
        '<text:p>a<text:tab/>b<text:line-break/>c<text:s text:c="bad"/></text:p>'
        "<office:annotation><text:p>a comment</text:p></office:annotation>"
        "</table:table-cell>"
    )
    path = make_ods(tmp_path, ods_table("T", row(text("Note")), row(cell)))
    assert read_table(path).rows == [{"Note": "Line one x   y\na\tb\nc "}]


def test_ods_long_space_runs_are_capped(tmp_path):
    cell = '<table:table-cell><text:p>a<text:s text:c="999999999"/>b</text:p></table:table-cell>'
    path = make_ods(tmp_path, ods_table("T", row(text("A")), row(cell)))
    value = read_table(path).rows[0]["A"]
    assert value.startswith("a ") and value.endswith(" b")
    assert len(value) <= 2 + limits.MAX_SPACES


def test_ods_repeated_trailing_cells_and_rows_stay_small(tmp_path):
    # what LibreOffice writes for the unused rest of a sheet
    rows = (
        row(text("Name"), text("City"), empty(16382)),
        row(text("Anna"), empty(), text("Bern"), empty(16381)),
        row(empty(16384), repeat=1048573),
        row(empty(16384)),
    )
    path = make_ods(tmp_path, ods_table("T", *rows))
    start = time.monotonic()
    table = read_table(path)
    assert time.monotonic() - start < 2
    assert table == Table(["Name", "City", "C"], [{"Name": "Anna", "City": "", "C": "Bern"}])


def test_ods_repeated_content(tmp_path):
    rows = (row(text("A"), text("B"), text("C")), row(text("x", repeat=3), repeat=2))
    path = make_ods(tmp_path, ods_table("T", *rows))
    assert read_table(path).rows == [{"A": "x", "B": "x", "C": "x"}] * 2


def test_ods_repeated_header_row(tmp_path):
    path = make_ods(tmp_path, ods_table("T", row(text("x"), repeat=3)))
    assert read_table(path) == Table(["x"], [{"x": "x"}, {"x": "x"}])


def test_ods_empty_rows_between_data_are_skipped(tmp_path):
    rows = (row(text("A")), row(empty(5), repeat=1000), row(text("1")))
    assert read_table(make_ods(tmp_path, ods_table("T", *rows))).rows == [{"A": "1"}]


def test_ods_covered_cells_are_empty(tmp_path):
    merged = (
        '<table:table-cell office:value-type="string" table:number-columns-spanned="2">'
        "<text:p>wide</text:p></table:table-cell>"
        '<table:covered-table-cell table:number-columns-repeated="1"/>'
    )
    rows = (row(text("A"), text("B"), text("C")), row(merged, text("c")))
    path = make_ods(tmp_path, ods_table("T", *rows))
    assert read_table(path).rows == [{"A": "wide", "B": "", "C": "c"}]


def test_ods_header_rows_and_groups(tmp_path):
    table = (
        '<table:table table:name="T"><table:table-header-rows>'
        f"{row(text('A'))}</table:table-header-rows>"
        f"<table:table-row-group>{row(text('1'))}</table:table-row-group></table:table>"
    )
    assert read_table(make_ods(tmp_path, table)).rows == [{"A": "1"}]


def test_ods_tables_inside_cells_are_ignored(tmp_path):
    inner = ods_table("inner", row(text("nested")))
    cell = f'<table:table-cell office:value-type="string"><text:p>v</text:p>{inner}</table:table-cell>'
    path = make_ods(tmp_path, ods_table("T", row(text("A")), row(cell)))
    assert read_table(path).rows == [{"A": "v"}]


def test_ods_sheets(tmp_path):
    path = make_ods(
        tmp_path,
        ods_table("First", row(text("A")), row(text("1"))),
        ods_table("Second", row(text("B")), row(text("2"))),
    )
    assert sheet_names(path) == ["First", "Second"]
    assert read_table(path).rows == [{"A": "1"}]
    assert read_table(path, sheet=1).rows == [{"B": "2"}]
    assert read_table(path, sheet="Second").rows == [{"B": "2"}]
    assert code_of(path, sheet="Third") == "not_table"
    assert code_of(path, sheet=2) == "not_table"


def test_ods_without_sheets(tmp_path):
    path = make_ods(tmp_path)
    assert sheet_names(path) == []
    assert code_of(path) == "not_table"


def test_ods_sheet_without_content_is_empty(tmp_path):
    path = make_ods(tmp_path, ods_table("T", row(empty(1024), repeat=1048576)))
    assert code_of(path) == "empty"


def test_ods_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_COLUMNS", 10)
    wide = make_ods(tmp_path, ods_table("T", row(text("x", repeat=11))), name="w.ods")
    assert code_of(wide) == "too_large"
    gap = make_ods(tmp_path, ods_table("T", row(empty(10), text("x"))), name="g.ods")
    assert code_of(gap) == "too_large"
    monkeypatch.setattr(limits, "MAX_ROWS", 5)
    tall = make_ods(tmp_path, ods_table("T", row(text("x"), repeat=1048576)), name="t.ods")
    assert code_of(tall) == "too_large"


def test_ods_found_by_content_not_by_suffix(tmp_path):
    path = make_ods(tmp_path, ods_table("T", row(text("A")), row(text("1"))), name="x.bin")
    assert read_table(path).rows == [{"A": "1"}]


def test_ods_without_content_is_damaged(tmp_path):
    path = write_zip(
        tmp_path, "a.ods", {"mimetype": "application/vnd.oasis.opendocument.spreadsheet"}
    )
    assert code_of(path) == "damaged"


@pytest.mark.parametrize(
    "content",
    [
        "<office:document-content",  # not well-formed
        '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY lol "lol">]><a>&lol;</a>',
        '<?xml version="1.0"?><!DOCTYPE a SYSTEM "http://example.com/a.dtd"><a/>',
        '<?xml version="1.0" encoding="ISO-8859-1"?><a/>',
        '<?xml version="1.0" encoding="UTF-16"?><a/>'.encode("utf-16"),
        '<a xmlns:x="u">&undefined;</a>',
    ],
)
def test_ods_with_bad_xml_is_damaged(tmp_path, content):
    path = write_zip(
        tmp_path,
        "a.ods",
        {"mimetype": "application/vnd.oasis.opendocument.spreadsheet", "content.xml": content},
    )
    assert code_of(path) == "damaged"


def test_doctype_split_across_chunks_is_found():
    data = b" " * (GuardedStream.CHUNK - 4) + b"<!DOCTYPE a><a/>"
    stream = GuardedStream(io.BytesIO(data), cap=len(data))
    with pytest.raises(MergeError) as info:
        while stream.read(GuardedStream.CHUNK):
            pass
    assert info.value.code == "damaged"


def test_members_larger_than_announced_are_cut(tmp_path):
    stream = GuardedStream(io.BytesIO(b"<a>" + b"x" * 100 + b"</a>"), cap=50)
    with pytest.raises(MergeError) as info:
        stream.read(1000)
        stream.read(1000)
    assert info.value.code == "too_large"


def test_zip_members_over_the_limit(tmp_path, monkeypatch):
    path = make_ods(tmp_path, ods_table("T", row(text("A" * 500))))
    monkeypatch.setattr(limits, "MAX_MEMBER_BYTES", 400)
    assert code_of(path) == "too_large"


def test_zip_entries_over_the_limit(tmp_path, monkeypatch):
    members = {"mimetype": "application/vnd.oasis.opendocument.spreadsheet"}
    members["content.xml"] = ods_content(ods_table("T", row(text("A")), row(text("1"))))
    members.update({f"extra/{i}.xml": "<x/>" for i in range(3)})
    path = write_zip(tmp_path, "many.ods", members)
    assert read_table(path).rows == [{"A": "1"}]
    monkeypatch.setattr(limits, "MAX_ZIP_ENTRIES", 4)  # 5 entries
    assert code_of(path) == "too_large"
    with pytest.raises(MergeError) as info:
        sheet_names(path)
    assert info.value.code == "too_large"


def test_zip_entries_are_counted_even_if_the_end_record_lies(tmp_path, monkeypatch):
    members = {f"{i}.xml": "<x/>" for i in range(5)}
    data = bytearray(write_zip(tmp_path, "a.zip", members).read_bytes())
    end = data.rindex(b"PK\x05\x06")
    data[end + 8 : end + 12] = (1).to_bytes(2, "little") * 2  # "one entry"
    monkeypatch.setattr(limits, "MAX_ZIP_ENTRIES", 4)
    assert code_of(write(tmp_path, "b.zip", bytes(data))) == "too_large"


def test_zip_with_a_huge_central_directory_is_refused_before_reading_it(tmp_path, monkeypatch):
    data = bytearray(write_zip(tmp_path, "a.zip", {"x.xml": "<x/>"}).read_bytes())
    end = data.rindex(b"PK\x05\x06")
    data[end + 12 : end + 16] = (10**8).to_bytes(4, "little")
    assert code_of(write(tmp_path, "b.zip", bytes(data))) == "too_large"
    data[end + 12 : end + 16] = b"\xff" * 4  # "see the ZIP64 record", but there is none
    assert code_of(write(tmp_path, "c.zip", bytes(data))) == "too_large"


def zip64(entries: int, record: bool = True, prepend: bytes = b"") -> bytes:
    """An archive whose end record claims 1 entry and whose ZIP64 record,
    which zipfile believes, claims `entries`; the directory holds them."""
    entry = (
        struct.pack("<4s4B4HL2L5H2L", b"PK\x01\x02", 20, 3, 20, 0, *[0] * 7, 1, *[0] * 6) + b"a"
    )
    directory = entry * entries
    start = 4  # offsets do not count what was put before the archive
    record_data = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, len(directory), 4
    )
    if not record:
        record_data = b"\x00" * len(record_data)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, start + len(directory), 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 46, 4, 0)
    return prepend + b"PK\x03\x04" + directory + record_data + locator + end


def test_zip64_entry_count_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_ZIP_ENTRIES", 50)
    assert code_of(write(tmp_path, "a.xlsx", zip64(51))) == "too_large"
    # something before the archive: zipfile finds the record before the locator
    assert (
        code_of(write(tmp_path, "b.xlsx", zip64(51, prepend=b"PK\x03\x04" + b"x" * 100)))
        == "too_large"
    )
    assert code_of(write(tmp_path, "c.xlsx", zip64(51, record=False))) == "damaged"
    # few entries: the archive is read (and is no spreadsheet)
    assert code_of(write(tmp_path, "d.xlsx", zip64(3))) in ("damaged", "not_table")


def test_zip_that_is_neither_ods_nor_xlsx(tmp_path):
    path = write_zip(tmp_path, "a.zip", {"word/document.xml": "<w/>"})
    assert code_of(path) == "not_table"


def test_broken_zip_is_damaged(tmp_path):
    path = make_ods(tmp_path, ods_table("T", row(text("A")), row(text("1"))))
    data = bytearray(path.read_bytes())
    start = data.index(b"content.xml") + len("content.xml")
    data[start + 10 : start + 40] = b"\xff" * 30  # inside the deflated data
    assert code_of(write(tmp_path, "b.ods", bytes(data))) == "damaged"
    assert code_of(write(tmp_path, "c.ods", b"PK\x03\x04" + b"\x00" * 30)) == "damaged"


def test_encrypted_zip_member_is_damaged(tmp_path):
    path = make_ods(tmp_path, ods_table("T", row(text("A")), row(text("1"))))
    data = bytearray(path.read_bytes())
    # set the "encrypted" flag in every local and central header
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        pos = data.find(sig)
        while pos >= 0:
            data[pos + offset] |= 1
            pos = data.find(sig, pos + 4)
    assert code_of(write(tmp_path, "e.ods", bytes(data))) == "damaged"


# --- XLSX ---------------------------------------------------------------------


def test_xlsx_strings(tmp_path):
    shared = [
        "<t>Name</t>",
        "<r><t>Ri</t></r><r><rPr><b/></rPr><t>ch</t></r><rPh><t>phonetic</t></rPh>",
        '<t xml:space="preserve"> padded </t>',
        "<t>Note</t>",
    ]
    cells = (
        xrow('<c r="A1" t="s"><v>0</v></c>', '<c r="B1" t="s"><v>3</v></c>'),
        xrow(
            '<c r="A2" t="s"><v>1</v></c>',
            '<c r="B2" t="inlineStr"><is><r><t>in</t></r><r><t>line</t></r></is></c>',
        ),
        xrow('<c r="A3" t="s"><v>2</v></c>', '<c r="B3" t="str"><f>A3</f><v>formula</v></c>'),
    )
    path = make_xlsx(tmp_path, "".join(cells), shared=shared)
    assert read_table(path).rows == [
        {"Name": "Rich", "Note": "inline"},
        {"Name": " padded ", "Note": "formula"},
    ]


def test_xlsx_numbers_booleans_and_errors(tmp_path):
    values = ["42", "3.5", "0.30000000000000004", "1E+20", "1.5E-7", "-7", "123456789012", "x"]
    header = xrow(*(inline(f"{chr(65 + i)}1", f"N{i}") for i in range(len(values) + 4)))
    data = xrow(
        *(num(f"{chr(65 + i)}2", v) for i, v in enumerate(values)),
        '<c r="I2" t="b"><v>1</v></c>',
        '<c r="J2" t="b"><v>0</v></c>',
        '<c r="K2" t="e"><f>1/0</f><v>#DIV/0!</v></c>',
        '<c r="L2" t="n"><v>INF</v></c>',
    )
    path = make_xlsx(tmp_path, header + data)
    assert list(read_table(path).rows[0].values()) == [
        *("42", "3.5", "0.3", "1E+20", "1.5E-07", "-7", "123456789012", "x"),
        *("TRUE", "FALSE", "#DIV/0!", "INF"),
    ]


def test_xlsx_gaps_and_cells_without_reference(tmp_path):
    cells = (
        xrow(inline("A1", "A"), inline("C1", "C")),
        '<row r="5"><c><v>1</v></c><c><v>2</v></c><c r="E5"><v>5</v></c><c><v>6</v></c></row>',
        '<row r="6"><c r="B6"/><c r="C6"><v></v></c></row>',
    )
    path = make_xlsx(tmp_path, "".join(cells))
    table = read_table(path)
    assert table.columns == ["A", "B", "C", "D", "E", "F"]
    assert table.rows == [{"A": "1", "B": "2", "C": "", "D": "", "E": "5", "F": "6"}]


def test_xlsx_dates(tmp_path):
    styles = date_styles(
        (164, "dd.mm.yyyy"),
        (165, "[$-407]d. mmmm yyyy;@"),
        (166, "&quot;Kd. &quot;0"),  # text in quotes is no date
        (167, "0.00"),
        (168, "0%"),
        (169, "h:mm"),
        (170, "yyyy-mm-dd hh:mm:ss"),
        (171, "#,##0.000\\ &quot;CHF&quot;"),
        (172, "0.0%;[Red]-0.0%"),
        (173, "[h]:mm:ss"),
    )
    serial = "46337"  # 2026-11-11
    cells = [
        num("A2", serial, 1),
        num("B2", serial, 2),
        num("C2", serial, 3),
        num("D2", serial, 4),
        num("E2", "3.5", 5),
        num("F2", "0.355", 6),
        num("G2", "46337.6041666667", 7),
        num("H2", "46337.6041666667", 8),
        num("I2", "1234.5", 9),
        num("J2", "0.1234", 10),
        num("K2", "1.5", 11),
        num("L2", "7", 99),  # style index out of range: General
    ]
    header = xrow(*(inline(f"{chr(65 + i)}1", chr(65 + i)) for i in range(len(cells))))
    path = make_xlsx(tmp_path, header + xrow(*cells), styles=styles)
    assert list(read_table(path).rows[0].values()) == [
        *("2026-11-11", "2026-11-11", "2026-11-11", "46337", "3.50", "36%"),
        *("14:30", "2026-11-11 14:30:00", "1234.500", "12.3%", "36:00:00", "7"),
    ]


@pytest.mark.parametrize(
    ("format_id", "serial", "shown"),
    [
        (14, "1", "1900-01-01"),
        (14, "59", "1900-02-28"),
        (14, "60", "1900-02-29"),  # Excel's leap year bug, shown like Excel does
        (14, "61", "1900-03-01"),
        (14, "46337.99", "2026-11-11"),
        (14, "0", "0"),  # Excel shows "1900-01-00"; no real date
        (14, "-1", "-1"),
        (14, "3000000", "3000000"),  # after 9999-12-31
        (18, "0.75", "6:00 PM"),
        (19, "0.5", "12:00:00 PM"),
        (20, "0.999999", "00:00"),
        (21, "0.0000115740740740741", "00:00:01"),
        (22, "46337.25", "2026-11-11 06:00"),
        (45, "0.0006944444", "01:00"),
        (47, "0.0006944444", "01:00.0"),
        (46, "1.5", "36:00:00"),
        (2, "3.14159", "3.14"),
        (49, "007", "7"),
        (2, "1E+16", "1E+16"),  # too large for fixed decimals
    ],
)
def test_xlsx_builtin_formats(tmp_path, format_id, serial, shown):
    styles = f'<cellXfs><xf numFmtId="0"/><xf numFmtId="{format_id}"/></cellXfs>'
    path = make_xlsx(tmp_path, xrow(inline("A1", "V")) + xrow(num("A2", serial, 1)), styles=styles)
    assert read_table(path).rows == [{"V": shown}]


@pytest.mark.parametrize("flag", ['date1904="1"', 'date1904="true"'])
def test_xlsx_1904_date_system(tmp_path, flag):
    styles = '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
    path = make_xlsx(
        tmp_path,
        xrow(inline("A1", "D")) + xrow(num("A2", "44875", 1), num("B2", "0", 1)),
        styles=styles,
        workbook_pr=f"<workbookPr {flag}/>",
    )
    assert read_table(path).rows == [{"D": "2026-11-11", "B": "1904-01-01"}]


def test_xlsx_iso_date_cells(tmp_path):
    cells = xrow(inline("A1", "D"), inline("B1", "T")) + xrow(
        '<c r="A2" t="d"><v>2026-11-11T00:00:00</v></c>',
        '<c r="B2" t="d"><v>2026-11-11T14:30:00</v></c>',
    )
    assert read_table(make_xlsx(tmp_path, cells)).rows == [
        {"D": "2026-11-11", "T": "2026-11-11 14:30:00"}
    ]


def test_xlsx_sheets(tmp_path):
    path = make_xlsx(
        tmp_path,
        xrow(inline("A1", "A")) + xrow(inline("A2", "1")),
        xrow(inline("A1", "B")) + xrow(inline("A2", "2")),
        names=("Erste", "Zweite"),
        targets=("worksheets/sheet1.xml", "/xl/worksheets/sheet2.xml"),
    )
    assert sheet_names(path) == ["Erste", "Zweite"]
    assert read_table(path).rows == [{"A": "1"}]
    assert read_table(path, sheet="Zweite").rows == [{"B": "2"}]
    assert read_table(path, sheet=1).rows == [{"B": "2"}]
    assert code_of(path, sheet="Dritte") == "not_table"
    assert code_of(path, sheet=-1) == "not_table"


def test_xlsx_sheet_target_outside_xl(tmp_path):
    path = make_xlsx(
        tmp_path,
        names=("S",),
        targets=("../sheet.xml",),
        extra={
            "sheet.xml": f'<worksheet xmlns="{MAIN}"><sheetData>'
            f"{xrow(inline('A1', 'A'))}</sheetData></worksheet>"
        },
    )
    assert read_table(path) == Table(["A"], [])


def test_xlsx_strict_namespace(tmp_path):
    strict = "http://purl.oclc.org/ooxml/spreadsheetml/main"
    strict_rel = "http://purl.oclc.org/ooxml/officeDocument/relationships"
    path = write_zip(
        tmp_path,
        "s.xlsx",
        {
            "xl/workbook.xml": f'<workbook xmlns="{strict}" xmlns:r="{strict_rel}"><sheets>'
            '<sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG_REL}">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{strict}"><sheetData>'
            f"{xrow(inline('A1', 'A'))}{xrow(inline('A2', '1'))}</sheetData></worksheet>",
        },
    )
    assert read_table(path).rows == [{"A": "1"}]


@pytest.mark.parametrize(
    "change",
    [
        {"xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG_REL}"/>'},
        {"xl/worksheets/sheet1.xml": None},
        {
            "xl/workbook.xml": f'<workbook xmlns="{MAIN}"><sheets><sheet name="S"/></sheets></workbook>'
        },
        {"xl/workbook.xml": "<workbook"},
    ],
)
def test_xlsx_broken_structure_is_damaged(tmp_path, change):
    good = make_xlsx(tmp_path, xrow(inline("A1", "A")), name="good.xlsx")
    with zipfile.ZipFile(good) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    for member, data in change.items():
        if data is None:
            del members[member]
        else:
            members[member] = data.encode()
    assert code_of(write_zip(tmp_path, "bad.xlsx", members)) == "damaged"


@pytest.mark.parametrize(
    "cell",
    [
        '<c r="A2" t="s"><v>5</v></c>',  # no such shared string
        '<c r="A2" t="s"><v>x</v></c>',
        '<c r="ZZZZ2"><v>1</v></c>',  # not a column Excel has
        '<c r="XFE2"><v>1</v></c>',
        '<c r="2"><v>1</v></c>',
    ],
)
def test_xlsx_bad_cells_are_damaged(tmp_path, cell):
    path = make_xlsx(tmp_path, xrow(inline("A1", "A")) + xrow(cell), shared=["<t>s</t>"])
    assert code_of(path) == "damaged"


def test_xlsx_missing_shared_strings_is_damaged(tmp_path):
    path = make_xlsx(tmp_path, xrow('<c r="A1" t="s"><v>0</v></c>'))
    assert code_of(path) == "damaged"


def test_xlsx_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_COLUMNS", 10)
    path = make_xlsx(tmp_path, xrow(inline("K1", "far")))
    assert code_of(path) == "too_large"
    monkeypatch.setattr(limits, "MAX_CELLS", 3)
    shared = make_xlsx(tmp_path, xrow(inline("A1", "A")), shared=["<t>s</t>"] * 4, name="s.xlsx")
    assert code_of(shared) == "too_large"


def test_xlsx_merged_cells_show_their_value_once(tmp_path):
    """LibreOffice keeps the content of cells hidden by merging; like the
    covered cells of ODS they are empty."""
    rows = (
        '<row r="1">' + inline("A1", "A") + inline("B1", "B") + inline("C1", "C") + "</row>"
        '<row r="2">'
        + inline("A2", "wide")
        + inline("B2", "hidden")
        + inline("C2", "c")
        + "</row>"
        '<row r="4">' + inline("A4", "tall") + inline("C4", "x") + "</row>"
        '<row r="5">' + inline("A5", "hidden") + inline("B5", "b") + "</row>"
        '<row r="6">' + inline("C6", "gone") + "</row>"
    )
    merges = (
        '<mergeCells count="4"><mergeCell ref="A2:B2"/><mergeCell ref="A4:A5"/>'
        '<mergeCell ref="C7:B6"/><mergeCell ref="D9:E10"/></mergeCells>'
    )
    path = make_xlsx(
        tmp_path,
        "",
        extra={
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData>{rows}'
            f"</sheetData>{merges}</worksheet>"
        },
    )
    assert read_table(path).rows == [
        {"A": "wide", "B": "", "C": "c"},
        {"A": "tall", "B": "", "C": "x"},
        {"A": "", "B": "b", "C": ""},
    ]


def test_xlsx_merged_header_cells(tmp_path):
    rows = xrow(inline("A1", "Name"), inline("B1", "old"), inline("C1", "Zip"))
    rows += xrow(inline("A2", "1"), inline("B2", "2"), inline("C2", "3"))
    path = make_xlsx(
        tmp_path,
        "",
        extra={
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData>{rows}'
            '</sheetData><mergeCells><mergeCell ref="A1:B1"/><mergeCell ref="C1"/>'
            "</mergeCells></worksheet>"
        },
    )
    assert read_table(path) == Table(["Name", "B", "Zip"], [{"Name": "1", "B": "2", "Zip": "3"}])


@pytest.mark.parametrize("ref", ["", "A1:", "1:2", "A0:B1", "A1:B9999999", "XFE1:XFE2"])
def test_xlsx_bad_merged_ranges_are_damaged(tmp_path, ref):
    sheet = (
        f'<worksheet xmlns="{MAIN}"><sheetData>{xrow(inline("A1", "A"))}</sheetData>'
        f'<mergeCells><mergeCell ref="{ref}"/></mergeCells></worksheet>'
    )
    path = make_xlsx(tmp_path, "", extra={"xl/worksheets/sheet1.xml": sheet})
    assert code_of(path) == "damaged"


@pytest.mark.parametrize("number", ["0", "x", "1048577"])
def test_xlsx_bad_row_numbers_are_damaged(tmp_path, number):
    path = make_xlsx(tmp_path, f'<row r="{number}">{inline("A1", "A")}</row>')
    assert code_of(path) == "damaged"


def test_xlsx_merged_ranges_cannot_make_reading_slow(tmp_path, monkeypatch):
    rows = "".join(xrow(inline(f"A{n}", "x"), inline(f"B{n}", "y")) for n in range(1, 21))
    merges = '<mergeCell ref="A1:B20"/>' * 5
    sheet = (
        f'<worksheet xmlns="{MAIN}"><sheetData>{rows}</sheetData>'
        f"<mergeCells>{merges}</mergeCells></worksheet>"
    )
    path = make_xlsx(tmp_path, "", extra={"xl/worksheets/sheet1.xml": sheet})
    assert read_table(path) == Table(["x"], [])  # only A1 keeps its value
    monkeypatch.setattr(limits, "MAX_CELLS", 60)  # 40 cells, then 5 ranges of 20 rows
    assert code_of(path) == "too_large"
    monkeypatch.setattr(limits, "MAX_CELLS", 4)
    ranges = '<mergeCell ref="C3"/>' * 5
    sheet = (
        f'<worksheet xmlns="{MAIN}"><sheetData>{xrow(inline("A1", "x"))}</sheetData>'
        f"<mergeCells>{ranges}</mergeCells></worksheet>"
    )
    path = make_xlsx(tmp_path, "", extra={"xl/worksheets/sheet1.xml": sheet}, name="r.xlsx")
    assert code_of(path) == "too_large"  # more ranges than cells allowed


def test_xlsx_escaped_characters(tmp_path):
    path = make_xlsx(
        tmp_path,
        xrow(inline("A1", "A"), inline("B1", "B"))
        + xrow(inline("A2", "a_x000D_\nb_x0000_\t"), '<c r="B2" t="s"><v>0</v></c>')
        + xrow('<c r="A3" t="str"><v>x_x000D_y</v></c>'),
        shared=["<t>_x005F_x000D_ and _xD800_ and _x00e9_</t>"],
    )
    assert read_table(path).rows == [
        {"A": "a\nb\t", "B": "_x000D_ and _xD800_ and é"},
        {"A": "xy", "B": ""},
    ]


def test_xml_memory_limits(tmp_path, monkeypatch):
    """Elements outside rows are dropped as they end and counted; a row,
    nesting and the small parts have limits of their own."""
    header = xrow(inline("A1", "A")) + xrow(num("A2", "1"))
    filler = "<a/>" * 50
    sheet = f'<worksheet xmlns="{MAIN}"><cols>{filler}</cols><sheetData>{header}</sheetData></worksheet>'
    extra = {"xl/worksheets/sheet1.xml": sheet}
    path = make_xlsx(tmp_path, header, extra=extra)
    assert read_table(path).rows == [{"A": "1"}]
    monkeypatch.setattr(limits, "MAX_OTHER_ELEMENTS", 40)
    assert code_of(path) == "too_large"
    monkeypatch.setattr(limits, "MAX_ROW_ELEMENTS", 5)
    wide = xrow(*(num("A%d" % i, "1") for i in range(1, 3)))  # 2 cells, 2 values
    assert read_table(make_xlsx(tmp_path, header + wide, name="b.xlsx")).columns == ["A"]
    many = "<row>" + "<c/>" * 6 + "</row>"
    assert code_of(make_xlsx(tmp_path, header + many, name="c.xlsx")) == "too_large"
    monkeypatch.setattr(limits, "MAX_DEPTH", 8)
    deep = "<x>" * 8 + "</x>" * 8
    assert code_of(make_xlsx(tmp_path, header + deep, name="d.xlsx")) == "too_large"
    deep_ods = make_ods(tmp_path, content=ods_content(ods_table("T", row(text(deep)))))
    assert code_of(deep_ods) == "too_large"
    monkeypatch.setattr(limits, "MAX_PART_BYTES", 100)
    assert code_of(make_xlsx(tmp_path, header, name="e.xlsx")) == "too_large"  # workbook.xml


def test_tags_attributes_and_names_are_bounded(tmp_path, monkeypatch):
    """The parser keeps a start tag whole and every name it has seen: long
    tags, many attributes and many distinct names are refused."""
    header = xrow(inline("A1", "A")) + xrow(num("A2", "1"))

    def sheet(before: str = "", rows: str = "") -> dict[str, str]:
        data = (
            f'<worksheet {before} xmlns="{MAIN}"><sheetData>{header}{rows}</sheetData></worksheet>'
        )
        return {"xl/worksheets/sheet1.xml": data}

    monkeypatch.setattr(limits, "MAX_TAG_BYTES", 64 * 1024)
    long_tag = " ".join('a%d="%s"' % (i, ">" * 1000) for i in range(70))  # ">" is allowed
    assert code_of(make_xlsx(tmp_path, header, extra=sheet(long_tag))) == "too_large"
    monkeypatch.setattr(limits, "MAX_ATTRIBUTES", 3)
    attrs = 'a="" b="" c="" d=""'
    assert code_of(make_xlsx(tmp_path, header, extra=sheet(attrs), name="b.xlsx")) == "too_large"
    monkeypatch.setattr(limits, "MAX_NAMES", 12)
    names = "".join('<row a%d=""/>' % i for i in range(12))
    path = make_xlsx(tmp_path, header, extra=sheet(rows=names), name="c.xlsx")
    assert code_of(path) == "too_large"
    assert read_table(make_xlsx(tmp_path, header, name="d.xlsx")).rows == [{"A": "1"}]
    monkeypatch.setattr(limits, "MAX_ELEMENTS", 8)
    assert (
        code_of(make_xlsx(tmp_path, header + xrow(num("A3", "2")), name="e.xlsx")) == "too_large"
    )


def read_all(data: bytes, chunk: int) -> None:
    stream = GuardedStream(io.BytesIO(data), 10**7)
    stream.CHUNK = chunk
    while stream.read(chunk):
        pass


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 400])  # reads shorter than the limit, as in use
def test_tags_are_measured_across_reads_and_quotes(monkeypatch, chunk):
    """A tag ends at the first ">" outside quotes, wherever the reads split
    it; comments, CDATA and processing instructions end at their own marks."""
    monkeypatch.setattr(limits, "MAX_TAG_BYTES", 500)
    fine = (
        b'<?xml version="1.0"?><a b=">" c=\'">\'>'
        + b"x>" * 1000
        + b"</a>"
        + b"<!-- <b> ' \" -- -> --><![CDATA[ <c d=' ]]><e/>"
        + b"<f "
        + b'g=">" ' * 70
        + b"/>"  # 420 bytes
    )
    read_all(fine, chunk)
    for long in (
        b"<a " + b'b=">" ' * 100 + b"/>",
        b"<a " + b"b='>' " * 100 + b"/>",
        b'<a b="' + b">" * 600 + b'"/>',
        b"<a " + b"b " * 300 + b"/>",
        b"</a" + b" " * 600 + b">",
    ):
        with pytest.raises(MergeError, match="tag longer"):
            read_all(b"<r>" + long, chunk)
    for long in (b"<!--" + b"x" * 600 + b"-->", b"<?pi " + b"x?" * 300 + b"?>"):
        with pytest.raises(MergeError, match="comment or processing instruction longer"):
            read_all(b"<r>" + long, chunk)
    # the parser passes CDATA on in pieces, as text: not held whole
    read_all(b"<r><![CDATA[" + b"]>" * 300 + b"]]></r>", chunk)


@pytest.mark.parametrize("markup", [b"<?a?>", b"<!---->", b"<![CDATA[]]>"])
def test_floods_of_comments_are_refused_quickly(tmp_path, markup):
    header = xrow(inline("A1", "A")) + xrow(num("A2", "1"))
    flood = markup.decode() * 20_000
    data = f'<worksheet xmlns="{MAIN}"><sheetData>{header}</sheetData><x>{flood}</x></worksheet>'
    start = time.monotonic()
    path = make_xlsx(tmp_path, header, extra={"xl/worksheets/sheet1.xml": data})
    assert code_of(path) == "too_large"
    assert time.monotonic() - start < 2
    few = f'<worksheet xmlns="{MAIN}"><sheetData>{header}</sheetData>{markup.decode() * 50}</worksheet>'
    path = make_xlsx(tmp_path, header, extra={"xl/worksheets/sheet1.xml": few}, name="b.xlsx")
    assert read_table(path).rows == [{"A": "1"}]


def test_cancel_is_seen_between_reads():
    import threading

    from tavletta.merge.table import cancellable

    cancel = threading.Event()
    stream = GuardedStream(io.BytesIO(b"<a/>" * 100_000), 10**7)
    with cancellable(cancel):
        stream.read(1000)
        cancel.set()
        with pytest.raises(MergeError, match="cancelled"):
            stream.read(1000)


def test_long_tags_of_real_files_are_read(tmp_path):
    """Conditional formats over many ranges: a long sqref (XLSX), a long
    target range address (ODS)."""
    sqref = " ".join("R%d" % i for i in range(2, 30000, 2))  # about 100 KB
    cf = f'<conditionalFormatting sqref="{sqref}"><cfRule type="cellIs" priority="1"/></conditionalFormatting>'
    header = xrow(inline("A1", "A")) + xrow(num("A2", "1"))
    data = f'<worksheet xmlns="{MAIN}"><sheetData>{header}</sheetData>{cf}</worksheet>'
    path = make_xlsx(tmp_path, header, extra={"xl/worksheets/sheet1.xml": data})
    assert read_table(path).rows == [{"A": "1"}]
    target = " ".join("T.A%d:T.A%d" % (i, i) for i in range(1, 30000))  # about 400 KB
    formats = (
        '<calcext:conditional-formats><calcext:conditional-format calcext:target-range-address="'
        + target
        + '"/></calcext:conditional-formats>'
    )
    table = ods_table("T", row(text("A")), row(text("1"))).replace(
        "</table:table>", formats + "</table:table>"
    )
    assert read_table(make_ods(tmp_path, table)).rows == [{"A": "1"}]


def test_empty_rows_are_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_ROW_TAGS", 4)
    header = xrow(inline("A1", "A")) + xrow(num("A2", "1"))
    assert read_table(make_xlsx(tmp_path, header + "<row/>" * 2)).rows == [{"A": "1"}]
    assert code_of(make_xlsx(tmp_path, header + "<row/>" * 3, name="b.xlsx")) == "too_large"
    rows = [row(text("A")), row(text("1"))] + ["<table:table-row/>"] * 3
    assert code_of(make_ods(tmp_path, ods_table("T", *rows))) == "too_large"
    # counted per sheet
    path = make_ods(tmp_path, ods_table("T", *rows[:4]), ods_table("U", *rows[:4]), name="b.ods")
    assert len(read_book(path).sheets) == 2


def test_sheet_count_is_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_SHEETS", 2)
    sheets = [xrow(inline("A1", "A"))] * 3
    assert code_of(make_xlsx(tmp_path, *sheets)) == "too_large"
    tables = [ods_table("T%d" % i, row(text("A"))) for i in range(3)]
    with pytest.raises(MergeError, match="sheets"):
        read_book(make_ods(tmp_path, *tables))
    assert len(read_book(make_ods(tmp_path, *tables[:2], name="b.ods")).sheets) == 2


@pytest.mark.parametrize(
    ("code", "raw", "shown"),
    [
        ("00000", "12", "00012"),
        ("000.0", "-1.25", "-001.3"),
        ("0,", "1234567", "1235"),
        ("#,##0,,", "1234567", "1"),
        ("#,##0", "1234567", "1234567"),  # no thousands separator
        ("# ?/?", "0.5", "0.5"),  # fractions: General
        ("# ??/??", "2.25", "2.25"),
        ("[m]:ss", "0.1", "144:00"),
        ("[mm]", "0.001", "01"),
        ("[s]", "0.01", "864"),
        ("[s].00", "0.000011", "0.95"),
        ("[h]:mm", "0.25", "6:00"),
        ("[hh]:mm", "1.25", "30:00"),
        ("[h]:mm", "1e300", "1E+300"),  # Excel shows "#####"
        ("", "0.30000000000000004", "0.3"),
        ("", "123456.78901234", "123456.78901"),
        ("", "4006381333931", "4006381333931"),  # whole numbers keep their digits
        ("", "1.5e20", "1.5E+20"),
        ("", "0.00001", "1E-05"),
    ],
)
def test_number_formats(code, raw, shown):
    from tavletta.merge.numfmt import display

    assert display(raw, code, False) == shown


def test_xlsx_styles_are_streamed(tmp_path, monkeypatch):
    """Only the first cellXfs counts (not cellStyleXfs); fonts and fills of
    tens of thousands of styles are dropped as they are read."""
    fonts = "<fonts>" + '<font><b/><sz val="11"/></font>' * 3000 + "</fonts>"
    styles = (
        '<numFmts><numFmt numFmtId="164" formatCode="0.000"/></numFmts>'
        + '<dxfs><dxf><numFmt numFmtId="164" formatCode="0.0%"/></dxf></dxfs>'  # not a cell style
        + '<numFmts><numFmt numFmtId="164" formatCode="0.0"/></numFmts>'  # a second list: ignored
        + fonts
        + '<cellStyleXfs><xf numFmtId="14"/><xf numFmtId="14"/></cellStyleXfs>'
        + '<cellXfs><xf numFmtId="0"><alignment wrapText="1"/></xf><xf numFmtId="164"/></cellXfs>'
        + '<cellXfs><xf numFmtId="14"/><xf numFmtId="14"/></cellXfs>'
    )
    sheet = xrow(inline("A1", "A"), inline("B1", "B")) + xrow(num("A2", "2", 0), num("B2", "2", 1))
    path = make_xlsx(tmp_path, sheet, styles=styles)
    assert read_table(path).rows == [{"A": "2", "B": "2.000"}]
    monkeypatch.setattr(limits, "MAX_STYLES", 1)
    assert code_of(path) == "too_large"


def test_xlsx_empty_sheet(tmp_path):
    assert code_of(make_xlsx(tmp_path, "")) == "empty"


def test_xlsx_style_without_number_formats(tmp_path):
    styles = '<cellXfs><xf/><xf numFmtId="x"/><xf numFmtId="14"/></cellXfs>'
    cells = xrow(inline("A1", "A"), inline("B1", "B"), inline("C1", "C"))
    cells += xrow(num("A2", "5", 1), '<c r="B2" s="bad"><v>6</v></c>', num("C2", "7", -2))
    path = make_xlsx(tmp_path, cells, styles=styles)
    assert read_table(path).rows == [{"A": "5", "B": "6", "C": "7"}]


def test_xlsx_other_elements_in_rows_are_ignored(tmp_path):
    cells = xrow(inline("A1", "A"), "<extLst/>", inline("B1", "B"))
    assert read_table(make_xlsx(tmp_path, cells)).columns == ["A", "B"]


# --- the whole book: sheets and rows at once ---------------------------------------


def test_book_of_a_csv_file(tmp_path):
    book = read_book(write(tmp_path, "a.csv", "A\n1\n"))
    assert book == Book(Table(["A"], [{"A": "1"}]), [], "")


def test_book_of_an_ods_file_in_one_pass(tmp_path, monkeypatch):
    path = make_ods(
        tmp_path,
        ods_table("Erste", row(text("A")), row(text("1"))),
        ods_table("Zweite", row(text("B")), row(text("2"))),
        ods_table("Dritte", row(text("C"))),
    )
    assert read_book(path) == Book(
        Table(["A"], [{"A": "1"}]), ["Erste", "Zweite", "Dritte"], "Erste"
    )
    assert read_book(path, "Zweite").table.rows == [{"B": "2"}]
    parsed = []
    events = ods.events
    monkeypatch.setattr(
        ods, "events", lambda zf, name, records: parsed.append(name) or events(zf, name, records)
    )
    assert read_book(path, "Zweite").sheets == ["Erste", "Zweite", "Dritte"]
    assert parsed == ["content.xml"]


def test_book_of_a_sheet_that_is_gone_is_the_first_one(tmp_path):
    ods_path = make_ods(
        tmp_path,
        ods_table("Erste", row(text("A")), row(text("1"))),
        ods_table("Zweite", row(text("B"))),
    )
    book = read_book(ods_path, "Renamed")
    assert (book.sheet, book.table.rows) == ("Erste", [{"A": "1"}])
    xlsx_path = make_xlsx(
        tmp_path, xrow(inline("A1", "A")), xrow(inline("A1", "B")), names=("Eins", "Zwei")
    )
    assert read_book(xlsx_path, "Zwei") == Book(Table(["B"], []), ["Eins", "Zwei"], "Zwei")
    assert read_book(xlsx_path, "Drei").sheet == "Eins"


def test_book_without_sheets(tmp_path):
    with pytest.raises(MergeError) as info:
        read_book(make_ods(tmp_path, content=ods_content()), "Any")
    assert info.value.code == "not_table"
    with pytest.raises(MergeError) as info:
        read_book(make_xlsx(tmp_path, name="none.xlsx"))
    assert info.value.code == "not_table"


def test_only_plain_files_are_read(tmp_path):
    """A named pipe would block the reading thread for ever."""
    os.mkfifo(tmp_path / "pipe.csv")
    with pytest.raises(MergeError) as info:
        read_book(tmp_path / "pipe.csv")
    assert info.value.code == "not_table"
    assert code_of(tmp_path) == "not_table"  # a folder
    with pytest.raises(FileNotFoundError):
        read_book(tmp_path / "gone.csv")


# --- property tests -----------------------------------------------------------

MEMBERS = [
    "mimetype",
    "content.xml",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/worksheets/sheet1.xml",
    "xl/sharedStrings.xml",
    "xl/styles.xml",
]
TAGS = [
    "office:document-content",
    "office:spreadsheet",
    "table:table",
    "table:table-row",
    "table:table-cell",
    "table:covered-table-cell",
    "text:p",
    "text:s",
    "workbook",
    "sheets",
    "sheet",
    "Relationships",
    "Relationship",
    "worksheet",
    "sheetData",
    "row",
    "c",
    "v",
    "is",
    "t",
    "si",
    "cellXfs",
    "xf",
    "numFmt",
    "mergeCell",
]
ATTRS = [
    'table:name="T"',
    'table:number-columns-repeated="99999999"',
    'table:number-rows-repeated="99999999"',
    'office:value-type="date"',
    'office:date-value="2026-11-11"',
    'text:c="-5"',
    'r="A1"',
    'r="XFD1048576"',
    't="s"',
    't="b"',
    's="0"',
    'numFmtId="14"',
    'name="S"',
    'r:id="rId1"',
    'Id="rId1"',
    'Target="worksheets/sheet1.xml"',
    'formatCode="dd"',
    'ref="A1:B2"',
    'ref="B1:A99999"',
]
XML_HEAD = f'<root {ODS_NS} xmlns:r="{REL}">'


@st.composite
def xmlish(draw) -> str:
    parts = [XML_HEAD]
    for _ in range(draw(st.integers(0, 12))):
        tag = draw(st.sampled_from(TAGS))
        attrs = " ".join(draw(st.lists(st.sampled_from(ATTRS), max_size=3)))
        body = draw(st.sampled_from(["", "1", "x", "-1e400", "46337", "0", "{a}"]))
        parts.append(f"<{tag} {attrs}>{body}</{tag}>")
    parts.append("</root>")
    xml = "".join(parts)
    if draw(st.booleans()):  # cut somewhere
        xml = xml[: draw(st.integers(0, len(xml)))]
    return xml


member_data = st.one_of(
    st.binary(max_size=200), xmlish(), st.just("application/vnd.oasis.opendocument.spreadsheet")
)


def outcome(path) -> None:
    try:
        table = read_table(path)
    except MergeError as e:
        assert e.code in ("not_table", "damaged", "too_large", "empty")
        return
    assert isinstance(table, Table)
    for item in table.rows:
        assert list(item) == table.columns
        assert all(isinstance(v, str) for v in item.values())


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(data=st.binary(max_size=300))
def test_any_bytes_give_a_table_or_a_merge_error(tmp_path, data):
    outcome(write(tmp_path, "any.csv", data))


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(members=st.dictionaries(st.sampled_from(MEMBERS), member_data, max_size=6))
def test_any_archive_gives_a_table_or_a_merge_error(tmp_path, members):
    outcome(write_zip(tmp_path, "any.zip", members))


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(position=st.integers(0, 10_000), value=st.integers(0, 255))
def test_damaged_spreadsheets_give_a_table_or_a_merge_error(tmp_path, position, value):
    styles = '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
    good = make_xlsx(
        tmp_path,
        xrow(inline("A1", "A"), '<c r="B1" t="s"><v>0</v></c>') + xrow(num("A2", "46337", 1)),
        shared=["<t>B</t>"],
        styles=styles,
    )
    data = bytearray(good.read_bytes())
    data[position % len(data)] = value
    outcome(write(tmp_path, "broken.xlsx", bytes(data)))


# --- placeholders -------------------------------------------------------------


def test_fill_replaces_known_names():
    row_ = {"Name": "Anna", "City": "Bern", "": "nothing"}
    assert fill("{Name} from {City}", row_) == "Anna from Bern"
    assert fill("{ Name }", row_) == "Anna"
    assert fill("{Unknown} {Name}", row_) == "{Unknown} Anna"
    # with a data file "{{" and "}}" are always escapes
    assert fill("{{Name}} {{ and }}", row_) == "{Name} { and }"
    assert fill("{{Name}} {{ and }} {City}", row_) == "{Name} { and } Bern"
    assert fill("{{{Name}}}", row_) == "{Anna}"
    assert fill("{Name", row_) == "{Name"
    assert fill("a } b {", row_) == "a } b {"
    assert fill("{}", row_) == "{}"


def test_fill_does_not_evaluate_format_specs():
    row_ = {"Name": "Anna"}
    assert fill("{Name.__class__}", row_) == "{Name.__class__}"
    assert fill("{Name!r:>10}", row_) == "{Name!r:>10}"
    assert fill("{0}", row_) == "{0}"
    # a value with braces is inserted as it is, not filled again
    assert fill("{Name}", {"Name": "{Name}"}) == "{Name}"


def test_fill_is_one_pass():
    """Values are never looked at again: placeholders, variables and doubled
    braces in a cell print as they are."""
    row_ = {"Name": "{=date} {Other} {{ }}", "Other": "x"}
    assert fill("{Name}", row_) == "{=date} {Other} {{ }}"
    assert fill("{{{Name}}}", row_) == "{{=date} {Other} {{ }}}"


def test_braces_are_always_escapes_with_a_data_file():
    """A data file is chosen on purpose: its texts follow the placeholder
    syntax, "{{" is a brace whether or not the text has a placeholder.
    Without a data file nothing is filled (see variables.expand)."""
    assert fill("a {{b}}", {}) == "a {b}"
    assert fill("a {{b}} {}", {"b": "x"}) == "a {b} {}"
    assert fill('{{"id":{b}}}', {"b": "7"}) == '{"id":7}'
    assert fill("{{=date}}", {}) == "{=date}"
    assert fill("{=date} {{x}}", {}) == "{=date} {x}"


def test_variables_are_never_columns():
    """{=date}, {=counter:0001} … belong to variables; a column of that name
    does not fill them, and they are no column names."""
    row = {"=date": "column", "Name": "Anna", "= x": "y"}
    text = "{=date} {= x} {=counter:0001} {{=date}} {Name}"
    assert fill(text, row) == "{=date} {= x} {=counter:0001} {=date} Anna"
    assert placeholders(text) == ["Name"]


def test_placeholders():
    assert placeholders("{A} {{B}} {C}{A} { D }{") == ["A", "C", "D"]
    assert placeholders("") == []


@given(
    text_=st.text(alphabet="{}ab :.\n", max_size=40),
    row_=st.dictionaries(st.text(alphabet="{}ab :.", max_size=4), st.text(max_size=6), max_size=4),
)
def test_fill_never_raises(text_, row_):
    result = fill(text_, row_)
    assert isinstance(result, str)
    if "{" not in text_ and "}" not in text_:
        assert result == text_
    # nothing to replace and no doubled braces: the text stays as it is
    if "{{" not in text_ and "}}" not in text_:
        assert fill(text_, {}) == text_


@given(
    name=st.text(alphabet=st.characters(blacklist_characters="{}"), min_size=1, max_size=10),
    value=st.text(max_size=10),
)
def test_fill_inserts_any_value(name, value):
    assume(not name.strip().startswith("="))  # a variable, never a column
    if name.strip():
        assert fill("<{%s}>" % name, {name.strip(): value}) == "<%s>" % value
