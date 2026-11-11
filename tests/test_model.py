import base64
import json

import pytest

from etiketto import model
from etiketto.model import (
    FORMAT_VERSION,
    CodeElement,
    ImageElement,
    Label,
    LabelError,
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
    assert not (tmp_path / "a.etiketto.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == FORMAT_VERSION
    assert [e["kind"] for e in data["elements"]] == ["text", "image", "code"]


def test_save_keeps_umlauts_readable(tmp_path):
    path = tmp_path / "u.etiketto"
    Label(elements=[TextElement(text="Grüße")]).save(path)
    assert "Grüße" in path.read_text(encoding="utf-8")


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "a.etiketto"
    path.write_text("old content")
    written = []

    def broken_replace(self, target):
        written.append(self.read_text(encoding="utf-8"))
        raise OSError("disk full")

    monkeypatch.setattr(model.Path, "replace", broken_replace)
    with pytest.raises(OSError):
        Label(tape_mm=12).save(path)
    assert path.read_text() == "old content"
    assert json.loads(written[0])["tape_mm"] == 12  # went to the temporary file


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
        {"elements": [{"kind": "code", "symbology": "pdf417"}]},
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
