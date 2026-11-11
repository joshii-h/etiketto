"""Avahi over D-Bus with a fake bus that checks the D-Bus signatures."""

import socket

import pytest
from gi.repository import Gio, GLib

from etiketto.backends import mdns
from etiketto.backends.base import FoundPrinter

# Avahi's D-Bus API: method -> (argument signature, reply signature)
SIGNATURES = {
    "ServiceBrowserNew": ("(iissu)", "(o)"),
    "ResolveService": ("(iisssiu)", "(iissssisqaayu)"),
    "ResolveHostName": ("(iisiu)", "(iisisu)"),
    "Free": (None, None),
}
BROWSER_PATH = "/Client1/ServiceBrowser1"


class FakeReply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class FakeBus:
    """Answers the Avahi calls mdns.py makes; records them."""

    def __init__(self, items=(), resolve=None, hostname=None, fail=(), all_for_now=True):
        self.items = list(items)
        self.resolve = resolve or {}
        self.hostname = hostname
        self.fail = set(fail)
        self.all_for_now = all_for_now
        self.calls = []
        self.handler = None
        self.unsubscribed = []

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        assert (sender, iface, member, path) == (mdns.AVAHI, mdns.BROWSER, None, None)
        self.handler = callback
        # GDBus calls back in the thread-default context of the subscriber
        self.context = GLib.MainContext.get_thread_default()
        return 42

    def signal_unsubscribe(self, sub):
        self.unsubscribed.append(sub)

    def emit(self, signal, params, path=BROWSER_PATH):
        """Deliver a signal the next time the subscriber's context runs."""

        def deliver(*_args):
            self.handler(None, ":1.5", path, mdns.BROWSER, signal, FakeReply(params))
            return False

        source = GLib.idle_source_new()
        source.set_callback(deliver)
        source.attach(self.context)

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout, cancel):
        assert name == mdns.AVAHI
        args_sig, reply_sig = SIGNATURES[method]
        assert (params.get_type_string() if params else None) == args_sig, method
        assert (reply_type.dup_string() if reply_type else None) == reply_sig, method
        args = params.unpack() if params else ()
        self.calls.append((method, path, args))
        if method in self.fail:
            raise GLib.Error("failed")
        return getattr(self, "_" + method)(path, iface, args)

    def _ServiceBrowserNew(self, path, iface, args):
        assert (path, iface) == ("/", mdns.SERVER)
        assert args == (-1, 0, "_ipp._tcp", "local", 0)
        # signals arrive while the call returns; the browser filters by path
        self.emit("ItemNew", _item("VC-500W (other browser)"), path="/Client1/ServiceBrowser9")
        for item in self.items:
            self.emit("ItemNew", item)
        if self.all_for_now:
            self.emit("AllForNow", ())
        return FakeReply((BROWSER_PATH,))

    def _ResolveService(self, path, iface, args):
        name = args[2]
        assert args[5:] == (0, 0)  # IPv4 address, no flags
        if name not in self.resolve:
            raise GLib.Error("timeout")
        host, address, *txt = self.resolve[name]
        record = txt[0] if txt else []
        return FakeReply((0, 0, name, "_ipp._tcp", "local", host, 0, address, 631, record, 0))

    def _ResolveHostName(self, path, iface, args):
        assert args == (-1, 0, args[2], 0, 0)
        return FakeReply((0, 0, args[2], 0, self.hostname or "", 0))

    def _Free(self, path, iface, args):
        assert (path, iface) == (BROWSER_PATH, mdns.BROWSER)
        return

    def methods(self):
        return [method for method, _path, _args in self.calls]


def _item(name):
    return (2, 0, name, "_ipp._tcp", "local", 0)


@pytest.fixture
def bus(monkeypatch):
    def use(fake):
        def bus_get_sync(bus_type, cancellable):
            assert bus_type == Gio.BusType.SYSTEM
            if isinstance(fake, Exception):
                raise fake
            return fake

        monkeypatch.setattr(Gio, "bus_get_sync", bus_get_sync)
        return fake

    return use


def is_vc(name):
    return "VC-500W" in name


def test_browse_filters_resolves_sorts_and_frees(bus):
    fake = bus(
        FakeBus(
            items=[
                _item("VC-500W (Kitchen)"),
                _item("Office LaserJet"),
                _item("VC-500W (Attic)"),
                _item("VC-500W (Broken)"),
            ],
            resolve={
                "VC-500W (Kitchen)": ("VC-500W6382.local", "192.0.2.55"),
                "VC-500W (Attic)": ("VC-500W1111.local", "192.0.2.56"),
            },
        )
    )
    assert mdns.browse("_ipp._tcp", is_vc, timeout=5) == [
        FoundPrinter("VC-500W (Attic)", "VC-500W1111.local", "192.0.2.56"),
        FoundPrinter("VC-500W (Kitchen)", "VC-500W6382.local", "192.0.2.55"),
    ]
    resolved = sorted(args[2] for method, _p, args in fake.calls if method == "ResolveService")
    # not "VC-500W (other browser)": a signal for another browser object
    assert resolved == ["VC-500W (Attic)", "VC-500W (Broken)", "VC-500W (Kitchen)"]
    assert fake.methods()[:1] == ["ServiceBrowserNew"] and fake.methods()[1] == "Free"
    assert fake.unsubscribed == [42]


def test_browse_stops_at_all_for_now(bus):
    bus(FakeBus(items=[_item("VC-500W")], resolve={"VC-500W": ("h.local", "192.0.2.1")}))
    start = GLib.get_monotonic_time()
    assert len(mdns.browse("_ipp._tcp", is_vc, timeout=10)) == 1
    assert GLib.get_monotonic_time() - start < 2_000_000  # far below the 10 s


def test_browse_waits_until_timeout_without_all_for_now(bus):
    fake = bus(FakeBus(all_for_now=False))
    start = GLib.get_monotonic_time()
    assert mdns.browse("_ipp._tcp", is_vc, timeout=0.15) == []
    assert GLib.get_monotonic_time() - start >= 150_000
    assert "Free" in fake.methods()


def test_browse_failure_signal_ends_the_search(bus, monkeypatch):
    fake = bus(FakeBus(all_for_now=False))
    original = fake._ServiceBrowserNew

    def failing(path, iface, args):
        reply = original(path, iface, args)
        fake.emit("Failure", ("Daemon gone",))
        return reply

    monkeypatch.setattr(fake, "_ServiceBrowserNew", failing)
    start = GLib.get_monotonic_time()
    assert mdns.browse("_ipp._tcp", is_vc, timeout=10) == []
    assert GLib.get_monotonic_time() - start < 2_000_000


def test_browse_without_bus_or_browser(bus):
    bus(GLib.Error("no system bus"))
    assert mdns.browse("_ipp._tcp", is_vc, timeout=0.01) == []
    fake = bus(FakeBus(fail={"ServiceBrowserNew"}))
    assert mdns.browse("_ipp._tcp", is_vc, timeout=0.01) == []
    assert fake.unsubscribed == [42]  # subscription removed even on failure
    assert "Free" not in fake.methods()  # there is no browser to free


def test_browse_free_failure_is_ignored(bus):
    fake = bus(FakeBus(fail={"Free"}))
    assert mdns.browse("_ipp._tcp", is_vc, timeout=1) == []
    assert "Free" in fake.methods()


def test_avahi_resolve(bus):
    bus(FakeBus(hostname="192.0.2.77"))
    assert mdns.avahi_resolve("VC.local") == "192.0.2.77"
    bus(FakeBus(hostname=""))
    assert mdns.avahi_resolve("VC.local") is None
    bus(FakeBus(fail={"ResolveHostName"}))
    assert mdns.avahi_resolve("VC.local") is None
    bus(GLib.Error("no bus"))
    assert mdns.avahi_resolve("VC.local") is None


# --- host resolution ---------------------------------------------------------


def _gaierror(*_args, **_kwargs):
    raise socket.gaierror("not found")


V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::7", 0, 0, 0))
LINK_LOCAL = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::7", 0, 0, 3))


def _v4(n):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.%d" % n, 0))


@pytest.mark.parametrize(
    "found, address",
    [
        ([_v4(7), _v4(8)], "192.0.2.7"),  # many: only one, or each waits the timeout
        ([V6, _v4(7)], "192.0.2.7"),  # IPv4 even if IPv6 comes first
        ([V6], "2001:db8::7"),
        ([LINK_LOCAL], "fe80::7%3"),  # the interface stays
    ],
)
def test_resolve_host_gives_one_address(monkeypatch, found, address):
    asked = []

    def getaddrinfo(host, port, family, type):
        asked.append(family)
        return found

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(mdns, "avahi_resolve", lambda host: pytest.fail("no avahi needed"))
    assert mdns.resolve_host("printer.lan") == address
    mdns.resolve_host("printer.lan", socket.AF_INET)
    assert asked == [socket.AF_UNSPEC, socket.AF_INET]


def test_resolve_host_names_the_idna_codec_refuses(monkeypatch):
    def getaddrinfo(*_args):
        raise UnicodeError("label empty or too long")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(OSError, match="not a host name"):
        mdns.resolve_host("a..b")


@pytest.mark.parametrize("host", ["", "printer.lan\x00.evil", "printer\nlan", "a\x7fb"])
def test_resolve_host_refuses_control_characters(monkeypatch, host):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("not resolved"))
    with pytest.raises(OSError, match="not a host name"):
        mdns.resolve_host(host)


def test_resolve_host_non_local_failure_propagates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    monkeypatch.setattr(mdns, "avahi_resolve", lambda host: pytest.fail("no avahi for dns"))
    with pytest.raises(socket.gaierror):
        mdns.resolve_host("printer.example")


def test_resolve_host_local_via_avahi(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    asked = []
    monkeypatch.setattr(mdns, "avahi_resolve", lambda host: asked.append(host) or "192.0.2.9")
    assert mdns.resolve_host("VC.local") == "192.0.2.9"
    assert asked == ["VC.local"]


def test_resolve_host_local_not_found(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    monkeypatch.setattr(mdns, "avahi_resolve", lambda host: None)
    with pytest.raises(OSError, match="VC.local"):
        mdns.resolve_host("VC.local")


def test_browse_services_gives_port_and_txt(bus):
    record = [b"rp=ipp/print", b"PDL=application/pdf,image/urf", b"=no key", [102, 108]]
    bus(
        FakeBus(
            items=[_item("VC-500W (Office)")],
            resolve={"VC-500W (Office)": ("hp.local", "192.0.2.20", record)},
        )
    )
    services = mdns.browse_services("_ipp._tcp", is_vc, timeout=5)
    txt = {"rp": "ipp/print", "pdl": "application/pdf,image/urf", "fl": ""}
    assert services == [mdns.Service("VC-500W (Office)", "hp.local", "192.0.2.20", 631, txt)]
