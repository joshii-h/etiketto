"""Crop an image element: drag on the image, or type the area in percent."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from etiketto.model import ImageElement  # noqa: E402
from etiketto.render import _decoded  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402
from etiketto.ui.shipping import CropView  # noqa: E402
from etiketto.ui.widgets import spin_row  # noqa: E402

if TYPE_CHECKING:
    from etiketto.ui.window import Window

SHOWN_PX = 900  # the image is shown at most this large
WHOLE = [0.0, 0.0, 1.0, 1.0]


def crop_text(crop) -> str:
    """ "Whole image" or "60 × 40 %, from 10 %, 20 %"."""
    x, y, w, h = crop
    if list(crop) == WHOLE:
        return _("Whole image")
    return _("%(width)d × %(height)d %%, from %(left)d %%, %(top)d %%") % {
        "width": round(w * 100),
        "height": round(h * 100),
        "left": round(x * 100),
        "top": round(y * 100),
    }


class CropDialog(Adw.Dialog):
    """The image with the kept area; Apply hands it to on_done."""

    def __init__(self, window: Window, element: ImageElement, on_done: Callable[[list], None]):
        super().__init__(title=_("Crop image"), content_width=720, content_height=600)
        self.window = window
        self.on_done = on_done
        self.crop = list(element.crop)
        self._syncing = False
        self.view = CropView(self._on_drag)
        image = _decoded(element.data).copy()
        image.thumbnail((SHOWN_PX, SHOWN_PX))
        self.view.set_page(image)
        self.view.set_crop(self.crop)
        self.view.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Drag to choose the part of the image to keep")],
        )
        self.spins = self._spins()
        self._build()

    def _build(self) -> None:
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        apply = Gtk.Button(label=_("Apply"))
        apply.add_css_class("suggested-action")
        apply.connect("clicked", lambda _b: self.apply())
        header.pack_end(apply)
        group = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_bottom=12)
        for spin in self.spins:
            group.add(spin)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.view)
        box.append(group)
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

    def _spins(self) -> list[Adw.SpinRow]:
        titles = (_("Left"), _("Top"), _("Width"), _("Height"))
        return [
            spin_row(
                title,
                round(value * 100, 1),
                0 if index < 2 else 1,
                99 if index < 2 else 100,
                1,
                1,
                lambda v, i=index: self._on_spin(i, v),
                subtitle="%",
            )
            for index, (title, value) in enumerate(zip(titles, self.crop, strict=True))
        ]

    def _on_spin(self, index: int, value: float) -> None:
        if self._syncing:
            return
        crop = list(self.crop)
        crop[index] = value / 100
        x, y = min(crop[0], 0.99), min(crop[1], 0.99)
        crop = [x, y, max(0.01, min(crop[2], 1 - x)), max(0.01, min(crop[3], 1 - y))]
        self.crop = crop
        self.view.set_crop(crop)

    def _on_drag(self, crop, _final: bool) -> None:
        self.crop = [float(v) for v in crop]
        self._syncing = True
        for spin, value in zip(self.spins, self.crop, strict=True):
            spin.set_value(round(value * 100, 1))
        self._syncing = False

    def apply(self) -> None:
        self.on_done([round(v, 4) for v in self.crop])
        self.close()
