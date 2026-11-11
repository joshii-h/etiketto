"""Shipping labels: reading PDF pages and images, finding the label on a
page, the crop handles, label sizes and the label made of the area.

The documents are synthetic (shipping_samples.py): A4 pages with a 4 × 6 in
label in a corner or sideways in the upper half, a receipt and instructions,
as PDF or as a noisy scan.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, ImageDraw
from shipping_samples import A4, LABEL, a4_pdf, encode, page_image

from etiketto.backends import get_backend
from etiketto.model import Label, LabelError, PageElement
from etiketto.render import mm_to_px, render
from etiketto.shipping import crop as cropping
from etiketto.shipping import detect, source, targets
from etiketto.shipping.source import SourceError

pytest.importorskip("cairo")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def values(image: Image.Image) -> set[int]:
    return set(image.convert("L").tobytes())


def _clear_caches():
    for cached in (source._document, source.draw):
        if hasattr(cached, "cache_clear"):  # not while a test replaces it
            cached.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(scope="module")
def pdf():
    return b64(a4_pdf("top-left", pages=2))


def has_poppler() -> bool:
    try:
        source._poppler()
    except SourceError:
        return False
    return True


needs_poppler = pytest.mark.skipif(not has_poppler(), reason="Poppler (poppler-glib) missing")

# where the label is on the sample pages, as fractions of the page
TOP_LEFT = (30 / A4[0], 30 / A4[1], LABEL[0] / A4[0], LABEL[1] / A4[1])
TOP = (40 / A4[0], 50 / A4[1], LABEL[1] / A4[0], LABEL[0] / A4[1])


def covers(crop, label, slack=0.03) -> bool:
    """The crop holds all of the label and not much more."""
    x, y, w, h = crop
    lx, ly, lw, lh = label
    inside = x <= lx + 0.002 and y <= ly + 0.002 and x + w >= lx + lw - 0.002
    inside = inside and y + h >= ly + lh - 0.002
    return inside and w <= lw + slack and h <= lh + slack


def square_image(mode="RGB", size=(40, 60)) -> Image.Image:
    """White with a black square in the upper left corner."""
    image = Image.new(mode, size, "white")
    ImageDraw.Draw(image).rectangle((0, 0, 9, 9), fill="black")
    return image


# --- source ---------------------------------------------------------------------------


def test_is_pdf():
    assert source.is_pdf(b"%PDF-1.7\n")
    assert source.is_pdf(b"junk before\n%PDF-1.4")
    assert not source.is_pdf(b"\x89PNG")


@needs_poppler
def test_pdf_page_info(pdf):
    info = source.page_info(pdf, 1)
    assert (info.pages, info.pdf) == (2, True)
    assert (info.width, info.height) == pytest.approx(A4)
    assert info.width_mm() == pytest.approx(210, abs=0.1)
    # a page after the last is the last
    assert source.page_info(pdf, 9) == info


def test_image_page_info():
    data = b64(encode(square_image()))
    info = source.page_info(data, 3)
    assert (info.width, info.height, info.pages, info.pdf) == (40, 60, 1, False)
    assert info.width_mm() is None


@pytest.mark.parametrize("raw", [b"no image", b"%PDF-1.4 damaged"])
def test_unreadable(raw):
    if source.is_pdf(raw) and not has_poppler():
        pytest.skip("Poppler missing")
    with pytest.raises(SourceError) as error:
        source.page_info(b64(raw))
    assert error.value.code == "unreadable"


def test_bad_base64():
    with pytest.raises(SourceError) as error:
        source.page_info("abc")
    assert error.value.code == "unreadable"


def test_pdf_without_poppler(monkeypatch):
    import gi

    def missing(name, version):
        raise ValueError("Namespace %s not available" % name)

    monkeypatch.setattr(gi, "require_version", missing)
    with pytest.raises(SourceError) as error:
        source.page_info(b64(b"%PDF-1.4"))
    assert error.value.code == "no_pdf"
    assert "Poppler" in str(error.value)


def test_pdf_without_pages():
    class Empty:
        def get_n_pages(self):
            return 0

    with pytest.raises(SourceError):
        source._pdf_page(Empty(), 0)


@needs_poppler
def test_page_without_size(monkeypatch, pdf):
    class Page:
        def get_size(self):
            return (0.0, 0.0)

    monkeypatch.setattr(source, "_pdf_page", lambda doc, index: (Page(), 1))
    with pytest.raises(SourceError):
        source.page_info(pdf)


def test_fitted():
    assert source.fitted((100, 150), (50, 100)) == (50, 75, 0.5)
    assert source.fitted((100, 150), (None, 300)) == (200, 300, 2.0)
    assert source.fitted((100, 150), (50, 100), "fill") == (50, 100, pytest.approx(2 / 3))
    # "fill" without a width: as wide as needed
    assert source.fitted((100, 150), (None, 30), "fill") == (20, 30, 0.2)


def test_turned_and_area():
    assert source.turned(10, 20, 90) == (20, 10)
    assert source.turned(10, 20, 180) == (10, 20)
    assert source.area((0.5, 0.25, 0.5, 0.5), 200, 400) == (100, 100, 100, 200)


@needs_poppler
def test_pdf_area_is_drawn_sharp_at_the_size(pdf):
    image = source.draw(pdf, 0, TOP_LEFT, 0, (1200, 1800))
    # fitted into the box: 4 × 6 in at 300 dpi
    assert image.size == (1200, 1800)
    # no grey: bars and text are whole printer dots
    assert values(image) == {0, 255}
    smooth = source.draw(pdf, 0, TOP_LEFT, 0, (1200, 1800), sharp=False)
    assert len(values(smooth)) > 2


@needs_poppler
def test_pdf_rotation_is_clockwise():
    # a page with a black square in the upper left corner
    import cairo

    buf = io.BytesIO()
    surface = cairo.PDFSurface(buf, 200, 300)
    ctx = cairo.Context(surface)
    ctx.rectangle(0, 0, 50, 50)
    ctx.fill()
    surface.finish()
    data = b64(buf.getvalue())
    turned = source.draw(data, 0, detect.FULL, 90, (300, 200))
    assert turned.size == (300, 200)
    assert turned.getpixel((290, 10)) == (0, 0, 0)  # now in the upper right corner
    assert turned.getpixel((10, 10)) == (255, 255, 255)
    upside = source.draw(data, 0, detect.FULL, 180, (200, 300))
    assert upside.getpixel((190, 290)) == (0, 0, 0)


@needs_poppler
def test_pdf_fill_covers_the_box(pdf):
    image = source.draw(pdf, 0, TOP_LEFT, 0, (1200, 1000), "fill")
    assert image.size == (1200, 1000)


def test_image_enlarged_with_whole_pixels():
    data = b64(encode(square_image()))
    image = source.draw(data, 0, detect.FULL, 0, (120, 180))
    assert image.size == (120, 180)
    assert values(image) == {0, 255}
    # the 10 px square became exactly 30 px
    assert Image.eval(image.convert("L"), lambda v: 255 - v).getbbox() == (0, 0, 30, 30)


def test_image_reduced_and_turned():
    data = b64(encode(square_image()))
    image = source.draw(data, 0, detect.FULL, 90, (30, 20))
    assert image.size == (30, 20)
    assert image.getpixel((29, 0))[0] < 128  # clockwise: upper right
    assert image.getpixel((0, 19))[0] > 128


def test_image_crop_and_fill():
    data = b64(encode(square_image(size=(100, 100))))
    image = source.draw(data, 0, (0.0, 0.0, 0.5, 0.5), 0, (100, 50), "fill")
    assert image.size == (100, 50)


@pytest.mark.parametrize("box", [(97, 31), (31, 97), (64, 64)])
def test_image_area_has_no_black_edges(box):
    """Whole pixels of an odd area never leave a strip outside the image."""
    data = b64(encode(Image.new("RGB", (101, 67), "white")))
    for fit in ("fit", "fill"):
        drawn = source.draw(data, 0, (0.013, 0.031, 0.517, 0.701), 90, box, fit)
        assert values(drawn) == {255}


def test_transparent_image_is_on_white_paper():
    image = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    drawn = source.draw(b64(encode(image)), 0, detect.FULL, 0, (10, 10))
    assert values(drawn) == {255}


def test_exif_rotation_is_applied():
    image = square_image(size=(60, 40))
    exif = Image.Exif()
    exif[0x0112] = 6  # shown turned clockwise
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    info = source.page_info(b64(buf.getvalue()))
    assert (info.width, info.height) == (40, 60)


def test_too_large():
    data = b64(encode(square_image()))
    with pytest.raises(SourceError) as error:
        source.draw(data, 0, detect.FULL, 0, (20_000, 30_000))
    assert error.value.code == "too_large"


@needs_poppler
def test_preview(pdf):
    image = source.preview(pdf, 0, 700)
    assert max(image.size) == 700
    assert image.size == (round(700 * A4[0] / A4[1]), 700)


# --- detection ------------------------------------------------------------------------


@pytest.mark.parametrize("noise", [False, True])
@pytest.mark.parametrize("layout, label", [("top-left", TOP_LEFT), ("top", TOP)])
def test_framed_label(layout, label, noise):
    found = detect.detect(page_image(layout, 120, noise))
    assert found.how == "frame" and found.certain
    assert covers(found.crop, label, slack=0.02)
    assert found.crop in found.candidates


@pytest.mark.parametrize("noise", [False, True])
@pytest.mark.parametrize("layout", ["top-left", "alone"])
def test_label_without_frame(layout, noise):
    found = detect.detect(page_image(layout, 120, noise, frame=False))
    assert found.how == "block"
    x, y, w, h = found.crop
    lx, ly, lw, lh = TOP_LEFT
    # the label's content: left, top and right edge; its footer ends early
    assert x <= lx + 0.01 and y <= ly + 0.01 and x + w >= lx + lw - 0.03
    assert w <= lw + 0.03 and h <= lh + 0.03 and y + h >= ly + 0.85 * lh


def test_sideways_label_without_frame():
    found = detect.detect(page_image("top", 120, frame=False))
    assert found.how == "block"
    x, y, w, h = found.crop
    lx, ly, lw, lh = TOP
    assert x <= lx + 0.01 and y <= ly + 0.01 and x + w >= lx + lw - 0.03
    assert w <= lw + 0.03 and h <= lh + 0.03


def test_blank_page():
    found = detect.detect(Image.new("RGB", (400, 566), (230, 230, 230)))
    assert (found.crop, found.how, found.certain) == (detect.FULL, "none", False)


def test_page_that_is_the_label():
    # an image of only the label (4 × 6 in): everything printed is the label
    page = page_image("alone", 120)
    width, height = page.size
    label = page.crop((0, 0, round(width * 0.6), round(height * 0.58)))
    found = detect.detect(label)
    assert found.how == "content"
    assert found.crop[2] > 0.8 and found.crop[3] > 0.8


def test_label_sized_pdf_page():
    page = page_image("alone", 120)
    assert detect.detect(page, width_mm=101.6).how == "content"
    assert detect.detect(page, width_mm=210).how == "frame"


def test_is_sheet():
    assert detect.is_sheet((1240, 1754))  # A4 by its shape
    assert detect.is_sheet((1275, 1650))  # Letter
    assert not detect.is_sheet((1200, 1800))  # 4 × 6 in
    assert detect.is_sheet((100, 141), width_mm=210)
    assert not detect.is_sheet((100, 150), width_mm=100)


def test_two_alike_labels_need_a_check():
    page = Image.new("L", (1240, 1754), 255)
    draw = ImageDraw.Draw(page)
    for left in (60, 660):
        for row in range(20):
            draw.rectangle((left, 80 + row * 40, left + 500, 100 + row * 40), fill=0)
    found = detect.detect(page)
    assert found.how == "block" and not found.certain
    assert len(found.candidates) >= 3


def test_only_small_blocks_fall_back_to_the_content():
    page = Image.new("L", (1240, 1754), 255)
    draw = ImageDraw.Draw(page)
    draw.rectangle((100, 100, 140, 120), fill=0)
    draw.rectangle((1000, 1500, 1040, 1520), fill=0)
    found = detect.detect(page)
    assert found.how == "content"
    assert found.candidates == [found.crop]


def test_dust_is_not_ink():
    page = Image.new("L", (200, 200), 255)
    page.putpixel((50, 50), 0)
    page.putpixel((51, 50), 0)
    assert detect.ink_mask(page).getbbox() is None
    ImageDraw.Draw(page).line((10, 100, 190, 100), fill=0)  # a thin line stays
    assert detect.ink_mask(page).getbbox() is not None


def test_thick_lines_are_one_line():
    rows = [b"\x00" * 10, b"\xff" * 10, b"\xff" * 10, b"\x00\x00" + b"\xff" * 8]
    lines = detect.long_lines(rows, 5)
    assert lines == [detect.Line(1, 0, 10, 3)]
    # far apart, they stay two lines
    assert len(detect.long_lines([b"\xff" * 10, b"", b"", b"", b"\xff" * 10], 5)) == 2


def test_barcode_evidence():
    page = Image.new("L", (400, 400), 0)
    bars = Image.new("L", (400, 400), 0)
    draw = ImageDraw.Draw(bars)
    for x in range(20, 380, 8):
        draw.rectangle((x, 100, x + 3, 200), fill=255)
    box = detect.Box(0, 0, 400, 400)
    assert detect.has_barcode(bars, box)
    assert not detect.has_barcode(page, box)


def test_box_helpers():
    box = detect.Box(10, 20, 110, 170)
    assert (box.width, box.height, box.area, box.ratio()) == (100, 150, 15000, 1.5)
    assert box.grown(30, (120, 180)) == detect.Box(0, 0, 120, 180)
    assert box.fractions((200, 300)) == (0.05, 20 / 300, 0.5, 0.5)


# --- crop handles ---------------------------------------------------------------------

CROP = (0.2, 0.2, 0.4, 0.4)


@pytest.mark.parametrize(
    "x, y, handle",
    [
        (0.4, 0.4, "move"),
        (0.05, 0.05, "new"),
        (0.2, 0.4, "w"),
        (0.6, 0.4, "e"),
        (0.4, 0.2, "n"),
        (0.4, 0.6, "s"),
        (0.2, 0.2, "nw"),
        (0.61, 0.59, "se"),
        (0.6, 0.2, "ne"),
        (0.2, 0.6, "sw"),
    ],
)
def test_handle_at(x, y, handle):
    assert cropping.handle_at(CROP, x, y, 0.02, 0.02) == handle


def test_drag_move_stays_on_the_page():
    assert cropping.dragged(CROP, "move", (0.4, 0.4), 0.1, -0.1) == pytest.approx(
        (0.3, 0.1, 0.4, 0.4)
    )
    assert cropping.dragged(CROP, "move", (0.4, 0.4), 0.9, 0.9) == pytest.approx(
        (0.6, 0.6, 0.4, 0.4)
    )


def test_drag_edges():
    assert cropping.dragged(CROP, "se", (0.6, 0.6), 0.1, 0.2) == pytest.approx(
        (0.2, 0.2, 0.5, 0.6)
    )
    assert cropping.dragged(CROP, "nw", (0.2, 0.2), -0.5, 0.1) == pytest.approx(
        (0.0, 0.3, 0.6, 0.3)
    )
    # an edge cannot pass the opposite one
    small = cropping.dragged(CROP, "e", (0.6, 0.4), -0.9, 0)
    assert small[2] == pytest.approx(cropping.MIN_SIDE)


def test_drag_new_area():
    assert cropping.dragged(CROP, "new", (0.8, 0.9), -0.3, -0.4) == pytest.approx(
        (0.5, 0.5, 0.3, 0.4)
    )
    # a click without moving: the smallest area, on the page
    assert cropping.dragged(CROP, "new", (1.0, 1.0), 0, 0) == pytest.approx(
        (0.98, 0.98, 0.02, 0.02)
    )


# --- targets and the label ------------------------------------------------------------


def keys(found) -> list[str]:
    return [t.key for t in found]


def test_targets_per_printer():
    cab, system, vc = (get_backend(i).capabilities for i in ("cab", "system", "vc500w"))
    assert keys(targets.targets_for(cab, 50)) == ["4x6", "100x150", "100x200", "a6", "roll62"]
    assert keys(targets.targets_for(system, 50)) == [t.key for t in targets.TARGETS]
    assert targets.targets_for(vc, 25) == [targets.Target("tape", 25)]


def test_matching_target(make_label):
    found = targets.targets_for(get_backend("system").capabilities, 50)
    label = Label.new_for("system")
    label.set_media_size(105, 148)
    assert targets.matching(found, label).key == "a6"
    label.set_media_size(50, 25)
    assert targets.matching(found, label).key == "4x6"


def page_element(crop=TOP, rotation=0) -> PageElement:
    image = page_image("top", 60)
    return PageElement(data=b64(encode(image)), name="dhl.png", crop=list(crop), rotation=rotation)


def test_upright():
    portrait = targets.TARGETS[0]
    tape = targets.Target("tape", 50)
    assert targets.upright(page_element(TOP), portrait) == 90  # sideways: turn it
    assert targets.upright(page_element(TOP_LEFT), portrait) == 0
    assert targets.upright(page_element(TOP), tape) == 0
    assert targets.upright(page_element(TOP_LEFT), tape) == 90


def test_label_for_a_die_cut_printer():
    element = page_element(rotation=90)
    label = targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 1.5)
    assert label.media_size() == (101.6, 152.4)
    assert (label.orientation, label.auto_length, label.margin_mm) == ("across", False, 0.0)
    assert label.elements == [element]


def test_endless_label_follows_the_area():
    element = page_element(rotation=90)  # 2:3 upright
    roll = targets.TARGETS[-1]
    label = targets.make_label(get_backend("system"), element, roll, 1.0)
    width, height = label.media_size()
    assert width == 62
    assert height == pytest.approx(60 * 1.5 + 2, abs=0.2)


def test_endless_label_within_the_limits():
    element = page_element(crop=(0.0, 0.0, 0.01, 1.0))  # very tall
    label = targets.make_label(get_backend("system"), element, targets.TARGETS[-1], 1.0)
    assert label.media_size()[1] == 1000.0


def test_label_on_tape():
    element = page_element()
    label = targets.make_label(get_backend("vc500w"), element, targets.Target("tape", 50), 1.5)
    assert (label.tape_mm, label.auto_length, label.elements) == (50, True, [element])


# --- the element in a label file and in the render -------------------------------------


def test_page_element_roundtrip(tmp_path):
    element = page_element(crop=(0.1, 0.2, 0.3, 0.4), rotation=270)
    element.fit = "fill"
    label = Label(printer="cab", elements=[element])
    label.save(tmp_path / "x.etiketto")
    loaded = Label.load(tmp_path / "x.etiketto").elements[0]
    assert loaded == element


@pytest.mark.parametrize(
    "change",
    [
        {"crop": [0, 0, 1]},
        {"crop": "all"},
        {"crop": [0, 0, "1", 1]},
        {"rotation": 45},
        {"rotation": True},
        {"fit": "stretch"},
        {"page": 1.5},
        {"page": True},
        {"data": 5},
    ],
)
def test_page_element_validation(change):
    item = {"kind": "page", "data": "", "name": "x.pdf", **change}
    with pytest.raises(LabelError):
        Label.from_dict({"elements": [item]})


def test_page_element_is_repaired():
    item = {"kind": "page", "page": -3, "crop": [0.5, 1.5, 0.9, 0.0]}
    element = Label.from_dict({"elements": [item]}).elements[0]
    assert element.page == 0
    assert element.crop == [0.5, 0.99, 0.5, 0.01]


def test_page_element_from_file(tmp_path, monkeypatch):
    path = tmp_path / "label.pdf"
    path.write_bytes(b"%PDF-1.4")
    element = PageElement.from_file(path)
    assert (element.name, base64.b64decode(element.data)) == ("label.pdf", b"%PDF-1.4")
    import etiketto.model as model

    monkeypatch.setattr(model, "MAX_FILE_BYTES", 3)
    with pytest.raises(LabelError):
        PageElement.from_file(path)


def test_render_draws_the_area_at_the_label_size():
    """No scaling after drawing: the area fills the content box exactly, in
    whole black and white dots."""
    element = page_element(rotation=90)
    label = targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 1.5)
    dpi = 304.8
    rendered = render(label, dpi=dpi, safe_mm=1.5)
    assert rendered.image.size == (mm_to_px(101.6, dpi), mm_to_px(152.4, dpi))
    drawn = source.draw(
        element.data, 0, tuple(element.crop), 90, (1219 - 2 * 18, 1829 - 2 * 18), "fit"
    )
    assert drawn.size[0] == 1219 - 2 * 18 or drawn.size[1] == 1829 - 2 * 18
    # nothing outside the safety margin
    inner = rendered.image.crop((18, 18, 1219 - 18, 1829 - 18))
    blank = Image.new("RGB", rendered.image.size, "white")
    blank.paste(inner, (18, 18))
    assert blank.tobytes() == rendered.image.tobytes()


@needs_poppler
def test_render_pdf_area_keeps_whole_dots(pdf):
    element = PageElement(data=pdf, name="a.pdf", crop=list(TOP_LEFT))
    label = targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 1.5)
    for dpi in (203.2, 304.8, 599.44):
        image = render(label, dpi=dpi, safe_mm=1.5).image
        assert values(image) == {0, 255}


def test_render_warnings(monkeypatch):
    label = Label(printer="cab", auto_length=False, elements=[PageElement(data=b64(b"junk"))])
    warnings = render(label, dpi=50).warnings
    assert [w.key for w in warnings] == ["page_unreadable"]

    def missing(*_args, **_kwargs):
        raise SourceError("no_pdf")

    monkeypatch.setattr(source, "draw", missing)
    assert [w.key for w in render(label, dpi=50).warnings] == ["pdf_missing"]


def test_render_on_tape_grows_with_the_area():
    element = page_element()
    label = targets.make_label(get_backend("vc500w"), element, targets.Target("tape", 25), 1.5)
    rendered = render(label, dpi=100, safe_mm=1.5)
    inner = mm_to_px(25, 100) - 2 * mm_to_px(1.5, 100)
    w, h = targets.area_size(element)
    assert rendered.image.width == pytest.approx(inner * w / h + 2 * mm_to_px(1.5, 100), abs=2)
