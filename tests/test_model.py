import base64
import json

import pytest

from etiketto import model
from etiketto.model import FORMAT_VERSION, CodeElement, ImageElement, Label, TextElement


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


@pytest.mark.parametrize("tape", model.TAPE_WIDTHS_MM)
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
    with pytest.raises(ValueError, match="not an Etiketto file"):
        Label.from_dict(data)


def test_old_version_accepted_and_booleans_normalised():
    label = Label.from_dict(
        {"version": 0, "auto_length": 0, "elements": [{"kind": "text", "bold": 0, "italic": "yes"}]}
    )
    assert label.auto_length is False
    assert label.elements[0].bold is False and label.elements[0].italic is True


def test_from_dict_does_not_modify_input():
    data = {"version": 1, "elements": [], "tape_mm": 12}
    Label.from_dict(data)
    assert data == {"version": 1, "elements": [], "tape_mm": 12}


def test_load_rejects_invalid_json(tmp_path):
    path = tmp_path / "bad.etiketto"
    path.write_text("{")
    with pytest.raises(ValueError):
        Label.load(path)


def test_image_from_file_and_raw(tmp_path):
    path = tmp_path / "pic.bin"
    path.write_bytes(b"\x00\x01binary")
    element = ImageElement.from_file(str(path))
    assert element.name == "pic.bin"
    assert element.data == base64.b64encode(b"\x00\x01binary").decode()
    assert element.raw() == b"\x00\x01binary"
