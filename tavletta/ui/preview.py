"""The label preview with its status line and zoom buttons."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from tavletta.ui.i18n import _  # noqa: E402
from tavletta.ui.widgets import flat_button  # noqa: E402

# Preview: screen pixels per mm at zoom 1
PREVIEW_PX_PER_MM = 6.0


def texture(image) -> Gdk.Texture:
    """The image's pixels as a texture, as they are: no PNG to encode and
    decode in the main loop (a page of 16 megapixels took 0.4 s)."""
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    rgb = image.mode == "RGB"
    memory = Gdk.MemoryFormat.R8G8B8 if rgb else Gdk.MemoryFormat.R8G8B8A8
    stride = image.width * (3 if rgb else 4)
    data = GLib.Bytes.new(image.tobytes())
    return Gdk.MemoryTexture.new(image.width, image.height, memory, data, stride)


class Preview(Gtk.Box):
    """The label preview with the status line and zoom buttons."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.zoom = 1.0
        self.fit_zoom = True
        self._size_mm: tuple[float, float] | None = None  # (length, height) of the shown label

        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.picture = Gtk.Picture(
            can_shrink=True,
            content_fit=Gtk.ContentFit.CONTAIN,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        for adjustment in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()):
            adjustment.connect("notify::page-size", self._on_page_size)
        self.picture.set_margin_top(24)
        self.picture.set_margin_bottom(24)
        self.picture.set_margin_start(24)
        self.picture.set_margin_end(24)
        self.picture.add_css_class("label-preview")
        self.scroller.set_child(self.picture)
        self.append(self.scroller)
        self.append(Gtk.Separator())
        self.append(self._build_status())

    def _build_status(self) -> Gtk.Box:
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.status = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.status.add_css_class("dim-label")
        box.append(self.status)
        box.append(self._build_rows())
        box.append(flat_button("zoom-fit-best-symbolic", _("Fit to window"), self.set_zoom))
        box.append(
            flat_button(
                "zoom-out-symbolic", _("Zoom out"), lambda: self.set_zoom(self.zoom / 1.25)
            )
        )
        box.append(
            flat_button("zoom-in-symbolic", _("Zoom in"), lambda: self.set_zoom(self.zoom * 1.25))
        )
        return box

    def _build_rows(self) -> Gtk.Box:
        """Serial labels: which row the preview shows, and buttons to step
        through the rows (actions win.row-first, …, of the window)."""
        self.rows = Gtk.Box(spacing=2, visible=False)
        buttons = (
            ("go-first-symbolic", _("First row"), "first"),
            ("go-previous-symbolic", _("Previous row"), "previous"),
            ("go-next-symbolic", _("Next row"), "next"),
            ("go-last-symbolic", _("Last row"), "last"),
        )
        for icon, tip, step in buttons:
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            button.set_action_name("win.row-" + step)
            self.rows.append(button)
            if step == "previous":
                self.row_label = Gtk.Label(margin_start=6, margin_end=6)
                self.row_label.add_css_class("numeric")
                self.rows.append(self.row_label)
        return self.rows

    def show_row(self, number: int, count: int) -> None:
        self.row_label.set_text(_("Row %(n)d of %(total)d") % {"n": number, "total": count})
        self.rows.set_visible(True)

    def hide_row(self) -> None:
        self.rows.set_visible(False)

    def show_image(self, image, length_mm: float, height_mm: float, text: str) -> None:
        """Show a rendered preview image and its status text."""
        self.picture.set_paintable(texture(image))
        self._size_mm = (length_mm, height_mm)
        self.apply_zoom()
        self.status.set_text(text)

    def clear(self, text: str) -> None:
        """No picture (the label could not be drawn), only a status text."""
        self.picture.set_paintable(None)
        self.status.set_text(text)

    def _on_page_size(self, *_args) -> None:
        if self.fit_zoom:
            self.apply_zoom()

    def apply_zoom(self) -> None:
        if not self._size_mm:
            return
        length, height = self._size_mm
        if self.fit_zoom:
            avail_w = self.scroller.get_hadjustment().get_page_size() - 56
            avail_h = self.scroller.get_vadjustment().get_page_size() - 56
            if avail_w > 0 and avail_h > 0:
                px_per_mm = min(avail_w / length, avail_h / height, PREVIEW_PX_PER_MM * 4)
                self.zoom = px_per_mm / PREVIEW_PX_PER_MM
        px_per_mm = PREVIEW_PX_PER_MM * self.zoom
        self.picture.set_size_request(round(length * px_per_mm), round(height * px_per_mm))

    def set_zoom(self, zoom: float | None = None) -> None:
        """None = fit to the window."""
        self.fit_zoom = zoom is None
        if zoom is not None:
            self.zoom = min(8.0, max(0.1, zoom))
        self.apply_zoom()
