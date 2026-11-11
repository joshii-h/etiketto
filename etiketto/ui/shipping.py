"""The shipping label dialog: choose the area of a carrier's PDF or image,
how it is turned and the size it is printed at."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

import cairo  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from PIL import Image  # noqa: E402

from etiketto.model import Label, PageElement  # noqa: E402
from etiketto.render import render  # noqa: E402
from etiketto.shipping import crop as cropping  # noqa: E402
from etiketto.shipping import detect  # noqa: E402
from etiketto.shipping.source import page_info, preview  # noqa: E402
from etiketto.shipping.targets import Target, matching, targets_for, upright  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402
from etiketto.ui.texts import FITS, detection_hint, target_name  # noqa: E402
from etiketto.ui.widgets import combo_row, flat_button  # noqa: E402

if TYPE_CHECKING:
    from etiketto.ui.window import Window

PAGE_PX = 1400  # the page is shown and searched at this size (about 120 dpi on A4)
RESULT_DPI = 60  # the small preview of the label
HANDLE_PX = 10  # how near an edge the pointer grabs it
CURSORS = {
    "move": "move",
    "new": "crosshair",
    "n": "n-resize",
    "s": "s-resize",
    "e": "e-resize",
    "w": "w-resize",
    "nw": "nw-resize",
    "ne": "ne-resize",
    "sw": "sw-resize",
    "se": "se-resize",
}

Done = Callable[[PageElement, Target | None], None]
# the label with the area at the target (None: the size stays), for the preview
MakeLabel = Callable[[PageElement, Target | None], Label]


def cairo_surface(image: Image.Image) -> cairo.ImageSurface:
    """A Pillow image as a cairo surface (for drawing it in a widget)."""
    data = bytearray(image.convert("RGBA").tobytes("raw", "BGRa"))
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, image.width, image.height, image.width * 4
    )


class CropView(Gtk.DrawingArea):
    """The page with the selected area: drag inside to move it, its edges
    and corners to resize it, outside to draw a new one."""

    def __init__(self, on_change: Callable[[tuple, bool], None]):
        super().__init__(hexpand=True, vexpand=True)
        self.set_size_request(320, 420)
        self.on_change = on_change
        self.crop: cropping.Crop = (0.0, 0.0, 1.0, 1.0)
        self.surface: cairo.ImageSurface | None = None
        self._grab: tuple[str, tuple[float, float], cropping.Crop] | None = None
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", self._on_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    def set_page(self, image: Image.Image) -> None:
        self.surface = cairo_surface(image)
        self.queue_draw()

    def set_crop(self, crop) -> None:
        self.crop = tuple(crop)
        self.queue_draw()

    # --- geometry ----------------------------------------------------------------

    def page_box(self) -> tuple[float, float, float, float]:
        """Where the page is drawn: x, y, width, height in widget pixels."""
        if not self.surface:
            return (0.0, 0.0, 1.0, 1.0)
        width, height = self.get_width() - 16, self.get_height() - 16
        pw, ph = self.surface.get_width(), self.surface.get_height()
        scale = max(1e-6, min(width / pw, height / ph))
        w, h = pw * scale, ph * scale
        return ((self.get_width() - w) / 2, (self.get_height() - h) / 2, w, h)

    def to_page(self, x: float, y: float) -> tuple[float, float]:
        px, py, w, h = self.page_box()
        return ((x - px) / w, (y - py) / h)

    # --- drawing -----------------------------------------------------------------

    def _draw(self, _area, ctx, _width, _height) -> None:
        if not self.surface:
            return
        px, py, w, h = self.page_box()
        ctx.save()
        ctx.translate(px, py)
        ctx.scale(w / self.surface.get_width(), h / self.surface.get_height())
        ctx.set_source_surface(self.surface, 0, 0)
        ctx.paint()
        ctx.restore()
        x, y, cw, ch = self.crop
        rect = (px + x * w, py + y * h, cw * w, ch * h)
        # the page outside the area is dimmed, like a photo being cropped
        ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        ctx.rectangle(px, py, w, h)
        ctx.rectangle(*rect)
        ctx.set_source_rgba(0, 0, 0, 0.45)
        ctx.fill()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        ctx.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        ctx.set_line_width(2)
        ctx.rectangle(*rect)
        ctx.stroke()

    # --- pointer -----------------------------------------------------------------

    def _tolerance(self) -> tuple[float, float]:
        _px, _py, w, h = self.page_box()
        return (HANDLE_PX / w, HANDLE_PX / h)

    def handle_at(self, x: float, y: float) -> str:
        return cropping.handle_at(self.crop, *self.to_page(x, y), *self._tolerance())

    def _on_motion(self, _controller, x: float, y: float) -> None:
        self.set_cursor_from_name(CURSORS[self.handle_at(x, y)] if self.surface else None)

    def _on_begin(self, _gesture, x: float, y: float) -> None:
        if self.surface:
            self._grab = (self.handle_at(x, y), self.to_page(x, y), self.crop)

    def _on_update(self, _gesture, dx: float, dy: float) -> None:
        if not self._grab:
            return
        handle, start, original = self._grab
        _px, _py, w, h = self.page_box()
        self.set_crop(cropping.dragged(original, handle, start, dx / w, dy / h))
        self.on_change(self.crop, False)

    def _on_end(self, _gesture, _dx: float, _dy: float) -> None:
        if self._grab:
            self._grab = None
            self.on_change(self.crop, True)


class ShippingDialog(Adw.Dialog):
    """Choose the area of the page, its rotation and the label size; on
    "Create label" (or "Apply" for an area already on the label)
    on_done(element, target) gets the result (target None when editing)."""

    def __init__(
        self,
        window: Window,
        element: PageElement,
        make_label: MakeLabel,
        on_done: Done,
        edit: bool = False,
    ):
        super().__init__(title=_("Shipping label"), content_width=860, content_height=640)
        self.window = window
        self.element = copy.deepcopy(element)
        self.make_label = make_label
        self.on_done = on_done
        self.edit = edit
        self.turned_by_hand = edit  # an area already on a label keeps its rotation
        self.detection: detect.Detection | None = None
        self.candidate = 0
        self.targets = [] if edit else targets_for(window.backend.capabilities, self._tape())
        self.target: Target | None = None if edit else matching(self.targets, window.label)
        self.info = page_info(self.element.data, self.element.page)
        self._build()
        self._load_page(find=not edit)

    def _tape(self) -> float:
        """Endless tape: the inserted tape, else the tape of the open label."""
        info = self.window.printer.info
        widths = self.window.backend.capabilities.tape_widths_mm
        return info.tape_mm if info and info.tape_mm in widths else self.window.label.tape_mm

    # --- layout ------------------------------------------------------------------

    def _build(self) -> None:
        header = Adw.HeaderBar()
        self.done_button = Gtk.Button(label=_("Apply") if self.edit else _("Create label"))
        self.done_button.add_css_class("suggested-action")
        self.done_button.connect("clicked", lambda _b: self.finish())
        header.pack_end(self.done_button)
        view = Adw.ToolbarView(content=self._body())
        view.add_top_bar(header)
        self.set_child(view)

    def _body(self) -> Gtk.Widget:
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.crop_view = CropView(self._on_crop)
        left.append(self.crop_view)
        left.append(self._tool_buttons())
        self.hint = Gtk.Label(wrap=True, xalign=0, margin_start=12, margin_end=12)
        self.hint.add_css_class("dim-label")
        left.append(self.hint)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, width_request=280)
        self.result = Gtk.Picture(can_shrink=True, height_request=220)
        self.result.add_css_class("label-preview")
        right.append(self.result)
        right.append(self._settings())
        body = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=12)
        body.append(left)
        body.append(right)
        return body

    def _tool_buttons(self) -> Gtk.Box:
        box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        for icon, tip, action in (
            ("object-rotate-left-symbolic", _("Rotate left"), lambda: self.rotate(-90)),
            ("object-rotate-right-symbolic", _("Rotate right"), lambda: self.rotate(90)),
            ("edit-find-symbolic", _("Find the label"), self.find),
            ("zoom-fit-best-symbolic", _("Whole page"), self.whole_page),
            ("go-next-symbolic", _("Next suggestion"), self.next_candidate),
        ):
            box.append(flat_button(icon, tip, action))
        self.next_button = box.get_last_child()
        return box

    def _settings(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        if self.info.pages > 1:
            pages = Adw.SpinRow.new_with_range(1, self.info.pages, 1)
            pages.set_title(_("Page"))
            pages.set_value(self.element.page + 1)
            pages.connect("notify::value", lambda r, _p: self.set_page(int(r.get_value()) - 1))
            group.add(pages)
        if self.targets and self.target:
            unit = self.window.unit
            items = [(t.key, target_name(t, unit)) for t in self.targets]
            group.add(combo_row(_("Label size"), items, self.target.key, self.set_target))
        group.add(combo_row(_("Scale"), FITS, self.element.fit, self.set_fit))
        return group

    # --- the area ----------------------------------------------------------------

    def _load_page(self, find: bool) -> None:
        width_mm = page_info(self.element.data, self.element.page).width_mm()
        self.page_image = preview(self.element.data, self.element.page, PAGE_PX)
        self.page_width_mm = width_mm
        self.crop_view.set_page(self.page_image)
        if find:
            self.find()
        else:
            self.crop_view.set_crop(self.element.crop)
            self.next_button.set_sensitive(False)
            self.hint.set_text(detection_hint(None))
            self.update_result()

    def find(self) -> None:
        self.detection = detect.detect(self.page_image, self.page_width_mm)
        self.candidate = 0
        self.next_button.set_sensitive(len(self.detection.candidates) > 1)
        self.hint.set_text(detection_hint(self.detection))
        self.set_crop(self.detection.crop)

    def next_candidate(self) -> None:
        if not self.detection or len(self.detection.candidates) < 2:
            return
        self.candidate = (self.candidate + 1) % len(self.detection.candidates)
        self.set_crop(self.detection.candidates[self.candidate])

    def whole_page(self) -> None:
        self.set_crop(detect.FULL)

    def set_crop(self, crop) -> None:
        self.crop_view.set_crop(crop)
        self._on_crop(tuple(crop), True)

    def _on_crop(self, crop, final: bool) -> None:
        self.element.crop = [float(v) for v in crop]
        if final:
            self._turn_upright()
            self.update_result()

    def _turn_upright(self) -> None:
        if not self.turned_by_hand and self.target:
            self.element.rotation = upright(self.element, self.target)

    def rotate(self, degrees: int) -> None:
        self.turned_by_hand = True
        self.element.rotation = (self.element.rotation + degrees) % 360
        self.update_result()

    def set_page(self, page: int) -> None:
        self.element.page = page
        self._load_page(find=True)

    def set_target(self, key: str) -> None:
        self.target = next(t for t in self.targets if t.key == key)
        self._turn_upright()
        self.update_result()

    def set_fit(self, fit: str) -> None:
        self.element.fit = fit  # type: ignore[assignment]
        self.update_result()

    # --- result ------------------------------------------------------------------

    def update_result(self) -> None:
        """The small preview of the label as it will be printed."""
        label = self.make_label(self.element, self.target)
        backend = self.window.backend
        rendered = render(label, dpi=RESULT_DPI, safe_mm=self.window.printer_settings.safe_mm)
        image = backend.preview(rendered.image)
        texture = Gdk.MemoryTexture.new(
            image.width,
            image.height,
            Gdk.MemoryFormat.R8G8B8,
            GLib.Bytes.new(image.convert("RGB").tobytes()),
            image.width * 3,
        )
        self.result.set_paintable(texture)

    def finish(self) -> None:
        self.on_done(self.element, self.target)
        self.close()
