"""settings.json: global settings and the settings of each printer model."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import GLib

from etiketto.backends.base import Backend, ColorAdjust
from etiketto.model import DEFAULT_PRINTER
from etiketto.render import DEFAULT_SAFE_MM

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "etiketto"
TEMPLATE_DIR = Path(GLib.get_user_data_dir()) / "etiketto" / "templates"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# settings of Etiketto 0.1 that belong to the VC-500W
LEGACY_PRINTER_KEYS = ("host", "safe_mm", "cut", "mode")


def migrate(data: dict) -> dict:
    """Move the printer settings of Etiketto 0.1 to data["printers"]["vc500w"]."""
    legacy = {key: data.pop(key) for key in LEGACY_PRINTER_KEYS if key in data}
    if not legacy:
        return data
    if not isinstance(data.get("printers"), dict):
        data["printers"] = {}
    entry = data["printers"].setdefault(DEFAULT_PRINTER, {})
    if legacy.get("mode") == "normal":  # labelprinter's name for "color"
        legacy["mode"] = "color"
    for key, value in legacy.items():
        entry.setdefault(key, value)
    return data


MAX_TRUSTED_DATA = 200  # label and data file pairs the user agreed to read


def _data_pair(label_file: Path, data_file: Path) -> list[str]:
    """As stored: both resolved, so a link changed later is asked about again."""
    return [str(label_file.absolute().resolve()), str(data_file.resolve())]


class Settings:
    """settings.json: a dict, saved after each change."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(migrate(data) if isinstance(data, dict) else {})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # a leftover could have other permissions
        # only for this user: it names files and printers of this computer
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(self.data, indent=1))
        tmp.replace(SETTINGS_FILE)

    @property
    def units(self) -> str | None:
        return self.data.get("units")

    def set_units(self, value) -> None:
        self.data["units"] = value
        self.save()

    def trusts_data(self, label_file: Path, data_file: Path) -> bool:
        """Did the user agree to read this data file for this label file?"""
        return _data_pair(label_file, data_file) in self._trusted()

    def trust_data(self, label_file: Path, data_file: Path) -> None:
        pairs = [p for p in self._trusted() if p != _data_pair(label_file, data_file)]
        pairs.append(_data_pair(label_file, data_file))
        self.data["trusted_data"] = pairs[-MAX_TRUSTED_DATA:]
        self.save()

    def has_trusted_data(self) -> bool:
        return bool(self._trusted())

    def forget_trusted_data(self) -> None:
        self.data.pop("trusted_data", None)
        self.save()

    def _trusted(self) -> list[list[str]]:
        pairs = self.data.get("trusted_data")
        return [p for p in pairs if isinstance(p, list)] if isinstance(pairs, list) else []

    def printer(self, backend: Backend) -> PrinterSettings:
        return PrinterSettings(self, backend)


class PrinterSettings:
    """The settings of one printer model: data["printers"][backend.id].
    Junk (a non-dict "printers" or entry) is repaired on access."""

    def __init__(self, settings: Settings, backend: Backend):
        self.settings = settings
        self.backend = backend

    @property
    def _entry(self) -> dict:
        # looked up on every access: the settings dict may be replaced meanwhile
        data = self.settings.data
        printers = data.get("printers")
        if not isinstance(printers, dict):
            printers = data["printers"] = {}
        entry = printers.get(self.backend.id)
        if not isinstance(entry, dict):
            entry = printers[self.backend.id] = {}
        return entry

    def get(self, key, default=None):
        return self._entry.get(key, default)

    def update(self, **values) -> None:
        self._entry.update(values)
        self.settings.save()

    @property
    def host(self) -> str | None:
        return self.get("host") or self.backend.configured_host()

    @property
    def safe_mm(self) -> float:
        value = self.get("safe_mm", DEFAULT_SAFE_MM)
        return float(value) if isinstance(value, (int, float)) else DEFAULT_SAFE_MM

    @property
    def color_adjust(self) -> ColorAdjust:
        return ColorAdjust.from_dict(self.get("color_adjust"))

    @property
    def data_format(self) -> str:
        """The backend's default format (rawrgb for the VC-500W) unless
        settings.json names another supported one ("data_format": "jpeg")."""
        formats = self.backend.capabilities.data_formats
        value = self.get("data_format")
        return value if value in formats else formats[0]
