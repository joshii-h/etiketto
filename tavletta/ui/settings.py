"""settings.json: global settings and the settings of each printer model."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import GLib

from tavletta.backends.base import Backend, ColorAdjust, PrinterInfo, PrintOptions
from tavletta.model import DEFAULT_PRINTER
from tavletta.render import DEFAULT_SAFE_MM

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "tavletta"
TEMPLATE_DIR = Path(GLib.get_user_data_dir()) / "tavletta" / "templates"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# settings of early versions that belong to the VC-500W
LEGACY_PRINTER_KEYS = ("host", "safe_mm", "cut", "mode")


def migrate(data: dict) -> dict:
    """Move the printer settings of early versions to data["printers"]["vc500w"]."""
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
        key = self.backend.settings_id
        entry = printers.get(key)
        if not isinstance(entry, dict):
            entry = printers[key] = {}
        self.backend.migrate_settings(entry)
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

    def backend_settings(self) -> dict[str, object]:
        """The values of the backend's own settings (Capabilities.settings;
        a connected model has the same keys, maybe other values) for
        PrintOptions.settings; the backend checks them (Backend.setting)."""
        keys = [setting.key for setting in self.backend.capabilities.settings]
        return {key: self.get(key) for key in keys if self.get(key) is not None}

    @property
    def data_format(self) -> str:
        """The backend's default format (rawrgb for the VC-500W) unless
        settings.json names another supported one ("data_format": "jpeg")."""
        formats = self.backend.capabilities.data_formats
        value = self.get("data_format")
        return value if value in formats else formats[0]

    # --- the choices of the last print, for the dialog, the preview and the CLI

    @staticmethod
    def _one_of(value, choices) -> str:
        choices = tuple(choices)
        return value if value in choices else (choices or ("",))[0]

    def cut(self, info: PrinterInfo | None) -> str:
        return self._one_of(self.get("cut", "full"), self.backend.cut_modes(info))

    def mode(self, info: PrinterInfo | None) -> str:
        return self._one_of(self.get("mode"), [m.id for m in self.backend.modes(info)])

    def quality(self, info: PrinterInfo | None) -> str:
        return self._one_of(self.get("quality"), self.backend.qualities(info))

    def extra_switch(self, name: str, info: PrinterInfo | None) -> bool | None:
        """chain or high_res as last chosen; None if the printer has no such extra."""
        if name not in self.backend.extras(info):
            return None
        return self.get(name) is True

    def feed_mm(self, info: PrinterInfo | None) -> float | None:
        """The tape feed as last chosen, within the printer's range; None
        if the printer has no feed setting."""
        if "feed" not in self.backend.extras(info):
            return None
        low, high, default = self.backend.feed_mm(info)
        feed = self.get("feed_mm", default)
        ok = isinstance(feed, (int, float)) and not isinstance(feed, bool)
        return min(high, max(low, float(feed))) if ok else default

    def options(
        self, info: PrinterInfo | None = None, copies: int = 1, gap_mm: float = 3.0, **chosen
    ) -> PrintOptions:
        """The options of a job as last chosen, valid for the backend and the
        connected printer: what the print dialog offers, the preview shows
        and the command line prints with. `chosen` overrides some."""
        values: dict = {
            "copies": copies,
            "cut": self.cut(info),
            "mode": self.mode(info),
            "quality": self.quality(info),
            "color": self.color_adjust,
            "data_format": self.data_format,
            "gap_mm": gap_mm,
            "settings": self.backend_settings(),
            "info": info,
            "chain": self.extra_switch("chain", info) is True,
            "high_res": self.extra_switch("high_res", info) is True,
            "feed_mm": self.feed_mm(info),
        }
        values.update(chosen)
        return PrintOptions(**values)
