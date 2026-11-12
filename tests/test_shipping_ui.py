"""The shipping label dialog and its place in the window: real widgets,
faked file dialog and printer (see conftest.gui_env)."""

from __future__ import annotations

import base64

import pytest
from conftest import gtk_available, pump, render_now, toast_titles
from shipping_samples import A4, LABEL, a4_pdf, encode, page_image

pytestmark = pytest.mark.gui
if not gtk_available():
    pytest.skip("no display for GTK", allow_module_level=True)

import cairo  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from test_app import FakeFileDialog, drop, find, one, respond  # noqa: E402

from etiketto.model import Label, PageElement, TextElement  # noqa: E402
from etiketto.shipping import detect, source  # noqa: E402
from etiketto.shipping.detect import Detection  # noqa: E402
from etiketto.shipping.source import SourceError  # noqa: E402
from etiketto.shipping.targets import TARGETS, Target  # noqa: E402
from etiketto.ui import shipping, texts  # noqa: E402
from etiketto.ui.texts import detection_hint, target_name  # noqa: E402
from etiketto.units import INCHES, MILLIMETRES  # noqa: E402


def has_poppler() -> bool:
    try:
        source._poppler()
    except SourceError:
        return False
    return True


needs_poppler = pytest.mark.skipif(not has_poppler(), reason="Poppler (poppler-glib) missing")


@pytest.fixture
def file_dialog(monkeypatch):
    class Dialog(FakeFileDialog):
        instances = []

    monkeypatch.setattr(Gtk, "FileDialog", Dialog)
    return Dialog


@pytest.fixture
def sample_pdf(tmp_path):
    path = tmp_path / "parcel.pdf"
    path.write_bytes(a4_pdf("top-left", pages=2))
    return path


@pytest.fixture
def sample_png(tmp_path):
    path = tmp_path / "parcel.png"
    path.write_bytes(encode(page_image("top", 60)))
    return path


@pytest.fixture
def cab_window(window):
    window.set_backend("cab")
    pump()
    window.dirty = False
    return window


def open_dialog(window, path) -> shipping.ShippingDialog:
    window.files.shipping_from(path)
    pump()
    dialog = window.last_dialog
    assert isinstance(dialog, shipping.ShippingDialog)
    return dialog


def sized(view, width=500, height=700):
    """A crop view laid out at a fixed size (the dialog is not mapped in tests)."""
    view.get_width = lambda: width
    view.get_height = lambda: height
    return view


# --- texts ----------------------------------------------------------------------------


def test_target_names():
    assert target_name(TARGETS[0], MILLIMETRES) == "4 × 6″ (101.6 × 152.4 mm)"
    assert target_name(TARGETS[1], MILLIMETRES) == "100 × 150 mm"
    assert target_name(TARGETS[3], MILLIMETRES) == "A6 (105 × 148 mm)"
    assert target_name(TARGETS[-1], MILLIMETRES) == "Endless, 62 mm wide"
    assert target_name(Target("tape", 25), INCHES) == "Endless, 1″ wide"


def test_detection_hints():
    certain = detection_hint(Detection(detect.FULL, "frame"))
    assert certain == detection_hint(None)
    assert "Several areas" in detection_hint(Detection(detect.FULL, "block", certain=False))
    assert "Several areas" in detection_hint(Detection(detect.FULL, "frame", certain=False))
    assert "Everything printed" in detection_hint(Detection(detect.FULL, "content"))
    assert "Nothing" in detection_hint(Detection(detect.FULL, "none", certain=False))


def test_source_error_texts():
    assert "Poppler" in texts.source_error_text(SourceError("no_pdf"), "a.pdf")
    assert texts.source_error_text(SourceError("unreadable"), "a.pdf") == "Cannot open a.pdf"


def test_page_element_texts():
    element = PageElement(name="parcel.pdf")
    assert texts.element_title(element) == "parcel.pdf"
    assert texts.element_title(PageElement()) == "Shipping label"
    assert texts.element_icon(element) == "x-office-document-symbolic"
    from etiketto.render import RenderWarning

    assert "cannot be read" in texts.warning_text(RenderWarning("page_unreadable", {"n": 1}))
    assert "Poppler" in texts.warning_text(RenderWarning("pdf_missing", {"n": 2}))


# --- opening ----------------------------------------------------------------------------


@needs_poppler
def test_menu_opens_the_dialog(cab_window, file_dialog, sample_pdf):
    file_dialog.result = sample_pdf
    cab_window.activate_action("win.shipping", None)
    pump()
    assert file_dialog.instances[-1].kwargs["title"] == "Open shipping label"
    dialog = cab_window.last_dialog
    assert isinstance(dialog, shipping.ShippingDialog)
    assert dialog.detection.how == "frame"
    dialog.close()


def test_menu_cancelled_or_not_local(window, file_dialog):
    file_dialog.result = GLib.Error("Dismissed by user")
    window.files.open_shipping()
    file_dialog.result = "https://example.org/label.pdf"
    window.files.open_shipping()
    pump()
    assert toast_titles(window) == ["Only local files can be used"]
    assert not isinstance(window.last_dialog, shipping.ShippingDialog)


def test_menu_asks_before_discarding(window, file_dialog, sample_png):
    window.change(margin_mm=4.0)
    file_dialog.result = sample_png
    window.files.open_shipping()
    pump()
    assert file_dialog.instances[-1].method is None
    respond(window.get_visible_dialog(), "discard")
    assert file_dialog.instances[-1].method == "open"
    window.last_dialog.close()


def test_unusable_files(window, tmp_path, monkeypatch):
    window.files.shipping_from(tmp_path / "missing.pdf")
    (tmp_path / "notes.txt").write_text("not a label")
    window.files.shipping_from(tmp_path / "notes.txt")

    def no_poppler(*_args):
        raise SourceError("no_pdf", "Poppler missing")

    monkeypatch.setattr("etiketto.ui.files.page_info", no_poppler)
    window.files.shipping_from(tmp_path / "notes.txt")
    assert toast_titles(window) == [
        "Cannot open missing.pdf",
        "Cannot open notes.txt",
        "PDF files need Poppler (poppler-glib), which is not installed",
    ]


@needs_poppler
def test_drop_pdf(cab_window, sample_pdf):
    assert drop(cab_window, Gio.File.new_for_path(str(sample_pdf))) is True
    pump()
    assert isinstance(cab_window.last_dialog, shipping.ShippingDialog)
    cab_window.last_dialog.close()


# --- the dialog -------------------------------------------------------------------------


@needs_poppler
def test_create_label_from_pdf(cab_window, sample_pdf):
    dialog = open_dialog(cab_window, sample_pdf)
    # two pages: a page row; cab: no A4
    one(dialog, Adw.SpinRow, title="Page")
    size_row = one(dialog, Adw.ComboRow, title="Label size")
    names = [size_row.get_model().get_string(i) for i in range(size_row.get_model().get_n_items())]
    assert names[0].startswith("4 × 6″") and not any(n.startswith("A4") for n in names)
    assert dialog.element.rotation == 0
    assert dialog.result.get_paintable() is not None
    dialog.done_button.emit("clicked")
    pump()
    label = cab_window.label
    assert label.media_size() == (101.6, 152.4)
    element = label.elements[0]
    assert isinstance(element, PageElement) and element.name == "parcel.pdf"
    # only the chosen page is in the label (and so in the saved file)
    assert source.page_info(element.data).pages == 1 and element.page == 0
    x, y, w, h = element.crop
    assert x <= 30 / A4[0] and x + w >= (30 + LABEL[0]) / A4[0]
    assert cab_window.dirty and cab_window.path is None
    render_now(cab_window)
    assert cab_window.rendered and not cab_window.rendered.warnings


def test_sideways_image_is_turned_upright(cab_window, sample_png):
    dialog = open_dialog(cab_window, sample_png)
    assert not find(dialog, Adw.SpinRow, title="Page")  # an image has one page
    assert dialog.detection.how == "frame"
    assert dialog.element.rotation == 90
    # rotating by hand: the detection no longer turns it
    dialog.rotate(90)
    assert dialog.element.rotation == 180
    dialog.find()
    assert dialog.element.rotation == 180
    dialog.rotate(-90)
    dialog.rotate(-90)
    dialog.rotate(-90)
    assert dialog.element.rotation == 270
    dialog.close()


def test_targets_fit_and_suggestions(window, sample_png):
    window.set_backend("system")
    pump()
    window.dirty = False
    dialog = open_dialog(window, sample_png)
    size_row = one(dialog, Adw.ComboRow, title="Label size")
    keys = [t.key for t in dialog.targets]
    size_row.set_selected(keys.index("roll62"))
    assert dialog.target.key == "roll62"
    one(dialog, Adw.ComboRow, title="Scale").set_selected(1)
    assert dialog.element.fit == "fill"
    candidates = dialog.detection.candidates
    assert len(candidates) > 1 and dialog.next_button.get_sensitive()
    dialog.next_candidate()
    assert tuple(dialog.element.crop) == candidates[1]
    for _ in candidates:
        dialog.next_candidate()
    assert tuple(dialog.element.crop) == candidates[1]
    dialog.whole_page()
    assert dialog.element.crop == [0.0, 0.0, 1.0, 1.0]
    dialog.finish()
    width, height = window.label.media_size()
    assert width == 62 and height > 62
    assert window.label.elements[0].fit == "fill"


def test_next_without_suggestions(window, tmp_path):
    from PIL import Image

    path = tmp_path / "blank.png"
    Image.new("RGB", (210, 297), "white").save(path)
    dialog = open_dialog(window, path)
    assert dialog.detection.how == "none"
    assert not dialog.next_button.get_sensitive()
    dialog.next_candidate()
    assert dialog.candidate == 0
    assert dialog.hint.get_text() == "Nothing is printed on this page."
    dialog.close()


def test_tape_printer_target(window, sample_png):
    dialog = open_dialog(window, sample_png)
    assert [t.key for t in dialog.targets] == ["tape"]
    assert dialog.element.rotation == 0  # sideways already runs along the tape
    dialog.finish()
    assert window.label.auto_length and window.label.tape_mm == 50


def test_tape_printer_takes_the_inserted_tape(make_window, gui_env, sample_png):
    gui_env.configured = "VC.local"
    window = make_window()
    window.change(tape_mm=12)
    window.dirty = False
    assert window.printer.info.tape_mm == 25
    dialog = open_dialog(window, sample_png)
    assert dialog.targets == [Target("tape", 25)]
    dialog.close()


@needs_poppler
def test_page_change_finds_again(cab_window, sample_pdf):
    dialog = open_dialog(cab_window, sample_pdf)
    dialog.whole_page()
    one(dialog, Adw.SpinRow, title="Page").set_value(2)
    assert dialog.element.page == 1
    assert dialog.detection.how == "frame"
    assert dialog.element.crop != [0.0, 0.0, 1.0, 1.0]
    dialog.close()


# --- the crop view ------------------------------------------------------------------------


def test_crop_view_drag(cab_window, sample_png):
    dialog = open_dialog(cab_window, sample_png)
    view = sized(dialog.crop_view)
    view.set_crop((0.2, 0.2, 0.4, 0.4))
    px, py, w, h = view.page_box()
    assert max(w, h) == pytest.approx(684) and px + w / 2 == pytest.approx(250)
    # drag the lower right corner
    x, y = px + 0.6 * w, py + 0.6 * h
    assert view.handle_at(x, y) == "se"
    view._on_motion(None, x, y)
    view._on_begin(None, x, y)
    view._on_update(None, 0.1 * w, 0.2 * h)
    assert dialog.element.crop == pytest.approx([0.2, 0.2, 0.5, 0.6])
    turned = dialog.element.rotation
    view._on_end(None, 0, 0)
    assert view._grab is None
    assert dialog.element.rotation == 0 != turned  # portrait now: no turn needed
    # a drag that did not start on the page does nothing at the end
    view._on_update(None, 5, 5)
    view._on_end(None, 0, 0)
    dialog.close()


def test_crop_view_draws(cab_window, sample_png):
    dialog = open_dialog(cab_window, sample_png)
    view = sized(dialog.crop_view, 300, 400)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 400)
    view._draw(view, cairo.Context(surface), 300, 400)
    surface.flush()
    # the page is drawn; outside the area it is dimmed
    data = bytes(surface.get_data())
    assert any(data)
    dialog.close()


def test_crop_view_without_page():
    view = sized(shipping.CropView(lambda crop, final: None))
    assert view.page_box() == (0.0, 0.0, 1.0, 1.0)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    view._draw(view, cairo.Context(surface), 10, 10)
    view._on_motion(None, 5, 5)
    view._on_begin(None, 5, 5)
    assert view._grab is None


# --- the element on the label ---------------------------------------------------------------


def shipping_label(window, path) -> PageElement:
    dialog = open_dialog(window, path)
    dialog.finish()
    pump()
    return window.label.elements[0]


def test_sidebar_rows_and_edit(cab_window, sample_png):
    element = shipping_label(cab_window, sample_png)
    cab_window.dirty = False
    area = one(cab_window.sidebar, Adw.ActionRow, title="Area")
    assert area.get_subtitle() == "parcel.png"
    one(cab_window.sidebar, Adw.ComboRow, title="Scale").set_selected(1)
    assert element.fit == "fill" and cab_window.dirty
    button = one(area, Gtk.Button)
    button.emit("clicked")
    pump()
    dialog = cab_window.last_dialog
    assert isinstance(dialog, shipping.ShippingDialog) and dialog.edit
    assert dialog.done_button.get_label() == "Apply"
    assert not find(dialog, Adw.ComboRow, title="Label size")
    assert dialog.element is not element and dialog.element.crop == element.crop
    size = cab_window.label.media_size()
    dialog.rotate(90)
    dialog.whole_page()
    assert element.rotation == 90  # unchanged until applied
    dialog.done_button.emit("clicked")
    pump()
    assert element.rotation == 180 and element.crop == [0.0, 0.0, 1.0, 1.0]
    assert cab_window.label.media_size() == size  # the label keeps its size
    assert cab_window.label.elements[0] is element


def test_edit_with_other_elements(cab_window, sample_png):
    element = shipping_label(cab_window, sample_png)
    cab_window.label.elements.append(TextElement(text="Fragile"))
    cab_window.files.edit_area(element)
    dialog = cab_window.last_dialog
    label = dialog.make_label(dialog.element, None)
    assert label.elements[0] is dialog.element and label.elements[1].text == "Fragile"
    assert cab_window.label.elements[0] is element
    dialog.close()


def test_edit_unreadable_area(window):
    element = PageElement(data=base64.b64encode(b"junk").decode(), name="gone.pdf")
    window.label.elements = [element]
    window.files.edit_area(element)
    assert toast_titles(window) == ["Cannot open gone.pdf"]


def test_cairo_surface_of_an_image():
    from PIL import Image

    surface = shipping.cairo_surface(Image.new("RGB", (3, 2), "red"))
    assert (surface.get_width(), surface.get_height()) == (3, 2)


def test_new_label_size_matches(window, sample_png):
    window.set_backend("system")
    window.label = Label.new_for("system")
    window.label.set_media_size(105, 148)
    dialog = open_dialog(window, sample_png)
    assert dialog.target.key == "a6"
    dialog.close()


@needs_poppler
def test_page_that_cannot_be_drawn(cab_window, sample_pdf, monkeypatch):
    dialog = open_dialog(cab_window, sample_pdf)
    crop = list(dialog.element.crop)

    def too_large(*_args):
        raise SourceError("too_large")

    monkeypatch.setattr(shipping, "preview", too_large)
    dialog.set_page(1)
    assert dialog.element.page == 0 and dialog.element.crop == crop
    assert toast_titles(cab_window)[-1] == "Cannot open parcel.pdf"
    dialog.close()


def test_shipping_file_types():
    from etiketto.ui import files

    assert "application/pdf" in files.SHIPPING_TYPES
    assert "svg" not in files.SHIPPING_SUFFIXES  # Pillow cannot read it
    assert {"png", "jpg", "tif", "webp"} <= set(files.SHIPPING_SUFFIXES)


@needs_poppler
def test_edit_keeps_only_the_page(cab_window, tmp_path):
    path = tmp_path / "parcel.pdf"
    path.write_bytes(a4_pdf("top-left", pages=2))
    element = PageElement.from_file(path)
    cab_window.label.elements = [element]
    cab_window.files.edit_area(element)
    dialog = cab_window.last_dialog
    dialog.set_page(1)
    dialog.done_button.emit("clicked")
    pump()
    assert element.page == 0 and source.page_info(element.data).pages == 1
