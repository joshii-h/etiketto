"""The one list of image formats a label may embed (tavletta.imageformats)."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from tavletta import imageformats
from tavletta import render as flow


def test_the_renderer_uses_the_list():
    assert flow.IMAGE_FORMATS is imageformats.FORMATS
    assert "EPS" not in imageformats.FORMATS and "ICO" not in imageformats.FORMATS


def test_suffixes_and_types_are_those_of_the_formats():
    """Every suffix and type offered is one Pillow reads as one of the
    formats, and each format has a suffix and a type."""
    Image.init()
    by_suffix = {s.lstrip("."): f for s, f in Image.registered_extensions().items()}
    assert {by_suffix[s] for s in imageformats.SUFFIXES} == set(imageformats.FORMATS)
    assert {Image.MIME[f] for f in imageformats.FORMATS} == set(imageformats.MIME_TYPES)
    names = imageformats.NAMES.upper().split(", ")
    assert names == list(imageformats.FORMATS)


def test_by_name_and_by_content(tmp_path):
    Image.new("RGB", (4, 4)).save(tmp_path / "scan", format="PNG")
    Image.new("RGB", (4, 4)).save(tmp_path / "a.ico")
    (tmp_path / "a.txt").write_text("x")
    assert imageformats.has_image_suffix("A.JPG") and imageformats.has_image_suffix("b.tif")
    assert not imageformats.has_image_suffix(tmp_path / "a.ico")
    assert imageformats.is_image(tmp_path / "scan")
    assert not imageformats.is_image(tmp_path / "a.ico")
    assert not imageformats.is_image(tmp_path / "a.txt")
    assert not imageformats.is_image(tmp_path / "missing.png")


@pytest.mark.parametrize("fmt", ["ICO", "PPM", "TGA"])
def test_the_renderer_refuses_other_formats(fmt):
    raw = io.BytesIO()
    Image.new("RGB", (4, 4)).save(raw, format=fmt)
    with (
        pytest.raises(Image.UnidentifiedImageError),
        flow._opened(base64.b64encode(raw.getvalue()).decode()),
    ):
        pass
