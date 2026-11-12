"""Shipping labels: reading PDF pages and images, finding the label on a
page, the crop handles, label sizes and the label made of the area.

The documents are synthetic (shipping_samples.py): A4 pages with a 4 × 6 in
label in a corner or sideways in the upper half, a receipt and instructions,
as PDF or as a noisy scan.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw
from shipping_samples import (
    A4,
    CODE_RUNS,
    CODE_Y,
    LABEL,
    _text,
    a4_pdf,
    code_label,
    draw_label,
    drawn_page,
    encode,
    gappy_label,
    instructions,
    page_image,
    page_pdf,
    raw_pdf,
    scanned_label,
)

from tavletta.backends import get_backend
from tavletta.backends.base import PrintOptions
from tavletta.model import Label, LabelError, PageElement, TextElement
from tavletta.render import mm_to_px, render
from tavletta.shipping import crop as cropping
from tavletta.shipping import detect, source, targets
from tavletta.shipping.source import SourceError

pytest.importorskip("cairo")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def values(image: Image.Image) -> set[int]:
    return set(image.convert("L").tobytes())


@pytest.fixture(autouse=True)
def _fresh_caches():
    source.clear_caches()
    yield
    source.clear_caches()


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


def covers(crop, label, slack=0.03, inset=0.002) -> bool:
    """The crop holds all of the label (up to inset on each edge: content
    without a frame may end a little inside the label) and not much more."""
    x, y, w, h = crop
    lx, ly, lw, lh = label
    inside = x <= lx + inset and y <= ly + inset and x + w >= lx + lw - inset
    inside = inside and y + h >= ly + lh - inset
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
    # an image whose comment holds the PDF header stays an image
    assert not source.is_pdf(b"\xff\xd8\xff\xfe\x00\x10%PDF-1.4 hello")
    assert not source.is_pdf(b"x" * 1024 + b"%PDF-1.4")


def test_image_with_pdf_header_in_its_comment():
    jpeg = encode(square_image(), "JPEG")
    raw = jpeg[:2] + b"\xff\xfe\x00\x10%PDF-1.4 hello" + jpeg[2:]
    info = source.page_info(b64(raw))
    assert (info.width, info.pdf) == (40, False)


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


# --- memory, image modes, pages ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["1", "L"])
def test_black_and_white_and_grey_images_stay_small(mode):
    data = b64(encode(square_image(mode)))
    drawn = source.draw(data, 0, detect.FULL, 0, (40, 60))
    assert drawn.mode == mode  # not three times (24 times) the memory as RGB
    reduced = source.draw(data, 0, detect.FULL, 0, (20, 30))
    assert reduced.mode == "L"
    assert source.draw(b64(encode(square_image())), 0, detect.FULL, 0, (40, 60)).mode == "RGB"


def test_grey_palette_and_transparent_grey_images():
    palette = square_image("L").convert("P")
    assert source.draw(b64(encode(palette)), 0, detect.FULL, 0, (40, 60)).mode == "L"
    clear = Image.new("LA", (10, 10), (0, 0))
    drawn = source.draw(b64(encode(clear)), 0, detect.FULL, 0, (10, 10))
    assert drawn.mode == "L" and values(drawn) == {255}


def test_16_bit_and_float_images_keep_their_greys():
    grey = Image.new("I;16", (4, 1))
    for x, value in enumerate((0, 1000, 30000, 65535)):
        grey.putpixel((x, 0), value)
    drawn = source.draw(b64(encode(grey)), 0, detect.FULL, 0, (4, 1))
    assert list(drawn.tobytes()) == [0, 3, 116, 255]  # not all white above 255
    floats = Image.new("F", (3, 1))
    for x, value in enumerate((0.0, 0.5, 1.0)):
        floats.putpixel((x, 0), value)
    drawn = source.draw(b64(encode(floats, "TIFF")), 0, detect.FULL, 0, (3, 1))
    assert list(drawn.tobytes()) == [0, 127, 255]
    wide = Image.new("I", (2, 1))
    wide.putpixel((1, 0), 1000)
    assert list(source.draw(b64(encode(wide, "TIFF")), 0, detect.FULL, 0, (2, 1)).tobytes()) == [
        0,
        255,
    ]


def test_too_many_pixels_to_decode(monkeypatch):
    data = b64(encode(square_image(size=(100, 100))))
    monkeypatch.setattr(source, "MAX_SOURCE_PIXELS", 9_999)
    with pytest.raises(SourceError) as error:
        source.page_info(data)
    assert error.value.code == "too_large"


def test_decompression_bomb(monkeypatch):
    data = b64(encode(square_image(size=(100, 100))))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(SourceError) as error:
        source.page_info(data)
    assert error.value.code == "too_large"


def test_only_small_drawings_are_kept(monkeypatch):
    data = b64(encode(square_image()))
    monkeypatch.setattr(source, "CACHE_ITEM_BYTES", 40 * 60 * 4)  # RGB: 4 bytes a pixel
    small = source.draw(data, 0, detect.FULL, 0, (40, 60))
    assert source.draw(data, 0, detect.FULL, 0, (40, 60)) is small
    large = source.draw(data, 0, detect.FULL, 0, (80, 120))
    assert source.draw(data, 0, detect.FULL, 0, (80, 120)) is not large


def test_cache_is_bounded_by_bytes():
    cache = source._Cache(10, max_bytes=10)
    for key in "abc":
        cache.put(key, b"xxxx")
    assert len(cache) == 2 and cache.get("a") is None and cache.get("c") == b"xxxx"
    cache.clear()
    assert len(cache) == 0


def tiff_pages() -> bytes:
    first = square_image("L", (40, 60))
    second = Image.new("L", (30, 20), 0)
    buf = io.BytesIO()
    first.save(buf, format="TIFF", save_all=True, append_images=[second])
    return buf.getvalue()


def test_tiff_pages():
    data = b64(tiff_pages())
    first, second = source.page_info(data, 0), source.page_info(data, 1)
    assert (first.pages, first.width, second.width, second.height) == (2, 40, 30, 20)
    assert values(source.draw(data, 1, detect.FULL, 0, (30, 20))) == {0}
    assert source.page_info(data, 9) == second  # a page after the last is the last


def test_copy_of_a_tiff_page_leaves_the_cache_alone():
    """The copy runs in the background: it neither fills nor empties the
    cache of the preview's image."""
    data = b64(tiff_pages())
    source.page_info(data, 0)  # the preview's image
    kept = list(source._pictures._values)
    source.only_page(data, 1)
    assert list(source._pictures._values) == kept


def test_image_decoded_across_a_clear_is_not_kept(monkeypatch):
    """A dialog closed (caches cleared) while an image was being decoded:
    the image is not put back."""
    data = b64(encode(square_image()))
    real = source._decode_frame

    def clearing(raw, frame):
        result = real(raw, frame)
        source.clear_caches()  # the dialog closes meanwhile
        return result

    monkeypatch.setattr(source, "_decode_frame", clearing)
    source.page_info(data)
    assert len(source._pictures) == 0


def test_only_the_page_of_a_tiff():
    data, page = source.only_page(b64(tiff_pages()), 1)
    assert page == 0
    info = source.page_info(data)
    assert (info.pages, info.width, info.height) == (1, 30, 20)
    single = b64(encode(square_image()))
    assert source.only_page(single, 0) == (single, 0)


@needs_poppler
def test_only_the_page_of_a_pdf(pdf):
    data, page = source.only_page(pdf, 1)
    assert page == 0 and len(data) < len(pdf)
    info = source.page_info(data)
    assert info.pages == 1 and (info.width, info.height) == pytest.approx(A4)
    original = source.draw(pdf, 1, TOP_LEFT, 0, (600, 900))
    assert source.draw(data, 0, TOP_LEFT, 0, (600, 900)).tobytes() == original.tobytes()


@needs_poppler
def test_whole_pdf_kept_if_the_copy_differs(pdf, monkeypatch):
    monkeypatch.setattr(source, "_same_drawing", lambda a, b: False)
    assert source.only_page(pdf, 1) == (pdf, 1)

    def broken(_page):
        raise SourceError("unreadable")

    monkeypatch.setattr(source, "_pdf_copy", broken)
    assert source.only_page(pdf, 1) == (pdf, 1)


@needs_poppler
def test_copy_check_compares_the_drawing(pdf):
    doc = source._open_pdf(base64.b64decode(pdf))
    page, _count = source._pdf_page(doc, 0)
    other = source._open_pdf(a4_pdf("top"))
    assert source._same_drawing(page, page)
    assert not source._same_drawing(page, source._pdf_page(other, 0)[0])
    label = source._open_pdf(page_pdf(lambda c, i: None, LABEL))
    assert not source._same_drawing(page, source._pdf_page(label, 0)[0])


@needs_poppler
def test_lines_thinner_than_a_dot_stay():
    def hairlines(ctx, _index):
        for i, width in enumerate((0.05, 0.1, 0.3)):
            ctx.set_line_width(width)
            ctx.move_to(10, 20 + i * 10)
            ctx.line_to(250, 20 + i * 10)
            ctx.stroke()
        for i in range(40):  # 0.25 mm bars and gaps
            ctx.rectangle(20 + i * 0.5 * 72 / 25.4, 100, 0.25 * 72 / 25.4, 40)
        ctx.fill()

    data = b64(page_pdf(hairlines, LABEL))
    dpi = 203.2
    image = source.draw(data, 0, detect.FULL, 0, (mm_to_px(101.6, dpi), mm_to_px(152.4, dpi)))
    assert values(image) == {0, 255}
    grey = image.convert("L")
    for i in range(3):
        y = round((20 + i * 10) * dpi / 72)
        rows = grey.crop((100, y - 3, 400, y + 4))
        assert rows.getextrema()[0] == 0  # every line is drawn, at least one dot
    # bars keep their width: one bar and one gap every 4 (or 3 or 5) dots
    row = grey.crop((60, round(120 * dpi / 72), 300, round(120 * dpi / 72) + 1)).tobytes()
    bars = [len(run) for run in row.split(b"\xff") if run]
    assert set(bars) <= {1, 2, 3}


def test_thin_lines_are_added_only_away_from_drawn_ink():
    sharp = Image.new("RGB", (10, 3), "white")
    sharp.putpixel((2, 1), (0, 0, 0))
    smooth = Image.new("L", (10, 3), 255)
    for x in (3, 7):  # an edge next to the bar, and a lost thin line
        smooth.putpixel((x, 1), 200)
    result = source.thin_lines(sharp, source.ink(smooth)).convert("L")
    assert result.getpixel((3, 1)) == 255 and result.getpixel((7, 1)) == 0


@needs_poppler
def test_each_thread_has_its_own_document(pdf):
    import threading

    documents = []

    def use():
        source.page_info(pdf)
        documents.append(source._pdf(pdf))

    use()
    thread = threading.Thread(target=use)
    thread.start()
    thread.join()
    assert documents[0] is not documents[1]
    assert source._pdf(pdf) is documents[0]


@pytest.mark.parametrize(
    "kind",
    [
        {},
        {"media": (0, 0, 612, 792)},
        {"media": (10.5, 20.25, 605.5, 861.25)},
        {"rotate": 90},
        {"rotate": 180},
        {"rotate": 270},
        {"crop": (20, 30, 400, 700)},
    ],
    ids=[
        "other rounding",
        "letter",
        "offset",
        "rotate 90",
        "rotate 180",
        "rotate 270",
        "crop box",
    ],
)
@needs_poppler
def test_only_the_page_of_pdfs_from_other_programs(kind):
    """Copies drawn the same but for rounding (positions written to fewer
    decimals) keep only the page."""
    data = b64(raw_pdf(**kind))
    copy, page = source.only_page(data, 1)
    assert copy is not data and page == 0
    original = source.page_info(data, 1)
    info = source.page_info(copy)
    assert info.pages == 1
    assert (info.width, info.height) == pytest.approx((original.width, original.height))


@needs_poppler
@pytest.mark.skipif(not shutil.which("gs"), reason="Ghostscript missing")
def test_only_the_page_of_a_ghostscript_pdf(tmp_path):
    source_pdf, made = tmp_path / "in.pdf", tmp_path / "gs.pdf"
    source_pdf.write_bytes(a4_pdf("top-left", pages=3))
    command = ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=pdfwrite"]
    subprocess.run([*command, "-sOutputFile=%s" % made, str(source_pdf)], check=True)
    data = b64(made.read_bytes())
    copy, page = source.only_page(data, 2)
    assert copy is not data and source.page_info(copy).pages == 1


def _page_of(raw: bytes):
    return source._pdf_page(source._open_pdf(raw), 0)[0]


def _label_page(extra):
    def draw(c, _index):
        draw_label(c, 30, 30, LW, LH)
        _text(c, 360, 40, 30, 8, 30)
        extra(c)

    return page_pdf(draw)


def _digits(text):
    def draw(c):
        c.select_font_face("Sans")
        c.set_font_size(12)
        c.move_to(300, 400)
        c.show_text(text)

    return draw


def _small_code(c):
    for i in range(6):  # a 6 mm square code
        for j in range(6):
            if (i + j) % 2:
                c.rectangle(400 + i * 2.8, 400 + j * 2.8, 2.8, 2.8)
    c.fill()


def _hairline(c):
    c.set_line_width(0.3)
    c.move_to(40, 450)
    c.line_to(460, 450)
    c.stroke()


@needs_poppler
@pytest.mark.parametrize(
    "first, second",
    [
        (_digits("PLZ 8004"), _digits("PLZ 8001")),
        (lambda c: None, _small_code),
        (lambda c: None, _hairline),
    ],
    ids=["another digit", "a missing small code", "a missing thin line"],
)
def test_small_changes_in_one_place_are_no_copy(first, second):
    """Differences in one small place are caught by tiles, however few
    pixels they are on the whole page."""
    a, b = _page_of(_label_page(first)), _page_of(_label_page(second))
    assert source._same_drawing(a, a)
    assert not source._same_drawing(a, b)


def test_shifted_pixels_are_rounding():
    a = Image.new("L", (128, 128), 255)
    ImageDraw.Draw(a).rectangle((20, 20, 60, 100), fill=0)
    shifted = Image.new("L", (128, 128), 255)
    ImageDraw.Draw(shifted).rectangle((21, 20, 61, 100), fill=0)  # one pixel over
    assert source._unexplained(a.convert("RGB"), shifted.convert("RGB"), 0).getbbox() is None
    missing = shifted.copy()
    ImageDraw.Draw(missing).rectangle((90, 90, 93, 93), fill=0)  # a dot more
    mask = source._unexplained(a.convert("RGB"), missing.convert("RGB"), 0)
    assert source._changed_tile(mask)
    assert not source._changed_tile(Image.new("L", (10, 10), 0))


def test_differing_pixels():
    a = Image.new("RGB", (10, 10), "white")
    b = a.copy()
    b.putpixel((1, 1), (253, 255, 255))  # 2 levels: rounding
    b.putpixel((2, 2), (0, 255, 255))
    assert source._differing(a, b, 0) == 2
    assert source._differing(a, b, 2) == 1


def test_a_label_at_600_dpi_is_kept():
    """100 × 150 mm at 24 dots/mm, as the preview of a 600 dpi printer draws it."""
    data = b64(encode(square_image()))
    box = (2400, 3600)
    drawn = source.draw(data, 0, detect.FULL, 0, box)
    assert source._image_bytes(drawn) == 2400 * 3600 * 4  # RGB takes 4 bytes a pixel
    assert source.draw(data, 0, detect.FULL, 0, box) is drawn
    grey = source.draw(b64(encode(square_image("L"))), 0, detect.FULL, 0, (20, 30))
    assert source._image_bytes(grey) == 600


def test_image_without_turning_is_not_copied(monkeypatch):
    calls = []
    monkeypatch.setattr(source.ImageOps, "exif_transpose", lambda image: calls.append(1))
    source.page_info(b64(encode(square_image())))
    assert calls == []


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
    assert found.how == "block" and found.certain
    # all four edges: the label's content reaches about its frameless edges
    assert covers(found.crop, TOP, slack=0.03, inset=0.012)


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


# --- detection: harder pages ------------------------------------------------------------

LW, LH = LABEL


def frac(x, y, w, h, size=A4):
    """A rectangle in points as fractions of the page."""
    return (x / size[0], y / size[1], w / size[0], h / size[1])


def printed(draw, size=A4):
    """Where draw(ctx) puts ink on the page, as fractions: the area a
    label without a frame should get (its print may end before its edges)."""
    mask = detect.ink_mask(drawn_page(draw, size))
    box = detect.tight(mask, detect.Box(0, 0, *mask.size))
    assert box
    return box.fractions(mask.size)


def gappy(x=30, y=30):
    return printed(lambda c: gappy_label(c, x, y))


def plain(x, y, w, h):
    """Where a label without a frame has print."""
    return printed(lambda c: draw_label(c, x, y, w, h, False))


@pytest.mark.parametrize(
    "draw, label",
    [
        (lambda c: gappy_label(c, 30, 30), gappy()),
        (lambda c: (gappy_label(c, 30, 30), instructions(c)), gappy()),
        (lambda c: (gappy_label(c, 150, 30), instructions(c)), gappy(150)),
    ],
    ids=["alone", "instructions below", "centred"],
)
def test_label_with_blank_space_inside_is_found_whole(draw, label):
    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "block" and found.certain
    assert covers(found.crop, label, slack=0.03, inset=0.012)


def two_best(found):
    return found.candidates[:2]


def test_two_alike_labels_without_frame_side_by_side():
    left, right = plain(20, 20, LW - 20, LH), plain(A4[0] - LW, 20, LW - 20, LH)

    def draw(c):
        draw_label(c, 20, 20, LW - 20, LH, False)
        draw_label(c, A4[0] - LW, 20, LW - 20, LH, False)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "block" and not found.certain
    best = two_best(found)
    for label in (left, right):
        assert any(covers(crop, label, slack=0.03, inset=0.012) for crop in best)


def test_two_different_labels_without_frame_side_by_side():
    left, right = plain(20, 20, LW - 20, LH), gappy(A4[0] - LW, 20)

    def draw(c):
        draw_label(c, 20, 20, LW - 20, LH, False)
        gappy_label(c, A4[0] - LW, 20)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "block" and not found.certain
    best = two_best(found)
    for label in (left, right):
        assert any(covers(crop, label, slack=0.03, inset=0.012) for crop in best)


def test_alike_framed_labels_need_a_check():
    def draw(c):
        for x in (10, 307):
            for y in (10, 430):
                draw_label(c, x, y, 278, 400)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "frame" and not found.certain
    labels = [frac(x, y, 278, 400) for x in (10, 307) for y in (10, 430)]
    assert sum(any(covers(c, label, 0.02) for c in found.candidates) for label in labels) >= 3


def test_border_around_the_page_is_not_the_label():
    def draw(c):
        c.set_line_width(1)
        c.rectangle(10, 10, A4[0] - 20, A4[1] - 20)
        c.stroke()
        draw_label(c, 30, 30, LW, LH)
        instructions(c)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "frame" and found.certain
    assert covers(found.crop, TOP_LEFT, slack=0.02)


def test_frame_of_a_table_does_not_hide_the_label():
    def draw(c):
        draw_label(c, 30, 30, LW, LH, False)
        c.set_line_width(1)
        c.rectangle(30, 500, 535, 300)
        c.stroke()
        for i in range(1, 6):
            c.move_to(30, 500 + i * 50)
            c.line_to(565, 500 + i * 50)
            c.stroke()

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "block"
    assert covers(found.crop, plain(30, 30, LW, LH), slack=0.03)


@pytest.mark.parametrize("frame, how", [(True, "frame"), (False, "content")])
def test_label_filling_the_page(frame, how):
    def draw(c):
        draw_label(c, 30, 30, 535, 780, frame)

    found = detect.detect(drawn_page(draw), 210)
    assert found.how == how
    assert covers(found.crop, printed(draw), slack=0.03)


A6 = (105 * 72 / 25.4, 148 * 72 / 25.4)


def a6_label(frame: bool, dpi: float = 200) -> Image.Image:
    """An image of only an A6 label: the shape of an A4 page."""
    return drawn_page(lambda c: draw_label(c, 8, 8, A6[0] - 16, A6[1] - 16, frame), A6, dpi)


@pytest.mark.parametrize("frame", [True, False])
def test_image_of_an_a6_label_is_the_label(frame):
    image = a6_label(frame)
    assert detect.is_sheet(image.size)  # by its shape it could be A4
    found = detect.detect(image)
    assert found.how in ("frame", "content")
    label = printed(lambda c: draw_label(c, 8, 8, A6[0] - 16, A6[1] - 16, frame), A6)
    assert covers(found.crop, label, slack=0.04)


def test_image_resolution_gives_the_page_size():
    buf = io.BytesIO()
    a6_label(False).save(buf, format="PNG", dpi=(200, 200))
    info = source.page_info(b64(buf.getvalue()))
    assert info.dpi == pytest.approx(200, abs=0.1)
    assert info.width_mm() == pytest.approx(105, abs=0.2)
    found = detect.detect(source.preview(b64(buf.getvalue()), 0, 1400), info.width_mm())
    assert found.how == "content" and found.crop[2] > 0.9
    # a program's default resolution says nothing about the page
    buf = io.BytesIO()
    a6_label(False).save(buf, format="PNG", dpi=(72, 72))
    assert source.page_info(b64(buf.getvalue())).width_mm() is None


def test_parts_of_the_label_do_not_make_it_uncertain():
    found = detect.detect(drawn_page(lambda c: gappy_label(c, 30, 30)), width_mm=210)
    assert found.certain and len(found.candidates) > 2


A5 = (148 * 72 / 25.4, 210 * 72 / 25.4)
A4_LANDSCAPE = (A4[1], A4[0])


def test_framed_a5_label_in_the_upper_half():
    """A frame may take half of the sheet (more than a block may)."""
    w, h = A5[1] - 20, A5[0] - 20

    def draw(c):
        draw_label(c, 10, 10, w, h)
        instructions(c, A4[1] / 2 + 40)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "frame" and found.certain
    assert covers(found.crop, frac(10, 10, w, h), slack=0.02)


def test_framed_a5_label_in_the_left_half_of_landscape_a4():
    w, h = A5[0] - 20, A5[1] - 20

    def draw(c):
        draw_label(c, 10, 10, w, h)
        _text(c, A5[0] + 30, 40, 40, 9, 60)

    found = detect.detect(drawn_page(draw, A4_LANDSCAPE), width_mm=297)
    assert found.how == "frame" and found.certain
    assert covers(found.crop, frac(10, 10, w, h, A4_LANDSCAPE), slack=0.02)


def test_framed_label_with_divider_lines_is_found_whole():
    """The sections between divider lines are frames too; the frame
    around them is the label."""
    mm = 72 / 25.4
    w, h = 100 * mm, 200 * mm

    def draw(c):
        draw_label(c, 30, 30, w, h)
        _text(c, 340, 40, 40, 8, 40)

    found = detect.detect(drawn_page(draw), width_mm=210)
    assert found.how == "frame" and found.certain
    assert covers(found.crop, frac(30, 30, w, h), slack=0.02)


def box(c, x, y, w, h):
    c.set_line_width(1.5)
    c.rectangle(x, y, w, h)
    c.stroke()


def detect_on(draw):
    return detect.detect(drawn_page(draw), width_mm=210)


def receipt(c, x=380):
    _text(c, x, 40, 25, 8, 28)


@pytest.mark.parametrize("x, y", [(40, 30), (10, 10)], ids=["inside", "in its corner"])
def test_label_in_a_larger_frame(x, y):
    """A framed label in a frame of more than half the sheet (with a
    receipt): the label, the large frame only later."""

    def draw(c):
        box(c, 10, 10, A4[0] - 20, A4[1] * 0.55)
        draw_label(c, x, y, LW, LH)
        receipt(c)
        instructions(c, 520)

    found = detect_on(draw)
    assert covers(found.crop, frac(x, y, LW, LH), slack=0.02)
    large = frac(10, 10, A4[0] - 20, A4[1] * 0.55)
    assert any(covers(crop, large, slack=0.02) for crop in found.candidates[1:])


def test_frameless_label_in_a_larger_frame():
    def draw(c):
        box(c, 10, 10, A4[0] - 20, A4[1] * 0.55)
        draw_label(c, 40, 30, LW, LH, False)
        receipt(c)
        instructions(c, 520)

    found = detect_on(draw)
    assert covers(found.crop, plain(40, 30, LW, LH), slack=0.03)


def both_offered(found, first, second, slack=0.02):
    """Two alike labels: not certain, each among the first suggestions."""
    assert not found.certain
    best = found.candidates[:2]
    for label in (first, second):
        assert any(covers(crop, label, slack=slack) for crop in best), label


def test_labels_sharing_a_side():
    def draw(c):
        draw_label(c, 20, 20, LW, LH)
        draw_label(c, 20 + LW, 20, LW - 30, LH)
        instructions(c)

    both_offered(detect_on(draw), frac(20, 20, LW, LH), frac(20 + LW, 20, LW - 30, LH))


def test_two_labels_in_a_common_frame():
    def draw(c):
        box(c, 10, 10, A4[0] - 20, LH + 40)
        draw_label(c, 25, 30, LW - 20, LH)
        draw_label(c, 25 + LW, 30, LW - 20, LH)
        instructions(c, 520)

    found = detect_on(draw)
    both_offered(found, frac(25, 30, LW - 20, LH), frac(25 + LW, 30, LW - 20, LH))


def test_stacked_labels_sharing_a_line():
    height = A4[1] / 2 - 15

    def draw(c):
        draw_label(c, 15, 15, A4[0] - 30, height)
        draw_label(c, 15, A4[1] / 2, A4[0] - 30, height)

    found = detect_on(draw)
    both_offered(found, frac(15, 15, A4[0] - 30, height), frac(15, A4[1] / 2, A4[0] - 30, height))


def test_grid_of_labels_sharing_lines():
    """2 × 2 labels whose lines are shared: cut into the four cells."""
    w, h = (A4[0] - 40) / 2, (A4[1] - 40) / 2

    def draw(c):
        for i in range(2):
            for j in range(2):
                draw_label(c, 20 + i * w, 20 + j * h, w, h)

    found = detect_on(draw)
    assert found.how == "frame" and not found.certain
    cells = [frac(20 + i * w, 20 + j * h, w, h) for i in range(2) for j in range(2)]
    assert any(covers(found.crop, cell, slack=0.02) for cell in cells)
    others = [cell for cell in cells if not covers(found.crop, cell, slack=0.02)]
    assert any(covers(crop, cell, 0.02) for crop in found.candidates[1:3] for cell in others)


def test_box_to_cut_along_offers_the_label_next():
    """A label in a box a little larger (to cut along): the box, then the
    label itself, and the user should check."""

    def draw(c):
        box(c, 20, 20, LW + 24, LH + 24)
        draw_label(c, 32, 32, LW, LH)
        receipt(c)
        instructions(c, 520)

    found = detect_on(draw)
    assert not found.certain
    assert covers(found.crop, frac(20, 20, LW + 24, LH + 24), slack=0.02)
    assert covers(found.candidates[1], frac(32, 32, LW, LH), slack=0.02)


def test_boxes_and_dividers_inside_a_label():
    """A label with a vertical divider and an address box: the label, not
    a part of it."""

    def draw(c):
        draw_label(c, 30, 30, LW, LH)
        c.set_line_width(1.5)
        c.move_to(30 + LW * 0.6, 30 + LH * 0.5)
        c.line_to(30 + LW * 0.6, 30 + LH)
        c.stroke()
        box(c, 40, 30 + LH * 0.22, LW * 0.5, LH * 0.25)
        receipt(c, 360)
        instructions(c)

    found = detect_on(draw)
    assert found.how == "frame" and found.certain
    assert covers(found.crop, TOP_LEFT, slack=0.02)


@pytest.mark.parametrize("frame", [True, False])
def test_label_beside_a_stub_in_one_frame(frame):
    """A band across the page, divided into the label and a stub."""

    def draw(c):
        box(c, 20, 20, A4[0] - 40, LH)
        c.move_to(20 + LW, 20)
        c.line_to(20 + LW, 20 + LH)
        c.stroke()
        draw_label(c, 20, 20, LW, LH, frame)
        _text(c, 30 + LW, 40, 30, 8, 30)
        instructions(c, 500)

    found = detect_on(draw)
    assert covers(found.crop, frac(20, 20, LW, LH), slack=0.02)


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
    return PageElement(
        data=b64(encode(image)), name="parcel.png", crop=list(crop), rotation=rotation
    )


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
    label.save(tmp_path / "x.tavletta")
    loaded = Label.load(tmp_path / "x.tavletta").elements[0]
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


def test_rotation_is_a_whole_number():
    item = {"kind": "page", "rotation": 90.0}
    element = Label.from_dict({"elements": [item]}).elements[0]
    assert element.rotation == 90 and isinstance(element.rotation, int)


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
    import tavletta.model as model

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


def bars_and_grey(grey_text: bool) -> Image.Image:
    """A 300 dpi scan of a label: bars 3 dots wide, and grey text if asked."""
    image = Image.new("L", (1200, 1800), 255)
    draw = ImageDraw.Draw(image)
    for x in range(100, 1100, 8):
        draw.rectangle((x, 400, x + 2, 900), fill=0)
    if grey_text:
        for y in range(1000, 1700, 20):  # lines of 55 % grey, 6 dots high
            draw.rectangle((100, y, 1100, y + 5), fill=140)
    return image


def mono_label(image: Image.Image):
    element = PageElement(data=b64(encode(image)), name="label.png")
    return targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 1.5)


def test_reduced_image_is_black_and_white_on_a_mono_printer():
    """An image reduced to the label has grey edges; a black and white
    printer gets a threshold, not a dot pattern of dithered grey."""
    label = mono_label(bars_and_grey(False))
    dpi = 203.2
    assert len(values(render(label, dpi=dpi, safe_mm=1.5).image)) > 2  # grey edges
    assert values(render(label, dpi=dpi, safe_mm=1.5, mono=True).image) == {0, 255}
    printed = get_backend("cab").prepare(label, PrintOptions(), 1.5)
    assert values(printed) == {0, 255}


def test_grey_content_is_dithered_not_lost():
    """Grey text lighter than the threshold would vanish: an area with grey
    of its own keeps its greys for the printer's dithering."""
    label = mono_label(bars_and_grey(True))
    rendered = render(label, dpi=203.2, safe_mm=1.5, mono=True).image
    assert len(values(rendered)) > 2
    printed = get_backend("cab").prepare(label, PrintOptions(), 1.5).convert("L")
    text = printed.crop((200, 1100, 1000, 1500)).tobytes()  # the grey lines
    black = text.count(0) / len(text)
    assert 0.05 < black < 0.5  # dithered dots, not blank paper and not solid black


def runs(row: bytes) -> int:
    """Runs of black and white between the first and the last change."""
    changes = sum(1 for a, b in zip(row, row[1:], strict=False) if (a < 128) != (b < 128))
    return max(0, changes - 1)


def code_runs(raw: bytes, name: str) -> list[int]:
    """The bars and spaces of code_label() as a black and white printer
    prints them (cab, 12 dots/mm), counted on four rows."""
    backend = get_backend("cab")
    element = PageElement(data=b64(raw), name=name)
    label = targets.make_label(backend, element, targets.TARGETS[0], 0)
    printed = backend.prepare(label, PrintOptions(), 0).convert("L")
    scale = printed.width / LABEL[0]
    left, right = round(8 * scale), round((LABEL[0] - 4) * scale)
    rows = (round((CODE_Y + k) * scale) for k in (10, 25, 40, 55))
    return [runs(printed.crop((left, y, right, y + 1)).tobytes()) for y in rows]


@pytest.mark.parametrize("dpi", [100, 150, 200, 300])
@pytest.mark.parametrize("kind", ["png", "jpeg", "soft", "real"])
def test_scanned_barcode_keeps_its_bars_on_a_mono_printer(kind, dpi):
    """A clean, a JPEG, a soft and a noisy scan at 100 to 300 dpi: every bar
    and space comes out (not dithered into ragged dots)."""
    raw = scanned_label(dpi, kind)
    assert code_runs(raw, "label.png") == [CODE_RUNS] * 4


@pytest.mark.parametrize("logo", [0, 20, 60, 90])
def test_grey_logo_does_not_dither_the_barcode(logo):
    """A grey logo (up to 5 % of the label) keeps its grey; the bars next
    to it are still thresholded."""
    raw = scanned_label(300, "png", logo)
    assert code_runs(raw, "label.png") == [CODE_RUNS] * 4
    if logo:
        element = PageElement(data=b64(raw), name="label.png")
        label = targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 0)
        image = render(label, dpi=304.8, safe_mm=0, mono=True).image.convert("L")
        scale = image.width / LABEL[0]
        middle = round((20 + logo / 2) * scale), round((30 + logo / 2) * scale)
        assert 0 < image.getpixel(middle[::-1]) < 255  # grey, for the dithering


@needs_poppler
def test_vector_barcode_on_a_mono_printer():
    data = page_pdf(lambda c, _i: code_label(c, 60, grey_text=True), LABEL)
    assert code_runs(data, "label.pdf") == [CODE_RUNS] * 4


def test_levels_of_a_scan():
    from tavletta.shipping import mono

    image = Image.new("L", (100, 100), 230)  # grey paper
    ImageDraw.Draw(image).rectangle((10, 10, 40, 90), fill=60)  # grey ink
    assert mono.levels(image) == (60, 230)
    # a large grey area is not the black of the image
    ImageDraw.Draw(image).rectangle((50, 0, 99, 99), fill=150)
    assert mono.levels(image)[0] == 60
    assert mono.neighbourhood(1) == 5 and mono.neighbourhood(3) == 11
    assert mono.neighbourhood(30) == mono.MAX_NEAR


def test_black_and_white_keeps_own_grey():
    from tavletta.shipping import mono

    image = Image.new("L", (200, 200), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 60, 190), fill=0)  # black
    draw.rectangle((120, 10, 190, 190), fill=150)  # a grey band
    result = mono.black_and_white(image)
    assert result.getpixel((30, 100)) == 0 and result.getpixel((90, 100)) == 255
    assert result.getpixel((150, 100)) == 150  # left for the dithering
    flat = Image.new("L", (50, 50), 200)  # no black and white to tell apart
    assert set(mono.black_and_white(flat).tobytes()) == {255}


def _shaded_label() -> Image.Image:
    """Shaded boxes (85 to 45 % grey) with black text in them, grey text,
    and bars below, as a 300 dpi image."""
    image = Image.new("L", (1200, 1000), 255)
    draw = ImageDraw.Draw(image)
    for i, level in enumerate((217, 191, 153, 115)):
        top = 40 + i * 130
        draw.rectangle((40, top, 560, top + 100), fill=level)
        for x in range(60, 300, 16):  # black "letters" in the box
            draw.rectangle((x, top + 30, x + 5, top + 60), fill=0)
    for y in range(600, 640, 8):  # light grey text lines (#aaa to #ccc)
        draw.rectangle((640, y, 1100, y + 3), fill=190)
    for x in range(100, 1100, 8):
        draw.rectangle((x, 760, x + 2, 960), fill=0)
    return image


def test_shaded_boxes_keep_their_grey():
    """Light shading is not turned solid black by a tile's own levels, grey
    stays grey (for the dithering), black text in it stays black with
    paper around it, light grey text is kept."""
    from tavletta.shipping import mono

    result = mono.black_and_white(_shaded_label())
    for i, level in enumerate((217, 191, 153, 115)):
        top = 40 + i * 130
        inside = result.crop((330, top + 10, 550, top + 90))
        assert set(inside.tobytes()) == {level}, level  # grey, not black or white
        assert result.getpixel((62, top + 45)) == 0  # the letter
        assert result.getpixel((58, top + 45)) == 255  # paper right beside it
    assert result.getpixel((700, 601)) == 190  # light grey text
    bars = result.crop((100, 800, 1100, 900))
    assert set(bars.tobytes()) == {0, 255}


def test_a_line_of_text_is_no_barcode():
    """Rows through many letters are a barcode only over a tile's height:
    grey in a line of text keeps its grey."""
    from tavletta.shipping import mono

    image = Image.new("L", (1200, 1000), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 100, 1160, 160), fill=150)  # a grey band ...
    for x in range(60, 1100, 14):  # ... with a line of black letters, 20 rows high
        draw.rectangle((x, 120, x + 4, 139), fill=0)
    for x in range(60, 1100, 14):  # a line of letters on paper, 20 rows high
        draw.rectangle((x, 300, x + 4, 319), fill=0)
    for x in range(100, 1100, 8):
        draw.rectangle((x, 600, x + 2, 900), fill=0)
    result = mono.black_and_white(image)
    assert result.getpixel((1120, 130)) == 150
    rows = mono.barcode_rows(image.crop((0, 0, 400, 1000)), 90, 166)
    assert 130 not in rows  # letters on grey: no paper between them
    assert 310 in rows and 310 not in mono._tall(rows, 30)  # too low for a barcode
    assert 700 in mono._tall(rows, 30)


def test_noise_around_the_middle_makes_no_bars():
    """A noisy grey area at the middle between black and white crosses the
    middle often; with hysteresis it crosses no bars."""
    from tavletta.shipping import mono

    noise = Image.effect_noise((400, 40), 12)  # grey 128, spread around it
    assert mono.barcode_rows(noise, 70, 186) == []
    assert len(mono.barcode_rows(noise, 127, 129)) > 0


def test_spread_is_a_min_and_max_filter():
    """The same as Pillow's rank filters, to the edges."""
    from PIL import ImageChops, ImageFilter

    from tavletta.shipping import mono

    noise = Image.effect_noise((90, 70), 60)
    for size in (3, 5, 11, 15):
        for darkest, rank in ((True, ImageFilter.MinFilter), (False, ImageFilter.MaxFilter)):
            spread = mono.spread(noise, darkest, size)
            assert ImageChops.difference(spread, noise.filter(rank(size))).getbbox() is None
    assert mono.spread(noise, True, 1) is noise


def test_mono_area_is_kept_for_the_preview(monkeypatch):
    """The preview renders again on every change: the black and white of
    the page area is made once."""
    from tavletta.shipping import mono

    calls = []
    real = mono.black_and_white

    def counting(grey, scale=1.0):
        calls.append(scale)
        return real(grey, scale)

    monkeypatch.setattr(mono, "black_and_white", counting)
    label = mono_label(bars_and_grey(True))
    first = render(label, dpi=203.2, safe_mm=1.5, mono=True).image
    second = render(label, dpi=203.2, safe_mm=1.5, mono=True).image
    assert len(calls) == 1 and first.tobytes() == second.tobytes()


@needs_poppler
def test_page_area_stays_sharp_next_to_another_element(pdf):
    """A label of fixed length too short for all its elements: the page
    area is drawn again at the smaller size, not reduced with smoothing."""
    element = PageElement(data=pdf, name="a.pdf", crop=list(TOP_LEFT))
    label = targets.make_label(get_backend("cab"), element, targets.TARGETS[0], 1.5)
    label.elements.append(TextElement(text="Fragile"))
    import tavletta.render as rendering

    dpi = 203.2
    assert render(label, dpi=dpi, safe_mm=1.5).overflow
    frame = rendering._frame(label, dpi, 1.5, None, 0, [])
    room = rendering._room(label, frame, dpi, 0)
    parts = rendering._parts(label, frame, dpi, [], room, False)
    _length, scaled, _gap, overflow = rendering._fit_length(label, parts, frame, dpi, [], 0, False)
    assert overflow and scaled[0].width < parts[0].width
    assert values(scaled[0]) == {0, 255}


def test_smaller_falls_back_to_reducing(monkeypatch):
    import tavletta.render as rendering

    part = Image.new("RGBA", (100, 50), (0, 0, 0, 255))

    def broken(*_args, **_kwargs):
        raise SourceError("unreadable")

    monkeypatch.setattr(rendering, "render_page", broken)
    scaled = rendering._smaller(PageElement(data="x"), part, 0.5, False)
    assert scaled.size == (50, 25)
    assert rendering._smaller(TextElement(), part, 0.5, False).size == (50, 25)
