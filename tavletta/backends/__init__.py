"""Printer backends and their registry.

Built-in backends are registered on first use; third-party packages can add
their own through the entry point group "tavletta.backends" (the entry point
names a Backend subclass or instance).
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from tavletta.backends.base import Backend
from tavletta.model import DEFAULT_PRINTER

ENTRY_POINT_GROUP = "tavletta.backends"

_registry: dict[str, Backend] = {}
_loaded = False
log = logging.getLogger(__name__)


def register(backend: Backend) -> Backend:
    if not backend.id:
        raise ValueError("a backend needs an id")
    _registry[backend.id] = backend
    return backend


def unregister(backend_id: str) -> None:
    _registry.pop(backend_id, None)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from tavletta.backends.brother_ql import BrotherQL, BrotherQLDieCut
    from tavletta.backends.cab import Cab
    from tavletta.backends.dymo import Dymo
    from tavletta.backends.epl import Epl
    from tavletta.backends.escpos import EscPos
    from tavletta.backends.niimbot import Niimbot
    from tavletta.backends.ptouch import PTouch
    from tavletta.backends.system import SystemPrinter
    from tavletta.backends.tspl import Tspl
    from tavletta.backends.vc500w import VC500W
    from tavletta.backends.zpl import Zpl

    for backend in (
        VC500W(),
        Cab(),
        PTouch(),
        BrotherQL(),
        BrotherQLDieCut(),
        Niimbot(),
        SystemPrinter(),
        Zpl(),
        Epl(),
        Tspl(),
        EscPos(),
        Dymo(),
    ):
        register(backend)
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = entry.load()
            register(obj() if isinstance(obj, type) else obj)
        except Exception:  # noqa: BLE001 - a broken plugin must not stop the app
            log.exception("cannot load printer backend %s", entry.name)


def backends() -> list[Backend]:
    """All registered backends, the default first."""
    _load()
    return sorted(_registry.values(), key=lambda b: (b.id != DEFAULT_PRINTER, b.name.casefold()))


def get_backend(backend_id: str = DEFAULT_PRINTER) -> Backend:
    """KeyError for unknown ids."""
    _load()
    return _registry[backend_id]
