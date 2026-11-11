"""Printer backends and their registry.

Built-in backends are registered on first use; third-party packages can add
their own through the entry point group "etiketto.backends" (the entry point
names a Backend subclass or instance).
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from etiketto.backends.base import Backend
from etiketto.model import DEFAULT_PRINTER

DEFAULT_BACKEND = DEFAULT_PRINTER  # files without a printer are for it
ENTRY_POINT_GROUP = "etiketto.backends"

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
    from etiketto.backends.cab import Cab
    from etiketto.backends.vc500w import VC500W

    register(VC500W())
    register(Cab())
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = entry.load()
            register(obj() if isinstance(obj, type) else obj)
        except Exception:  # noqa: BLE001 - a broken plugin must not stop the app
            log.exception("cannot load printer backend %s", entry.name)


def backends() -> list[Backend]:
    """All registered backends, the default first."""
    _load()
    return sorted(_registry.values(), key=lambda b: (b.id != DEFAULT_BACKEND, b.name))


def get_backend(backend_id: str = DEFAULT_BACKEND) -> Backend:
    """KeyError for unknown ids."""
    _load()
    return _registry[backend_id]
