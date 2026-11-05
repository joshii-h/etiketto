"""Talk to the VC-500W through labelprinter (native protocol, port 9100)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from etiketto.model import TAPE_WIDTHS_MM

DEFAULT_PORT = 9100
PRINT_TIMEOUT = 180


@dataclass
class PrinterInfo:
    model: str
    tape_mm: int | None  # nominal cassette width, None = no cassette
    remaining_mm: int | None
    total_mm: int | None
    state: str


def nominal_width(width_mm: float) -> int:
    return min(TAPE_WIDTHS_MM, key=lambda w: abs(w - width_mm))


def configured_host() -> str | None:
    """Host from labelprinter's config (~/.config/labelprinter/config.json).

    Not visible inside the Flatpak sandbox; there the host is entered in the app.
    """
    config = Path.home() / ".config" / "labelprinter" / "config.json"
    try:
        return json.loads(config.read_text()).get("host") or None
    except (OSError, ValueError):
        return None


@dataclass
class FoundPrinter:
    name: str  # service name, e.g. "Brother VC-500W (Etikettendrucker)"
    host: str  # mDNS host name, e.g. "VC-500W6382.local"
    address: str  # IPv4 address


def discover(timeout: float = 3.0) -> list[FoundPrinter]:
    """Find VC-500W printers announced over Bonjour, via the system's Avahi
    daemon on D-Bus (works inside Flatpak with --system-talk-name)."""
    try:
        from gi.repository import Gio, GLib
    except ImportError:  # pragma: no cover - PyGObject is a hard dependency
        return []
    context = GLib.MainContext.new()
    context.push_thread_default()
    seen: dict[str, tuple] = {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        def on_item(_conn, _sender, _path, _iface, _signal, params):
            interface, protocol, name, stype, domain, _flags = params.unpack()
            if "VC-500W" in name.upper().replace(" ", "-") or "VC500W" in name.upper():
                seen[name] = (interface, protocol, name, stype, domain)

        # subscribe before creating the browser so no announcement is missed
        sub = bus.signal_subscribe(
            "org.freedesktop.Avahi",
            "org.freedesktop.Avahi.ServiceBrowser",
            "ItemNew",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_item,
        )
        try:
            bus.call_sync(
                "org.freedesktop.Avahi",
                "/",
                "org.freedesktop.Avahi.Server",
                "ServiceBrowserNew",
                GLib.Variant("(iissu)", (-1, 0, "_ipp._tcp", "local", 0)),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
            while GLib.get_monotonic_time() < deadline:
                context.iteration(False) or GLib.usleep(20_000)
        finally:
            bus.signal_unsubscribe(sub)

        found = []
        for interface, protocol, name, stype, domain in seen.values():
            try:
                reply = bus.call_sync(
                    "org.freedesktop.Avahi",
                    "/",
                    "org.freedesktop.Avahi.Server",
                    "ResolveService",
                    GLib.Variant("(iisssiu)", (interface, protocol, name, stype, domain, 0, 0)),
                    GLib.VariantType("(iissssisqaayu)"),
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                ).unpack()
            except GLib.Error:
                continue
            found.append(FoundPrinter(name=name, host=reply[5], address=reply[7]))
        return sorted(found, key=lambda f: f.name)
    except GLib.Error:
        return []
    finally:
        context.pop_thread_default()


def resolve_host(host: str) -> str:
    """Return an address for host. Inside Flatpak the runtime has no nss-mdns,
    so *.local names are resolved through the host's Avahi daemon over D-Bus."""
    try:
        socket.getaddrinfo(host, None)
        return host
    except socket.gaierror:
        if not host.endswith(".local"):
            raise
    address = _avahi_resolve(host)
    if not address:
        raise OSError("cannot resolve %s (mDNS); enter the printer's IP address" % host)
    return address


def _avahi_resolve(host: str) -> str | None:
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync(
            "org.freedesktop.Avahi",
            "/",
            "org.freedesktop.Avahi.Server",
            "ResolveHostName",
            # interface: any, protocol: IPv4, name, address protocol: IPv4, flags
            GLib.Variant("(iisiu)", (-1, 0, host, 0, 0)),
            GLib.VariantType("(iisisu)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return reply.unpack()[4] or None
    except Exception:  # noqa: BLE001 - no Avahi, no bus, not found
        return None


def _connect(host: str, port: int):
    from labelprinter.connection import Connection
    from labelprinter.printer import LabelPrinter

    connection = Connection(resolve_host(host), port, PRINT_TIMEOUT)
    return connection, LabelPrinter(connection)


def query(host: str, port: int = DEFAULT_PORT) -> PrinterInfo:
    connection, printer = _connect(host, port)
    try:
        config = printer.get_configuration()
        status = printer.get_status()
    finally:
        connection.close()
    tape = nominal_width(config.tape_width * 25.4) if config.tape_width else None
    remaining = total = None
    if config.tape_length_initial and status.tape_length_remaining != -1.0:
        total = int(config.tape_length_initial * 2.54)
        remaining = int(status.tape_length_remaining * 2.54)
    return PrinterInfo(config.model or "VC-500W", tape, remaining, total, status.print_state)


def print_label(
    image: Image.Image,
    host: str,
    port: int = DEFAULT_PORT,
    mode: str = "vivid",
    cut: str = "full",
    copies: int = 1,
    progress=None,
) -> None:
    """Print an image in printer orientation (width = tape width)."""
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="etiketto-")
    os.close(fd)
    try:
        # The printer scales the image to the tape width (autofit), so the DPI
        # tag is informative only; it matches the mode's line density.
        dpi = 317 if mode == "vivid" else 264
        image.convert("RGB").save(path, quality=95, dpi=(dpi, dpi))
        connection, printer = _connect(host, port)
        try:
            for n in range(1, copies + 1):
                if progress:
                    progress(n, copies)
                status = printer.get_status()
                if status.print_state != "IDLE":
                    printer.wait_to_turn_idle(PRINT_TIMEOUT)
                with open(path, "rb") as handle:
                    printer.print_jpeg(handle, mode, cut)
                printer.wait_to_turn_idle(PRINT_TIMEOUT)
        finally:
            connection.close()
    finally:
        os.unlink(path)
