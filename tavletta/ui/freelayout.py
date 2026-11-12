"""The rows of the free layout (tavletta.freelayout) in the sidebar: the
choice of layout in the "Content" group, each element's position and size
on a page of its own, a text's size in mm, a font's weights, a QR code's
error correction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Adw, Gtk, Pango, PangoCairo  # noqa: E402

from tavletta import codes, freelayout  # noqa: E402
from tavletta.model import (  # noqa: E402
    MAX_MODULE_MM,
    MAX_SIZE_MM,
    CodeElement,
    ImageElement,
    TextElement,
)
from tavletta.ui import styles  # noqa: E402
from tavletta.ui.i18n import _  # noqa: E402
from tavletta.ui.texts import ROTATIONS  # noqa: E402
from tavletta.ui.widgets import combo_row, length_row, spin_row  # noqa: E402
from tavletta.variables import expand_label  # noqa: E402

if TYPE_CHECKING:
    from tavletta.ui.window import Window

LAYOUTS = [("flow", _("One after another")), ("free", _("Free"))]
# what adopt() sets: taken over from a copy with the variables filled in
ADOPTED = ("x_mm", "y_mm", "size_pt", "module_mm", "height_mm", "size_mm", "width_mm")


def is_free(window: Window) -> bool:
    return window.label.layout == "free"


def description(window: Window) -> str:
    """The "Content" group's description for the label's layout."""
    if is_free(window):
        return _("Each element at its own position, from the label's top left corner")
    return _("Elements are placed one after another along the tape")


def layout_row(window: Window) -> Adw.ComboRow:
    return combo_row(_("Layout"), LAYOUTS, window.label.layout, lambda v: set_layout(window, v))


def set_layout(window: Window, layout: str) -> None:
    """Switch the label's layout. To the free layout for the first time
    (every element still at 0, 0), the elements keep the positions and
    sizes they had one after another (freelayout.adopt)."""
    label = window.label
    if layout == label.layout:
        return
    if layout == "free" and all(e.x_mm == 0 and e.y_mm == 0 for e in label.elements):
        expanded, _warnings = expand_label(label, window.preview_values(), window.data.row)
        freelayout.adopt(expanded, window.print_dpi(), window.printer_settings.safe_mm)
        for element, placed in zip(label.elements, expanded.elements, strict=True):
            for name in ADOPTED:
                if hasattr(element, name):
                    setattr(element, name, getattr(placed, name))
    label.layout = "free" if layout == "free" else "flow"
    window.rebuild_sidebar()
    window.mark_changed()


def fine_row(window: Window, title: str, mm: float, upper: float, on_change) -> Adw.SpinRow:
    """A length to a hundredth of a millimetre (a thousandth of an inch):
    positions and module sizes as other label designers give them."""
    unit = window.unit
    digits = 2 if unit.key == "mm" else 3
    return spin_row(
        title,
        unit.to_display(mm, digits),
        0,
        unit.to_display(upper, digits),
        unit.step(0.1),
        digits,
        lambda v: on_change(round(unit.from_display(v), 4)),
        subtitle=unit.symbol,
    )


def position_row(window: Window, element, set_attr) -> Adw.ActionRow:
    """The page of the element's position: across the tape and the space
    after it, or in the free layout where it is and how large."""
    if not is_free(window):
        return styles.position_row(window, element, set_attr)
    return styles.subpage_row(
        window,
        element,
        _("Position and size"),
        _("From the label's top left corner"),
        lambda group: _free_rows(window, group, element, set_attr),
    )


def _free_rows(window: Window, group, element, set_attr) -> None:
    hint = {
        TextElement: _("To the top left corner of the letters' ink"),
        CodeElement: _("To the top left corner of the first module, without the quiet zone"),
    }.get(type(element), "")
    x = fine_row(window, "X", element.x_mm, MAX_SIZE_MM, lambda v: set_attr(x_mm=v))
    y = fine_row(window, "Y", element.y_mm, MAX_SIZE_MM, lambda v: set_attr(y_mm=v))
    for row in (x, y):
        if hint:
            row.set_subtitle("%s – %s" % (window.unit.symbol, hint))
        group.add(row)
    if isinstance(element, CodeElement):
        _code_rows(window, group, element, set_attr)
    elif isinstance(element, ImageElement):
        group.add(
            length_row(
                window.unit,
                _("Height"),
                element.height_mm or freelayout.FREE_SIZE_MM,
                0.5,
                MAX_SIZE_MM,
                0.5,
                lambda v: set_attr(height_mm=v),
            )
        )


def _code_rows(window: Window, group, element: CodeElement, set_attr) -> None:
    module = fine_row(
        window,
        _("Module size"),
        element.module_mm or _default_module(element),
        MAX_MODULE_MM,
        lambda v: set_attr(module_mm=v),
    )
    module.set_subtitle(
        _("%(unit)s, rounded to whole printer dots") % {"unit": window.unit.symbol}
    )
    group.add(module)
    if codes.SPECS[element.symbology].layout not in ("matrix", "stacked"):
        group.add(
            length_row(
                window.unit,
                _("Bar height"),
                element.height_mm or _default_bars(element),
                1,
                MAX_SIZE_MM,
                0.5,
                lambda v: set_attr(height_mm=v),
            )
        )
    group.add(
        combo_row(
            _("Rotation"),
            [(str(k), text) for k, text in ROTATIONS],
            str(element.rotation),
            lambda v: set_attr(rotation=int(v)),
        )
    )


def _default_module(element: CodeElement) -> float:
    matrix = codes.SPECS[element.symbology].layout in ("matrix", "stacked")
    return freelayout.FREE_MODULE_MM if matrix else freelayout.FREE_BAR_MM


def _default_bars(element: CodeElement) -> float:
    from tavletta.render import BAR_HEIGHT_MM, BAR_MODULE_MM

    return round(_default_module(element) * BAR_HEIGHT_MM / BAR_MODULE_MM, 1)


def text_size_row(window: Window, element: TextElement, set_attr) -> Adw.SpinRow:
    """The font size in the free layout: the font's height (its em) in mm,
    as label designers give it; stored in points (TextElement.size_pt)."""
    mm = freelayout.pt_to_mm(element.size_pt) if element.size_pt > 0 else freelayout.FREE_TEXT_MM
    row = fine_row(
        window, _("Size"), mm, 100, lambda v: set_attr(size_pt=freelayout.mm_to_pt(max(0.1, v)))
    )
    row.set_subtitle(_("%(unit)s, the font's height") % {"unit": window.unit.symbol})
    return row


# --- the weights of a font -------------------------------------------------------------


def font_weights(family: str) -> list[tuple[int, str]]:
    """The weights the family has as fonts of their own (not slanted, not
    made up by the renderer), with the font's name for them: (300,
    "Light"), (600, "Semibold") …; regular and bold if it has none."""
    try:
        found = PangoCairo.FontMap.get_default().get_family(family)
    except (TypeError, AttributeError):  # pragma: no cover - Pango before 1.46
        found = None
    weights: dict[int, str] = {}
    for face in found.list_faces() if found else []:
        desc = face.describe()
        if face.is_synthesized() or desc.get_style() != Pango.Style.NORMAL:
            continue
        weights.setdefault(int(desc.get_weight()), face.get_face_name())
    if not weights:
        weights = {400: _("Regular"), 700: _("Bold")}
    return sorted(weights.items())


class WeightRow(Adw.ComboRow):
    """The font's weight: as the bold button says (0), or one of the
    family's weights (TextElement.weight)."""

    def __init__(self, element: TextElement, set_attr):
        super().__init__(title=_("Weight"))
        self.element, self.set_attr = element, set_attr
        self.keys: list[int] = []
        self._filling = False
        self.connect("notify::selected", self._selected)
        self.set_family(element.font)

    def set_family(self, family: str) -> None:
        weights = font_weights(family or "Sans")
        if self.element.weight and self.element.weight not in dict(weights):
            weights = sorted([*weights, (self.element.weight, str(self.element.weight))])
        self.keys = [0] + [weight for weight, _name in weights]
        names = [_("As the bold button sets it")] + [name for _weight, name in weights]
        self._filling = True
        self.set_model(Gtk.StringList.new(names))
        self.set_selected(self.keys.index(self.element.weight))
        self._filling = False

    def show_default(self) -> None:
        """The bold button was used: the weight follows it again."""
        self._filling = True
        self.set_selected(0)
        self._filling = False

    def _selected(self, _row, _pspec) -> None:
        if self._filling:
            return
        self.set_attr(weight=self.keys[self.get_selected()])


# --- a QR code's error correction ---------------------------------------------------------


def error_correction_row(element: CodeElement, set_attr) -> Adw.ComboRow:
    items = [
        ("", _("Standard (medium)")),
        # translators: the share of a QR code that may be damaged and still read
        ("L", _("Low (%d %%)") % 7),
        ("M", _("Medium (%d %%)") % 15),
        ("Q", _("Quartile (%d %%)") % 25),
        ("H", _("High (%d %%)") % 30),
    ]
    row = combo_row(
        _("Error correction"),
        items,
        element.error_correction,
        lambda v: set_attr(error_correction=v),
    )
    row.set_subtitle(_("How much of the code may be damaged and still be read"))
    row.set_visible(element.symbology == "qr")
    return row
