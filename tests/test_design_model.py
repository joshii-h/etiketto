"""The file format of the design options: written only when not default,
the features "styles" and "shapes", round trips, untrusted values, and
refusal by readers that do not know them."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tavletta import model
from tavletta.model import (
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    PageElement,
    ShapeElement,
    SymbolElement,
    TextElement,
)

STYLED_TEXT = {
    "underline": True,
    "strikethrough": True,
    "letter_spacing": 12.0,
    "line_spacing": 1.5,
    "shrink": True,
    "min_size_pt": 6.0,
    "max_width_mm": 30.0,
    "rotation": 90,
    "inverted": True,
    "box_color": "#ffcc00",
    "padding_mm": 1.0,
    "corner_mm": 2.0,
    "frame_mm": 0.3,
    "outline_mm": 0.2,
    "outline_color": "#000000",
    "shadow_mm": 0.4,
    "shadow_color": "#333333",
    "valign": "top",
    "gap_mm": 0.0,
}
STYLED_IMAGE = {
    "mode": "threshold",
    "threshold": 100,
    "brightness": 10.0,
    "contrast": -20.0,
    "gamma": 1.8,
    "invert": True,
    "rotation": 270,
    "mirror": True,
    "flip": True,
    "crop": [0.1, 0.2, 0.5, 0.6],
    "corner_mm": 3.0,
    "border_mm": 0.5,
    "border_color": "#ff0000",
    "valign": "bottom",
    "gap_mm": 4.0,
}


def roundtrip(label: Label) -> Label:
    return Label.from_dict(json.loads(json.dumps(label.to_dict())))


# --- defaults stay out of the file -----------------------------------------------------


def test_a_label_without_design_options_is_written_as_before():
    label = Label(
        elements=[
            TextElement(text="x"),
            ImageElement(data="AA==", name="a.png"),
            CodeElement(),
            SymbolElement(name="star", path="M0,0Z"),
            PageElement(data="AA=="),
        ]
    )
    data = label.to_dict()
    assert data["features"] == ["symbols", "pages"]  # their own, no "styles"
    assert set(data["elements"][0]) == {
        "text",
        "font",
        "bold",
        "italic",
        "size_pt",
        "color",
        "align",
        "name",
        "kind",
    }
    assert set(data["elements"][1]) == {"data", "name", "kind"}
    assert "valign" not in data["elements"][2] and "gap_mm" not in data["elements"][3]
    assert {"border_radius_mm", "border_style", "border_inset_mm"}.isdisjoint(data)


def test_only_changed_fields_are_written():
    data = Label(elements=[TextElement(text="x", underline=True)]).to_dict()
    assert data["version"] == 2 and data["features"] == ["styles"]
    element = data["elements"][0]
    assert element["underline"] is True
    assert "strikethrough" not in element and "shadow_color" not in element


def test_every_style_field_has_a_default_like_before():
    for cls, names in model.STYLE_FIELDS.items():
        known = {f.name for f in fields(cls)}
        assert set(names) <= known, cls
        assert not model.styled(cls(), names), cls
    assert not model.styled(Label(), model.LABEL_STYLE_FIELDS)


@pytest.mark.parametrize(
    "element",
    [
        TextElement(**STYLED_TEXT),
        ImageElement(data="AA==", **STYLED_IMAGE),
        CodeElement(valign="top"),
        SymbolElement(path="M0,0Z", gap_mm=1.0),
        PageElement(data="AA==", valign="bottom"),
    ],
)
def test_style_fields_make_the_feature_and_survive_a_round_trip(element):
    label = Label(elements=[element])
    assert "styles" in label.features()
    assert roundtrip(label) == label


def test_label_border_style_round_trip():
    label = Label(border_mm=1, border_radius_mm=2.5, border_style="double", border_inset_mm=1)
    data = label.to_dict()
    assert data["features"] == ["styles"]
    assert (data["border_radius_mm"], data["border_style"], data["border_inset_mm"]) == (
        2.5,
        "double",
        1,
    )
    assert roundtrip(label) == label


def test_shapes_make_their_own_feature():
    label = Label(elements=[ShapeElement(shape="ellipse", fill="hatched")])
    data = label.to_dict()
    assert data["features"] == ["shapes"]
    assert data["elements"][0]["kind"] == "shape"
    assert data["elements"][0]["shape"] == "ellipse"
    assert roundtrip(label) == label
    label.elements[0].valign = "top"
    assert label.features() == ["styles", "shapes"]


def test_a_background_image_keeps_no_image_options(tmp_path):
    label = Label(background_image=ImageElement(data="AA==", name="bg.png"))
    data = label.to_dict()
    assert data["background_image"] == {"data": "AA==", "name": "bg.png", "kind": "image"}
    path = tmp_path / "bg.tavletta"
    label.save(path)
    assert Label.load(path) == label


def test_files_of_earlier_versions_load_unchanged(tmp_path):
    old = {
        "version": 1,
        "tape_mm": 25,
        "border_mm": 0.5,
        "elements": [
            {"kind": "text", "text": "Hi", "size_pt": 0},
            {"kind": "image", "data": "AA==", "name": "a.png"},
        ],
    }
    label = Label.from_dict(old)
    text, image = label.elements
    assert (text.underline, text.valign, text.gap_mm, text.rotation) == (False, "center", None, 0)
    assert (image.mode, image.crop, image.threshold) == ("auto", [0.0, 0.0, 1.0, 1.0], 128)
    assert label.border_style == "solid" and label.features() == []


# --- older readers ---------------------------------------------------------------------


def older_reader(monkeypatch):
    """A Tavletta from before the design options: it knows neither the
    features nor the shape element."""
    monkeypatch.setattr(
        model, "FEATURES", frozenset({"barcodes", "symbols", "serial", "variables"})
    )
    kinds = {k: v for k, v in model.ELEMENT_TYPES.items() if k != "shape"}
    monkeypatch.setattr(model, "ELEMENT_TYPES", kinds)


@pytest.mark.parametrize(
    "label",
    [
        Label(elements=[TextElement(text="x", inverted=True)]),
        Label(elements=[ImageElement(data="AA==", mode="threshold")]),
        Label(border_mm=1, border_style="dotted"),
        Label(elements=[CodeElement(gap_mm=1.0)]),
        Label(elements=[ShapeElement()]),
    ],
)
def test_older_readers_refuse_labels_that_need_the_new_features(monkeypatch, label):
    data = json.loads(json.dumps(label.to_dict()))
    older_reader(monkeypatch)
    with pytest.raises(LabelError) as error:
        Label.from_dict(data)
    assert error.value.code == "newer"


def test_older_readers_refuse_a_shape_even_without_the_feature(monkeypatch):
    data = Label(elements=[ShapeElement()]).to_dict()
    data.pop("features")
    data["version"] = 1
    older_reader(monkeypatch)
    with pytest.raises(LabelError) as error:
        Label.from_dict(data)
    assert error.value.code == "newer"


def test_older_readers_still_open_labels_without_them(monkeypatch):
    data = json.loads(json.dumps(Label(elements=[TextElement(text="x")]).to_dict()))
    older_reader(monkeypatch)
    assert Label.from_dict(data).elements[0].text == "x"


# --- untrusted values ------------------------------------------------------------------


def element_with(kind: str, **values) -> dict:
    base = {"text": {"text": "x"}, "image": {"data": "AA=="}, "shape": {}}[kind]
    return {"kind": kind, **base, **values}


@pytest.mark.parametrize(
    "kind, values",
    [
        ("text", {"underline": "yes"}),
        ("text", {"rotation": 45}),
        ("text", {"rotation": True}),
        ("text", {"valign": "middle"}),
        ("text", {"gap_mm": "1"}),
        ("text", {"box_color": 1}),
        ("text", {"letter_spacing": None}),
        ("image", {"mode": "photo"}),
        ("image", {"threshold": 1.5}),
        ("image", {"crop": [0, 0, 1]}),
        ("image", {"mirror": 1}),
        ("shape", {"shape": "star"}),
        ("shape", {"fill": "gradient"}),
        ("shape", {"arrows": "left"}),
        ("shape", {"line_mm": "thick"}),
        ("shape", {"color": None}),
    ],
)
def test_invalid_values_are_damaged(kind, values):
    with pytest.raises(LabelError) as error:
        Label.from_dict({"elements": [element_with(kind, **values)]})
    assert error.value.code == "damaged"


def test_invalid_border_style_is_damaged():
    with pytest.raises(LabelError):
        Label.from_dict({"border_style": "wavy"})


def test_numbers_are_clamped():
    label = Label.from_dict(
        {
            "border_radius_mm": 999,
            "border_inset_mm": -3,
            "elements": [
                element_with("text", letter_spacing=1000, line_spacing=0, gap_mm=500),
                element_with("image", threshold=999, gamma=0, crop=[0.9, 0.9, 1, 1]),
                element_with("shape", width_mm=0, line_mm=99, height_mm=-1),
            ],
        }
    )
    text, image, shape = label.elements
    assert (label.border_radius_mm, label.border_inset_mm) == (50, 0)
    assert (text.letter_spacing, text.line_spacing, text.gap_mm) == (200, 0.5, 50)
    assert (image.threshold, image.gamma) == (254, 0.2)
    assert image.crop == pytest.approx([0.9, 0.9, 0.1, 0.1])
    assert (shape.width_mm, shape.line_mm, shape.height_mm) == (0.1, 10, 0)


def test_positional_arguments_keep_their_meaning():
    """The placement fields are keyword-only: TextElement("x") is still the text."""
    assert TextElement("x").text == "x"
    assert CodeElement("data", "ean13").symbology == "ean13"


@pytest.mark.parametrize(
    "item",
    [
        TextElement(outline_color="#ff0000"),
        TextElement(shadow_color="#00ff00"),
        TextElement(min_size_pt=8),
        TextElement(padding_mm=2, corner_mm=1),
        ImageElement(threshold=50),
        ImageElement(border_color="#ff0000"),
    ],
)
def test_fields_without_effect_are_kept_but_no_feature(item):
    """Written, so they survive saving and opening, but no reason to refuse
    the file in earlier versions (which ignore fields they do not know)."""
    label = Label(elements=[item])
    data = json.loads(json.dumps(label.to_dict()))
    assert "features" not in data and data["version"] == 1
    assert Label.from_dict(data) == label


def test_earlier_versions_ignore_fields_they_do_not_know(monkeypatch):
    data = Label(elements=[TextElement(text="x", outline_color="#ff0000")]).to_dict()
    known = {
        "text",
        "font",
        "bold",
        "italic",
        "size_pt",
        "color",
        "align",
        "name",
        "kind",
    }
    monkeypatch.setattr(
        model,
        "_known_fields",
        lambda cls, d: (
            {k: v for k, v in d.items() if k in known}
            if cls is TextElement
            else {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        ),
    )
    assert Label.from_dict(data).elements[0].outline_color == "#ffffff"  # dropped, no error


def test_fields_with_effect_are_written():
    label = Label(elements=[TextElement(outline_mm=0.2, outline_color="#ff0000")])
    assert label.to_dict()["elements"][0]["outline_color"] == "#ff0000"
    image = ImageElement(mode="threshold", threshold=50)
    assert Label(elements=[image]).to_dict()["elements"][0]["threshold"] == 50


def test_label_border_shape_without_border_is_no_feature():
    label = Label(border_radius_mm=3, border_style="dotted")
    data = label.to_dict()
    assert "features" not in data and data["border_style"] == "dotted"
    assert Label.from_dict(data) == label


style_values = {
    "underline": st.booleans(),
    "letter_spacing": st.floats(-50, 200, allow_nan=False),
    "line_spacing": st.floats(0.5, 3, allow_nan=False),
    "shrink": st.booleans(),
    "min_size_pt": st.floats(0, 300, allow_nan=False),
    "rotation": st.sampled_from(model.ROTATIONS),
    "inverted": st.booleans(),
    "box_color": st.sampled_from(["", "#ff0000"]),
    "padding_mm": st.floats(0, 20, allow_nan=False),
    "outline_mm": st.floats(0, 5, allow_nan=False),
    "outline_color": st.sampled_from(["#ffffff", "#000000"]),
    "shadow_color": st.sampled_from(["#808080", "#123456"]),
    "valign": st.sampled_from(model.VALIGNS),
    "gap_mm": st.one_of(st.none(), st.floats(0, 50, allow_nan=False)),
}


@settings(max_examples=150, deadline=None)
@given(st.fixed_dictionaries({}, optional=style_values))
def test_saving_and_opening_keeps_every_design_field(values):
    label = Label(elements=[TextElement(text="x", **values)])
    data = json.loads(json.dumps(label.to_dict()))
    again = Label.from_dict(data)
    assert again == label
    assert again.to_dict() == data
