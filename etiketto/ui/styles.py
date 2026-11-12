"""The design options of the elements in the "Content" group: text style,
box and outline, image adjustments, shapes, and where an element sits.
The everyday rows stay in the element's row; the rest is folded into
expanders so the sidebar stays tidy."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from etiketto.model import ImageElement, ShapeElement, TextElement  # noqa: E402
from etiketto.ui.i18n import _  # noqa: E402
from etiketto.ui.texts import (  # noqa: E402
    ARROWS,
    DIRECTIONS,
    FILLS,
    IMAGE_MODES,
    LINE_STYLES,
    ROTATIONS,
    SHAPES,
    VALIGNS,
)
from etiketto.ui.widgets import (  # noqa: E402
    color_button,
    combo_row,
    expander,
    flat_button,
    length_row,
    spin_row,
    switch_row,
)

if TYPE_CHECKING:
    from etiketto.ui.window import Window

SetAttr = Callable[..., None]
ROTATION_ITEMS = [(str(k), text) for k, text in ROTATIONS]
DEFAULT_GAP_MM = 2.0  # what "own spacing" starts with


def _rotation_row(element, set_attr: SetAttr) -> Adw.ComboRow:
    return combo_row(
        _("Rotation"),
        ROTATION_ITEMS,
        str(element.rotation),
        lambda v: set_attr(rotation=int(v)),
    )


# --- every element ------------------------------------------------------------------


def position_rows(window: Window, element, set_attr: SetAttr) -> Adw.ExpanderRow:
    """Across the tape (top, middle, bottom) and the space after it."""
    row = expander(_("Position"), _("Across the tape and the space after it"))
    row.add_row(
        combo_row(_("Vertical alignment"), VALIGNS, element.valign, lambda v: set_attr(valign=v))
    )
    gap = length_row(
        window.unit,
        _("Space after it"),
        element.gap_mm if element.gap_mm is not None else DEFAULT_GAP_MM,
        0,
        50,
        0.5,
        lambda v: set_attr(gap_mm=v),
    )
    gap.set_sensitive(element.gap_mm is not None)

    def own(active: bool) -> None:
        gap.set_sensitive(active)
        set_attr(gap_mm=round(window.unit.from_display(gap.get_value()), 3) if active else None)

    row.add_row(
        switch_row(
            _("Own spacing"),
            element.gap_mm is not None,
            own,
            _("Instead of the label's spacing between elements"),
        )
    )
    row.add_row(gap)
    return row


# --- text --------------------------------------------------------------------------


def text_rows(window: Window, row: Adw.ExpanderRow, element: TextElement, set_attr) -> None:
    row.add_row(_text_fit(window, element, set_attr))
    row.add_row(_text_box(window, element, set_attr))
    row.add_row(_text_effects(window, element, set_attr))


def _text_fit(window: Window, element: TextElement, set_attr) -> Adw.ExpanderRow:
    row = expander(_("Spacing and fitting"))
    row.add_row(
        spin_row(
            _("Letter spacing"),
            element.letter_spacing,
            -50,
            200,
            1,
            0,
            lambda v: set_attr(letter_spacing=v),
            subtitle=_("percent of the font size"),
        )
    )
    row.add_row(
        spin_row(
            _("Line spacing"),
            element.line_spacing,
            0.5,
            3,
            0.05,
            2,
            lambda v: set_attr(line_spacing=v),
            subtitle=_("times the normal line height"),
        )
    )
    row.add_row(
        switch_row(
            _("Shrink to fit"),
            element.shrink,
            lambda v: set_attr(shrink=v),
            _("Smaller until it fits the tape height and the label length"),
        )
    )
    row.add_row(
        spin_row(
            _("Smallest size"),
            element.min_size_pt,
            0,
            300,
            1,
            0,
            lambda v: set_attr(min_size_pt=v),
            subtitle=_("pt, when shrinking"),
        )
    )
    row.add_row(
        length_row(
            window.unit,
            _("Wrap width"),
            element.max_width_mm,
            0,
            500,
            1,
            lambda v: set_attr(max_width_mm=v),
            _("%(unit)s, 0 = only at line breaks"),
        )
    )
    row.add_row(_rotation_row(element, set_attr))
    return row


def _text_box(window: Window, element: TextElement, set_attr) -> Adw.ExpanderRow:
    unit = window.unit
    row = expander(_("Box and frame"))
    row.add_row(
        switch_row(
            _("White on black"),
            element.inverted,
            lambda v: set_attr(inverted=v),
            _("The text cut out of a box in the text colour"),
        )
    )
    box = Adw.ActionRow(title=_("Background box"), subtitle=_("A box behind the text"))
    colour = color_button(element.box_color or "#ffff00", lambda v: set_attr(box_color=v))
    colour.set_sensitive(bool(element.box_color))
    switch = Gtk.Switch(active=bool(element.box_color), valign=Gtk.Align.CENTER)
    switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Background box")])

    def boxed(sw, _pspec) -> None:
        colour.set_sensitive(sw.get_active())
        set_attr(box_color=_hex(colour) if sw.get_active() else "")

    switch.connect("notify::active", boxed)
    box.add_suffix(colour)
    box.add_suffix(switch)
    box.set_activatable_widget(switch)
    row.add_row(box)
    for title, attr, upper in (
        (_("Padding"), "padding_mm", 20),
        (_("Corner radius"), "corner_mm", 50),
    ):
        row.add_row(
            length_row(
                unit,
                title,
                getattr(element, attr),
                0,
                upper,
                0.5,
                lambda v, a=attr: set_attr(**{a: v}),
            )
        )
    row.add_row(
        length_row(
            unit,
            _("Frame"),
            element.frame_mm,
            0,
            5,
            0.1,
            lambda v: set_attr(frame_mm=v),
            _("%(unit)s, 0 = none"),
        )
    )
    return row


def _hex(button: Gtk.ColorDialogButton) -> str:
    from etiketto.ui.widgets import rgba_to_hex

    return rgba_to_hex(button.get_rgba())


def _text_effects(window: Window, element: TextElement, set_attr) -> Adw.ExpanderRow:
    row = expander(
        _("Outline and shadow"),
        _("At least one printer dot; black-and-white printers print the shadow solid"),
    )
    for title, attr, colour_attr in (
        (_("Outline"), "outline_mm", "outline_color"),
        (_("Shadow"), "shadow_mm", "shadow_color"),
    ):
        line = length_row(
            window.unit,
            title,
            getattr(element, attr),
            0,
            5,
            0.1,
            lambda v, a=attr: set_attr(**{a: v}),
            _("%(unit)s, 0 = none"),
        )
        line.add_suffix(
            color_button(
                getattr(element, colour_attr), lambda v, a=colour_attr: set_attr(**{a: v})
            )
        )
        row.add_row(line)
    return row


# --- images ------------------------------------------------------------------------


def image_rows(window: Window, row: Adw.ExpanderRow, element: ImageElement, set_attr) -> None:
    row.add_row(_crop_row(window, element, set_attr))
    row.add_row(_image_adjust(element, set_attr))
    row.add_row(_image_mono(element, set_attr))
    row.add_row(_image_frame(window, element, set_attr))


def _crop_row(window: Window, element: ImageElement, set_attr) -> Adw.ActionRow:
    from etiketto.ui.image_crop import CropDialog, crop_text

    crop = Adw.ActionRow(title=_("Crop"), subtitle=crop_text(element.crop))

    def apply(area: list[float]) -> None:
        crop.set_subtitle(crop_text(area))
        set_attr(crop=area)

    choose = Gtk.Button(label=_("Crop…"), valign=Gtk.Align.CENTER)
    choose.set_sensitive(bool(element.data))
    choose.connect("clicked", lambda _b: window.present_dialog(CropDialog(window, element, apply)))
    crop.add_suffix(choose)
    crop.add_suffix(
        flat_button("edit-clear-symbolic", _("Whole image"), lambda: apply([0.0, 0.0, 1.0, 1.0]))
    )
    return crop


def _image_adjust(element: ImageElement, set_attr) -> Adw.ExpanderRow:
    row = expander(_("Adjust"), _("Brightness, contrast, turning and mirroring"))
    for title, attr, lower, upper, step, digits in (
        (_("Brightness"), "brightness", -100, 100, 5, 0),
        (_("Contrast"), "contrast", -100, 100, 5, 0),
        (_("Gamma"), "gamma", 0.2, 5, 0.1, 1),
    ):
        row.add_row(
            spin_row(
                title,
                getattr(element, attr),
                lower,
                upper,
                step,
                digits,
                lambda v, a=attr: set_attr(**{a: v}),
            )
        )
    row.add_row(switch_row(_("Invert"), element.invert, lambda v: set_attr(invert=v)))
    row.add_row(_rotation_row(element, set_attr))
    row.add_row(
        switch_row(_("Mirror"), element.mirror, lambda v: set_attr(mirror=v), _("Left and right"))
    )
    row.add_row(
        switch_row(_("Flip"), element.flip, lambda v: set_attr(flip=v), _("Top and bottom"))
    )
    return row


def _image_mono(element: ImageElement, set_attr) -> Adw.ExpanderRow:
    row = expander(_("Black and white"), _("How black-and-white printers print the image"))
    level = spin_row(
        _("Threshold"),
        element.threshold,
        1,
        254,
        1,
        0,
        lambda v: set_attr(threshold=int(v)),
        subtitle=_("Lighter pixels print white"),
    )
    level.set_sensitive(element.mode == "threshold")

    def mode(value: str) -> None:
        level.set_sensitive(value == "threshold")
        set_attr(mode=value)

    row.add_row(combo_row(_("Method"), IMAGE_MODES, element.mode, mode))
    row.add_row(level)
    return row


def _image_frame(window: Window, element: ImageElement, set_attr) -> Adw.ExpanderRow:
    row = expander(_("Corners and border"))
    row.add_row(
        length_row(
            window.unit,
            _("Corner radius"),
            element.corner_mm,
            0,
            50,
            0.5,
            lambda v: set_attr(corner_mm=v),
        )
    )
    border = length_row(
        window.unit,
        _("Border"),
        element.border_mm,
        0,
        5,
        0.1,
        lambda v: set_attr(border_mm=v),
        _("%(unit)s, 0 = none"),
    )
    border.add_suffix(color_button(element.border_color, lambda v: set_attr(border_color=v)))
    row.add_row(border)
    return row


# --- shapes ------------------------------------------------------------------------

# which rows a shape uses
SHAPE_ROWS = {
    "line": {"direction", "width", "height", "style", "arrows"},
    "rectangle": {"width", "height", "style", "corner", "fill"},
    "ellipse": {"width", "height", "style", "fill"},
    "separator": {"style", "height"},
}


def shape_rows(window: Window, row: Adw.ExpanderRow, element: ShapeElement, set_attr) -> None:
    unit = window.unit
    rows: dict[str, Adw.PreferencesRow] = {
        "direction": combo_row(
            _("Direction"), DIRECTIONS, element.direction, lambda v: set_attr(direction=v)
        ),
        "width": length_row(
            unit, _("Length"), element.width_mm, 0.1, 500, 1, lambda v: set_attr(width_mm=v)
        ),
        "height": length_row(
            unit,
            _("Height"),
            element.height_mm,
            0,
            500,
            1,
            lambda v: set_attr(height_mm=v),
            _("%(unit)s, 0 = fill tape height"),
        ),
        "style": combo_row(
            _("Line style"), LINE_STYLES, element.line_style, lambda v: set_attr(line_style=v)
        ),
        "arrows": combo_row(_("Arrows"), ARROWS, element.arrows, lambda v: set_attr(arrows=v)),
        "corner": length_row(
            unit,
            _("Corner radius"),
            element.corner_mm,
            0,
            50,
            0.5,
            lambda v: set_attr(corner_mm=v),
        ),
        "fill": combo_row(_("Fill"), FILLS, element.fill, lambda v: set_attr(fill=v)),
    }

    def show(shape: str) -> None:
        for key, widget in rows.items():
            widget.set_visible(key in SHAPE_ROWS[shape])

    def set_shape(shape: str) -> None:
        show(shape)
        set_attr(shape=shape)

    row.add_row(combo_row(_("Shape"), SHAPES, element.shape, set_shape))
    line = length_row(
        unit,
        _("Line width"),
        element.line_mm,
        0,
        10,
        0.1,
        lambda v: set_attr(line_mm=v),
        _("%(unit)s, at least one printer dot"),
    )
    line.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
    row.add_row(line)
    for widget in rows.values():
        row.add_row(widget)
    show(element.shape)
