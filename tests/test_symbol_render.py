"""Symbols on the label: drawn at the printer's resolution, crisp black and
white for mono printers, coloured on colour printers; and in label files."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import ink_bbox
from PIL import Image, ImageChops

from tavletta import model
from tavletta.backends import get_backend
from tavletta.backends.base import PrintOptions
from tavletta.model import CodeElement, Label, LabelError, SymbolElement
from tavletta.render import mm_to_px, render, render_symbol
from tavletta.symbols import find

SQUARE = "M0,0H24V24H0Z"  # fills the whole symbol
# the left half of the square: shows which way a symbol is turned
HALF = "M0,0H12V24H0Z"


def printer_symbol(**kwargs) -> SymbolElement:
    symbol = find("printer")
    assert symbol
    return SymbolElement(name=symbol.name, path=symbol.path, **kwargs)


def colours(image: Image.Image) -> set:
    return {colour for _count, colour in image.getcolors(maxcolors=1 << 24)}


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_mono_symbols_are_pure_black_and_white(dpi):
    label = Label(tape_mm=25, margin_mm=0, elements=[printer_symbol(color="#ff0000")])
    image = render(label, dpi=dpi, safe_mm=0, mono=True).image
    assert colours(image) == {(0, 0, 0), (255, 255, 255)}  # no grey edges, no red
    # drawn at the resolution: the printer icon's ink is 20 × 18 of 24 units
    x0, y0, x1, y1 = ink_bbox(image, "white")
    size = mm_to_px(25, dpi)
    assert (x1 - x0) / size == pytest.approx(20 / 24, abs=2 / size)
    assert (y1 - y0) / size == pytest.approx(18 / 24, abs=2 / size)


@pytest.mark.parametrize(
    ("background", "color", "ink"),
    [
        ("#000000", "#ffffff", 255),
        ("#000000", "#ffff66", 255),
        ("#000000", "#7f7f7f", 255),
        ("#000000", "#0000ff", 255),  # lighter than black: white
        ("#000000", "#000000", 0),
        ("#ffffff", "#ffff66", 0),  # a light colour on white stays visible
        ("#ffffff", "#e0e0e0", 0),
        ("#ffffff", "#ffffff", 255),
        ("#808080", "#c0c0c0", 255),
        ("#808080", "#404040", 0),
    ],
)
def test_mono_symbols_are_black_or_white_against_the_background(background, color, ink):
    """White only where the colour is lighter than the background: a white
    symbol on a black label stays white, a yellow one on white paper black."""
    label = Label(tape_mm=25, margin_mm=0, background=background)
    label.elements = [SymbolElement(path=SQUARE, color=color)]
    image = render(label, dpi=203, safe_mm=0, mono=True).image
    size = image.height
    assert image.getpixel((size // 2, size // 2)) == (ink, ink, ink)


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_symbols_on_colour_printers_are_smooth_and_coloured(dpi):
    label = Label(tape_mm=25, margin_mm=0, elements=[printer_symbol(color="#0000ff")])
    image = render(label, dpi=dpi, safe_mm=0).image
    found = colours(image)
    assert (0, 0, 255) in found and len(found) > 10  # antialiased edges
    assert all(r == g for r, g, _b in found)  # only blue on white


def test_mono_printers_keep_symbols_crisp_after_dithering():
    """A die-cut label on a cab printer: the symbol survives the dithering
    of prepare() unchanged, without stray dots along its edges."""
    backend = get_backend("cab")
    label = Label.new_for("cab")
    label.margin_mm = 0
    label.elements = [printer_symbol()]
    printed = backend.prepare(label, PrintOptions(), safe_mm=0)
    assert label.orientation == "across"  # printed as rendered, not turned
    dpi = backend.raster(label.tape_mm, PrintOptions()).dpi
    expected = render(replace(label, auto_length=False), dpi=dpi, safe_mm=0, mono=True).image
    assert ink_bbox(expected, "white")
    assert ImageChops.difference(printed.convert("RGB"), expected).getbbox() is None


def test_size_rotation_and_the_tape_height():
    tall = render_symbol(SymbolElement(path=SQUARE), 100, 300)
    assert tall.size == (100, 100)  # 0: the content height
    small = render_symbol(SymbolElement(path=SQUARE, size_mm=5), 100, 300)
    assert small.size == (mm_to_px(5, 300),) * 2
    big = render_symbol(SymbolElement(path=SQUARE, size_mm=50), 100, 300)
    assert big.size == (100, 100)  # never taller than the content
    upright = render_symbol(SymbolElement(path=HALF), 40, 300)
    for rotation in (90, 180, 270):
        turned = render_symbol(SymbolElement(path=HALF, rotation=rotation), 40, 300)
        # PIL turns counter-clockwise, the symbol clockwise
        assert ImageChops.difference(turned, upright.rotate(-rotation)).getbbox() is None
    left = upright.getchannel("A")
    assert left.getpixel((5, 20)) == 255 and left.getpixel((35, 20)) == 0


def test_scaled_down_symbols_are_drawn_again():
    """A fixed length too short for the content: the symbol is drawn at the
    smaller size instead of being resampled, so it stays black and white."""
    wide = SymbolElement(path=SQUARE)
    label = Label(tape_mm=25, auto_length=False, length_mm=20, margin_mm=0)
    label.elements = [printer_symbol(), wide, printer_symbol()]
    rendered = render(label, dpi=300, safe_mm=0, mono=True)
    assert rendered.overflow
    assert colours(rendered.image) == {(0, 0, 0), (255, 255, 255)}


def test_the_saved_path_is_drawn_not_the_library():
    element = SymbolElement(name="printer", path=SQUARE)
    image = render_symbol(element, 30, 300)
    assert image.getchannel("A").getextrema() == (255, 255)  # the square, not the printer
    by_name = render_symbol(SymbolElement(name="printer"), 30, 300)  # no path: the library
    expected = render_symbol(printer_symbol(), 30, 300)
    assert ImageChops.difference(by_name, expected).getbbox() is None


def test_unknown_or_damaged_symbols_warn():
    label = Label(
        tape_mm=25,
        elements=[SymbolElement(name="not-in-the-library"), SymbolElement(path="L1,1")],
    )
    rendered = render(label)
    assert [(w.key, dict(w.params)) for w in rendered.warnings] == [
        ("symbol_unknown", {"n": 1}),
        ("element_failed", {"n": 2}),
    ]


# --- label files -----------------------------------------------------------------------


def test_symbols_are_saved_and_loaded(tmp_path):
    label = Label(tape_mm=25, elements=[printer_symbol(size_mm=12.5, rotation=90)])
    path = tmp_path / "label.tavletta"
    label.save(path)
    data = json.loads(path.read_text())
    # older versions refuse it: by the feature, or before it by the unknown kind
    assert data["version"] == 2 and data["features"] == ["symbols"]
    assert data["elements"][0] == {
        "name": "printer",
        "path": find("printer").path,
        "size_mm": 12.5,
        "rotation": 90,
        "color": "#000000",
        "kind": "symbol",
    }
    assert Label.load(path) == label


def test_symbol_fields_are_checked():
    def load(**fields):
        item = {"kind": "symbol", "name": "star", "path": "M0,0Z", **fields}
        return Label.from_dict({"tape_mm": 25, "elements": [item]}).elements[0]

    assert load(rotation=180.0).rotation == 180 and isinstance(load(rotation=180.0).rotation, int)
    assert load(size_mm=5000).size_mm == model.MAX_SIZE_MM
    assert load(size_mm=-3).size_mm == 0
    for bad in (
        {"rotation": 45},
        {"rotation": True},
        {"rotation": [90]},
        {"name": 3},
        {"name": "x" * (model.MAX_SYMBOL_NAME + 1)},
        {"path": None},
        {"path": "M0,0" + "L1,1" * model.MAX_SYMBOL_PATH},
        {"color": 0},
        {"size_mm": "big"},
    ):
        with pytest.raises(LabelError) as error:
            load(**bad)
        assert error.value.code == "damaged", bad


def test_versions_without_symbols_refuse_them_as_newer(monkeypatch):
    """What an Tavletta from before the symbol library does with such a
    file: the unknown kind of element makes it "newer", nothing is dropped."""
    data = Label(tape_mm=25, elements=[printer_symbol()]).to_dict()
    monkeypatch.delitem(model.ELEMENT_TYPES, "symbol")
    with pytest.raises(LabelError) as error:
        Label.from_dict(data)
    assert error.value.code == "newer"


def test_symbols_and_barcodes_are_both_listed():
    label = Label(
        tape_mm=25,
        elements=[printer_symbol(), CodeElement(data="ABC", symbology="datamatrix")],
    )
    data = label.to_dict()
    assert data["version"] == 2 and data["features"] == ["barcodes", "symbols"]
    assert Label.from_dict(data) == label
