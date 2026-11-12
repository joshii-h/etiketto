"""Label sheets: which label goes to which position, and the pages."""

from __future__ import annotations

import pytest
from conftest import ink_bbox
from PIL import Image
from test_sheets import _sheet

from etiketto.sheets import find
from etiketto.sheets import layout as sheet_layout
from etiketto.sheets.layout import (
    MAX_LABELS,
    Pages,
    SheetJob,
    clip_preview,
    compose_page,
    first_positions,
    in_margins,
    page_slots,
    px,
    shape_mask,
)

DPI = 300


# --- which label goes where ----------------------------------------------------------


def test_slots_start_at_a_position_then_full_sheets():
    sheet = _sheet()  # 24 per sheet
    assert page_slots(sheet, 3, range(5, 24)) == [[5, 6, 7]]
    pages = page_slots(sheet, 40, range(20, 24))
    assert pages[0] == [20, 21, 22, 23] and pages[1] == list(range(24))
    assert pages[2] == list(range(12)) and len(pages) == 3
    assert page_slots(sheet, 2, [7, 3, 3, 99, -1]) == [[3, 7]]
    assert page_slots(sheet, 0, []) == []
    assert page_slots(sheet, 2, []) == [[0, 1]]  # nothing chosen: all
    assert sum(len(p) for p in page_slots(sheet, 10**6, [])) == MAX_LABELS
    assert first_positions(sheet, [30]) == list(range(24))


def _label(width_mm, height_mm, color="red"):
    return Image.new("RGB", (px(width_mm, DPI), px(height_mm, DPI)), color)


def test_a_page_has_each_label_at_its_exact_place():
    sheet = find("Avery L7160")
    page = compose_page(sheet, [(4, _label(sheet.width, sheet.height))], DPI)
    assert page.size == (2480, 3508) and page.info["size_mm"] == (210, 297)
    left, top, right, bottom = sheet.rect(4)
    assert ink_bbox(page, "white") == (
        px(left, DPI),
        px(top, DPI),
        px(left, DPI) + px(sheet.width, DPI),
        px(top, DPI) + px(sheet.height, DPI),
    )


def test_every_position_can_get_its_own_label():
    """A mail merge gives each position another label."""
    sheet = _sheet()
    colors = ["red", "green", "blue"]
    job = SheetJob(sheet, 27, tuple(range(21, 24)))
    asked = []

    def label_at(n):
        asked.append(n)
        return _label(70, 37, colors[n % 3])

    pages = Pages(job, label_at, DPI)
    assert len(pages) == 2 and asked == []  # composed only when asked for
    first = pages[0]
    assert asked == [0, 1, 2]
    centre = [first.getpixel((px(x + 35, DPI), px(y + 18, DPI))) for x, y in sheet.positions()]
    assert centre[21:] == [(255, 0, 0), (0, 128, 0), (0, 0, 255)]
    assert set(centre[:21]) == {(255, 255, 255)}
    assert pages[0] is first and asked == [0, 1, 2]  # the last page is kept
    second = pages[-1]
    assert asked[3:] == list(range(3, 27))
    assert second.getpixel((px(35, DPI), px(18.5, DPI))) == (255, 0, 0)  # label 3
    assert [p.size for p in pages[0:2]] == [first.size] * 2
    assert list(pages) and len(list(pages)) == 2
    with pytest.raises(IndexError):
        pages[2]


def test_a_rotated_label_is_turned_onto_the_sheet():
    sheet = _sheet()
    label = _label(37, 70, "white")  # designed lying on its side
    label.paste((0, 0, 0), (0, 0, 20, 20))  # its top left corner
    page = compose_page(sheet, [(0, label)], DPI, rotated=True)
    # turned a quarter to the left: its top left corner is now bottom left
    assert ink_bbox(page, "white") == (0, px(0.5, DPI) + px(37, DPI) - 20, 20, 443)


def test_another_resolution_is_scaled_to_the_label():
    sheet = _sheet()
    page = compose_page(sheet, [(0, Image.new("RGB", (70, 37), "black"))], DPI)
    assert ink_bbox(page, "white") == (0, px(0.5, DPI), px(70, DPI), px(0.5, DPI) + px(37, DPI))


@pytest.mark.parametrize("shape", ["round", "ellipse"])
def test_round_labels_are_clipped_to_their_shape(shape):
    sheet = _sheet(shape=shape, width=40.0, height=40.0, layouts=((1, 1, 10.0, 10.0, 0, 0),))
    page = compose_page(sheet, [(0, _label(40, 40, "black"))], DPI)
    left, top, size = px(10, DPI), px(10, DPI), px(40, DPI)
    assert page.getpixel((left + 2, top + 2)) == (255, 255, 255)  # a corner: not on the label
    assert page.getpixel((left + size // 2, top + size // 2)) == (0, 0, 0)
    assert page.getpixel((left + size // 2, top + 1)) == (0, 0, 0)  # the edge of the circle


def test_a_cd_without_its_hole_and_cut_off_at_the_sides():
    sheet = _sheet(
        shape="cd",
        width=100.0,
        height=117.0,
        hole=41.0,
        diameter=117.0,
        paper=(120.0, 240.0),
        layouts=((1, 1, 10.0, 0.0, 0.0, 0.0),),
    )
    mask = shape_mask(sheet, (px(100, DPI), px(117, DPI)))
    w, h = mask.size
    assert mask.getpixel((w // 2, h // 2)) == 0  # the hole
    assert mask.getpixel((w // 2, h // 2 + px(30, DPI))) == 255  # on the disc
    assert mask.getpixel((0, h // 2)) == 255  # the disc goes on: cut off by the box
    assert mask.getpixel((0, 0)) == 0
    assert shape_mask(_sheet(), (10, 10)) is None


def test_outlines_for_a_test_print():
    sheet = find("Avery L7160")
    page = compose_page(sheet, [], DPI, outlines=True)
    left, top, right, _bottom = (px(v, DPI) for v in sheet.rect(0))
    middle = (top + px(sheet.rect(0)[3], DPI)) // 2
    assert page.getpixel((left, middle))[0] == sheet_layout.OUTLINE_GREY
    assert page.getpixel((left + 5, middle)) == (255, 255, 255)  # only a thin line
    for shape in ("round", "cd"):
        other = _sheet(shape=shape, width=40.0, height=40.0, hole=10.0, diameter=50.0)
        assert ink_bbox(compose_page(other, [], DPI, outlines=True), "white")


def test_preview_shows_the_shape():
    sheet = _sheet(shape="round", width=40.0, height=20.0)
    image = _label(40, 20, "black")
    clipped = clip_preview(image, sheet)
    assert clipped.getpixel((0, 0)) == (255, 255, 255)
    assert clipped.getpixel((image.width // 2, image.height // 2)) == (0, 0, 0)
    turned = clip_preview(_label(20, 40, "black"), sheet, rotated=True)
    assert turned.size == (px(20, DPI), px(40, DPI)) and turned.getpixel((0, 0))[0] == 255
    plain = _label(70, 37)
    assert clip_preview(plain, _sheet()) is plain


def test_labels_whose_content_reaches_into_the_printers_margins():
    sheet = _sheet()  # 3 × 8 of 70 × 37 from the very edge
    margins = (6.35, 3.4, 6.35, 3.4)  # top, right, bottom, left
    cut = in_margins(sheet, range(sheet.count), margins)
    # first and last column, first and last row
    assert cut == [0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18, 20, 21, 22, 23]
    assert in_margins(sheet, range(sheet.count), margins, inset_mm=7) == []
    assert in_margins(sheet, [4, 99, -1], margins) == []
    assert in_margins(find("Avery L7160"), range(21), margins, 1.5) == []


def test_the_l7160_page_matches_the_template_to_the_pixel():
    """Every position where the template says, also on the second page."""
    sheet = find("Avery L7160")
    job = SheetJob(sheet, 22, outlines=False)
    pages = Pages(job, lambda n: _label(sheet.width, sheet.height, "black"), DPI)
    assert len(pages) == 2
    for number, page in enumerate(pages):
        for index, (x, y) in enumerate(sheet.positions()):
            filled = number == 0 or index == 0
            colour = page.getpixel((px(x, DPI) + 10, px(y, DPI) + 10))
            assert colour == ((0, 0, 0) if filled else (255, 255, 255)), (number, index)
            # nothing in the gap to the left of it (2 mm between the columns)
            assert page.getpixel((px(x, DPI) - 3, px(y, DPI) + 10)) == (255, 255, 255)
