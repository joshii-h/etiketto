"""Bonjour / mDNS through the system's Avahi daemon on D-Bus.

Works inside Flatpak with --system-talk-name=org.freedesktop.Avahi, where the
runtime has no nss-mdns of its own.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

from gi.repository import Gio, GLib

from etiketto.backends.base import FoundPrinter

AVAHI = "org.freedesktop.Avahi"
SERVER = "org.freedesktop.Avahi.Server"
BROWSER = "org.freedesktop.Avahi.ServiceBrowser"
IF_UNSPEC, PROTO_INET = -1, 0


def _call(bus, path, interface, method, params, reply, timeout_ms):
    """A blocking Avahi method call; GLib.Error if it fails."""
    reply_type = GLib.VariantType(reply) if reply else None
    return bus.call_sync(
        AVAHI, path, interface, method, params, reply_type, Gio.DBusCallFlags.NONE, timeout_ms, None
    )


def browse(service_type: str, match: Callable[[str], bool], timeout: float = 3.0):
    """Services of a type whose name matches, resolved to host and address.

    Stops when Avahi reports that it has seen everything it knows about
    ("AllForNow"); timeout is the upper limit."""
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        seen = _collect(bus, context, service_type, match, timeout)
        found = [_resolve_service(bus, item) for item in seen.values()]
        return sorted((f for f in found if f), key=lambda f: f.name)
    except GLib.Error:
        return []
    finally:
        context.pop_thread_default()


def _collect(bus, context, service_type, match, timeout) -> dict[str, tuple]:
    seen: dict[str, tuple] = {}
    state: dict = {"path": None, "done": False}

    def on_signal(_conn, _sender, path, _iface, signal, params):
        if state["path"] and path != state["path"]:
            return  # another browser of this process
        if signal == "ItemNew":
            interface, protocol, name, stype, domain, _flags = params.unpack()
            if match(name):
                seen[name] = (interface, protocol, name, stype, domain)
        elif signal in ("AllForNow", "Failure"):
            state["done"] = True

    # subscribe before creating the browser so no announcement is missed
    sub = bus.signal_subscribe(
        AVAHI, BROWSER, None, None, None, Gio.DBusSignalFlags.NONE, on_signal
    )
    try:
        params = GLib.Variant("(iissu)", (IF_UNSPEC, PROTO_INET, service_type, "local", 0))
        reply = _call(bus, "/", SERVER, "ServiceBrowserNew", params, "(o)", 3000)
        state["path"] = reply.unpack()[0]
        deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
        while not state["done"] and GLib.get_monotonic_time() < deadline:
            if not context.iteration(False):
                GLib.usleep(10_000)
    finally:
        bus.signal_unsubscribe(sub)
        if state["path"]:
            _free(bus, state["path"])
    return seen


def _free(bus, path: str) -> None:
    try:
        _call(bus, path, BROWSER, "Free", None, None, 1000)
    except GLib.Error:
        pass  # Avahi frees it anyway when we disconnect


def _resolve_service(bus, item) -> FoundPrinter | None:
    interface, protocol, name, stype, domain = item
    params = GLib.Variant("(iisssiu)", (interface, protocol, name, stype, domain, PROTO_INET, 0))
    try:
        reply = _call(bus, "/", SERVER, "ResolveService", params, "(iissssisqaayu)", 3000).unpack()
    except GLib.Error:
        return None
    return FoundPrinter(name=name, host=reply[5], address=reply[7])


def resolve_host(host: str) -> str:
    """An address for host. Inside Flatpak *.local names are resolved through
    the host's Avahi daemon, because the runtime has no nss-mdns."""
    try:
        socket.getaddrinfo(host, None)
        return host
    except socket.gaierror:
        if not host.endswith(".local"):
            raise
    address = avahi_resolve(host)
    if not address:
        raise OSError("cannot resolve %s (mDNS)" % host)
    return address


def avahi_resolve(host: str) -> str | None:
    # interface: any, protocol: IPv4, name, address protocol: IPv4, flags
    params = GLib.Variant("(iisiu)", (IF_UNSPEC, PROTO_INET, host, PROTO_INET, 0))
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = _call(bus, "/", SERVER, "ResolveHostName", params, "(iisisu)", 5000)
        return reply.unpack()[4] or None
    except Exception:  # noqa: BLE001 - no Avahi, no bus, not found
        return None
