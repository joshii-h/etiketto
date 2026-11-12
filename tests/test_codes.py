"""Symbologies: what they encode (read back with a decoder), drawn in whole
printer dots with their quiet zones, and what they refuse."""

import sys

import pytest
from conftest import ink_bbox
from PIL import Image

from etiketto import codes
from etiketto import render as render_module
from etiketto.model import BARCODES, SYMBOLOGIES, CodeElement, Label, LabelError
from etiketto.render import RenderWarning, mm_to_px, render

# symbology: (content, what a scanner reads)
SAMPLES = {
    "qr": ("https://example.org", "https://example.org"),
    "code128": ("ABC-123", "ABC-123"),
    "ean13": ("400638133393", "4006381333931"),
    "code39": ("ABC-123 $/+%", "ABC-123 $/+%"),
    "code93": ("Abc-123!", "Abc-123!"),
    "ean8": ("9638507", "96385074"),
    "upca": ("036000291452", "0036000291452"),  # read as the EAN-13 it is part of
    "upce": ("0123456", "0012345000065"),  # read as the UPC-A it stands for
    "itf14": ("1540014128876", "15400141288763"),
    "gs1_128": ("(01)09501101530003(17)260101(10)AB12", "(01)09501101530003(17)260101(10)AB12"),
    "datamatrix": ("Grüße aus Zürich 123", "Grüße aus Zürich 123"),
    "gs1_datamatrix": ("(01)09501101530003(21)XYZ", "(01)09501101530003(21)XYZ"),
    "pdf417": ("Etiketto, 8000 Zürich, PDF417", "Etiketto, 8000 Zürich, PDF417"),
    "aztec": ("Ticket 4711 日本", "Ticket 4711 日本"),
}
# GS1 symbol identifiers: FNC1 in first position, as the standard asks
GS1_IDS = {"gs1_128": "]C1", "gs1_datamatrix": "]d2"}
DPIS = (203, 300, 600)


def test_every_symbology_has_a_sample_and_a_spec():
    assert set(SAMPLES) == set(SYMBOLOGIES) == set(codes.SPECS)


def _page(part, border=20):
    """The code in black on white, with some extra white around it."""
    page = Image.new("L", (part.width + 2 * border, part.height + 2 * border), 255)
    page.paste(0, (border, border), part)
    return page


@pytest.mark.parametrize("dpi", DPIS)
@pytest.mark.parametrize("symbology", SAMPLES)
def test_codes_decode(symbology, dpi):
    zxingcpp = pytest.importorskip("zxingcpp")
    data, expected = SAMPLES[symbology]
    part = render_module.render_code(CodeElement(data, symbology=symbology), mm_to_px(15, dpi))
    found = zxingcpp.read_barcodes(_page(part))
    assert [result.text for result in found] == [expected]
    if symbology in GS1_IDS:
        assert found[0].symbology_identifier == GS1_IDS[symbology]


def test_ean13_is_the_same_from_zint():
    """EAN-13 stays with python-barcode; Zint would give the same modules
    (Code 128 and QR codes it would encode differently: other code sets,
    other masks, so labels made before would print other modules)."""
    zint = pytest.importorskip("zint")
    for data in ("400638133393", "978020137962", "000000000000"):
        ours = codes.encode(CodeElement(data, symbology="ean13"))
        theirs = codes._zint_modules(codes._zint_symbol(zint, "ean8", data), "ean13", data)
        assert ours.rows == theirs.rows
        assert ours.text == theirs.text


# --- whole dots and quiet zones ----------------------------------------------


def _expected_start(box, modules, quiet, module):
    total = modules + sum(quiet)
    if module * total <= box:
        return (box - module * total) // 2 + quiet[0] * module
    return max(0, (box - module * modules) // 2)


@pytest.mark.parametrize("dpi", DPIS)
@pytest.mark.parametrize("symbology", ["qr", "datamatrix", "gs1_datamatrix", "pdf417", "aztec"])
def test_2d_codes_are_whole_dots_inside_their_quiet_zone(symbology, dpi):
    element = CodeElement(SAMPLES[symbology][0], symbology=symbology)
    height = mm_to_px(15, dpi)
    part = render_module.render_code(element, height)
    symbol, spec = codes.encode(element), codes.SPECS[symbology]
    module, quiet = part.code_module, (spec.quiet[0],) * 2
    row_px = spec.row_modules * module
    tall, wide = len(symbol.rows) * spec.row_modules, symbol.width
    assert part.height == height
    assert module >= 2 and render_module.code_problems(part) == ()
    x0 = _expected_start(part.width, wide, quiet, module)
    y0 = _expected_start(height, tall, quiet, module)
    # every module exactly module x module dots (PDF417 rows: 3 modules high)
    modules = Image.frombytes(
        "L", (wide, len(symbol.rows)), bytes(255 * int(bit) for bit in "".join(symbol.rows))
    )
    expected = Image.new("L", part.size, 0)
    expected.paste(
        modules.resize((wide * module, len(symbol.rows) * row_px), Image.Resampling.NEAREST),
        (x0, y0),
    )
    assert part.getchannel("A").tobytes() == expected.tobytes()
    # the quiet zone stays blank, and lies inside the element
    assert x0 >= spec.quiet[0] * module and y0 >= spec.quiet[0] * module
    assert part.width - (x0 + wide * module) >= spec.quiet[0] * module
    assert height - (y0 + tall * module) >= spec.quiet[0] * module


LINEAR = ["code128", "code39", "code93", "ean13", "ean8", "upca", "upce", "itf14", "gs1_128"]


@pytest.mark.parametrize("dpi", DPIS)
@pytest.mark.parametrize("symbology", LINEAR)
def test_barcodes_are_whole_dots_with_their_quiet_zones(symbology, dpi):
    element = CodeElement(SAMPLES[symbology][0], symbology=symbology)
    height = mm_to_px(15, dpi)
    part = render_module.render_code(element, height)
    page = _page(part, 0)
    assert set(page.tobytes()) == {0, 255}  # 1 bit: nothing a printer has to round
    spec = codes.SPECS[symbology]
    pattern, quiet = render_module._linear_pattern(codes.encode(element), spec)
    y = round(height * 6 / render_module.BAR_TOTAL_MM)  # through the bars
    row = page.crop((0, y, page.width, y + 1)).tobytes()
    module, first = part.code_module, row.index(0)
    assert module >= 2 and render_module.code_problems(part) == ()
    drawn = row[first : first + len(pattern) * module]
    assert drawn == bytes(0 if pattern[i // module] == "1" else 255 for i in range(len(drawn)))
    assert set(row[:first]) == {255} and set(row[first + len(drawn) :]) == {255}
    # the quiet zones the standard asks for (ITF-14: inside its bearer bars)
    assert first >= quiet[0] * module
    assert page.width - first - len(drawn) >= quiet[1] * module


@pytest.mark.parametrize(
    "symbology, quiet",
    [
        ("code39", (10, 10)),
        ("code93", (10, 10)),
        ("gs1_128", (10, 10)),
        ("ean8", (7, 7)),
        ("upca", (9, 9)),
        ("upce", (9, 7)),
        ("itf14", (10, 10)),
        ("datamatrix", (1, 1)),
        ("pdf417", (2, 2)),
        ("aztec", (0, 0)),
    ],
)
def test_quiet_zones_as_the_standards_ask(symbology, quiet):
    assert render_module.QUIET[symbology] == quiet


def test_itf14_has_bearer_bars_around_its_quiet_zones():
    element = CodeElement("1540014128876", symbology="itf14")
    height = 326  # 20 pixels per designed mm
    page = _page(render_module.render_code(element, height), 0)
    module = 6  # 0.3 mm
    top, bars = 20, 200
    x0, y0, x1, _y1 = ink_bbox(page.crop((0, 0, page.width, top + bars)), 255)
    assert y0 == top
    bearer = codes.ITF14_BEARER * module
    for y in (top, top + bearer - 1, top + bars - bearer, top + bars - 1):  # above and below
        assert set(page.crop((x0, y, x1, y + 1)).tobytes()) == {0}
    middle = top + bars // 2
    row = page.crop((x0, middle, x1, middle + 1)).tobytes()
    assert set(row[:bearer]) == {0}  # left and right
    assert set(row[bearer : bearer + 10 * module]) == {255}  # then the quiet zone
    assert row[bearer + 10 * module] == 0  # then the bars
    assert set(row[-bearer:]) == {0} and set(row[-bearer - 10 * module : -bearer]) == {255}
    assert ink_bbox(page.crop((0, top + bars, page.width, page.height)), 255)  # the digits


def test_pdf417_rows_are_three_modules_high_and_it_is_about_twice_as_wide():
    element = CodeElement("x" * 200, symbology="pdf417")
    symbol = codes.encode(element)
    tall = len(symbol.rows) * 3 + 4
    assert 1.5 < (symbol.width + 4) / tall < 2.5
    part = render_module.render_code(element, 400)
    assert part.height == 400 and 1.5 < part.width / part.height < 2.5


@pytest.mark.parametrize(
    "symbology, data, digits",
    [
        ("upca", "036000291452", ["0", "36000", "29145", "2"]),
        ("upce", "0123456", ["0", "123456", "5"]),
        ("ean8", "9638507", ["9638", "5074"]),
    ],
)
def test_ean_and_upc_digits_as_gs1_places_them(symbology, data, digits):
    part = render_module.render_code(CodeElement(data, symbology=symbology), 300)
    page = _page(part, 0)
    symbol = codes.encode(CodeElement(data, symbology=symbology))
    assert symbol.text == "".join(digits)
    module, scale = part.code_module, 300 / render_module.BAR_TOTAL_MM
    bottom = round(render_module.BAR_TOP_MM * scale) + round(render_module.BAR_HEIGHT_MM * scale)
    _runs, first, last = _bars_row(page, round(6 * scale))
    column = page.crop((first, 0, first + 1, page.height)).tobytes()
    assert len(column) - column[::-1].index(0) == bottom + 5 * module  # a guard bar: longer
    text = page.crop((0, bottom + 1, page.width, page.height))
    outside = digits[0] if len(digits) > 2 else None
    if outside:  # the first digit left of the bars, the last right of them
        assert ink_bbox(text.crop((0, 0, first, text.height)), 255)
        assert ink_bbox(text.crop((last, 0, text.width, text.height)), 255)
    else:
        assert not ink_bbox(text.crop((0, 0, first, text.height)), 255)


def _bars_row(page, y):
    row = page.crop((0, y, page.width, y + 1)).tobytes()
    first = row.index(0)
    return None, first, len(row) - row[::-1].index(0)


def test_gs1_128_text_has_its_ais_in_brackets():
    element = CodeElement("(01)09501101530003(17)260101", symbology="gs1_128")
    assert codes.encode(element).text == "(01)09501101530003(17)260101"
    assert codes.encode(CodeElement("ABC-1", symbology="code39")).text == "ABC-1"  # no *


def test_2d_codes_have_no_text():
    for symbology in ("datamatrix", "gs1_datamatrix", "pdf417", "aztec", "qr"):
        assert codes.encode(CodeElement(SAMPLES[symbology][0], symbology=symbology)).text == ""


def test_a_new_code_that_does_not_fit_warns(make_label):
    rendered = render(make_label(tape_mm=6, elements=[CodeElement("x" * 400, "datamatrix")]))
    assert RenderWarning("code_too_big", {"n": 1}) in rendered.warnings
    thin = render(make_label(tape_mm=12, elements=[CodeElement("A" * 30, "code39")]), dpi=203)
    assert RenderWarning("code_thin", {"n": 1}) in thin.warnings


def test_scaled_down_2d_code_keeps_whole_modules(make_label):
    code = CodeElement("x" * 60, symbology="pdf417")
    rendered = render(make_label(elements=[code], auto_length=False, length_mm=40), dpi=300)
    assert rendered.overflow
    assert set(rendered.image.convert("L").tobytes()) == {0, 255}


# --- what is refused ------------------------------------------------------------


@pytest.mark.parametrize(
    "symbology, data, key, params",
    [
        ("gs1_128", "01095011015300", "gs1_syntax", {}),
        ("gs1_128", "(01", "gs1_syntax", {}),
        ("gs1_128", "(0A)12", "gs1_syntax", {}),
        ("gs1_128", "(01)", "gs1_syntax", {}),
        ("gs1_128", "(10)Grüße", "gs1_charset", {}),
        ("gs1_128", "(01)09501101530004", "gs1_check", {"ai": "01"}),
        ("gs1_datamatrix", "(00)123456789012345678", "gs1_check", {"ai": "00"}),
        ("gs1_128", "(01)0950110153000", "gs1_length", {"ai": "01"}),
        ("gs1_datamatrix", "(9999)1", "gs1_ai", {"ai": "9999"}),
        ("gs1_128", "(17)261301", "gs1_value", {"ai": "17"}),  # month 13
        ("gs1_128", "(01)09501101530003(10)AB 12", "gs1_value", {"ai": "10"}),  # no spaces
        ("gs1_128", "(01)09501101530003(10)" + "A" * 20 + "(91)" + "B" * 90, "code_too_long", {}),
        ("ean8", "963850", "ean8_digits", {}),
        ("ean8", "96385075", "code_check", {}),
        ("upca", "0360002914", "upca_digits", {}),
        ("upca", "036000291453", "code_check", {}),
        ("upce", "2123456", "upce_digits", {}),
        ("upce", "012345", "upce_digits", {}),
        ("upce", "01234566", "code_check", {}),
        ("itf14", "123456789012", "itf14_digits", {}),
        ("itf14", "15400141288764", "code_check", {}),
        ("itf14", "１５４００１４１２８８７６", "itf14_digits", {}),  # full-width digits
        ("code39", "abc", "code39_charset", {}),  # Zint would make it ABC
        ("code39", "A*B", "code39_charset", {}),
        ("code93", "Grüße", "code93_charset", {}),
        ("code93", "tab\there", "code93_charset", {}),
        ("datamatrix", "x" * 5000, "code_too_long", {}),
        ("aztec", "x" * 5000, "code_too_long", {}),
        ("pdf417", "x" * 3000, "code_too_long", {}),
        ("aztec", "", "code_empty", {}),
    ],
)
def test_invalid_content_warns(make_label, symbology, data, key, params):
    rendered = render(make_label(elements=[CodeElement(data, symbology=symbology)]))
    assert rendered.warnings == [RenderWarning(key, {"n": 1, **params})]


@pytest.mark.parametrize(
    "symbology, data",
    [
        ("ean8", "96385074"),
        ("upca", "036000291452"),
        ("upca", "03600029145"),
        ("upce", "01234565"),
        ("itf14", "15400141288763"),
        ("gs1_128", "(00)123456789012345675"),
        ("gs1_128", "(17)260100"),  # day 00: the end of the month
        ("code93", " ~Az09!"),
    ],
)
def test_valid_content_is_accepted(symbology, data):
    assert codes.encode(CodeElement(data, symbology=symbology)).rows


def test_gs1_check_digit():
    assert codes.gs1_check_digit("400638133393") == 1
    assert codes.gs1_check_digit("9638507") == 4
    assert codes.gs1_check_digit("1540014128876") == 3


@pytest.mark.parametrize(
    "message, key, params",
    [
        ("Error 259: Invalid data length for AI (01)", "gs1_length", {"ai": "01"}),
        ("Error 999: something new", "code_invalid", {}),
        ("no number at all", "code_invalid", {}),
        ("Error 276: Invalid check digit '5', expecting '4'", "code_check", {}),
    ],
)
def test_zint_errors_become_warning_keys(message, key, params):
    symbology = "gs1_128" if "AI" in message else "ean8"
    error = codes._zint_error(message, symbology)
    assert (error.key, error.params) == (key, params)


def test_without_zint_the_new_types_warn_and_the_first_ones_still_work(make_label, monkeypatch):
    monkeypatch.setitem(sys.modules, "zint", None)  # import zint raises ImportError
    codes._encode.cache_clear()
    try:
        elements = [CodeElement("Hi", "datamatrix"), CodeElement("x"), CodeElement("A", "code128")]
        rendered = render(make_label(elements=elements))
    finally:
        codes._encode.cache_clear()
    assert rendered.warnings == [RenderWarning("code_unavailable", {"n": 1})]


# --- files ------------------------------------------------------------------------


def test_new_types_are_saved_as_a_feature(tmp_path):
    import json

    label = Label(elements=[CodeElement(SAMPLES[s][0], symbology=s) for s in SYMBOLOGIES])
    path = tmp_path / "codes.etiketto"
    label.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["version"], data["features"]) == (2, ["barcodes"])
    assert Label.load(path) == label
    for symbology in BARCODES:  # each one alone makes the file need the feature
        assert Label(elements=[CodeElement("1", symbology)]).features() == ["barcodes"]
    assert Label(elements=[CodeElement("1", "ean13")]).to_dict()["version"] == 1


def test_older_versions_refuse_new_types(monkeypatch):
    data = Label(elements=[CodeElement("Hi", "aztec")]).to_dict()
    # a version 1 reader: refuses format 2 as written by a newer Etiketto
    monkeypatch.setattr("etiketto.model.FORMAT_VERSION", 1)
    with pytest.raises(LabelError) as info:
        Label.from_dict(data)
    assert info.value.code == "newer"


@pytest.mark.parametrize(
    "extra, code",
    [
        ({"features": ["barcodes", "holograms"]}, "newer"),
        ({"features": "barcodes"}, "damaged"),
        ({"features": [5]}, "damaged"),
    ],
)
def test_features_are_checked(extra, code):
    with pytest.raises(LabelError) as info:
        Label.from_dict({"version": 2, **extra})
    assert info.value.code == code


# --- text beyond ASCII, readings, odd input --------------------------------------

# Latin-1 text a label may well carry: without an ECI, scanners guess at its
# bytes and read "37°" as katakana; with ECI 26 (UTF-8) all of it comes back
LATIN = [
    "Größe: 5½ kg",
    "Café ¡Olé!",
    "Müller & Söhne AG, Zürich",
    "Señor Nuñez",
    "Ærøskøbing",
    "Preis: £5 · 20°C",
    "©2026 Firma»",
    "Élève à l'école «Bât. É»",
    "Zürich",
    "Füsse",
    "Gerät Nr. 12 – Lager",
    "Ø 12 mm ±0,5",
    "Strasse 5, 8000 Zürich",
    "Küche",
    "é",
    "ü",
    "Jörg Müller",
    "naïve façade",
    "Temp 37°",
    "µF 100",
    "Inventar-Nr. 0815 Büro 3.OG",
    "Öl",
    "Rüebli",
    "CHF 5.– für Äpfel",
    "Björn Ångström",
]


@pytest.mark.parametrize("symbology", ["datamatrix", "pdf417", "aztec"])
def test_latin_text_reads_back_exactly(symbology):
    zxingcpp = pytest.importorskip("zxingcpp")
    for text in [*LATIN, "ÿ\x80", "caf\x85"]:  # C1 controls too
        part = render_module.render_code(CodeElement(text, symbology=symbology), 500)
        assert [r.text for r in zxingcpp.read_barcodes(_page(part))] == [text], text


def test_code39_warns_about_full_ascii_pairs(make_label):
    rendered = render(make_label(elements=[CodeElement("PART+A1", "code39")]), dpi=600)
    assert rendered.warnings == [RenderWarning("code39_full_ascii", {"n": 1})]
    plain = render(make_label(elements=[CodeElement("PART+1 $/", "code39")]), dpi=600)
    assert plain.warnings == []


@pytest.mark.parametrize("data", ["1631706", "1811004", "0421203"])
def test_upce_digits_without_a_upca_number(data):
    with pytest.raises(codes.CodeError) as info:
        codes.encode(CodeElement(data, "upce"))
    assert info.value.key == "upce_form"


def test_gs1_values_with_round_brackets_in_square_bracket_syntax():
    zxingcpp = pytest.importorskip("zxingcpp")
    data = "[01]09501101530003[21]A(B)C"
    for symbology in ("gs1_128", "gs1_datamatrix"):
        part = render_module.render_code(CodeElement(data, symbology), 300)
        found = zxingcpp.read_barcodes(_page(part))
        assert [r.text for r in found] == ["(01)09501101530003(21)A(B)C"]
        assert found[0].symbology_identifier == GS1_IDS[symbology]
    assert codes.encode(CodeElement(data, "gs1_128")).text == "(01)09501101530003(21)A(B)C"
    with pytest.raises(codes.CodeError) as info:
        codes.encode(CodeElement("(21)A(B)C", "gs1_128"))
    assert info.value.key == "gs1_syntax"


def test_a_qr_code_too_long_warns():
    with pytest.raises(codes.CodeError) as info:
        codes.encode(CodeElement("x" * 8000))
    assert info.value.key == "code_too_long"


@pytest.mark.parametrize("symbology", SYMBOLOGIES)
def test_lone_surrogates_are_refused(make_label, symbology):
    rendered = render(make_label(elements=[CodeElement("a\ud800", symbology)]))
    assert rendered.warnings == [RenderWarning("code_invalid", {"n": 1})]


def test_text_wider_than_its_room_is_made_smaller():
    ink = render_module._text_in("WWWWWWWW", 100, 50)
    assert ink is not None and ink.width <= 50


def test_ean_bars_without_their_digits(monkeypatch):
    monkeypatch.setattr(render_module, "_ink", lambda *args: None)
    part = render_module.render_code(CodeElement("400638133393", "ean13"), 300)
    assert part.code_module >= 2 and ink_bbox(_page(part, 0), 255)
