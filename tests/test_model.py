import base64
import io

import pytest
from PIL import Image

from zinklabel.model import CodeElement, ImageElement, Label, TextElement


def png_b64(size=(20, 10)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_roundtrip(tmp_path):
    label = Label(
        tape_mm=25,
        auto_length=False,
        length_mm=80,
        background="#ffeeaa",
        background_image=ImageElement(data=png_b64(), name="bg.png"),
        elements=[
            TextElement(text="Hallo\nWelt", bold=False, color="#123456"),
            ImageElement(data=png_b64(), name="x.png"),
            CodeElement(data="4006381333931", symbology="ean13"),
        ],
    )
    path = tmp_path / "a.zlabel"
    label.save(path)
    assert Label.load(path) == label
    assert not (tmp_path / "a.zlabel.tmp").exists()


def test_rejects_unknown_element_and_width():
    with pytest.raises(ValueError):
        Label.from_dict({"elements": [{"kind": "video"}]})
    with pytest.raises(ValueError):
        Label.from_dict({"tape_mm": 33})


def test_rejects_newer_format():
    with pytest.raises(ValueError):
        Label.from_dict({"version": 999})


def test_ignores_unknown_fields():
    label = Label.from_dict({"tape_mm": 12, "future_option": True})
    assert label.tape_mm == 12
