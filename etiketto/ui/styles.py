"""The design options of the elements in the "Content" group: text style,
box and outline, image adjustments, shapes, and where an element sits.
The everyday rows stay in the element's row; the rest opens as a page of
its own in the sidebar (subpage_row()), so the sidebar stays tidy and
every list is one level deep."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from etiketto.model import EFFECTIVE, ImageElement, ShapeElement, TextElement  # noqa: E402
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
    element_title,
)
from etiketto.ui.widgets import (  # noqa: E402
    color_button,
    combo_row,
    flat_button,
    length_row,
    rgba_to_hex,
    spin_row,
    switch_row,
)

if TYPE_CHECKING:
    from etiketto.ui.window import Window

SetAttr = Callable[..., None]
Rows = Callable[[Adw.PreferencesGroup], None]
ROTATION_ITEMS = [(str(k), text) for k, text in ROTATIONS]
DEFAULT_GAP_MM = 2.0  # what "own spacing" starts with


def subpage_row(window: Window, element, title: str, subtitle: str, rows: Rows) -> Adw.ActionRow:
    """A row that opens a page of further options in the sidebar; the
    page's header names the element, so it is clear what is edited."""
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
    row.connect(
        "activated",
        lambda _r: window.push_subpage(title, element_title(element), rows, element),
    )
    return row


class _Follow:
    """set_attr for a page of options that also keeps rows without effect
    insensitive (model.EFFECTIVE: the outline colour while there is no
    outline, the smallest size while not shrinking …)."""

    def __init__(self, element, set_attr: SetAttr):
        self.element, self.set_attr = element, set_attr
        self.conditions = EFFECTIVE.get(type(element).__name__, {})
        # field: (widget, hint shown while it is off)
        self.widgets: dict[str, tuple[Gtk.Widget, str]] = {}

    def __call__(self, **changes) -> None:
        self.set_attr(**changes)
        self.update()

    def track(self, name: str, widget: Gtk.Widget, hint: str = "") -> Gtk.Widget:
        """Follow a widget; hint: why it is off (a row's subtitle or a
        button's tooltip while it is)."""
        self.widgets[name] = (widget, hint)
        self.update()
        return widget

    def update(self) -> None:
        for name, (widget, hint) in self.widgets.items():
            on = self.conditions[name](self.element)
            if hint and on != widget.get_sensitive():
                _swap_hint(widget, hint, on)
            widget.set_sensitive(on)


def _swap_hint(widget: Gtk.Widget, hint: str, on: bool) -> None:
    """Show the hint while a widget is off; back on, what it showed before
    (read when it went off, so later changes of it are kept)."""
    row = isinstance(widget, Adw.ActionRow)
    if not on:
        widget.own_text = widget.get_subtitle() if row else widget.get_tooltip_text()
    text = (getattr(widget, "own_text", "") or "") if on else hint
    if row:
        widget.set_subtitle(text)
    else:
        widget.set_tooltip_text(text)


def _page(rows, window: Window, element, set_attr: SetAttr, group) -> None:
    rows(window, group, element, _Follow(element, set_attr))


def _rotation_row(element, set_attr: SetAttr) -> Adw.ComboRow:
    return combo_row(
        _("Rotation"),
        ROTATION_ITEMS,
        str(element.rotation),
        lambda v: set_attr(rotation=int(v)),
    )


def _named(button: Gtk.Widget, name: str) -> Gtk.Widget:
    """An accessible name for a button that shows only a colour."""
    button.update_property([Gtk.AccessibleProperty.LABEL], [name])
    button.set_tooltip_text(name)
    return button


# --- every element ------------------------------------------------------------------


def position_row(window: Window, element, set_attr: SetAttr) -> Adw.ActionRow:
    return subpage_row(
        window,
        element,
        _("Position"),
        _("Across the tape and the space after it"),
        lambda group: _position_rows(window, group, element, set_attr),
    )


def _position_rows(window: Window, group, element, set_attr: SetAttr) -> None:
    """Across the tape (top, middle, bottom) and the space after it."""
    group.add(
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
        window.history.end_step()  # a switch: not joined with spinning before or after
        set_attr(gap_mm=round(window.unit.from_display(gap.get_value()), 3) if active else None)
        window.history.end_step()

    group.add(
        switch_row(
            _("Own spacing"),
            element.gap_mm is not None,
            own,
            _("Instead of the label's spacing between elements"),
        )
    )
    group.add(gap)


# --- text --------------------------------------------------------------------------


def text_rows(window: Window, row: Adw.ExpanderRow, element: TextElement, set_attr) -> None:
    for title, subtitle, rows in (
        (
            _("Spacing and fitting"),
            _("Letter and line spacing, shrinking, wrapping, rotation"),
            _text_fit,
        ),
        (_("Box and frame"), _("White on black, a box behind the text, a frame"), _text_box),
        (
            _("Outline and shadow"),
            _("At least one printer dot; black-and-white printers print the shadow solid"),
            _text_effects,
        ),
    ):
        row.add_row(
            subpage_row(
                window,
                element,
                title,
                subtitle,
                partial(_page, rows, window, element, set_attr),
            )
        )


def _text_fit(window: Window, group, element: TextElement, set_attr) -> None:
    group.add(
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
    group.add(
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
    group.add(
        switch_row(
            _("Shrink to fit"),
            element.shrink,
            lambda v: set_attr(shrink=v),
            _("Smaller until it fits the tape height and the label length"),
        )
    )
    smallest = spin_row(
        _("Smallest size"),
        element.min_size_pt,
        0,
        300,
        1,
        0,
        lambda v: set_attr(min_size_pt=v),
        subtitle=_("pt, when shrinking"),
    )
    group.add(set_attr.track("min_size_pt", smallest))
    group.add(
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
    group.add(_rotation_row(element, set_attr))


def _text_box(window: Window, group, element: TextElement, set_attr) -> None:
    unit = window.unit
    group.add(
        switch_row(
            _("White on black"),
            element.inverted,
            lambda v: set_attr(inverted=v),
            _("The text cut out of a box in the text colour"),
        )
    )
    box = Adw.ActionRow(title=_("Background box"), subtitle=_("A box behind the text"))
    colour = color_button(element.box_color or "#ffff00", lambda v: set_attr(box_color=v))
    _named(colour, _("Colour of the background box"))
    colour.set_sensitive(bool(element.box_color))
    switch = Gtk.Switch(active=bool(element.box_color), valign=Gtk.Align.CENTER)
    switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Background box")])

    def boxed(sw, _pspec) -> None:
        colour.set_sensitive(sw.get_active())
        set_attr(box_color=rgba_to_hex(colour.get_rgba()) if sw.get_active() else "")

    switch.connect("notify::active", boxed)
    box.add_suffix(colour)
    box.add_suffix(switch)
    box.set_activatable_widget(switch)
    group.add(box)
    for title, attr, upper in (
        (_("Padding"), "padding_mm", 20),
        (_("Corner radius"), "corner_mm", 50),
    ):
        row = length_row(
            unit,
            title,
            getattr(element, attr),
            0,
            upper,
            0.5,
            lambda v, a=attr: set_attr(**{a: v}),
            _("%(unit)s, with a box or frame"),
        )
        group.add(set_attr.track(attr, row))
    group.add(
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


def _text_effects(window: Window, group, element: TextElement, set_attr) -> None:
    for title, attr, colour_attr, name, hint in (
        (
            _("Outline"),
            "outline_mm",
            "outline_color",
            _("Colour of the outline"),
            _("Choose a width first"),
        ),
        (
            _("Shadow"),
            "shadow_mm",
            "shadow_color",
            _("Colour of the shadow"),
            _("Choose an offset first"),
        ),
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
        colour = color_button(
            getattr(element, colour_attr), lambda v, a=colour_attr: set_attr(**{a: v})
        )
        line.add_suffix(set_attr.track(colour_attr, _named(colour, name), hint))
        group.add(line)


# --- images ------------------------------------------------------------------------


def image_rows(window: Window, row: Adw.ExpanderRow, element: ImageElement, set_attr) -> None:
    row.add_row(_crop_row(window, element, set_attr))
    for title, subtitle, rows in (
        (_("Adjust"), _("Brightness, contrast, turning and mirroring"), _image_adjust),
        (_("Black and white"), _("How black-and-white printers print the image"), _image_mono),
        (
            _("Corners and border"),
            _("Rounded corners and a border around the image"),
            _image_frame,
        ),
    ):
        row.add_row(
            subpage_row(
                window,
                element,
                title,
                subtitle,
                partial(_page, rows, window, element, set_attr),
            )
        )


def _crop_row(window: Window, element: ImageElement, set_attr) -> Adw.ActionRow:
    from etiketto.ui.image_crop import CropDialog, crop_text

    crop = Adw.ActionRow(title=_("Crop"), subtitle=crop_text(element.crop))

    def apply(area: list[float]) -> None:
        crop.set_subtitle(crop_text(area))
        set_attr(crop=area)

    def open_dialog(_button) -> None:
        try:
            dialog = CropDialog(window, element, apply)
        except Exception:  # noqa: BLE001 - a damaged image: say so, keep going
            window.toast(_("The image cannot be read"))
            return
        window.present_dialog(dialog)

    choose = Gtk.Button(label=_("Crop…"), valign=Gtk.Align.CENTER)
    choose.set_sensitive(bool(element.data))
    choose.connect("clicked", open_dialog)
    crop.add_suffix(choose)
    crop.add_suffix(
        flat_button("edit-clear-symbolic", _("Whole image"), lambda: apply([0.0, 0.0, 1.0, 1.0]))
    )
    return crop


def _image_adjust(window: Window, group, element: ImageElement, set_attr) -> None:
    for title, attr, lower, upper, step, digits in (
        (_("Brightness"), "brightness", -100, 100, 5, 0),
        (_("Contrast"), "contrast", -100, 100, 5, 0),
        (_("Gamma"), "gamma", 0.2, 5, 0.1, 1),
    ):
        group.add(
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
    group.add(switch_row(_("Invert"), element.invert, lambda v: set_attr(invert=v)))
    group.add(_rotation_row(element, set_attr))
    group.add(
        switch_row(_("Mirror"), element.mirror, lambda v: set_attr(mirror=v), _("Left and right"))
    )
    group.add(switch_row(_("Flip"), element.flip, lambda v: set_attr(flip=v), _("Top and bottom")))


def _image_mono(window: Window, group, element: ImageElement, set_attr) -> None:
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
    group.add(combo_row(_("Method"), IMAGE_MODES, element.mode, lambda v: set_attr(mode=v)))
    group.add(set_attr.track("threshold", level, _("Only for the method Threshold")))


def _image_frame(window: Window, group, element: ImageElement, set_attr) -> None:
    group.add(
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
    colour = color_button(element.border_color, lambda v: set_attr(border_color=v))
    border.add_suffix(
        set_attr.track(
            "border_color", _named(colour, _("Colour of the border")), _("Choose a width first")
        )
    )
    group.add(border)


# --- shapes ------------------------------------------------------------------------

# which rows a shape uses; a line's length and height depend on its direction
SHAPE_ROWS = {
    "line": {"direction", "style", "arrows"},
    "rectangle": {"width", "height", "style", "corner", "fill"},
    "ellipse": {"width", "height", "style", "fill"},
    "separator": {"style"},
}
LINE_EXTENT = {
    "horizontal": {"width"},
    "vertical": {"height"},
    "up": {"width", "height"},
    "down": {"width", "height"},
}


def shown_shape_rows(element: ShapeElement) -> set[str]:
    rows = set(SHAPE_ROWS[element.shape])
    if element.shape == "line":
        rows |= LINE_EXTENT[element.direction]
    return rows


def shape_rows(window: Window, row: Adw.ExpanderRow, element: ShapeElement, set_attr) -> None:
    unit = window.unit
    rows: dict[str, Adw.PreferencesRow] = {
        "direction": combo_row(
            _("Direction"), DIRECTIONS, element.direction, lambda v: changed(direction=v)
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

    def changed(**kwargs) -> None:
        set_attr(**kwargs)
        shown = shown_shape_rows(element)
        for key, widget in rows.items():
            widget.set_visible(key in shown)

    row.add_row(combo_row(_("Shape"), SHAPES, element.shape, lambda v: changed(shape=v)))
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
    line.add_suffix(_named(color_button(element.color, lambda v: set_attr(color=v)), _("Colour")))
    row.add_row(line)
    for widget in rows.values():
        row.add_row(widget)
    shown = shown_shape_rows(element)
    for key, widget in rows.items():
        widget.set_visible(key in shown)
