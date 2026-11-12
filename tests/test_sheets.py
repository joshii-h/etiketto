"""Label sheets: the template database and its converter."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from tavletta import sheets
from tavletta.sheets import Sheet, find, search

ROOT = Path(__file__).resolve().parent.parent


def _sheet(**kwargs) -> Sheet:
    values = {
        "brand": "Test",
        "part": "1",
        "description": "",
        "paper": (210.0, 297.0),
        "shape": "rectangle",
        "width": 70.0,
        "height": 37.0,
        "layouts": ((3, 8, 0.0, 0.5, 70.0, 37.0),),
        "paper_name": "A4",
    }
    values.update(kwargs)
    return Sheet(**values)


# --- the database ----------------------------------------------------------------------


def test_the_database_has_the_common_sheets():
    l7160 = find("Avery L7160")
    assert l7160 and l7160.count == 21 and l7160.paper == (210, 297)
    assert l7160.paper_name == "A4" and l7160.shape == "rectangle" and l7160.corner > 0
    assert find("Zweckform 3475").count == 24
    assert find("Avery 5160").paper_name == "US Letter"
    assert find("No such sheet") is None


def test_every_sheet_is_usable():
    ids = [sheet.id for sheet in sheets.sheets()]
    assert len(ids) == len(set(ids)) > 1700
    for sheet in sheets.sheets():
        assert sheet.shape in sheets.SHAPES
        assert min(sheet.width, sheet.height) >= 5  # what a label file may have
        assert sheet.count >= 1
        for index in range(sheet.count):
            left, top, right, bottom = sheet.rect(index)
            assert left >= -1 and top >= -1, sheet.id
            assert right <= sheet.paper[0] + 1 and bottom <= sheet.paper[1] + 1, sheet.id


def test_the_license_comes_with_the_data():
    text = (ROOT / "tavletta" / "sheets" / "LICENSE.glabels").read_text()
    assert "Jaye Evins" in text and "Permission is hereby granted" in text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Avery L7160", "Avery L7160"),
        ("avery l7160", "Avery L7160"),
        ("Zweckform 3475", "Zweckform 3475"),
        ("zweckform3475", "Zweckform 3475"),
        ("Avery L 7160", "Avery L7160"),
    ],
)
def test_search_by_brand_and_part(text, expected):
    assert expected in [sheet.id for sheet in search(text)]


@pytest.mark.parametrize("size", ["70x37", "70 x 37", "70×37", "37x70", "70,05x36"])
def test_search_by_size_either_way_round(size):
    found = search(size)
    assert "Zweckform 3475" in [sheet.id for sheet in found]
    wanted = sorted(float(v.replace(",", ".")) for v in re.split(r"\s*[x×]\s*", size))
    for sheet in found:  # within a millimetre, also turned
        got = sorted((sheet.width, sheet.height))
        assert all(abs(a - b) <= 1 for a, b in zip(got, wanted, strict=True)), sheet.id


def test_search_by_size_and_brand_and_paper():
    found = search("herma 70x37", "A4")
    assert found and all(s.brand == "Herma" and s.paper_name == "A4" for s in found)
    assert all(s.paper_name == "US Letter" for s in search("", "US Letter"))
    other = search("", "other")
    assert other and all(s.paper_name not in ("A4", "US Letter") for s in other)
    assert len(search("")) == len(sheets.sheets())
    assert search("nothing like this") == []


def test_positions_row_by_row_over_several_layouts():
    sheet = _sheet(
        width=20.0,
        height=10.0,
        layouts=(
            (2, 2, 10.0, 10.0, 60.0, 20.0),  # columns 1 and 3
            (1, 2, 40.0, 10.04, 0.0, 20.0),  # column 2, a hair lower: same rows
        ),
    )
    assert sheet.count == 6
    assert sheet.positions() == [
        (10.0, 10.0),
        (40.0, 10.04),
        (70.0, 10.0),
        (10.0, 30.0),
        (40.0, 30.04),
        (70.0, 30.0),
    ]
    assert sheet.rect(1) == (40.0, 10.04, 60.0, 20.04)
    assert sheet.position_at(45, 15) == 1 and sheet.position_at(5, 5) is None


def test_a_spot_two_layouts_share_counts_once():
    sheet = _sheet(layouts=((2, 1, 0.0, 0.0, 70.0, 0.0), (1, 1, 0.0, 0.0, 0.0, 0.0)))
    assert sheet.count == 2


def test_cd_disc():
    cd = _sheet(shape="cd", width=60.0, height=118.0, hole=41.0)
    assert cd.disc == 118.0
    assert _sheet(shape="cd", width=117.0, height=117.0, diameter=118.0).disc == 118.0
    assert _sheet().disc == 0


# --- the converter -------------------------------------------------------------------


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location(
        "glabels_templates", ROOT / "tools" / "glabels_templates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAPERS = """<?xml version="1.0"?>
<Glabels-paper-sizes>
  <Paper-size id="A4" name="A4" width="210mm" height="297mm"/>
  <Paper-size id="US-Letter" name="US Letter" width="8.5in" height="11in"/>
</Glabels-paper-sizes>
"""

TEMPLATES = """<?xml version="1.0"?>
<Glabels-templates>
  <Template brand="Acme" part="10" size="A4" _description="Address labels">
    <Meta category="label"/>
    <Label-rectangle id="0" width="70mm" height="37mm" round="2mm" x_waste="0" y_waste="0">
      <Markup-margin size="1mm"/>
      <Layout nx="3" ny="8" x0="0" y0="0.5mm" dx="70mm" dy="37mm"/>
    </Label-rectangle>
  </Template>
  <Template brand="Acme" part="11" equiv="10"/>
  <Template brand="Acme" part="12" equiv="404"/>
  <Template brand="Acme" part="2" size="US-Letter" description="Round">
    <Label-round id="0" radius="0.75in" waste="0">
      <Layout nx="4" ny="5" x0="0.5in" y0="0.5in" dx="2in" dy="2in"/>
      <Layout nx="1" ny="1" x0="4in" y0="9in" dx="0" dy="0"/>
    </Label-round>
  </Template>
  <Template brand="Acme" part="CD" size="other" width="120mm" height="240mm">
    <Label-cd id="0" radius="58.5mm" width="100mm" hole="20.5mm">
      <Layout nx="1" ny="2" x0="10mm" y0="0" dx="0" dy="120mm"/>
    </Label-cd>
  </Template>
  <Template brand="Acme" part="Oval" size="A4">
    <Label-ellipse id="0" width="50mm" height="30mm">
      <Layout nx="1" ny="1" x0="10mm" y0="10mm" dx="0" dy="0"/>
    </Label-ellipse>
  </Template>
  <Template brand="Acme" part="Odd" size="A4">
    <Label-rectangle id="0" width="20mm" height="10mm" round="in">
      <Layout nx="1" ny="1" x0="72" y0="72" dx="0" dy="0"/>
    </Label-rectangle>
  </Template>
  <Template brand="Acme" part="Roll" size="roll">
    <Label-rectangle id="0" width="50mm" height="20mm">
      <Layout nx="1" ny="1" x0="0" y0="0" dx="0" dy="0"/>
    </Label-rectangle>
  </Template>
  <Template brand="Acme" part="Typo" size="A4">
    <Label-rectangle id="0" width="64mm" height="21mm">
      <Layout nx="3" ny="13" x0="9mm" y0="12mm" dx="64mm" dy="210mm"/>
    </Label-rectangle>
  </Template>
  <Template brand="Acme" part="Tiny" size="A4">
    <Label-rectangle id="0" width="3mm" height="20mm">
      <Layout nx="1" ny="1" x0="0" y0="0" dx="0" dy="0"/>
    </Label-rectangle>
  </Template>
  <Template brand="Acme" part="Two" size="A4">
    <Label-rectangle id="0" width="20mm" height="10mm">
      <Layout nx="1" ny="1" x0="0" y0="0" dx="0" dy="0"/>
    </Label-rectangle>
    <Label-round id="1" radius="5mm">
      <Layout nx="1" ny="1" x0="50mm" y0="0" dx="0" dy="0"/>
    </Label-round>
  </Template>
  <Template brand="Acme" part="Path" size="A4">
    <Label-path id="0" d_units="mm" d="M 0 0 l 10 0 l 0 10 Z">
      <Layout nx="1" ny="1" x0="0" y0="0" dx="0" dy="0"/>
    </Label-path>
  </Template>
</Glabels-templates>
"""


@pytest.fixture
def glabels(tmp_path):
    folder = tmp_path / "templates"
    folder.mkdir()
    (folder / "paper-sizes.xml").write_text(PAPERS)
    (folder / "acme-templates.xml").write_text(TEMPLATES)
    (folder / "LICENSE").write_text("MIT, Copyright (C) 2001-2026 Jaye Evins\n")
    return folder


def test_converter_lengths(converter):
    assert converter.mm("210mm") == 210
    assert converter.mm("8.5in") == 215.9
    assert converter.mm("72") == converter.mm("72pt") == 25.4
    assert converter.mm("1cm") == 10 and converter.mm("6pc") == 25.4
    assert converter.mm("3.93386e-16pt") == 0 and converter.mm("1E1mm") == 10
    assert converter.mm(".5in") == 12.7
    with pytest.raises(ValueError):
        converter.mm("in")


def test_converter_keeps_sheets_and_resolves_aliases(converter, glabels):
    entries = {e["part"]: e for e in converter.templates(glabels)}
    # rolls, typing errors, too small labels, two kinds of label and free
    # forms are left out; the alias of an unknown part too
    assert [e["part"] for e in converter.templates(glabels)] == [
        "2",
        "10",
        "11",
        "CD",
        "Odd",
        "Oval",
    ]  # natural order
    a4 = entries["10"]
    assert a4["paper"] == [210, 297] and a4["paper_name"] == "A4"
    assert a4["corner"] == 2 and a4["layouts"] == [[3, 8, 0, 0.5, 70, 37]]
    assert a4["description"] == "Address labels" and a4["categories"] == ["label"]
    assert entries["11"] == dict(a4, part="11")
    round_ = entries["2"]
    assert round_["shape"] == "round" and round_["width"] == round_["height"] == 38.1
    assert round_["paper_name"] == "US Letter" and len(round_["layouts"]) == 2
    cd = entries["CD"]
    assert cd["paper"] == [120, 240] and cd["paper_name"] == ""
    # the disc as high as the label, cut off at the sides
    assert (cd["width"], cd["height"], cd["hole"]) == (100, 117, 41) and "diameter" not in cd
    assert "corner" not in entries["Odd"]  # round="in": no radius
    assert entries["Oval"]["shape"] == "ellipse"


NAMESPACED = """<?xml version="1.0"?>
<Glabels-templates xmlns="http://glabels.org/xmlns/3.0/">
  <Template brand="Beta" part="N1" size="A4" _description="In a namespace">
    <Meta category="label"/>
    <Label-rectangle id="0" width="105mm" height="3.70e1mm" round="0">
      <Layout nx="2" ny="8" x0="0" y0="7.86772e-17pt" dx="105mm" dy="37mm"/>
    </Label-rectangle>
  </Template>
  <Template brand="Beta" part="N2" equiv="N1"/>
</Glabels-templates>
"""


def test_converter_reads_files_in_the_glabels_namespace(converter, glabels):
    (glabels / "beta-templates.xml").write_text(NAMESPACED)
    report = {}
    entries = {e["part"]: e for e in converter.templates(glabels, report)}
    assert entries["N1"]["layouts"] == [[2, 8, 0, 0, 105, 37]]
    assert entries["N1"]["height"] == 37 and entries["N2"]["brand"] == "Beta"
    assert report == {"acme-templates.xml": (12, 5), "beta-templates.xml": (2, 1)}


def test_converter_fails_on_a_file_without_templates(converter, glabels):
    (glabels / "empty-templates.xml").write_text(
        '<?xml version="1.0"?><Glabels-templates xmlns="urn:other"/>'
    )
    with pytest.raises(ValueError, match="empty-templates.xml: no templates"):
        converter.templates(glabels)


def test_converter_writes_the_data_and_the_license(converter, glabels, tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "OUT", tmp_path / "out")
    converter.main(glabels)
    data = json.loads((tmp_path / "out" / "templates.json").read_text())
    assert len(data) == 6
    assert "Jaye Evins" in (tmp_path / "out" / "LICENSE.glabels").read_text()
    loaded = [sheets._sheet(entry) for entry in data]
    cd = next(s for s in loaded if s.part == "CD")
    assert cd.disc == 117 and cd.count == 2


def test_sizes_in_a_search_are_numbers():
    found = search("Avery 63.5x38,1")
    assert found and all(s.brand == "Avery" for s in found)
    assert all(sheets._has_size(s, 63.5, 38.1) for s in found)  # "38,1" is 38.1 mm


@pytest.mark.parametrize("text", ["Igepa EA-381x212", "EA-381x212", "ea381x212", "381x212"])
def test_a_part_number_that_looks_like_a_size_is_found(text):
    assert "Igepa EA-381x212" in [sheet.id for sheet in search(text)]


def test_a_shape_this_version_does_not_draw_is_a_rectangle():
    import json

    entry = json.loads(sheets.DATA.read_text(encoding="utf-8"))[0]
    assert sheets._sheet({**entry, "shape": "star"}).shape == "rectangle"
    assert sheets._sheet(entry).shape in sheets.SHAPES
