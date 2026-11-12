"""ZPL, EPL2, TSPL, ESC/POS and DYMO on the byte level: jobs for known
images byte for byte (the examples of docs/zpl.md and the others, taken
from LPrint's sequences), Zebra's run-length code both ways, status answers
including garbage, and the backend contract against a fake printer."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from fake_raw import (
    COUNTERS,
    ZPL_HI,
    ZPL_HQES,
    ZPL_HS,
    ZPL_HS_PAPER_OUT,
    FakePrinter,
    install,
)
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from etiketto.backends import dymo, epl, escpos, get_backend, raw, tspl, zpl
from etiketto.backends.base import (
    CoverOpen,
    HeadOpen,
    LabelNotFound,
    LabelTooWide,
    OutOfLabels,
    OutOfPaper,
    PaperJam,
    PortClosed,
    PrinterError,
    PrinterInfo,
    PrinterPaused,
    PrintOptions,
    RenderAgain,
    ResolutionUnknown,
    RibbonError,
)
from etiketto.backends.raw import Bitmap
from etiketto.model import Label

ESC = b"\x1b"


def bits(*rows: bytes, black_is_one: bool = True, width: int | None = None) -> Bitmap:
    return Bitmap(width or len(rows[0]) * 8, rows, black_is_one)


# --- ZPL ------------------------------------------------------------------------------


def test_zpl_graphic_matches_lprint():
    # the example of docs/zpl.md: 16 dots, rows FF F0, FF F0, 00 00
    image = bits(b"\xff\xf0", b"\xff\xf0", b"\x00\x00")
    assert zpl.download(image, "R:LPRINT.GRF") == b"~DGR:LPRINT.GRF,6,2,IF0:,"


@pytest.mark.parametrize(
    "count, code",
    [(1, "F"), (2, "HF"), (19, "YF"), (20, "gF"), (39, "gYF"), (400, "zF"), (401, "zGF")],
)
def test_zpl_run_lengths(count, code):
    assert zpl.rle_run("F", count) == code


@pytest.mark.parametrize(
    "row, previous, code",
    [
        (b"\x00\x00", None, ","),  # all zero: fill the row
        (b"\xff\xff", None, "JF"),
        (b"\x12\x34", None, "1234"),
        (b"\x10\x00", None, "10,"),  # odd zero run: one "0" first, then the comma
        (b"\x01\x00", None, "01,"),
        (b"\x00\x01", None, "I01"),
        (b"\x12\x34", b"\x12\x34", ":"),
    ],
)
def test_zpl_rows(row, previous, code):
    assert zpl.encode_row(row, previous) == code


def decode_zpl(data: str, bpr: int) -> list[bytes]:
    """Zebra's alphabet, written from the manual: G..Y 1..19, g..z 20..400,
    ":" repeats the previous row, "," fills with 0, "!" with 1."""
    rows: list[bytes] = []
    hexrow, count = "", 0
    for char in data:
        if "G" <= char <= "Y":
            count += ord(char) - ord("F")
        elif "g" <= char <= "z":
            count += (ord(char) - ord("f")) * 20
        elif char in ":,!":
            if char == ":":
                hexrow = rows[-1].hex().upper()
            else:
                hexrow += ("0" if char == "," else "F") * (bpr * 2 - len(hexrow))
        else:
            hexrow += char * (count or 1)
            count = 0
        if len(hexrow) == bpr * 2:
            rows.append(bytes.fromhex(hexrow))
            hexrow = ""
    assert hexrow == "" and count == 0
    return rows


@given(
    st.integers(1, 40).flatmap(
        lambda bpr: st.lists(
            st.one_of(
                st.binary(min_size=bpr, max_size=bpr),
                st.sampled_from([b"\x00" * bpr, b"\xff" * bpr, b"\x0f" * bpr]),
            ),
            min_size=1,
            max_size=12,
        )
    )
)
def test_zpl_compression_roundtrip(rows):
    image = bits(*rows)
    assert decode_zpl(zpl.encode_graphic(image), image.bpr) == list(rows)


def test_zpl_long_runs_roundtrip():
    rows = [b"\xaa" * 300, b"\x00" * 299 + b"\x01", b"\xf0" + b"\x00" * 299, b"\x11" * 300]
    assert decode_zpl(zpl.encode_graphic(bits(*rows)), 300) == rows


def test_zpl_job():
    image = bits(b"\xff\xf0", b"\xff\xf0", b"\x00\x00", width=12)
    assert zpl.build_job(image) == (
        b"~DGR:ETIKETTO.GRF,6,2,IF0:,\n"
        b"^XA\n^PON\n^PW12\n^LH0,0\n^LL3\n^PQ1,0,1,N\n"
        b"^FO0,0^XGR:ETIKETTO.GRF,1,1^FS\n^XZ\n"
        b"^XA^IDR:ETIKETTO.GRF^FS^XZ\n"
    )


def test_zpl_job_with_settings():
    job = zpl.build_job(
        bits(b"\x80"), copies=3, media="mark", finish="peel", darkness=50, speed=4
    ).decode()
    assert job.startswith("~SD15\n~DGR:ETIKETTO.GRF,1,1,80\n^XA\n^MMP,N\n^PR4,4,4\n^PON\n")
    assert "^MNM\n^PQ3,0,1,N\n" in job
    assert "^MNY" in zpl.build_job(bits(b"\x80"), media="gap").decode()
    assert "^MN" not in zpl.build_job(bits(b"\x80"), media="printer").decode()
    assert "^MMC,Y" in zpl.build_job(bits(b"\x80"), finish="cut").decode()
    assert "^MNN" in zpl.build_job(bits(b"\x80"), media="continuous").decode()
    assert "~SD30" in zpl.build_job(bits(b"\x80"), darkness=100).decode()
    assert "~SD00" in zpl.build_job(bits(b"\x80"), darkness=0).decode()
    assert "^MM" not in zpl.build_job(bits(b"\x80"), finish="printer").decode()


@pytest.mark.parametrize(
    "answer, expected",
    [
        (ZPL_HI, ("ZD621-203dpi", "V93.21.18Z", "203")),
        (b"\x02GX430t,V61.17,12,2104KB\x03", ("GX430t", "V61.17", "300")),
        (b"\x02ZT610,V1,24,\x03", ("ZT610", "V1", "600")),
        (b"\x02ZT610,V1,9,\x03", ("ZT610", "V1", "")),  # no known resolution
        (b"\x02ZT610,V1,x\x03", ("", "", "")),  # not a ~HI frame
        (b"\x02only,two\x03", ("", "", "")),
        (b"ZD621-203dpi,V93,8", ("", "", "")),  # no frame
        (b"", ("", "", "")),
        (b"\xff\xfe\x02\x00", ("", "", "")),
    ],
)
def test_zpl_identity(answer, expected):
    identity = zpl.parse_identity(answer)
    assert (identity.model, identity.firmware, identity.dpi) == expected


def test_zpl_host_status():
    status = zpl.parse_host_status(ZPL_HS)
    assert status is not None and not status.busy
    assert zpl.host_status_error(status) is None
    out = zpl.parse_host_status(ZPL_HS_PAPER_OUT)
    assert isinstance(zpl.host_status_error(out), OutOfLabels)


def _hs(line1: str, line2: str) -> bytes:
    """A ~HS answer from the fields Etiketto reads; the rest padded to the
    manual's 12 and 11 fields."""
    one, two = line1 + ",0,0,0,000,0,0,0", line2 + ",1,000"
    return b"\x02%s\x03\r\n\x02%s\x03\r\n\x021234,0\x03\r\n" % (one.encode(), two.encode())


@pytest.mark.parametrize(
    "line1, line2, cls",
    [
        ("030,0,1,1245,000", "000,0,0,0,1,2,6,0,00000000", PrinterPaused),
        ("030,0,0,1245,000", "000,0,1,0,1,2,6,0,00000000", HeadOpen),
        ("030,0,0,1245,000", "000,0,0,1,1,2,6,0,00000000", RibbonError),
        ("030,1,1,1245,000", "000,0,1,1,1,2,6,0,00000000", OutOfLabels),  # the first counts
    ],
)
def test_zpl_host_status_problems(line1, line2, cls):
    status = zpl.parse_host_status(_hs(line1, line2))
    assert status is not None
    error = zpl.host_status_error(status)
    assert type(error) is cls and error.details.startswith("~HS: ")


@pytest.mark.parametrize(
    "line1, line2, busy",
    [
        ("030,0,0,1245,002", "000,0,0,0,1,2,6,0,00000000", True),  # formats waiting
        ("030,0,0,1245,000", "000,0,0,0,1,2,6,0,00000004", True),  # labels to print
        ("030,0,0,1245,000", "000,0,0,0,1,2,6,0,0000000x", False),
    ],
)
def test_zpl_busy(line1, line2, busy):
    status = zpl.parse_host_status(_hs(line1, line2))
    assert status is not None and status.busy is busy


def test_zpl_frames_are_found_behind_a_late_answer():
    # ~HI answered late: its frame comes first in the answer to ~HS
    status = zpl.parse_host_status(ZPL_HI + ZPL_HS_PAPER_OUT)
    assert status is not None and status.paper_out
    assert zpl.parse_identity(ZPL_HS + ZPL_HI).model == "ZD621-203dpi"
    assert zpl.parse_identity(ZPL_HS) == zpl.Identity()


@pytest.mark.parametrize(
    "answer",
    [
        b"",
        b"garbage",
        b"\x02030,0,0,1245,000\x03",  # one frame only
        b"\x02030,0\x03\x02000,0,0,0,1,2,6,0,0,1,000\x03",  # first frame too short
        b"\x02030,0,0,1245,000,0,0,0,000,0,0,0\x03\x02000,0,0\x03",  # second too short
    ],
)
def test_zpl_host_status_garbage(answer):
    assert zpl.parse_host_status(answer) is None


@pytest.mark.parametrize(
    "bits_, cls",
    [
        (0x00000001, OutOfLabels),
        (0x00000002, RibbonError),
        (0x00000004, HeadOpen),
        (0x00001000, PaperJam),
        (0x00010000, PrinterPaused),
        (0x00080000, LabelNotFound),
        (0x00000008, PrinterError),
        (0x00000100, PrinterError),  # a bit without a name: still an error
    ],
)
def test_zpl_extended_errors(bits_, cls):
    answer = b"PRINTER STATUS\r\n   ERRORS:         1 00000000 %08X\r\n" % bits_
    found = zpl.parse_errors(answer)
    assert found == bits_
    assert type(zpl.errors_error(found)) is cls


def test_zpl_extended_status_garbage():
    assert zpl.parse_errors(ZPL_HQES) == 0
    assert zpl.errors_error(0) is None
    assert zpl.parse_errors(b"") is None
    assert zpl.parse_errors(b"ERRORS: x y z") is None


# --- EPL2 -----------------------------------------------------------------------------


def test_epl_job():
    # 12 black dots in two rows, a blank row: 0 = black, the blank row is not sent
    image = raw.bitmap(_black_rows(16, [(0, 12), (0, 12), None]), black_is_one=False)
    assert epl.build_job(image, media="gap") == (
        b"\nN\nq16\nQ3,24\nGW0,0,2,1\n\x00\x0f\nGW0,1,2,1\n\x00\x0f\nP1\n"
    )
    # the printer's own sensor setting: no Q
    assert epl.build_job(image).startswith(b"\nN\nq16\nGW0,0,2,1\n")


def test_epl_job_with_settings():
    image = bits(b"\x0f", black_is_one=False)
    job = epl.build_job(image, copies=2, media="mark", gap=16, darkness=50)
    assert job == b"\nN\nD7\nq8\nQ1,B16\nGW0,0,1,1\n\x0f\nP2\n"
    assert b"Q1,0\n" in epl.build_job(image, media="continuous")
    assert b"D15\n" in epl.build_job(image, darkness=100)


def test_epl_skips_white_rows_not_black_ones():
    # LPrint skips rows of 0x00, which are black in EPL2
    image = bits(b"\x00", b"\xff", black_is_one=False)
    assert epl.rows(image) == b"GW0,0,1,1\n\x00\n"


# --- TSPL -----------------------------------------------------------------------------


def test_tspl_job():
    image = raw.bitmap(_black_rows(16, [(0, 8)] * 8), black_is_one=False)
    data = b"\x00\xff" * 8
    assert (
        tspl.build_job(image, 8.0, media="gap")
        == (b"SIZE 2 mm,1 mm\nGAP 3 mm,0 mm\nDIRECTION 0,0\nCLS\nBITMAP 0,0,2,8,1," + data)
        + b"PRINT 1,1\n"
    )
    assert tspl.build_job(image, 8.0).startswith(b"SIZE 2 mm,1 mm\nDIRECTION 0,0\nCLS\n")
    assert b"DIRECTION 1,0\n" in tspl.build_job(image, 8.0, upside_down=True)


def test_tspl_job_with_settings():
    job = tspl.build_job(
        bits(b"\x0f", black_is_one=False),
        12.0,
        copies=4,
        media="mark",
        gap_mm=2.5,
        finish="cut",
        darkness=50,
        speed=3,
    )
    assert job == (
        b"SIZE 0.67 mm,0.08 mm\nBLINE 2.5 mm,0 mm\nDIRECTION 0,0\nDENSITY 8\nSPEED 3\n"
        b"SET PEEL OFF\nSET TEAR OFF\nSET CUTTER 1\nCLS\nBITMAP 0,0,1,1,1,\x0fPRINT 4,1\n"
    )
    plain = bits(b"\x0f", black_is_one=False)
    assert b"GAP 0 mm,0 mm" in tspl.build_job(plain, 8.0, media="continuous")
    # the printer keeps the mode: each one switches the other two off
    assert b"SET CUTTER OFF\nSET PEEL OFF\nSET TEAR ON\n" in tspl.build_job(
        plain, 8.0, finish="tear"
    )
    assert b"SET CUTTER OFF\nSET TEAR OFF\nSET PEEL ON\n" in tspl.build_job(
        plain, 8.0, finish="peel"
    )
    assert b"SET " not in tspl.build_job(plain, 8.0, finish="printer")


@pytest.mark.parametrize(
    "answer, cls",
    [
        (b"\x01", HeadOpen),
        (b"\x02", PaperJam),
        (b"\x03", HeadOpen),
        (b"\x04", OutOfLabels),
        (b"\x05", OutOfLabels),  # out of paper and head open: paper first
        (b"\x08", RibbonError),
        (b"\x10", PrinterPaused),
        (b"\x80", PrinterError),
    ],
)
def test_tspl_status(answer, cls):
    assert type(tspl.status_error(tspl.parse_status(answer))) is cls


@pytest.mark.parametrize("answer", [b"", b"\x00\x00", b"OK\r\n"])
def test_tspl_status_garbage(answer):
    assert tspl.parse_status(answer) is None


def test_tspl_ready_and_printing():
    assert tspl.status_error(0x00) is None
    assert tspl.status_error(0x20) is None  # printing is no problem


@pytest.mark.parametrize(
    "answer, model",
    [
        (b"TTP-244 Plus\r", "TTP-244 Plus"),
        (b" TE200 \r\n", "TE200"),
        (b"\x00\xffX-1\x07\r", "X-1"),
        (b"", ""),
        (b"A" * 100, "A" * 64),
    ],
)
def test_tspl_model(answer, model):
    assert tspl.parse_model(answer) == model


# --- ESC/POS --------------------------------------------------------------------------


def test_escpos_job_matches_lprint():
    # docs/escpos.md: 16 dots, rows AA AA and 55 55, no margin. LPrint feeds
    # one inch (the byte is the dpi) and cuts with ESC i; Etiketto sets the
    # motion units to dots first (GS P), cuts with GS V and only when asked to.
    image = bits(b"\xaa\xaa", b"\x55\x55")
    assert escpos.build_job(image, raw.DPI["203"]) == bytes.fromhex(
        "1b40 1d50cbcb 1d4c0000 1d7630000200 0200 aaaa5555 1b4acb 1b40"
    )
    assert escpos.build_job(image, 180.0, cut="full") == bytes.fromhex(
        "1b40 1d50b4b4 1d4c0000 1d7630000200 0200 aaaa5555 1b4ab4 1d5600 1b40"
    )
    assert escpos.build_job(image, 180.0, cut="half").endswith(bytes.fromhex("1d5601 1b40"))


def test_escpos_feeds_blank_rows():
    blank = b"\x00"
    image = bits(*([blank] * 600 + [b"\x80"] + [blank] * 2))
    page = escpos.page(image, 203.2)
    assert page == (
        b"\x1bJ\xff\x1bJ\xff\x1bJ\x5a"  # the spacing at the start stays
        + b"\x1dv0\x00\x01\x00\x01\x00\x80"
        + b"\x1bJ\x02"
        + b"\x1bJ\xcb"
    )


def test_escpos_splits_large_images():
    row = b"\xff" * 72  # 80 mm paper at 203 dpi
    image = bits(*([row] * 1000))
    page = escpos.page(image, 203.2)
    per_block = 32768 // 72
    blocks = re.finditer(rb"\x1dv0\x00H\x00(..)", page, re.DOTALL)
    heights = [int.from_bytes(m.group(1), "little") for m in blocks]
    assert heights == [per_block, per_block, 1000 - 2 * per_block]


def test_escpos_copies():
    job = escpos.build_job(bits(b"\x80"), 203.2, copies=3, cut="full")
    assert job.count(b"\x1dV\x00") == 3 and job.count(b"\x1b@") == 2


@pytest.mark.parametrize(
    "offline, paper, cls",
    [
        (b"\x16", b"\x12", CoverOpen),
        (b"\x32", b"\x12", OutOfPaper),  # stopped at the paper end
        (b"\x12", b"\x72", OutOfPaper),
        (b"\x52", b"\x12", PrinterError),
        (b"", b"\x72", OutOfPaper),  # one of the two answered
        (b"\x12", b"\x1e", type(None)),  # paper near the end: no problem yet
        (b"\x12", b"\x12", type(None)),
    ],
)
def test_escpos_status(offline, paper, cls):
    error = escpos.status_error(escpos.parse_status(offline), escpos.parse_status(paper))
    assert type(error) is cls


@pytest.mark.parametrize("answer", [b"", b"\x00", b"\xff", b"\x12\x12", b"OK"])
def test_escpos_status_garbage(answer):
    assert escpos.parse_status(answer) is None


def test_escpos_status_details():
    error = escpos.status_error(None, 0x72)
    assert error is not None and error.details == "DLE EOT 2 --, 4 72: paper end"


@pytest.mark.parametrize(
    "tape, dpi, dots", [(58, 203.2, 384), (80, 203.2, 576), (80, 180.0, 512), (58, 180.0, 344)]
)
def test_escpos_head_widths(tape, dpi, dots):
    assert escpos.head_dots(tape, dpi) == dots


def test_escpos_rotates_the_label_into_rows(monkeypatch):
    backend = escpos.EscPos()
    label = Label(printer="escpos", tape_mm=58, elements=[], background="#000000")
    image = backend.prepare(label, PrintOptions())
    assert image.height == 384 and image.info[raw.DPI_INFO] == raw.DPI["203"]
    # one black dot at the start of the label, at its top edge
    image = Image.new("RGB", (40, 384), "white")
    image.putpixel((0, 0), (0, 0, 0))
    job = backend.build_job(image, 203.2, PrintOptions(cut="none"), "printer.invalid")
    # the first row leaves first; the label's top edge is the head's right end
    first = job[job.index(b"\x1dv0") + 8 :][:48]
    assert first == b"\x00" * 47 + b"\x01"
    assert backend.build_job(image, 203.2, PrintOptions(cut="odd"), "x").count(b"\x1dV") == 0


# --- DYMO -----------------------------------------------------------------------------


def test_dymo_job_matches_lprint():
    # docs/dymo.md: rows FF 00, blank, 00 FF; darkness 50 % = ESC d. LPrint
    # selects the roll with a binary 1; DYMO's manual wants ASCII "1" (31)
    image = bits(b"\xff\x00", b"\x00\x00", b"\x00\xff")
    assert dymo.build_job(image, darkness=50) == ESC * 200 + bytes.fromhex(
        "1b40 1b510000 1b4200 1b4c0003 1b4402 1b7131 1b6416ff00 1b660101 1600ff 1b45"
    )
    # a little longer than the label: the printer searches the next sense hole
    assert b"\x1bL\x01\x2c" in dymo.build_job(image, length=300)


def test_dymo_skips_blank_rows():
    blank = b"\x00"
    image = bits(*([blank] * 300 + [b"\x01"] + [blank] * 5))
    assert dymo.rows(image) == b"\x1bf\x01\xff\x1bf\x01\x2d\x16\x01"  # the end is not sent


@pytest.mark.parametrize(
    "darkness, code", [(0, b"c"), (33, b"c"), (34, b"d"), (67, b"e"), (100, b"g")]
)
def test_dymo_density(darkness, code):
    page = dymo.page(bits(b"\x01"), darkness)
    assert ESC + code + b"\x16" in page
    assert b"\x1bq1\x16" in dymo.page(bits(b"\x01"))  # not set: the printer's own


def test_dymo_copies_and_head():
    assert dymo.build_job(bits(b"\x01"), copies=2).count(b"\x1bE") == 2
    assert dymo.head_dots("LabelWriter 4XL") == 1248
    assert dymo.head_dots("LabelWriter 450") == 672
    assert dymo.head_dots("") == 672


@pytest.mark.parametrize(
    "answer, cls",
    [(b"\xa0", OutOfLabels), (b"\xc0", PaperJam), (b"\x81", PrinterError), (b"\x03", type(None))],
)
def test_dymo_status(answer, cls):
    assert type(dymo.status_error(dymo.parse_status(answer))) is cls


@pytest.mark.parametrize("answer", [b"", b"\x07", b"\x03\x03"])
def test_dymo_status_garbage(answer):
    assert dymo.parse_status(answer) is None


def test_dymo_query(monkeypatch):
    printer = FakePrinter({ESC + b"A": b"\x03"})
    install(monkeypatch, printer)
    backend = dymo.Dymo()
    assert backend.query("usb://DYMO/LabelWriter%20450").state == "IDLE"
    printer.answers[ESC + b"A"] = b"\xa0"
    info = backend.query("usb://DYMO/LabelWriter%20450")
    assert (info.state, info.error) == ("ERROR", "out_of_labels")
    with pytest.raises(OutOfLabels):
        backend.print_label(Image.new("RGB", (100, 40)), "usb://x/LabelWriter", PrintOptions())
    assert printer.jobs == []


def test_dymo_4xl_on_the_network(monkeypatch):
    from etiketto.backends import mdns

    install(monkeypatch, FakePrinter())
    searches = []
    service = mdns.Service("DYMO LabelWriter 4XL", "lw.local", "192.0.2.5", 9100, {})

    def browse(service_type, match, timeout):
        searches.append(timeout)
        return [service]

    monkeypatch.setattr(raw, "_browse", browse)
    backend = dymo.Dymo()
    assert backend.query("none.invalid").model == "" and searches == [1.0]
    backend.query("none.invalid")  # looked once, not announced: not again
    assert searches == [1.0]
    # that search saw lw.local: known without another one
    info = backend.query("lw.local")  # the model from its announcement
    assert info.model == "DYMO LabelWriter 4XL" and searches == [1.0]
    fresh = dymo.Dymo()
    assert fresh.query("lw.local").model == "DYMO LabelWriter 4XL"
    assert searches == [1.0, 1.0]
    backend.print_label(Image.new("RGB", (1200, 40), "white"), "lw.local", PrintOptions())


def test_dymo_wide_labels(monkeypatch):
    backend = dymo.Dymo()
    fake = install(monkeypatch, FakePrinter())
    # 57 mm labels are a dot wider than the 450's head: cut, not refused
    image = Image.new("RGB", (673, 40), "white")
    backend.print_label(image, "usb://DYMO/LabelWriter%20450", PrintOptions())
    assert b"\x1bD\x54" in fake.printer.jobs[0]  # 84 bytes per row
    with pytest.raises(LabelTooWide) as raised:
        backend.print_label(
            Image.new("RGB", (800, 40)), "usb://DYMO/LabelWriter%20450", PrintOptions()
        )
    assert raised.value.params["max_mm"] == 56.9
    backend.print_label(
        Image.new("RGB", (800, 40)), "usb://DYMO/LabelWriter%204XL", PrintOptions()
    )


# --- backends -------------------------------------------------------------------------


def _black_rows(width: int, spans: list) -> Image.Image:
    """An image with a black span (start, end) per row, None = white row."""
    image = Image.new("L", (width, len(spans)), 255)
    for y, span in enumerate(spans):
        if span:
            for x in range(*span):
                image.putpixel((x, y), 0)
    return image


def test_zpl_query_reads_resolution_and_model(monkeypatch):
    backend = zpl.Zpl()
    fake = install(
        monkeypatch,
        FakePrinter(
            {b"~HI\n": ZPL_HI.replace(b",8,", b",12,"), b"~HS\n": ZPL_HS, b"~HQES\n": ZPL_HQES}
        ),
    )
    info = backend.query("zebra.invalid")
    assert (info.model, info.firmware, info.state, info.error) == (
        "ZD621-203dpi",
        "V93.21.18Z",
        "IDLE",
        "",
    )
    assert backend.display_name(info) == "ZD621-203dpi"
    assert backend.display_name(None) == "Zebra ZPL"
    assert fake.calls == [("zebra.invalid", 9100, raw.usb.BUSY_WAIT)]
    # "auto": the reported 12 dots per mm
    assert backend.raster(25, PrintOptions(host="zebra.invalid")).dpi == raw.DPI["300"]
    assert backend.raster(25, PrintOptions(host="other.invalid")).dpi == raw.DPI["203"]
    assert backend.raster(25, PrintOptions(settings={"dpi": "600"})).dpi == raw.DPI["600"]


def test_zpl_query_states(monkeypatch):
    backend = zpl.Zpl()
    printer = FakePrinter({b"~HS\n": ZPL_HS_PAPER_OUT})
    install(monkeypatch, printer)
    info = backend.query("zebra.invalid")
    assert (info.state, info.error, info.model) == ("ERROR", "out_of_labels", "")
    printer.answers[b"~HS\n"] = ZPL_HS.replace(b"030,0,0,1245", b"030,0,1,1245")
    assert backend.query("zebra.invalid").state == "PAUSED"
    printer.answers[b"~HS\n"] = ZPL_HS.replace(b"1245,000", b"1245,001")
    assert backend.query("zebra.invalid").state == "BUSY"
    printer.answers[b"~HQES\n"] = b"\x02 ERRORS: 1 00000000 00001000\x03"
    printer.answers[b"~HS\n"] = ZPL_HS
    assert backend.query("zebra.invalid").error == "jam"


def test_zpl_silent_printer_is_unknown_not_an_error(monkeypatch):
    backend = zpl.Zpl()
    printer = FakePrinter()
    install(monkeypatch, printer)
    info = backend.query("zebra.invalid")
    assert (info.state, info.error) == ("UNKNOWN", "")
    assert backend.raster(25, PrintOptions()).dpi == raw.DPI["203"]  # nothing reported
    # "As reported by the printer" from a printer that never told: refused
    with pytest.raises(ResolutionUnknown):
        backend.print_label(Image.new("RGB", (16, 8), "white"), "zebra.invalid", PrintOptions())
    assert printer.jobs == []
    # a printer that ignores ~HS is not asked ~HQES either; the job goes out
    printer.requests.clear()
    chosen = PrintOptions(settings={"dpi": "203"})
    backend.print_label(Image.new("RGB", (16, 8), "white"), "zebra.invalid", chosen)
    assert len(printer.jobs) == 1 and printer.requests == [b"~HS\n"]


def test_zpl_auto_resolution_per_printer(monkeypatch):
    backend = zpl.Zpl()
    printer = FakePrinter({b"~HI\n": ZPL_HI.replace(b",8,", b",12,"), b"~HS\n": ZPL_HS})
    install(monkeypatch, printer)
    backend.query("a.invalid")  # 300 dpi
    assert backend.reported == {"a.invalid": raw.DPI["300"]}
    # rendered at 150 dpi: twice as large, every dot a block of dots
    image = Image.new("RGB", (8, 4), "white")
    image.info[raw.DPI_INFO] = raw.DPI["300"] / 2
    backend.print_label(image, "a.invalid", PrintOptions())
    assert b"^PW16\n" in printer.jobs[-1]
    # no answer now: what it reported before
    del printer.answers[b"~HI\n"]
    backend.print_label(image, "a.invalid", PrintOptions())
    assert b"^PW16\n" in printer.jobs[-1]
    # rendered at 600 dpi: halving would drop thin lines, render anew
    image.info[raw.DPI_INFO] = raw.DPI["600"]
    with pytest.raises(RenderAgain):
        backend.print_label(image, "a.invalid", PrintOptions())
    # rendered at 203 dpi: 1.5 times would spoil lines, render anew
    image.info[raw.DPI_INFO] = raw.DPI["203"]
    with pytest.raises(RenderAgain):
        backend.print_label(image, "a.invalid", PrintOptions())
    assert backend.dpi(PrintOptions(host="a.invalid")) == raw.DPI["300"]
    # a printer never heard of: asked with a longer timeout, then refused
    asked = []
    real_ask = raw.Session.ask
    monkeypatch.setattr(
        raw.Session,
        "ask",
        lambda self, request, done, timeout=None: (
            asked.append(timeout) or real_ask(self, request, done, timeout)
        ),
    )
    with pytest.raises(ResolutionUnknown, match="in time"):
        backend.print_label(image, "b.invalid", PrintOptions())
    assert asked[-1] == zpl.FIRST_ANSWER_TIMEOUT
    backend.print_label(image, "a.invalid", PrintOptions(settings={"dpi": "203"}))
    assert None in asked  # status questions: the usual timeout


def test_whole_ratios():
    assert [raw.whole_ratio(f) for f in (2.0, 0.5, 3.0, 1.5, 304.8 / 203.2)] == [
        True,
        False,
        True,
        False,
        False,
    ]


def test_zpl_print_uses_settings(monkeypatch):
    backend = zpl.Zpl()
    printer = FakePrinter({b"~HI\n": ZPL_HI, b"~HS\n": ZPL_HS, b"~HQES\n": ZPL_HQES})
    install(monkeypatch, printer)
    options = PrintOptions(
        copies=2,
        settings={"media": "continuous", "finish": "cut", "darkness": 55, "speed": 99, "junk": 1},
    )
    backend.print_label(Image.new("RGB", (16, 8), "white"), "zebra.invalid", options)
    job = printer.jobs[0].decode()
    # speed clamped to 12 in/s
    assert "~SD16" in job and "^PR12,12,12" in job and "^MMC,Y" in job and "^MNN" in job
    assert "^PQ2,0,1,N" in job


def test_tspl_query(monkeypatch):
    backend = tspl.Tspl()
    printer = FakePrinter({ESC + b"!?": b"\x00", b"~!T\r\n": b"TE200\r"})
    install(monkeypatch, printer)
    info = backend.query("tspl.invalid")
    assert (info.model, info.state, info.error) == ("TE200", "IDLE", "")
    assert backend.display_name(info) == "TE200"
    printer.answers[ESC + b"!?"] = b"\x20"
    assert backend.query("tspl.invalid").state == "BUSY"
    printer.answers[ESC + b"!?"] = b"\x01"
    assert backend.query("tspl.invalid").error == "head_open"
    # a compatible printer that answers nothing: unknown, and not asked for its model
    printer.answers = {}
    info = backend.query("tspl.invalid")
    assert (info.model, info.state) == ("", "UNKNOWN")
    assert backend.display_name(info) == backend.name


def test_escpos_query(monkeypatch):
    backend = escpos.EscPos()
    printer = FakePrinter({b"\x10\x04\x02": b"\x12", b"\x10\x04\x04": b"\x12"})
    install(monkeypatch, printer)
    assert backend.query("pos.invalid").state == "IDLE"
    printer.answers[b"\x10\x04\x02"] = b"\x16"
    assert backend.query("pos.invalid").error == "cover_open"
    printer.answers = {}
    assert backend.query("pos.invalid").state == "UNKNOWN"


@pytest.mark.parametrize("backend_id, asked", [("epl", []), ("dymo", [b"\x1bA"])])
def test_silent_printers(monkeypatch, backend_id, asked):
    backend = type(get_backend(backend_id))()
    printer = FakePrinter()
    install(monkeypatch, printer)
    info = backend.query("printer.invalid")
    assert (info.state, info.error, info.model) == ("UNKNOWN", "", "")
    assert printer.jobs == [] and printer.requests == asked
    printer.refuse = True
    with pytest.raises(PortClosed):
        backend.query("printer.invalid")


def test_usb_model_names_the_printer(monkeypatch):
    install(monkeypatch, FakePrinter())
    info = dymo.Dymo().query("usb://DYMO/LabelWriter%20450?serial=1")
    assert info.model == "LabelWriter 450"
    assert raw.usb_model("usb://[x") == ""


@pytest.mark.parametrize(
    "backend_id, problem, cls",
    [
        ("zpl", {b"~HS\n": ZPL_HS_PAPER_OUT}, OutOfLabels),
        ("tspl", {ESC + b"!?": b"\x04"}, OutOfLabels),
        ("escpos", {b"\x10\x04\x02": b"\x16"}, CoverOpen),
    ],
)
def test_a_problem_stops_the_job_before_it_is_sent(monkeypatch, backend_id, problem, cls):
    printer = FakePrinter(problem)
    install(monkeypatch, printer)
    backend = get_backend(backend_id)
    image = backend.prepare(_sample_label(backend), PrintOptions())
    with pytest.raises(cls):
        backend.print_label(image, "printer.invalid", PrintOptions())
    assert printer.jobs == []


def test_print_renders_at_the_chosen_resolution(monkeypatch):
    backend = epl.Epl()
    printer = FakePrinter()
    install(monkeypatch, printer)
    options = PrintOptions(settings={"dpi": "300", "media": "gap"}, gap_mm=2.0)
    label = _sample_label(backend)
    image = backend.prepare(label, options)
    assert image.size == (1200, 600)  # 100 × 50 mm at 12 dots per mm
    assert image.info[raw.DPI_INFO] == raw.DPI["300"]
    backend.print_label(image, "epl.invalid", options)
    assert b"\nq1200\nQ600,24\n" in printer.jobs[0]  # the gap: 2 mm = 24 dots
    # an image without the resolution (not from prepare) uses the setting
    image.info.clear()
    backend.print_label(image, "epl.invalid", options)
    assert b"\nq1200\nQ600,24\n" in printer.jobs[1]
    # at 203 dpi the same image would be 150 mm wide: refused, not sent
    with pytest.raises(LabelTooWide):
        backend.print_label(image, "epl.invalid", PrintOptions())
    assert len(printer.jobs) == 2


def test_content_along_the_roll_is_turned():
    backend = tspl.Tspl()
    label = _sample_label(backend)
    label.set_orientation("along")
    assert backend.prepare(label, PrintOptions()).size == (800, 400)


def _sample_label(backend) -> Label:
    label = Label(printer=backend.id, auto_length=False, orientation="across", elements=[])
    if backend.capabilities.continuous:
        label.tape_mm = backend.capabilities.tape_widths_mm[0]
    else:
        label.set_media_size(100, 50)
    return label


# --- the contract ---------------------------------------------------------------------

FAIL = {
    "zpl": {b"~HS\n": ZPL_HS_PAPER_OUT},
    "tspl": {ESC + b"!?": b"\x04"},
    "escpos": {b"\x10\x04\x04": b"\x72"},
}
READY = {
    "zpl": {b"~HI\n": ZPL_HI, b"~HS\n": ZPL_HS, b"~HQES\n": ZPL_HQES},
    "tspl": {ESC + b"!?": b"\x00"},
    "escpos": {b"\x10\x04\x02": b"\x12", b"\x10\x04\x04": b"\x12"},
}


class RawHarness:
    """The harness of BackendContract for the raw languages."""

    backend_id = ""

    @pytest.fixture
    def harness(self, monkeypatch):
        printer = FakePrinter(READY.get(self.backend_id))
        install(monkeypatch, printer)
        backend = type(get_backend(self.backend_id))()

        def fail():
            if self.backend_id in FAIL:
                printer.answers.update(FAIL[self.backend_id])
            else:  # no status to report a problem: switched off
                printer.refuse = True

        count = COUNTERS[self.backend_id]
        return SimpleNamespace(
            backend=backend, fail=fail, printed=lambda: sum(count(j) for j in printer.jobs)
        )


class TestZpl(RawHarness, BackendContract):
    backend_id = "zpl"


class TestEpl(RawHarness, BackendContract):
    backend_id = "epl"


class TestTspl(RawHarness, BackendContract):
    backend_id = "tspl"


class TestEscPos(RawHarness, BackendContract):
    backend_id = "escpos"


class TestDymo(RawHarness, BackendContract):
    backend_id = "dymo"


def test_states_without_status_show_as_unknown():
    assert raw.state(False, None) == "UNKNOWN"
    assert raw.state(True, PrinterPaused()) == "PAUSED"
    assert raw.state(True, OutOfLabels()) == "ERROR"
    assert raw.state(True, None, busy=True) == "BUSY"
    assert raw.state(True, None) == "IDLE"
    assert isinstance(PrinterInfo("", None, None, None, raw.state(False, None)), PrinterInfo)


@pytest.mark.parametrize("backend_id", ["zpl", "epl", "escpos", "dymo"])
def test_upside_down_turns_the_image(monkeypatch, backend_id):
    printer = FakePrinter()
    install(monkeypatch, printer)
    backend = type(get_backend(backend_id))()
    image = Image.new("RGB", (64, 32), "white")
    image.putpixel((0, 0), (0, 0, 0))
    settings = {"upside_down": True, "dpi": "203"}
    jobs = []
    for options in (PrintOptions(settings={"dpi": "203"}), PrintOptions(settings=settings)):
        expected = backend.build_job(
            image.rotate(180) if options.settings.get("upside_down") else image,
            backend.dpi(options),
            options,
            "printer.invalid",
        )
        backend.print_label(image, "printer.invalid", options)
        assert printer.jobs[-1] == expected
        jobs.append(expected)
    assert jobs[0] != jobs[1]


def test_tspl_turns_by_command(monkeypatch):
    printer = FakePrinter()
    install(monkeypatch, printer)
    image = Image.new("RGB", (64, 32), "white")
    tspl.Tspl().print_label(image, "x", PrintOptions(settings={"upside_down": True}))
    assert b"DIRECTION 1,0\n" in printer.jobs[0]


def test_languages_without_a_resolution_question():
    assert raw.RawBackend.printer_dpi(epl.Epl(), raw.Session(None)) is None


class _Changing(dict):
    """The resolution a printer reported, changing after the first look (a
    query answering while a label is prepared)."""

    def __init__(self, first, then):
        super().__init__()
        self.answers = [first, then]

    def get(self, key, default=None):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.mark.parametrize("backend_id", ["zpl", "escpos"])
def test_prepare_reads_the_resolution_once(backend_id, monkeypatch):
    """The resolution the image says it was made for is the one it was
    rendered at, also when the printer reports another one meanwhile."""
    backend = get_backend(backend_id)
    monkeypatch.setattr(backend, "reported", _Changing(8 * 25.4, 12 * 25.4))
    options = PrintOptions(settings={"dpi": "auto"}, host="printer.local")
    if backend_id == "zpl":
        label = Label(printer="zpl", auto_length=False, orientation="across")
        label.set_media_size(50, 25)
        image = backend.prepare(label, options)
        assert image.info[raw.DPI_INFO] == 8 * 25.4
        assert image.width == round(50 / 25.4 * 8 * 25.4)  # 400 dots: 203 dpi
    else:
        label = Label(printer="escpos", tape_mm=80)
        image = backend.prepare(label, options)
        assert image.info[raw.DPI_INFO] == 8 * 25.4
        assert image.height == escpos.head_dots(80, 8 * 25.4)
