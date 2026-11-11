"""Display units: millimetres or inches, chosen from the locale.

The model always stores millimetres; only the interface converts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MM_PER_INCH = 25.4

# Territories that use US customary units for everyday lengths
IMPERIAL_TERRITORIES = {"US", "LR", "MM"}

# Brother's own names for the cassettes in the US (CZ-1001 ... CZ-1005)
INCH_TAPE_NAMES = {9: "3/8″", 12: "1/2″", 19: "3/4″", 25: "1″", 50: "2″"}


def locale_territory(environ=None) -> str | None:
    """Territory of the measurement locale, e.g. "US" for en_US.UTF-8."""
    environ = os.environ if environ is None else environ
    for variable in ("LC_ALL", "LC_MEASUREMENT", "LANG"):
        value = environ.get(variable)
        if value:
            locale = value.split(".")[0].split("@")[0]
            if "_" in locale:
                return locale.split("_", 1)[1].upper()
            return None
    return None


def default_unit(environ=None) -> str:
    return "in" if locale_territory(environ) in IMPERIAL_TERRITORIES else "mm"


@dataclass(frozen=True)
class Unit:
    key: str  # "mm" or "in"
    symbol: str
    factor: float  # display units per millimetre
    digits: int

    def to_display(self, mm: float, digits: int | None = None) -> float:
        """Rounded to the digits the spin button shows, so that focusing it
        does not change the value (and mark the label as changed)."""
        return round(mm * self.factor, self.digits if digits is None else digits)

    def from_display(self, value: float) -> float:
        return value / self.factor

    def step(self, mm_step: float) -> float:
        """A sensible spin step in display units for a step given in mm."""
        if self.key == "mm":
            return mm_step
        return 0.01 if mm_step < 1 else 0.05 if mm_step < 5 else 0.1

    def format(self, mm: float) -> str:
        return "%.*f %s" % (self.digits, mm * self.factor, self.symbol)

    def format_long(self, mm: float) -> str:
        """Tape lengths: metres or feet like on the cassette box."""
        if self.key == "mm":
            return "%s m" % ("%.1f" % (mm / 1000)).removesuffix(".0")
        return "%.1f ft" % (mm / MM_PER_INCH / 12)

    def tape_name(self, width_mm: float) -> str:
        if self.key == "in":
            name = INCH_TAPE_NAMES.get(round(width_mm)) if width_mm == round(width_mm) else None
            return name or "%.2f″" % (width_mm / MM_PER_INCH)
        return "%s mm" % _number(width_mm)

    def label_size(self, width_mm: float, length_mm: float) -> str:
        if self.key == "in":
            return "%s × %.2f″" % (self.tape_name(width_mm), length_mm / MM_PER_INCH)
        return "%s × %.0f mm" % (_number(width_mm), length_mm)

    def size(self, width_mm: float, height_mm: float) -> str:
        """A die-cut label: width × height."""
        if self.key == "in":
            return "%.2f × %.2f″" % (width_mm / MM_PER_INCH, height_mm / MM_PER_INCH)
        return "%s × %s mm" % (_number(width_mm), _number(height_mm))


def _number(mm: float) -> str:
    """25 -> "25", 50.8 -> "50.8"."""
    return ("%.1f" % mm).removesuffix(".0")


MILLIMETRES = Unit("mm", "mm", 1.0, 1)
INCHES = Unit("in", "in", 1 / MM_PER_INCH, 2)


def unit_for(setting: str | None, environ=None) -> Unit:
    """setting: "mm", "in" or "auto"/None (from the locale)."""
    key = setting if setting in ("mm", "in") else default_unit(environ)
    return INCHES if key == "in" else MILLIMETRES
