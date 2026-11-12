"""The free layout (tavletta.freelayout): the model and its file format,
where elements land in printer dots, a QR code's modules on whole dots and
its error correction, the counter in a text and a code, the command line,
and the label made after a cablabel template."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from PIL import Image

from tavletta import codes, freelayout, typeset
from tavletta import render as flow
from tavletta.model import (
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    PageElement,
    ShapeElement,
    SheetChoice,
    SymbolElement,
    TextElement,
)
from tavletta.render import RenderWarning, render
from tavletta.variables import Values, expand_label

DPI = 12 * 25.4  # a cab printer at 300 dpi: 12 dots per mm
FONT = "Source Sans 3"
SQUARE = "M0,0H24V24H0Z"


def _installed(family: str) -> bool:
    """Whether the family has a light face of its own (not a substitute)."""
    import gi

    gi.require_version("PangoCairo", "1.0")
    from gi.repository import PangoCairo

    found = PangoCairo.FontMap.get_default().get_family(family)
    return bool(found) and any(int(f.describe().get_weight()) == 300 for f in found.list_faces())


# the label of the cablabel template is set in Source Sans; the tests of its
# metrics need it (Fedora: adobe-source-sans-pro-fonts)
needs_source_sans = pytest.mark.skipif(not _installed(FONT), reason="Source Sans 3 missing")


def cab_label(*elements, **fields) -> Label:
    """A die-cut label of 40 × 18.6 mm on a cab printer, free layout."""
    label = Label.new_for("cab")
    label.orientation = "across"
    label.set_media_size(40.0, 18.6)
    label.gap_mm = 4.0
    label.layout = "free"
    label.elements = list(elements)
    for name, value in fields.items():
        setattr(label, name, value)
    return label


def dark(image: Image.Image) -> Image.Image:
    return image.convert("L").point(lambda v: 255 if v < 128 else 0)


def box(image: Image.Image, area=None):
    """The dark pixels' box (in the area, as image coordinates)."""
    part = image.crop(area) if area else image
    found = dark(part).getbbox()
    if found and area:
        return (found[0] + area[0], found[1] + area[1], found[2] + area[0], found[3] + area[1])
    return found


def mono(label: Label) -> Image.Image:
    return render(label, dpi=DPI, safe_mm=1.5, mono=True).image


# --- the model and its file --------------------------------------------------------------


def test_free_fields_round_trip(tmp_path):
    label = cab_label(
        TextElement(text="Shelf", font=FONT, weight=600, size_pt=14.17, x_mm=2.5, y_mm=3.43),
        CodeElement(
            data="1", error_correction="L", module_mm=0.67, x_mm=23.49, y_mm=3.75, rotation=90
        ),
        CodeElement(data="12345", symbology="code128", module_mm=0.3, height_mm=6),
        ImageElement(data="", height_mm=8, x_mm=1),
        SymbolElement(path=SQUARE, size_mm=5, x_mm=30),
        ShapeElement(width_mm=5, height_mm=2, y_mm=1),
    )
    path = tmp_path / "free.tavletta"
    label.save(path)
    stored = json.loads(path.read_text())
    assert stored["layout"] == "free" and "free_layout" in stored["features"]
    assert stored["elements"][0]["weight"] == 600
    assert stored["elements"][1]["error_correction"] == "L"
    assert Label.load(path) == label


def test_flow_files_stay_as_they_were():
    """No layout, no position, no weight: what earlier versions wrote."""
    label = Label(elements=[TextElement(), CodeElement()])
    data = label.to_dict()
    assert data["version"] == 1 and "features" not in data and "layout" not in data
    for item in data["elements"]:
        assert not {"x_mm", "y_mm", "weight", "error_correction", "module_mm"} & set(item)
    assert Label.from_dict(data).layout == "flow"


def test_options_of_the_free_layout_are_a_feature_in_the_flow_too():
    """A weight or an error correction changes how a flowing label prints:
    older versions must refuse it; a position does not (it is not used)."""
    assert Label(elements=[TextElement(weight=300)]).features() == ["free_layout"]
    assert Label(elements=[CodeElement(error_correction="H")]).features() == ["free_layout"]
    assert Label(elements=[TextElement(x_mm=5, y_mm=5)]).features() == []


def test_older_versions_refuse_the_free_layout():
    """What an Tavletta without the feature does with such a file: refuse it
    as one of a newer version, never print it one element after another."""
    data = cab_label(TextElement()).to_dict()
    data["features"].append("free_layout_2")
    with pytest.raises(LabelError) as caught:
        Label.from_dict(data)
    assert caught.value.code == "newer"


@pytest.mark.parametrize(
    ("field", "value"),
    [("x_mm", "1"), ("weight", 1.5), ("error_correction", "X"), ("rotation", 45)],
)
def test_damaged_free_fields(field, value):
    item = {"kind": "code" if field in ("error_correction", "rotation") else "text"}
    item[field] = value
    with pytest.raises(LabelError) as caught:
        Label.from_dict({"elements": [item]})
    assert caught.value.code == "damaged"


def test_free_fields_are_clamped():
    data = {
        "layout": "free",
        "features": ["free_layout"],
        "version": 2,
        "elements": [
            {"kind": "text", "x_mm": -5, "y_mm": 5000, "weight": 50},
            {"kind": "code", "module_mm": 99, "height_mm": -1},
            {"kind": "image", "height_mm": -3},
        ],
    }
    text, code, image = Label.from_dict(data).elements
    assert (text.x_mm, text.y_mm, text.weight) == (0, 1000, 100)
    assert (code.module_mm, code.height_mm, image.height_mm) == (10, 0, 0)
    with pytest.raises(LabelError):
        Label.from_dict({"layout": "grid"})


# --- where the elements land --------------------------------------------------------------


@needs_source_sans
def test_a_text_is_placed_by_its_ink():
    """x and y are the top left corner of the letters' ink, in whole dots."""
    text = TextElement(text="Shelf", font=FONT, weight=600, size_pt=14.17, x_mm=2.5, y_mm=3.43)
    image = mono(cab_label(text))
    assert box(image)[:2] == (30, 41)  # 2.5 mm × 12, 3.43 mm × 12 = 41.2


@needs_source_sans
def test_the_text_size_is_the_em():
    """A size of 5 mm is the font's em: Source Sans' capitals are 0.66 em."""
    text = TextElement(text="H", font=FONT, size_pt=freelayout.mm_to_pt(5.0), x_mm=1, y_mm=1)
    left, top, right, bottom = box(mono(cab_label(text)))
    assert bottom - top == pytest.approx(0.66 * 5 * 12, abs=1)


@needs_source_sans
def test_the_weight_is_drawn():
    """Light, regular and semibold of the same text: more ink with weight."""
    inks = []
    for weight in (300, 400, 600):
        text = TextElement(text="Kliniken", font=FONT, weight=weight, size_pt=14, x_mm=1, y_mm=1)
        inks.append(dark(mono(cab_label(text))).histogram()[255])
    assert inks[0] < inks[1] < inks[2]


def test_weights_between_pangos_take_the_nearest():
    assert int(typeset.weight(TextElement(weight=650))) in (600, 700)
    assert int(typeset.weight(TextElement(weight=0, bold=True))) == 700
    assert int(typeset.weight(TextElement(weight=0, bold=False))) == 400


@needs_source_sans
def test_a_family_ending_in_a_number_is_not_a_size():
    """ "Source Sans 3" is a family, not "Source Sans" at size 3."""
    layout = typeset.layout(TextElement(text="x", font=FONT), 50)
    assert layout.get_font_description().get_family() == FONT


def test_a_qr_code_is_placed_by_its_first_module():
    code = CodeElement(data="100001", error_correction="L", module_mm=0.67, x_mm=23.49, y_mm=3.75)
    image = mono(cab_label(code))
    # 21 modules of 8 dots: 0.67 mm at 12 dots/mm, 14 mm; 23.49 × 12 = 281.9
    assert box(image) == (282, 45, 282 + 168, 45 + 168)


@pytest.mark.parametrize(("module_mm", "dots"), [(0.67, 8), (0.5, 6), (0.04, 1), (0.71, 9)])
def test_modules_snap_to_whole_dots(module_mm, dots):
    code = CodeElement(data="100001", error_correction="L", module_mm=module_mm)
    part = freelayout.code_part(code, DPI, freelayout.Inks(mono=True))
    assert part.image.size == (21 * dots, 21 * dots)
    assert part.image.code_module == dots  # type: ignore[attr-defined]
    thin = "code_thin" in part.image.code_problems  # type: ignore[attr-defined]
    assert thin == (dots < 2)


def test_modules_on_multiples_of_the_step():
    code = CodeElement(data="1", module_mm=0.67)
    assert freelayout.module_px(code, DPI, steps=(1, 3)) == 6
    assert freelayout.module_px(code, DPI, steps=(2, 3)) == 6  # a multiple of both
    assert freelayout.module_px(code, DPI, steps=(4, 1)) == 8


def test_along_step_puts_codes_and_the_length_on_whole_pairs():
    """render(along_step=2), as the QL in high quality: the first module of
    a code and the label's length on even pixels, a fixed length kept as
    set."""
    code = CodeElement(data="12345", symbology="code128", module_mm=0.25, x_mm=3.1, y_mm=1)
    label = Label(tape_mm=24, layout="free", auto_length=False, length_mm=30.1, elements=[code])
    rendered = render(label, dpi=600, along_step=2)
    assert rendered.image.width % 2 == 0 and rendered.length_mm == 30.1
    ink = rendered.image.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()
    assert ink is not None and ink[0] % 2 == 0


def test_a_linear_code_is_placed_by_its_first_bar():
    code = CodeElement(
        data="12345", symbology="code128", module_mm=0.25, height_mm=5, x_mm=4, y_mm=2
    )
    image = mono(cab_label(code))
    left, top, _right, _bottom = box(image)
    assert (left, top) == (48, 24)
    bars = box(image, (0, 0, 480, 24 + 60))  # the bars: 5 mm, then the digits
    assert bars[3] - top == 60


def test_a_turned_code_keeps_its_corner():
    for rotation in (0, 90, 180, 270):
        code = CodeElement(data="12345", rotation=rotation, x_mm=3, y_mm=2)
        image = mono(cab_label(code))
        assert box(image)[:2] == (36, 24), rotation


def test_turned_boxes():
    assert freelayout.turned_box((1, 2, 4, 3), (10, 5), 90) == (2, 1, 3, 4)
    assert freelayout.turned_box((1, 2, 4, 3), (10, 5), 180) == (6, 2, 9, 3)
    assert freelayout.turned_box((1, 2, 4, 3), (10, 5), 270) == (2, 6, 3, 9)
    assert freelayout.turned_box((1, 2, 4, 3), (10, 5), 0) == (1, 2, 4, 3)


def test_images_symbols_shapes_and_pages_by_their_corner(png_b64):
    elements = [
        ImageElement(data=png_b64(color="black"), height_mm=4, x_mm=1, y_mm=1),
        SymbolElement(path=SQUARE, size_mm=3, x_mm=10, y_mm=2, color="#000000"),
        ShapeElement(shape="rectangle", width_mm=4, height_mm=3, fill="solid", x_mm=20, y_mm=3),
    ]
    image = mono(cab_label(*elements))
    assert box(image, (0, 0, 115, 223)) == (12, 12, 12 + 96, 12 + 48)
    assert box(image, (115, 0, 200, 223))[:2] == (120, 24)
    assert box(image, (200, 0, 480, 223))[:2] == (240, 36)
    page = cab_label(PageElement(data=png_b64(color="black"), x_mm=30, y_mm=10))
    assert box(mono(page))[:2] == (360, 120)


def test_size_zero_is_visible(png_b64):
    elements = [TextElement(text="X", size_pt=0), ImageElement(data=png_b64(color="black"))]
    rendered = render(cab_label(*elements), dpi=DPI, mono=True)
    assert box(rendered.image) is not None and not rendered.warnings


def test_beyond_the_edge_is_a_warning():
    text = TextElement(text="Far too wide", size_pt=40, x_mm=30, y_mm=2)
    rendered = render(cab_label(text), dpi=DPI, mono=True)
    assert RenderWarning("outside_label", {"n": 1}) in rendered.warnings


def test_an_element_that_fails_is_a_placeholder():
    rendered = render(cab_label(CodeElement(data="", x_mm=2)), dpi=DPI)
    assert rendered.warnings[0].key == "code_empty"
    assert box(rendered.image) is not None


def test_shape_without_line_or_fill():
    shape = ShapeElement(shape="rectangle", line_mm=0, fill="none")
    assert render(cab_label(shape), dpi=DPI).warnings == [
        RenderWarning("shape_invisible", {"n": 1})
    ]


def test_colour_and_endless_tape():
    """In colour with anti-aliasing; on endless tape as long as the content."""
    text = TextElement(text="Hi", color="#ff0000", size_pt=20, x_mm=50, y_mm=2)
    label = Label(tape_mm=50, layout="free", margin_mm=2, elements=[text])
    rendered = render(label, dpi=300)
    assert rendered.length_mm > 52 and not rendered.warnings
    assert (255, 0, 0) in {px for _n, px in rendered.image.getcolors(1 << 16)}


def test_on_a_background_image_the_inks_follow_what_lies_under(png_b64):
    label = cab_label(
        TextElement(text="Hi", size_pt=20, x_mm=2, y_mm=2),
        background_image=ImageElement(data=png_b64(color="black")),
    )
    flowing = render(replace(label, layout="flow"), dpi=DPI, mono=True)
    rendered = render(label, dpi=DPI, mono=True)
    assert rendered.warnings == flowing.warnings == [RenderWarning("no_contrast", {"n": 1})]


def test_the_print_image_is_the_label(tmp_path):
    """What the cab backend gets: 40 × 18.6 mm at 12 dots/mm, black and white."""
    from tavletta.backends import get_backend
    from tavletta.backends.base import PrintOptions

    image = get_backend("cab").prepare(cab_label(TextElement(x_mm=1)), PrintOptions())
    assert image.size == (480, 223) and image.info["black_and_white"]


# --- QR codes: error correction -----------------------------------------------------------


def test_error_correction_as_chosen():
    zxing = pytest.importorskip("zxingcpp")
    for level in "LMQH":
        code = CodeElement(data="100001", error_correction=level, module_mm=0.5)
        image = freelayout.code_part(code, DPI, freelayout.Inks(mono=True)).image
        flat = Image.new("L", (image.width + 60, image.height + 60), 255)
        flat.paste(0, (30, 30), image)
        found = zxing.read_barcodes(flat)
        assert found[0].text == "100001" and found[0].ec_level == level


def test_the_modules_of_cablabels_qr_code():
    """ECC L, the mask ISO/IEC 18004 chooses (7 for "100001", as cablabel
    and the printer draw it; segno's own choice would be mask 0)."""
    symbol = codes.encode(CodeElement(data="100001", error_correction="L"))
    assert symbol.rows[8][:9] == "110100110"  # the format information of L, mask 7
    standard = codes.encode(CodeElement(data="100001"))
    assert standard.rows != symbol.rows  # files of earlier versions: unchanged


def test_without_zint_from_segno(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_zint(name, *args, **kwargs):
        if name == "zint":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    codes._qr.cache_clear()
    monkeypatch.setattr(builtins, "__import__", no_zint)
    try:
        symbol = codes.encode(CodeElement(data="100001", error_correction="H"))
    finally:
        codes._qr.cache_clear()
    assert len(symbol.rows) == 21


def test_a_qr_code_beyond_ascii_says_its_encoding():
    symbol = codes.encode(CodeElement(data="Grüsse", error_correction="M"))
    assert symbol.width >= 21


# --- the counter -----------------------------------------------------------------------------


def test_the_counter_fills_text_and_code():
    label = cab_label(
        TextElement(
            text="{=counter}", font=FONT, weight=600, size_pt=12.76, x_mm=2.57, y_mm=14.69
        ),
        CodeElement(data="{=counter}", error_correction="L", module_mm=0.67, x_mm=23.49),
        counter_next=100001,
    )
    for copy, number in ((1, "100001"), (2, "202582")):
        values = Values.for_label(label, now=_now(), copies=2).for_copy(copy)
        filled, warnings = expand_label(label, values)
        assert not warnings
        assert [filled.elements[0].text, filled.elements[1].data] == [number, number]
        assert filled.layout == "free" and filled.elements[1].x_mm == 23.49


def _now():
    from datetime import datetime

    return datetime(2026, 11, 12, 12, 0)


# --- from one after another to free ---------------------------------------------------------


def test_adopt_keeps_the_places(png_b64):
    label = cab_label(
        TextElement(text="Shelf", size_pt=0),
        CodeElement(data="1"),
        CodeElement(data="12345", symbology="code128"),
        ImageElement(data=png_b64()),
        SymbolElement(path=SQUARE),
        ShapeElement(width_mm=3),
        layout="flow",
    )
    flowing = mono(label)
    freelayout.adopt(label, DPI)
    text, qr, bars, image, symbol, shape = label.elements
    assert text.size_pt > 0 and qr.module_mm > 0 and bars.height_mm > 0
    assert image.height_mm > 0 and symbol.size_mm > 0 and shape.height_mm > 0
    label.layout = "free"
    placed = mono(label)
    assert box(placed) == box(flowing)
    differing = sum(
        1 for a, b in zip(dark(placed).tobytes(), dark(flowing).tobytes(), strict=True) if a != b
    )
    assert differing < 0.02 * placed.width * placed.height


def _ql_die_cut(*elements) -> Label:
    label = Label(printer="brother_ql_diecut", auto_length=False, orientation="across")
    label.set_media_size(62, 29)
    label.elements = list(elements)
    return label


def _on_a_sheet(*elements) -> Label:
    label = Label(printer="system", orientation="across", auto_length=False)
    label.sheet = SheetChoice("Avery L7160", [0])
    label.use_sheet_size()
    label.elements = list(elements)
    return label


def _ink_of(label: Label, mono_: bool) -> tuple[int, int, int, int]:
    return box(render(label, dpi=DPI, mono=mono_).image)


def _adopted(label: Label, mono_: bool) -> tuple:
    """The ink one after another and in the free layout after adopt()."""
    flowing = _ink_of(label, mono_)
    freelayout.adopt(label, DPI, mono=mono_)
    label.layout = "free"
    return flowing, _ink_of(label, mono_)


@pytest.mark.parametrize("make", [cab_label, _ql_die_cut, _on_a_sheet])
@pytest.mark.parametrize("mono_", [False, True])
def test_adopt_on_a_label_of_fixed_length(make, mono_):
    """A die-cut label or one on a sheet: a text that fits is drawn only
    once its size is known; adopt() takes it over drawn (it failed on
    `_LaterText` before)."""
    label = make(TextElement(text="Shelf", size_pt=12), CodeElement(data="1"))
    label.layout, label.auto_length = "flow", False
    flowing, placed = _adopted(label, mono_)
    text = label.elements[0]
    assert text.size_pt == pytest.approx(12, abs=0.2) and text.x_mm > 0
    assert all(abs(a - b) <= 1 for a, b in zip(placed, flowing, strict=True))


@pytest.mark.parametrize("size", [0, 12, 30])
@pytest.mark.parametrize("mono_", [False, True])
def test_adopt_keeps_the_ink_of_a_text_reduced_to_fit(size, mono_):
    """A text too long for the fixed length is reduced (resampled in
    colour, laid out again in black and white): the free layout draws it
    with the same ink, a dot off at most."""
    text = TextElement(text="Example Clinic Maintenance 2027", size_pt=size)
    label = cab_label(text, layout="flow", auto_length=False)
    flowing, placed = _adopted(label, mono_)
    assert text.size_pt < 8
    assert all(abs(a - b) <= 1 for a, b in zip(placed, flowing, strict=True))


def test_adopt_keeps_a_text_that_failed():
    """A text that could not be drawn (a placeholder) keeps its size."""
    text = TextElement(text="Shelf", size_pt=0)
    label = cab_label(text, layout="flow", auto_length=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(flow, "render_element", _fails)
        freelayout.adopt(label, DPI)
    assert text.size_pt == 0 and (text.x_mm, text.y_mm) != (0, 0)


def test_matched_size_without_ink():
    """A text without ink (spaces) keeps the size it was drawn at; one that
    shows no ink when drawn again too."""
    blank = Image.new("RGBA", (40, 20))
    blank.info["size_px"] = 20.0
    how = (DPI, flow.Inks(), 20.0)
    size = freelayout._matched_size(TextElement(text="   "), blank, how)
    assert size == pytest.approx(20.0 / DPI * 72 * 20 / _height("   ", 20.0), abs=0.01)
    inked = Image.new("RGBA", (40, 20), (0, 0, 0, 255))
    assert freelayout._matched_size(TextElement(text="   "), inked, how) > 0


def _height(text: str, size: float) -> int:
    return typeset.size_of(TextElement(text=text), size, DPI, antialias=True, wrap_px=None)[1]


def _fails(*_args, **_kwargs):
    raise ValueError("broken")


def test_drawn_again_in_other_inks(monkeypatch):
    """_again(): the element in other inks if it comes out the same size,
    else (or if it fails) as it was drawn; too large stays an error."""
    element = SymbolElement(path=SQUARE, size_mm=2)
    image = Image.new("RGBA", (24, 24))
    how = (DPI, flow.Inks(mono=True), (1, 1))
    assert freelayout._again(element, image, how, (100, 100)).size == (24, 24)
    assert freelayout._again(element, image, how, (100, 100)) is not image
    small = Image.new("RGBA", (5, 5))
    assert freelayout._again(element, small, how, (100, 100)) is small
    monkeypatch.setattr(freelayout, "element_part", _fails)
    assert freelayout._again(element, image, how, (100, 100)) is image

    def too_large(*_args, **_kwargs):
        raise flow.RenderTooLarge("too large")

    monkeypatch.setattr(freelayout, "element_part", too_large)
    with pytest.raises(flow.RenderTooLarge):
        freelayout._again(element, image, how, (100, 100))


# --- the command line ---------------------------------------------------------------------------


def test_the_command_line_prints_the_free_layout(tmp_path, capsys):
    from tavletta import cli

    label = cab_label(
        TextElement(text="{=counter}", size_pt=12, x_mm=2, y_mm=2),
        CodeElement(data="{=counter}", error_correction="L", module_mm=0.67, x_mm=20, y_mm=2),
        counter_next=100001,
    )
    path = tmp_path / "free.tavletta"
    label.save(path)
    out = tmp_path / "out.png"
    code = cli.main(
        ["print", str(path), "--dry-run", "--no-query", "--no-save-counter", "--output", str(out)]
    )
    assert code == 0
    image = Image.open(out)
    assert image.size == (480, 223)
    assert box(image, (230, 0, 480, 223))[:2] == (240, 24)


def test_a_barcode_of_designed_height():
    """Bar height 0: as designed for the module (10 mm at 0.3 mm)."""
    code = CodeElement(data="12345", symbology="code128", module_mm=0.25, x_mm=2, y_mm=2)
    part = freelayout.code_part(code, DPI, freelayout.Inks(mono=True))
    assert part.image.height == round(round(3 * flow.BAR_HEIGHT_MM / 0.3) * 16.3 / 10)


def test_black_and_red():
    """A black-and-red printer keeps red text red, also in the free layout."""
    label = cab_label(TextElement(text="Hi", color="#ff0000", size_pt=20, x_mm=2, y_mm=2))
    image = render(label, dpi=DPI, mono=True, red=True).image
    assert (255, 0, 0) in {px for _n, px in image.getcolors(1 << 16)}


def test_each_element_against_what_lies_under_it():
    """A background image dark on the left, light on the right: a text on
    each side in the ink that shows there."""
    half = Image.new("RGB", (40, 20), "white")
    half.paste((0, 0, 0), (0, 0, 20, 20))
    import base64
    import io

    buf = io.BytesIO()
    half.save(buf, format="PNG")
    label = cab_label(
        TextElement(text="A", color="#808080", size_pt=20, x_mm=3, y_mm=4),
        TextElement(text="B", color="#808080", size_pt=20, x_mm=30, y_mm=4),
        background_image=ImageElement(data=base64.b64encode(buf.getvalue()).decode()),
    )
    rendered = render(label, dpi=DPI, mono=True)
    left = rendered.image.crop((0, 0, 200, 223)).convert("L")
    right = rendered.image.crop((300, 0, 480, 223)).convert("L")
    assert 255 in left.getextrema() and 0 in right.getextrema()
    assert not rendered.warnings
