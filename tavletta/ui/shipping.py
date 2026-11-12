"""The shipping label dialog: choose the area of a carrier's PDF or image,
how it is turned and the size it is printed at."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

import cairo  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from PIL import Image  # noqa: E402

from tavletta.model import Label, PageElement  # noqa: E402
from tavletta.render import render  # noqa: E402
from tavletta.shipping import crop as cropping  # noqa: E402
from tavletta.shipping import detect  # noqa: E402
from tavletta.shipping.source import (  # noqa: E402
    SourceError,
    clear_caches,
    only_page,
    page_info,
    preview,
)
from tavletta.shipping.targets import Target, matching, targets_for, upright  # noqa: E402
from tavletta.ui.i18n import _  # noqa: E402
from tavletta.ui.texts import (  # noqa: E402
    FITS,
    detection_hint,
    source_error_text,
    target_name,
)
from tavletta.ui.widgets import combo_row, flat_button  # noqa: E402

if TYPE_CHECKING:
    from tavletta.ui.window import Window

STORE_TIMEOUT = 10  # seconds Create label waits for the copy of a page
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


@contextmanager
def signal_blocked(widget, handler: int):
    widget.handler_block(handler)
    try:
        yield
    finally:
        widget.handler_unblock(handler)


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
        # per page: what goes into the label file (see _check_storage)
        self._stored: dict[int, tuple[str, int]] = {}
        self._storage_threads: dict[int, threading.Thread] = {}
        self._finishing = self._completed = False  # Create label waits for the copy
        self._build()
        self._load_page(find=not edit)
        # the decoded document and its drawings go with the dialog
        self.connect("closed", lambda _d: clear_caches())

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
        self.busy = Gtk.Spinner(visible=False)  # while Create label waits for the copy
        header.pack_end(self.busy)
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
        self.store_note = Gtk.Label(
            label=_("The whole document is stored in the label file, not only this page."),
            wrap=True,
            xalign=0,
            visible=False,
        )
        self.store_note.add_css_class("dim-label")
        self.store_note.add_css_class("caption")
        right.append(self.store_note)
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
            self.page_changed = pages.connect(
                "notify::value", lambda r, _p: self.set_page(int(r.get_value()) - 1)
            )
            self.page_row = pages
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
        self._check_storage()
        if find:
            self.find()
        else:
            self.crop_view.set_crop(self.element.crop)
            self.next_button.set_sensitive(False)
            self.hint.set_text(detection_hint(None))
            self.update_result()

    def _check_storage(self) -> None:
        """What goes into the label file: only this page of the document if
        it can be copied unchanged, else all of it (then a note says so).
        Copying and comparing a page takes up to a second: it is done in the
        background, with its own Poppler document, once per page."""
        page = self.element.page
        self.store_note.set_visible(False)
        if self.info.pages < 2:
            self._stored[page] = (self.element.data, page)
        if page in self._stored:
            self._show_storage(page)
            return
        running = self._storage_threads.get(page)
        if running is not None and running.is_alive():
            return  # the copy of this page is on its way already
        thread = threading.Thread(
            target=self._store_in_background,
            args=(self.element.data, page),
            name="tavletta-page-copy",
            daemon=True,
        )
        self._storage_threads[page] = thread
        thread.start()

    def _store_in_background(self, data: str, page: int) -> None:
        stored = (data, page)  # if anything goes wrong: the whole document
        try:
            stored = only_page(data, page)
        except Exception:  # noqa: BLE001 - never lose the label over the copy
            pass
        finally:
            self._stored[page] = stored
            GLib.idle_add(self._stored_ready, page)

    def _stored_ready(self, page: int) -> bool:
        if page == self.element.page:
            self._show_storage(page)
            if self._finishing:
                self._complete()
        return False

    def _show_storage(self, page: int) -> None:
        whole = self._stored[page][0] is self.element.data
        self.store_note.set_visible(self.info.pages > 1 and whole)

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
        """Show another page; a page that cannot be drawn (damaged, too
        large) is reported and the current one stays."""
        previous = self.element.page
        self.element.page = page
        try:
            self._load_page(find=True)
        except SourceError as e:
            self.element.page = previous
            with signal_blocked(self.page_row, self.page_changed):
                self.page_row.set_value(previous + 1)
            self.window.toast(source_error_text(e, self.element.name))

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
        rendered = render(
            label,
            dpi=RESULT_DPI,
            safe_mm=self.window.printer_settings.safe_mm,
            mono=backend.renders_mono(self.window.print_flow.mode),
            red=backend.renders_red(self.window.print_flow.mode),
        )
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
        """Hand over the area; if the copy of its page is still being made,
        when it is done (the button waits, the main loop runs on), or after
        STORE_TIMEOUT with the whole document."""
        if self.element.page in self._stored:
            self._complete()
            return
        self._finishing = True
        self.done_button.set_sensitive(False)
        self.busy.set_visible(True)
        self.busy.start()
        GLib.timeout_add(round(STORE_TIMEOUT * 1000), self._store_timeout, self.element.page)

    def _store_timeout(self, page: int) -> bool:
        if self._finishing and page == self.element.page and page not in self._stored:
            self._stored[page] = (self.element.data, page)
            self.window.toast(self.store_note.get_label())
            self._complete()
        return False

    def _complete(self) -> None:
        if self._completed:
            return
        self._completed = True
        page = self.element.page
        self.element.data, self.element.page = self._stored.get(page, (self.element.data, page))
        self.on_done(self.element, self.target)
        self.close()
