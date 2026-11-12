import base64
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from etiketto import model
from etiketto.model import (
    FORMAT_VERSION,
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    SheetChoice,
    SymbolElement,
    TextElement,
)


def test_roundtrip(tmp_path, png_b64):
    label = Label(
        tape_mm=25,
        auto_length=False,
        length_mm=80,
        margin_mm=1.5,
        edge_mm=0.5,
        align="end",
        background="#ffeeaa",
        background_image=ImageElement(data=png_b64(), name="bg.png"),
        border_mm=0.4,
        border_color="#112233",
        elements=[
            TextElement(
                text="Hallo\nWelt",
                bold=False,
                italic=True,
                size_pt=14,
                color="#123456",
                align="right",
                font="Serif",
            ),
            ImageElement(data=png_b64(), name="x.png"),
            CodeElement(data="4006381333931", symbology="ean13", color="#ff0000"),
        ],
    )
    path = tmp_path / "a.etiketto"
    label.save(path)
    assert Label.load(path) == label
    assert [p.name for p in tmp_path.iterdir()] == ["a.etiketto"]  # no temporary file left
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and "features" not in data  # opens in every version
    assert [e["kind"] for e in data["elements"]] == ["text", "image", "code"]


def test_save_keeps_umlauts_readable(tmp_path):
    path = tmp_path / "u.etiketto"
    Label(elements=[TextElement(text="Grüße")]).save(path)
    assert "Grüße" in path.read_text(encoding="utf-8")


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "a.etiketto"
    path.write_text("old content")
    written = []

    def broken_replace(source, target):
        written.append(Path(source).read_text(encoding="utf-8"))
        raise OSError("disk full")

    monkeypatch.setattr(model.os, "replace", broken_replace)
    with pytest.raises(OSError):
        Label(tape_mm=12).save(path)
    assert path.read_text() == "old content"
    assert json.loads(written[0])["tape_mm"] == 12  # went to the temporary file
    assert [p.name for p in tmp_path.iterdir()] == ["a.etiketto"]  # and that is gone


def test_defaults_from_empty_dict():
    label = Label.from_dict({})
    assert label == Label()
    assert label.elements == [] and label.background_image is None


def test_ignores_unknown_fields():
    label = Label.from_dict(
        {
            "tape_mm": 12,
            "future_option": True,
            "elements": [{"kind": "text", "text": "a", "shadow": 3}],
        }
    )
    assert label.tape_mm == 12
    assert label.elements == [TextElement(text="a")]


def test_background_image_kind_is_forced():
    label = Label.from_dict({"background_image": {"data": "abc", "name": "b.png", "kind": "x"}})
    assert label.background_image == ImageElement(data="abc", name="b.png")
    assert Label.from_dict({"background_image": None}).background_image is None


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("length_mm", 1, 15.0),
        ("length_mm", 5000, 1000.0),
        ("length_mm", 60, 60.0),
        ("length_mm", 15, 15.0),
        ("length_mm", 1000, 1000.0),
        ("margin_mm", -3, 0.0),
        ("margin_mm", 99, 20.0),
        ("margin_mm", 2.5, 2.5),
        ("edge_mm", -1, 0.0),
        ("edge_mm", 11, 10.0),
        ("edge_mm", 10, 10.0),
        ("border_mm", -0.1, 0.0),
        ("border_mm", 5.5, 5.0),
        ("border_mm", 0.3, 0.3),
    ],
)
def test_label_numbers_clamped(field, value, expected):
    result = getattr(Label.from_dict({field: value}), field)
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize(
    "value, expected", [(-5, 0.0), (0, 0.0), (12, 12.0), (300, 300.0), (999, 300.0)]
)
def test_text_size_clamped(value, expected):
    label = Label.from_dict({"elements": [{"kind": "text", "size_pt": value}]})
    assert label.elements[0].size_pt == expected


@pytest.mark.parametrize("tape", (9, 12, 19, 25, 50))
def test_supported_tape_widths(tape):
    assert Label.from_dict({"tape_mm": tape}).tape_mm == tape


@pytest.mark.parametrize(
    "data",
    [
        {"tape_mm": 33},
        {"tape_mm": "50"},
        {"length_mm": "60"},
        {"length_mm": None},
        {"length_mm": True},
        {"margin_mm": [1]},
        {"edge_mm": False},
        {"border_mm": "1"},
        {"align": "left"},
        {"align": None},
        {"background": 0xFFFFFF},
        {"border_color": None},
        {"version": FORMAT_VERSION + 1},
        {"version": "1"},
        {"version": 1.0},
        {"elements": {"kind": "text"}},
        {"elements": "text"},
        {"elements": ["text"]},
        {"elements": [{"kind": "video"}]},
        {"elements": [{"text": "no kind"}]},
        {"elements": [{"kind": "text", "text": 5}]},
        {"elements": [{"kind": "text", "font": None}]},
        {"elements": [{"kind": "text", "color": 0}]},
        {"elements": [{"kind": "text", "size_pt": "12"}]},
        {"elements": [{"kind": "text", "size_pt": True}]},
        {"elements": [{"kind": "text", "align": "justify"}]},
        {"elements": [{"kind": "image", "data": None}]},
        {"elements": [{"kind": "image", "name": 3}]},
        {"elements": [{"kind": "code", "data": 4006381333931}]},
        {"elements": [{"kind": "code", "color": None}]},
        {"elements": [{"kind": "code", "symbology": "maxicode"}]},  # a newer type
        {"elements": [{"kind": "code", "symbology": 5}]},
        {"background_image": {"data": 5}},
        {"background_image": "abc"},
    ],
)
def test_invalid_values_raise(data):
    with pytest.raises(ValueError):
        Label.from_dict(data)


@pytest.mark.parametrize("data", [[], "x", None, 5])
def test_not_a_document(data):
    with pytest.raises(LabelError, match="not an Etiketto file") as info:
        Label.from_dict(data)
    assert info.value.code == "not_label"


@pytest.mark.parametrize(
    "data, code",
    [
        ({"version": FORMAT_VERSION + 1}, "newer"),
        ({"printer": "brother-ql-800"}, "newer"),  # a printer this version does not know
        ({"elements": [{"kind": "video"}]}, "newer"),  # an element kind added later
        ({"elements": [{"kind": None}]}, "damaged"),
        ({"elements": [{"text": "no kind"}]}, "damaged"),
        ({"version": "1"}, "damaged"),
        ({"version": True}, "damaged"),
        ({"printer": 5}, "damaged"),
        ({"tape_mm": 7}, "damaged"),
        ({"tape_mm": True}, "damaged"),
        ({"background_image": "abc"}, "damaged"),
        ({"background_image": [1, 2]}, "damaged"),
        ({"margin_mm": "1"}, "damaged"),
        ({"data_file": 5}, "damaged"),
        ({"data_file": "a\x00b.csv"}, "damaged"),
        ({"data_sheet": None}, "damaged"),
    ],
)
def test_error_codes(data, code):
    with pytest.raises(LabelError) as info:
        Label.from_dict(data)
    assert info.value.code == code
    assert str(info.value)  # English detail for bug reports


def test_printer_field(tmp_path):
    assert Label().printer == "vc500w"
    path = tmp_path / "p.etiketto"
    Label(tape_mm=12).save(path)
    assert json.loads(path.read_text())["printer"] == "vc500w"
    # files of Etiketto 0.1 have no printer: they are for the VC-500W
    path.write_text(json.dumps({"version": 1, "tape_mm": 19, "elements": []}))
    assert Label.load(path).printer == "vc500w"


def test_tape_widths_come_from_the_printer(monkeypatch):
    from fake_backends import FakeMono

    import etiketto.backends as registry

    registry.register(FakeMono())
    try:
        assert Label.from_dict({"printer": "fake-mono", "tape_mm": 24}).tape_mm == 24
        with pytest.raises(LabelError) as info:
            Label.from_dict({"printer": "fake-mono", "tape_mm": 50})
        assert info.value.code == "damaged"
    finally:
        registry.unregister("fake-mono")


def test_old_version_accepted_and_booleans_normalised():
    label = Label.from_dict(
        {
            "version": 0,
            "auto_length": 0,
            "elements": [{"kind": "text", "bold": 0, "italic": "yes"}],
        }
    )
    assert label.auto_length is False
    assert label.elements[0].bold is False and label.elements[0].italic is True


def test_from_dict_does_not_modify_input():
    data = {"version": 1, "elements": [], "tape_mm": 12}
    Label.from_dict(data)
    assert data == {"version": 1, "elements": [], "tape_mm": 12}


@pytest.mark.parametrize("content", [b"{", b"\xff\xfe\x00binary", b"", b"[1, 2]"])
def test_load_rejects_what_is_not_a_label(tmp_path, content):
    path = tmp_path / "bad.etiketto"
    path.write_bytes(content)
    with pytest.raises(LabelError) as info:
        Label.load(path)
    assert info.value.code == "not_label"


def test_load_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        Label.load(tmp_path / "missing.etiketto")


def test_image_from_file_and_raw(tmp_path):
    path = tmp_path / "pic.bin"
    path.write_bytes(b"\x00\x01binary")
    element = ImageElement.from_file(str(path))
    assert element.name == "pic.bin"
    assert element.data == base64.b64encode(b"\x00\x01binary").decode()
    assert element.raw() == b"\x00\x01binary"


# --- die-cut labels ------------------------------------------------------------------


def test_media_size_follows_the_orientation():
    label = Label(printer="cab", tape_mm=25, length_mm=50, orientation="across")
    assert label.media_size() == (50, 25)  # content along the 50 mm width
    label.set_orientation("along")
    assert label.media_size() == (50, 25)  # same label, turned content
    assert (label.tape_mm, label.length_mm) == (50, 25)
    label.set_orientation("along")  # no change
    assert (label.tape_mm, label.length_mm) == (50, 25)
    label.set_media_size(100, 150)
    assert (label.tape_mm, label.length_mm) == (100, 150)
    label.gap_mm = 2.5
    assert label.pitch_mm() == 152.5


def test_die_cut_label_round_trip(tmp_path):
    label = Label(printer="cab", orientation="across", auto_length=False, gap_mm=2)
    label.set_media_size(57.5, 32)
    path = tmp_path / "die-cut.etiketto"
    label.save(path)
    loaded = Label.load(path)
    assert loaded.media_size() == (57.5, 32) and loaded.gap_mm == 2
    assert loaded.orientation == "across" and not loaded.auto_length


def test_die_cut_validation():
    data = Label(printer="cab", tape_mm=1, length_mm=2000, gap_mm=99).to_dict()
    data["auto_length"] = True
    label = Label.from_dict(data)
    assert (label.tape_mm, label.length_mm, label.gap_mm) == (5, 1000, 50)
    assert not label.auto_length  # die-cut labels have a fixed size
    for field, value in (("tape_mm", "wide"), ("gap_mm", None), ("orientation", "diagonal")):
        with pytest.raises(LabelError) as caught:
            Label.from_dict(dict(data, **{field: value}))
        assert caught.value.code == "damaged"


def test_die_cut_labels_no_wider_than_the_print_head():
    label = Label(printer="cab", orientation="across")
    label.set_media_size(300, 1000)
    loaded = Label.from_dict(label.to_dict())
    assert loaded.media_size() == (105.6, 1000)
    label.set_orientation("along")
    assert Label.from_dict(label.to_dict()).media_size() == (105.6, 1000)


def test_tape_widths_are_whole_numbers():
    label = Label.from_dict(dict(Label().to_dict(), tape_mm=25.0))
    assert label.tape_mm == 25 and isinstance(label.tape_mm, int)


def test_tape_labels_keep_their_rules():
    data = Label(tape_mm=25, length_mm=10).to_dict()
    assert Label.from_dict(data).length_mm == 15  # the VC-500W's shortest label
    with pytest.raises(LabelError):
        Label.from_dict(dict(data, tape_mm=24))
    old = Label(tape_mm=25).to_dict()
    del old["gap_mm"], old["orientation"]  # a file of Etiketto 1.0
    assert Label.from_dict(old).orientation == "along"


def test_new_label_for_a_printer_model():

    assert Label.new_for("vc500w", 19).tape_mm == 19
    assert Label.new_for("vc500w", 33).tape_mm == 50  # unknown tape: the common one
    label = Label.new_for("cab")
    assert (label.printer, label.media_size(), label.auto_length) == ("cab", (50, 25), False)


def test_switch_printer_with_and_without_geometry():

    label = Label(tape_mm=12, auto_length=False, length_mm=70.0)
    before = label.geometry()
    label.switch_printer("cab")
    assert label.media_size() == (50, 25) and label.orientation == "across"
    label.switch_printer("vc500w", before)
    assert label.geometry() == before and label.printer == "vc500w"
    label.switch_printer("vc500w")  # tape to tape: nearest tape, length kept
    assert (label.tape_mm, label.auto_length) == (12, False)


def test_a_page_printer_turns_the_content_across():
    # cab with the content along the roll: the page has no roll, no direction row
    label = Label(printer="cab", tape_mm=50, length_mm=30, orientation="along", auto_length=False)
    label.switch_printer("system")
    assert label.orientation == "across" and label.media_size() == (50, 30)
    cab = label.geometry() | {"orientation": "along", "tape_mm": 50, "length_mm": 30}
    label.switch_printer("system", cab)  # also with a geometry from before
    assert label.orientation == "across" and label.media_size() == (50, 30)
    label.switch_printer("cab")  # the roll printer keeps what it has
    assert label.orientation == "across"


def test_a_page_file_with_the_content_along_opens_across():
    data = Label(printer="system", orientation="along", tape_mm=210, length_mm=297).to_dict()
    label = Label.from_dict(data)
    assert label.orientation == "across" and label.media_size() == (210, 297)


@pytest.mark.parametrize("kind", [[], ["text"], {}, {"a": 1}, 5, None, ""])
def test_element_kind_that_is_no_text_is_damaged(kind):
    with pytest.raises(LabelError) as info:
        Label.from_dict({"elements": [{"kind": kind}]})
    assert info.value.code == "damaged"


def test_a_huge_file_is_refused_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "huge.etiketto"
    path.write_text("{}")
    monkeypatch.setattr(model, "MAX_FILE_BYTES", 1)
    with pytest.raises(LabelError) as info:
        Label.load(path)
    assert info.value.code == "damaged" and "larger" in str(info.value)


def test_absurdly_nested_json_is_no_label(tmp_path):
    path = tmp_path / "deep.etiketto"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(LabelError) as info:
        Label.load(path)
    assert info.value.code == "not_label"


# --- label sheets --------------------------------------------------------------------


def test_a_label_sheet_is_saved_with_its_positions(tmp_path):
    label = Label(printer="system", orientation="across", elements=[TextElement(text="A")])
    label.sheet = SheetChoice("Avery L7160", [3, 4, 5], rotated=False)
    label.use_sheet_size()
    assert label.media_size() == (63.994, 38.1)
    path = tmp_path / "sheet.etiketto"
    label.save(path)
    saved = json.loads(path.read_text())
    assert saved["sheet"] == {"id": "Avery L7160", "positions": [3, 4, 5], "rotated": False}
    loaded = Label.load(path)
    assert loaded.sheet == label.sheet and loaded.media_size() == (63.994, 38.1)
    assert Label.from_dict({"printer": "system"}).sheet is None  # older files


def test_the_label_takes_its_sheets_size_also_turned():
    data = {"printer": "system", "tape_mm": 10, "length_mm": 10}
    data["sheet"] = {"id": "Zweckform 3475", "positions": [5, 5, 2, 10**6, -1], "rotated": True}
    label = Label.from_dict(data)
    assert label.sheet == SheetChoice("Zweckform 3475", [2, 5], rotated=True)
    assert label.media_size() == (36.026, 70.051)  # turned: as high as the sheet's is wide
    assert label.sheet_size() == (36.026, 70.051)


@pytest.mark.parametrize("rotated", [1, "false", None, [True]])
def test_sheet_rotated_must_be_true_or_false(rotated):
    data = {"printer": "system", "sheet": {"id": "Zweckform 3475", "rotated": rotated}}
    with pytest.raises(LabelError, match="rotated"):
        Label.from_dict(data)


def test_a_sheet_this_version_does_not_know_keeps_the_saved_size():
    data = {"printer": "system", "tape_mm": 40, "length_mm": 60}
    data["sheet"] = {"id": "Future 1"}
    label = Label.from_dict(data)
    assert label.sheet == SheetChoice("Future 1") and label.sheet_size() is None
    assert label.media_size() == (40, 60)


def test_only_page_printers_print_on_sheets():
    data = {"printer": "cab", "sheet": {"id": "Avery L7160"}}
    assert Label.from_dict(data).sheet is None
    label = Label(printer="system", orientation="across")
    label.sheet = SheetChoice("Avery L7160", [0])
    label.use_sheet_size()
    system = label.geometry()
    label.switch_printer("cab")
    assert label.sheet is None
    label.switch_printer("system", system)
    assert label.sheet == SheetChoice("Avery L7160", [0])
    assert system["sheet"] is not label.sheet  # a copy: later changes stay apart


@pytest.mark.parametrize(
    "sheet",
    ["Avery L7160", {"id": 5}, {"id": "x", "positions": "1-3"}, {"id": "x", "positions": [True]}],
)
def test_a_broken_sheet_is_damaged(sheet):
    with pytest.raises(LabelError) as caught:
        Label.from_dict({"printer": "system", "sheet": sheet})
    assert caught.value.code == "damaged"


# --- the link to a data file for serial labels ----------------------------------------


def test_data_file_beside_the_label_is_stored_relative(tmp_path):
    (tmp_path / "data").mkdir()
    label = Label(tape_mm=25, data_file=str(tmp_path / "data" / "rows.csv"), data_sheet="Mai")
    label.save(tmp_path / "a.etiketto")
    stored = json.loads((tmp_path / "a.etiketto").read_text(encoding="utf-8"))
    assert (stored["data_file"], stored["data_sheet"]) == ("data/rows.csv", "Mai")
    assert label.data_file == str(tmp_path / "data" / "rows.csv")  # saving changes nothing
    loaded = Label.load(tmp_path / "a.etiketto")
    assert loaded.data_file == str(tmp_path / "data" / "rows.csv")
    assert loaded.data_sheet == "Mai"


def test_moving_label_and_data_together_keeps_the_link(tmp_path):
    (tmp_path / "old").mkdir()
    label = Label(tape_mm=25, data_file=str(tmp_path / "old" / "rows.csv"))
    label.save(tmp_path / "old" / "a.etiketto")
    (tmp_path / "old").rename(tmp_path / "new")
    loaded = Label.load(tmp_path / "new" / "a.etiketto")
    assert loaded.data_file == str(tmp_path / "new" / "rows.csv")


def test_data_file_elsewhere_is_stored_absolute(tmp_path):
    (tmp_path / "labels").mkdir()
    data_file = str(tmp_path / "rows.csv")
    label = Label(tape_mm=25, data_file=data_file)
    label.save(tmp_path / "labels" / "a.etiketto")
    stored = json.loads((tmp_path / "labels" / "a.etiketto").read_text(encoding="utf-8"))
    assert stored["data_file"] == data_file
    assert Label.load(tmp_path / "labels" / "a.etiketto").data_file == data_file


def test_without_data_file(tmp_path):
    Label(tape_mm=25).save(tmp_path / "a.etiketto")
    stored = json.loads((tmp_path / "a.etiketto").read_text(encoding="utf-8"))
    assert (stored["data_file"], stored["data_sheet"]) == ("", "")
    assert Label.load(tmp_path / "a.etiketto").data_file == ""


def test_data_file_written_by_hand(tmp_path):
    """Relative paths may climb out of the label's folder, as written by hand."""
    path = tmp_path / "labels" / "a.etiketto"
    path.parent.mkdir()
    path.write_text(json.dumps({"tape_mm": 25, "data_file": "../shared/./rows.csv"}))
    assert Label.load(path).data_file == str(tmp_path / "shared" / "rows.csv")
    assert model.stored_data_path("", tmp_path) == ""
    assert model.resolved_data_path("", tmp_path) == ""


def test_a_data_file_is_a_feature_older_versions_refuse(tmp_path):
    """Version 1 readers refuse version 2 ("newer") instead of printing
    "{Name}" as it is; newer readers refuse features they do not know."""
    path = tmp_path / "a.etiketto"
    Label(tape_mm=25, data_file=str(tmp_path / "rows.csv")).save(path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert (stored["version"], stored["features"]) == (FORMAT_VERSION, ["serial"])
    assert Label.load(path).data_file == str(tmp_path / "rows.csv")
    with pytest.raises(LabelError) as info:
        Label.from_dict({"version": 2, "features": ["serial", "teleport"]})
    assert info.value.code == "newer" and "teleport" in str(info.value)
    for bad in ("serial", [1], {"serial": 1}):
        with pytest.raises(LabelError) as info:
            Label.from_dict({"version": 2, "features": bad})
        assert info.value.code == "damaged"


@given(data_file=st.text(max_size=20), sheet=st.text(max_size=5))
def test_version_2_is_never_written_without_features(data_file, sheet):
    """Readers rely on it: version 2 means "look at features"; a label with
    no feature stays version 1, which every version opens."""
    data = Label(data_file=data_file, data_sheet=sheet).to_dict()
    assert (data["version"] == 2) == bool(data.get("features"))
    assert (data["version"] == 1) == ("features" not in data)
    assert set(data.get("features", [])) <= model.FEATURES


def test_data_inside_the_label_folder(tmp_path):
    label_file = tmp_path / "labels" / "a.etiketto"
    (tmp_path / "labels" / "data").mkdir(parents=True)
    assert model.data_inside(str(tmp_path / "labels" / "data" / "rows.csv"), label_file)
    assert model.data_inside(str(tmp_path / "labels" / "rows.csv"), label_file)
    assert not model.data_inside(str(tmp_path / "rows.csv"), label_file)
    assert not model.data_inside("/etc/passwd", label_file)
    assert not model.data_inside("", label_file)
    assert not model.data_inside(str(tmp_path / "labels" / "rows.csv"), None)
    # a link inside the folder that leads out of it
    (tmp_path / "labels" / "out").symlink_to(tmp_path, target_is_directory=True)
    assert not model.data_inside(str(tmp_path / "labels" / "out" / "rows.csv"), label_file)
    # no hidden folders, only tables
    assert not model.data_inside(str(tmp_path / "labels" / ".ssh" / "id.csv"), label_file)
    assert not model.data_inside(str(tmp_path / "labels" / "id_rsa"), label_file)
    assert model.data_inside(str(tmp_path / "labels" / "data" / "Rows.XLSX"), label_file)


def test_files_of_older_versions_have_no_data_file():
    label = Label.from_dict({"tape_mm": 25})
    assert (label.data_file, label.data_sheet) == ("", "")


def test_a_label_with_both_features(tmp_path):
    path = tmp_path / "both.etiketto"
    label = Label(
        tape_mm=25,
        elements=[TextElement(text="{Name} {=date}")],
        data_file=str(tmp_path / "rows.csv"),
    )
    label.save(path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert (stored["version"], stored["features"]) == (2, ["serial", "variables"])
    assert Label.load(path) == label
    Label.store_counter(path, 9)  # the command line keeps what it does not change
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["features"] == ["serial", "variables"] and stored["counter_next"] == 9
    assert {"barcodes", "symbols", "pages", "sheets", "serial", "variables"} == model.FEATURES


def test_every_feature_at_once(tmp_path):
    from etiketto.model import PageElement
    from etiketto.symbols import find as find_symbol

    symbol = find_symbol("printer")
    label = Label(
        printer="system",
        orientation="across",
        elements=[
            TextElement(text="{Name} {=date}"),
            CodeElement(data="ABC", symbology="datamatrix"),
            SymbolElement(name=symbol.name, path=symbol.path),
            PageElement(data="eA==", name="label.pdf"),
        ],
        data_file=str(tmp_path / "rows.csv"),
    )
    label.sheet = SheetChoice("Avery L7160", [0])
    data = label.to_dict()
    assert data["version"] == 2
    assert data["features"] == ["barcodes", "symbols", "pages", "sheets", "serial", "variables"]
    assert set(data["features"]) == model.FEATURES  # every one there is
    assert Label.from_dict(data).features() == data["features"]


def test_shipping_labels_and_label_sheets_are_features_too(tmp_path):
    """The one rule: what an older version would print wrongly or refuse
    unclearly is a feature, new element kinds included."""
    from etiketto.model import PageElement

    page = Label(tape_mm=25, elements=[PageElement(data="eA==", name="label.pdf")])
    assert page.to_dict()["features"] == ["pages"]
    sheet = Label(printer="system", orientation="across", elements=[TextElement(text="A")])
    sheet.sheet = SheetChoice("Avery L7160", [0])
    data = sheet.to_dict()
    assert data["version"] == 2 and data["features"] == ["sheets"]
    assert Label.from_dict(data).sheet == sheet.sheet  # this version reads it
    with pytest.raises(LabelError) as info:  # one that does not know it refuses
        Label.from_dict({**data, "features": ["sheets", "holograms"]})
    assert info.value.code == "newer"
