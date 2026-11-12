"""Property-based tests: generated inputs instead of hand-picked examples.

Label files come from anywhere (downloads, other people, other versions), so
loading must turn every malformed one into a LabelError and nothing else.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tavletta.model import Label, LabelError
from tavletta.units import INCHES, MILLIMETRES

# JSON as json.loads() can return it
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=12), children, max_size=4)
    ),
    max_leaves=20,
)
FIELDS = list(Label.__dataclass_fields__) + ["version", "features"]
ELEMENT_FIELDS = [
    *("kind", "text", "font", "bold", "italic", "size_pt", "color", "align"),
    *("data", "name", "symbology"),
]
# elements with known fields set to arbitrary values, a real kind now and then
elements = st.lists(
    st.dictionaries(
        st.sampled_from(ELEMENT_FIELDS),
        json_values | st.sampled_from(["text", "image", "code"]),
        max_size=6,
    ),
    max_size=3,
)
# a label object with some known fields set to arbitrary values
label_like = st.dictionaries(st.sampled_from(FIELDS), json_values, max_size=8) | st.builds(
    lambda data, items: dict(data, elements=items),
    st.dictionaries(st.sampled_from(FIELDS), json_values, max_size=4),
    elements,
)


@settings(max_examples=300, deadline=None)  # the first call imports the backends
@given(
    st.one_of(
        json_values,
        label_like,
        st.builds(dict, background_image=elements.map(lambda items: items[0] if items else {})),
    )
)
def test_loading_any_json_gives_a_label_or_a_label_error(data):
    try:
        label = Label.from_dict(data)
    except LabelError as e:
        assert e.code in ("not_label", "newer", "damaged")
        return
    # what was accepted is valid and survives saving and loading again
    again = Label.from_dict(json.loads(json.dumps(label.to_dict())))
    assert again == label


@given(
    tape=st.sampled_from([9, 12, 19, 25, 50]),
    length=st.floats(min_value=15, max_value=1000),
    margin=st.floats(min_value=0, max_value=20),
)
def test_a_tape_label_keeps_its_values(tape, length, margin):
    label = Label(tape_mm=tape, auto_length=False, length_mm=length, margin_mm=margin)
    again = Label.from_dict(json.loads(json.dumps(label.to_dict())))
    assert (again.tape_mm, again.length_mm, again.margin_mm) == (tape, length, margin)


@given(
    width=st.floats(min_value=5, max_value=105.6),
    height=st.floats(min_value=5, max_value=1000),
    orientation=st.sampled_from(["across", "along"]),
)
def test_die_cut_size_survives_turning_the_content(width, height, orientation):
    label = Label(printer="cab", orientation="along")
    label.set_media_size(width, height)
    label.set_orientation(orientation)
    assert label.media_size() == (width, height)
    assert Label.from_dict(label.to_dict()).media_size() == (width, height)


@pytest.mark.parametrize("unit", [MILLIMETRES, INCHES])
@given(mm=st.floats(min_value=0, max_value=1000))
def test_shown_value_converts_back_within_its_rounding(unit, mm):
    shown = unit.to_display(mm)
    half_step = 0.5 * 10**-unit.digits  # in display units
    assert abs(unit.from_display(shown) - mm) <= unit.from_display(half_step) + 1e-9
    assert unit.to_display(unit.from_display(shown)) == shown  # stable once shown
