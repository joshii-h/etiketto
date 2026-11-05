import json
import socket
import sys
import tempfile
import types
from types import SimpleNamespace

import pytest
from PIL import Image

from etiketto import device
from etiketto.device import _connect as real_connect
from etiketto.device import nominal_width


@pytest.mark.parametrize(
    "inches, expected",
    [(2.0, 50), (1.0, 25), (0.75, 19), (0.5, 12), (0.35, 9), (0.1, 9), (3.0, 50)],
)
def test_nominal_width(inches, expected):
    assert nominal_width(inches * 25.4) == expected


# --- configured_host -------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(device.Path, "home", classmethod(lambda cls: tmp_path))
    config = tmp_path / ".config" / "labelprinter" / "config.json"
    config.parent.mkdir(parents=True)
    return config


def test_configured_host_reads_labelprinter_config(home):
    home.write_text(json.dumps({"host": "10.0.0.7", "port": 9100}))
    assert device.configured_host() == "10.0.0.7"


@pytest.mark.parametrize(
    "content", [None, "{not json", json.dumps({"host": ""}), json.dumps({"port": 1})]
)
def test_configured_host_missing_broken_or_empty(home, content):
    if content is not None:
        home.write_text(content)
    assert device.configured_host() is None


# --- query -----------------------------------------------------------------


def test_query_converts_inches_and_closes(fake_printer):
    fake_printer.printer.config = SimpleNamespace(
        model="Wedge", tape_width=0.5, tape_length_initial=100.0
    )
    fake_printer.printer.remaining = 40.0
    fake_printer.printer.states = ["BUSY"]
    info = device.query("printer.example", 1234)
    assert fake_printer.calls == [("printer.example", 1234)]
    assert info == device.PrinterInfo("Wedge", 12, 101, 254, "BUSY")
    assert fake_printer.connection.closed


def test_query_default_port(fake_printer):
    device.query("p")
    assert fake_printer.calls == [("p", 9100)]


def test_query_unknown_remaining_and_no_cassette(fake_printer):
    fake_printer.printer.config = SimpleNamespace(model="", tape_width=0, tape_length_initial=0)
    info = device.query("p")
    assert info.model == "VC-500W"
    assert info.tape_mm is None and info.remaining_mm is None and info.total_mm is None

    fake_printer.printer.config = SimpleNamespace(
        model="Wedge", tape_width=2.0, tape_length_initial=100.0
    )
    fake_printer.printer.remaining = -1.0
    info = device.query("p")
    assert info.tape_mm == 50
    assert info.remaining_mm is None and info.total_mm is None


def test_query_closes_connection_on_error(fake_printer):
    def broken():
        raise OSError("timeout")

    fake_printer.printer.get_status = broken
    with pytest.raises(OSError):
        device.query("p")
    assert fake_printer.connection.closed


# --- print_label -------------------------------------------------------------


@pytest.fixture
def tmpdir_for_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _calls(log, name):
    return [entry for entry in log if entry[0] == name]


@pytest.mark.parametrize("copies", [1, 3])
def test_print_label_copies_mode_cut_and_progress(fake_printer, tmpdir_for_jpeg, copies):
    progress = []
    image = Image.new("RGBA", (300, 120), "blue")
    device.print_label(
        image, "p", 4321, mode="normal", cut="half", copies=copies,
        progress=lambda n, total: progress.append((n, total)),
    )
    log = fake_printer.log
    assert fake_printer.calls == [("p", 4321)]
    assert _calls(log, "print_jpeg") == [("print_jpeg", "normal", "half")] * copies
    assert progress == [(n, copies) for n in range(1, copies + 1)]
    # printer idle before each copy: only the wait after printing
    assert _calls(log, "wait") == [("wait", device.PRINT_TIMEOUT)] * copies
    assert log[-1] == ("close",)
    # a real JPEG in the printer's orientation, DPI tag of the mode
    assert fake_printer.printer.jpeg_sizes == [("JPEG", (300, 120), (264, 264))] * copies
    assert list(tmpdir_for_jpeg.iterdir()) == []


def test_print_label_waits_when_busy_and_vivid_dpi(fake_printer, tmpdir_for_jpeg):
    fake_printer.printer.states = ["PRINTING", "IDLE"]
    device.print_label(Image.new("RGB", (10, 10)), "p", copies=2)
    names = [entry[0] for entry in fake_printer.log]
    assert names == [
        "get_status", "wait", "print_jpeg", "wait",
        "get_status", "print_jpeg", "wait",
        "close",
    ]
    assert _calls(fake_printer.log, "print_jpeg")[0] == ("print_jpeg", "vivid", "full")
    assert fake_printer.printer.jpeg_sizes[0][2] == (317, 317)


def test_print_label_cleans_up_after_error(fake_printer, tmpdir_for_jpeg):
    fake_printer.printer.fail_on_print = OSError("jam")
    with pytest.raises(OSError, match="jam"):
        device.print_label(Image.new("RGB", (10, 10)), "p", copies=3)
    assert len(_calls(fake_printer.log, "print_jpeg")) == 1
    assert fake_printer.connection.closed
    assert fake_printer.printer.jpeg_paths[0].startswith(str(tmpdir_for_jpeg))
    assert list(tmpdir_for_jpeg.iterdir()) == []


def test_print_label_removes_jpeg_when_connect_fails(monkeypatch, tmpdir_for_jpeg):
    def connect(host, port):
        raise OSError("unreachable")

    monkeypatch.setattr(device, "_connect", connect)
    with pytest.raises(OSError):
        device.print_label(Image.new("RGB", (10, 10)), "p")
    assert list(tmpdir_for_jpeg.iterdir()) == []


def test_connect_uses_resolved_host(monkeypatch):
    made = {}

    class Connection:
        def __init__(self, host, port, timeout):
            made["args"] = (host, port, timeout)

    class LabelPrinter:
        def __init__(self, connection):
            self.connection = connection

    monkeypatch.setitem(sys.modules, "labelprinter.connection", types.SimpleNamespace(Connection=Connection))
    monkeypatch.setitem(sys.modules, "labelprinter.printer", types.SimpleNamespace(LabelPrinter=LabelPrinter))
    monkeypatch.setattr(device, "resolve_host", lambda host: "192.0.2.1")
    connection, printer = real_connect("VC.local", 9100)
    assert made["args"] == ("192.0.2.1", 9100, device.PRINT_TIMEOUT)
    assert printer.connection is connection


# --- host resolution ---------------------------------------------------------


def _gaierror(*_args):
    raise socket.gaierror("not found")


def test_resolve_host_known_name_unchanged(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [("ok",)])
    monkeypatch.setattr(device, "_avahi_resolve", lambda host: pytest.fail("no avahi needed"))
    assert device.resolve_host("printer.lan") == "printer.lan"


def test_resolve_host_non_local_failure_propagates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    monkeypatch.setattr(device, "_avahi_resolve", lambda host: pytest.fail("no avahi for dns"))
    with pytest.raises(socket.gaierror):
        device.resolve_host("printer.example")


def test_resolve_host_local_via_avahi(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    asked = []
    monkeypatch.setattr(device, "_avahi_resolve", lambda host: asked.append(host) or "192.0.2.9")
    assert device.resolve_host("VC.local") == "192.0.2.9"
    assert asked == ["VC.local"]


def test_resolve_host_local_not_found(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    monkeypatch.setattr(device, "_avahi_resolve", lambda host: None)
    with pytest.raises(OSError, match="VC.local"):
        device.resolve_host("VC.local")


# --- Avahi over D-Bus (fake bus) ---------------------------------------------

from gi.repository import Gio, GLib  # noqa: E402


class FakeReply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class FakeBus:
    """Answers the Avahi calls device.py makes; records them."""

    def __init__(self, items=(), resolve=None, hostname=None, fail=()):
        self.items = list(items)
        self.resolve = resolve or {}
        self.hostname = hostname
        self.fail = set(fail)
        self.calls = []
        self.handler = None
        self.unsubscribed = []

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        assert (sender, iface, member) == (
            "org.freedesktop.Avahi", "org.freedesktop.Avahi.ServiceBrowser", "ItemNew",
        )
        self.handler = callback
        return 42

    def signal_unsubscribe(self, sub):
        self.unsubscribed.append(sub)

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout, cancel):
        args = params.unpack()
        self.calls.append((method, args))
        if method in self.fail:
            raise GLib.Error("failed")
        if method == "ServiceBrowserNew":
            assert args == (-1, 0, "_ipp._tcp", "local", 0)
            for item in self.items:  # announcements arrive right away
                self.handler(None, None, None, None, None, FakeReply(item))
            return FakeReply(("/browser",))
        if method == "ResolveService":
            name = args[2]
            if name not in self.resolve:
                raise GLib.Error("timeout")
            host, address = self.resolve[name]
            return FakeReply((0, 0, name, "_ipp._tcp", "local", host, 0, address, 631, [], 0))
        if method == "ResolveHostName":
            assert args == (-1, 0, args[2], 0, 0)
            return FakeReply((0, 0, args[2], 0, self.hostname or "", 0))
        raise AssertionError(method)


def _item(name):
    return (2, 0, name, "_ipp._tcp", "local", 0)


@pytest.fixture
def bus(monkeypatch):
    holder = {}

    def use(fake):
        def bus_get_sync(bus_type, cancellable):
            assert bus_type == Gio.BusType.SYSTEM
            if isinstance(fake, Exception):
                raise fake
            return fake

        monkeypatch.setattr(Gio, "bus_get_sync", bus_get_sync)
        holder["bus"] = fake
        return fake

    return use


def test_discover_filters_resolves_and_sorts(bus):
    fake = bus(
        FakeBus(
            items=[
                _item("Brother VC-500W (Kitchen)"),
                _item("Office LaserJet"),
                _item("brother vc500w"),
                _item("Brother VC-500W (Broken)"),
            ],
            resolve={
                "Brother VC-500W (Kitchen)": ("VC-500W6382.local", "192.0.2.55"),
                "brother vc500w": ("VC-500W1111.local", "192.0.2.56"),
            },
        )
    )
    found = device.discover(timeout=0.01)
    assert found == [
        device.FoundPrinter("Brother VC-500W (Kitchen)", "VC-500W6382.local", "192.0.2.55"),
        device.FoundPrinter("brother vc500w", "VC-500W1111.local", "192.0.2.56"),
    ]
    resolved = sorted(args[2] for method, args in fake.calls if method == "ResolveService")
    assert "Office LaserJet" not in resolved
    assert fake.unsubscribed == [42]


def test_discover_without_bus_or_browser(bus):
    bus(GLib.Error("no system bus"))
    assert device.discover(timeout=0.01) == []
    fake = bus(FakeBus(fail={"ServiceBrowserNew"}))
    assert device.discover(timeout=0.01) == []
    assert fake.unsubscribed == [42]  # subscription removed even on failure


def test_avahi_resolve(bus):
    bus(FakeBus(hostname="192.0.2.77"))
    assert device._avahi_resolve("VC.local") == "192.0.2.77"
    bus(FakeBus(hostname=""))
    assert device._avahi_resolve("VC.local") is None
    bus(FakeBus(fail={"ResolveHostName"}))
    assert device._avahi_resolve("VC.local") is None
    bus(GLib.Error("no bus"))
    assert device._avahi_resolve("VC.local") is None

