"""Bonjour / mDNS through the system's Avahi daemon on D-Bus.

Works inside Flatpak with --system-talk-name=org.freedesktop.Avahi, where the
runtime has no nss-mdns of its own.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from gi.repository import Gio, GLib

from tavletta.backends.base import FoundPrinter

AVAHI = "org.freedesktop.Avahi"
SERVER = "org.freedesktop.Avahi.Server"
BROWSER = "org.freedesktop.Avahi.ServiceBrowser"
IF_UNSPEC, PROTO_INET = -1, 0


def _call(bus, path, interface, method, params, reply, timeout_ms):
    """A blocking Avahi method call; GLib.Error if it fails."""
    reply_type = GLib.VariantType(reply) if reply else None
    return bus.call_sync(
        AVAHI,
        path,
        interface,
        method,
        params,
        reply_type,
        Gio.DBusCallFlags.NONE,
        timeout_ms,
        None,
    )


@dataclass(frozen=True)
class Service:
    """A resolved DNS-SD service: what browse() reports, plus port and the
    TXT record (keys in lower case), e.g. an IPP printer's "rp" path."""

    name: str
    host: str
    address: str
    port: int
    txt: dict[str, str]


def browse(service_type: str, match: Callable[[str], bool], timeout: float = 3.0):
    """Printers of a service type whose name matches, as FoundPrinter."""
    return [
        FoundPrinter(name=s.name, host=s.host, address=s.address)
        for s in browse_services(service_type, match, timeout)
    ]


def browse_services(
    service_type: str, match: Callable[[str], bool], timeout: float = 3.0
) -> list[Service]:
    """Services of a type whose name matches, resolved.

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
    with suppress(GLib.Error):  # Avahi frees it anyway when we disconnect
        _call(bus, path, BROWSER, "Free", None, None, 1000)


def _resolve_service(bus, item) -> Service | None:
    interface, protocol, name, stype, domain = item
    params = GLib.Variant("(iisssiu)", (interface, protocol, name, stype, domain, PROTO_INET, 0))
    try:
        reply = _call(bus, "/", SERVER, "ResolveService", params, "(iissssisqaayu)", 3000).unpack()
    except GLib.Error:
        return None
    return Service(name, reply[5], reply[7], reply[8], _txt(reply[9]))


def _txt(record) -> dict[str, str]:
    """TXT strings ("key=value", as byte arrays) as a dict."""
    entries = {}
    for raw in record:
        text = bytes(raw).decode("utf-8", "replace")
        key, _sep, value = text.partition("=")
        if key:
            entries[key.lower()] = value
    return entries


def resolve_host(host: str, family: int = socket.AF_UNSPEC) -> str:
    """One numeric address for host, IPv4 if it has one (printers on port
    9100 answer there; labelprinter only speaks IPv4, so the VC-500W asks
    for AF_INET). Inside Flatpak *.local names are resolved through the
    host's Avahi daemon, because the runtime has no nss-mdns.

    One address: socket.create_connection() with a name tries every address
    it resolves to and waits the full timeout for each, so a name with many
    dead addresses would hang (CPython gh-106283). Control characters are
    refused: getaddrinfo() silently cuts a name at a null byte and would
    resolve another host (CPython gh-160898)."""
    if not host or any(ord(c) < 32 or c == "\x7f" for c in host):
        raise OSError("cannot resolve %r: not a host name" % host)
    try:
        found = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
    except UnicodeError as e:  # e.g. "a..b": the idna codec refuses it
        raise OSError("cannot resolve %r: not a host name" % host) from e
    except socket.gaierror:
        if not host.endswith(".local"):
            raise
    else:
        return _address(found)
    address = avahi_resolve(host)
    if not address:
        raise OSError("cannot resolve %s (mDNS)" % host)
    return address


def _address(found: list) -> str:
    """The IPv4 address if there is one, else the first; an IPv6 link-local
    address keeps its interface ("fe80::1%3"), it is useless without."""
    ipv4 = [info for info in found if info[0] == socket.AF_INET]
    sockaddr = (ipv4 or found)[0][4]
    address = str(sockaddr[0])
    if len(sockaddr) > 3 and sockaddr[3] and "%" not in address:
        address += "%%%d" % sockaddr[3]  # scope id
    return address


def avahi_resolve(host: str) -> str | None:
    # interface: any, protocol: IPv4, name, address protocol: IPv4, flags
    params = GLib.Variant("(iisiu)", (IF_UNSPEC, PROTO_INET, host, PROTO_INET, 0))
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = _call(bus, "/", SERVER, "ResolveHostName", params, "(iisisu)", 5000)
    except Exception:  # noqa: BLE001 - no Avahi, no bus, not found
        return None
    return reply.unpack()[4] or None
