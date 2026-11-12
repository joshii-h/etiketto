"""Shared fixtures: images, labels, a fake VC-500W and a GTK window.

The printer is never contacted: vc500w._connect is always replaced, either by
a refusal (autouse) or by FakeVC500W, which answers the protocol like the
device does. The GUI tests replace the backend's query/print/discover.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from etiketto.backends import cab, get_backend, ptouch, usb, vc500w
from etiketto.backends.base import PrinterInfo, Progress
from etiketto.model import Label


def _png_b64(size=(20, 10), color="red", mode="RGB", fmt="PNG", exif=None) -> str:
    buf = io.BytesIO()
    img = Image.new(mode, size, color)
    if exif is not None:
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def png_b64():
    """Factory: base64 encoded image file of the given size and colour."""
    return _png_b64


@pytest.fixture
def make_label():
    """Factory for labels; defaults keep renders small and font independent."""

    def factory(**kwargs) -> Label:
        kwargs.setdefault("tape_mm", 25)
        kwargs.setdefault("elements", [])
        return Label(**kwargs)

    return factory


def ink_bbox(image: Image.Image, background) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1 exclusive) of everything that is not background."""
    blank = Image.new(image.mode, image.size, background)
    return ImageChops.difference(image.convert(blank.mode), blank).getbbox()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Safety net: no test may open a real connection to the printer."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("network access to the printer in a test")

    monkeypatch.setattr(vc500w, "_connect", refuse)
    monkeypatch.setattr(cab, "_connect", refuse)
    monkeypatch.setattr(ptouch, "_connect", refuse)
    monkeypatch.setattr(ptouch, "_browse", refuse)  # nor search the network
    # nor see the USB printers of the machine the tests run on
    monkeypatch.setattr(usb, "SYSFS", Path("/nonexistent/usbmisc"))
    monkeypatch.setattr(usb, "USB_DEVICES", Path("/nonexistent/usb-devices"))


# --- fake VC-500W on the protocol level ------------------------------------

XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'


def answer(code=0, comment="", payload=None) -> str:
    """A status answer as the printer sends it; read answers carry a payload
    after the status block (labelprinter skips two bytes before it)."""
    head = XML_HEAD + "<status>\n<code>%d</code>\n<comment>%s</comment>\n" % (code, comment)
    if payload is None:
        return head + "</status>\n"
    return head + "<datasize>%d</datasize>\n</status>\n\n" % (len(payload) + 1) + payload


def status(state="IDLE", stage="SUCCESS", error="NONE", remain=150.0) -> dict:
    return {"state": state, "stage": stage, "error": error, "remain": remain}


class FakeVC500W:
    """Answers config/status/print requests; records everything."""

    def __init__(self):
        self.config = {
            "model_name": "Wedge",
            "serial_number": "B0123456789",
            "fw_version_number": "2026031504",
            "crossweb_dpi": "320",
            "cassette_type": "1",
            "width_inches": "1.000",
            "width_inches_media": "0.950",
            "media_id": "ABC123456XYZ",
            "cassette_sn": "180529-0116G133390-0084",
            "media_length_initial": "197",
        }
        self.modes = [("vivid", 0, 317), ("color", 1, 264), ("bw", 2, 400)]
        self.statuses = [status()]  # one per status request, the last repeats
        self.after_print = []  # statuses after the data, before turning idle again
        self.print_code = 0
        self.print_codes: list[int] = []  # answers to the next print requests, then print_code
        self.print_comment = ""
        self.log: list = []
        self.closed = 0

    def config_xml(self) -> str:
        fields = "".join("<%s>%s</%s>\n" % (k, v, k) for k, v in self.config.items())
        modes = "".join(
            "<media_mode>\n<mode>%s</mode>\n<speed>%d</speed>\n<lpi>%d</lpi>\n</media_mode>\n" % m
            for m in self.modes
        )
        return XML_HEAD + "<config>\n" + fields + modes + "</config>\n"

    def status_xml(self) -> str:
        s = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        remain = "" if s["remain"] is None else "<remain>%s</remain>\n" % s["remain"]
        return (
            XML_HEAD
            + "<status>\n<print_state>%(state)s</print_state>\n" % s
            + "<print_job_stage>%(stage)s</print_job_stage>\n" % s
            + "<print_job_error>%(error)s</print_job_error>\n" % s
            + remain
            + "</status>\n"
        )

    def prints(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "print"]

    def data(self) -> list[bytes]:
        return [entry[1] for entry in self.log if entry[0] == "data"]

    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]


class FakeConnection:
    def __init__(self, device: FakeVC500W):
        self.device = device
        self.pending: list[str] = []

    def send_message(self, question):
        xml = question.get_data()
        assert xml.startswith(XML_HEAD)
        device = self.device
        if "<path>/config.xml</path>" in xml:
            device.log.append(("config",))
            self.pending.append(answer(payload=device.config_xml()))
        elif "<path>/status.xml</path>" in xml:
            device.log.append(("status",))
            self.pending.append(answer(payload=device.status_xml()))
        else:
            assert "<print>" in xml, xml
            device.log.append(("print", xml[len(XML_HEAD) :]))
            code = device.print_codes.pop(0) if device.print_codes else device.print_code
            self.pending.append(answer(code, device.print_comment))

    def get_message(self, long_timeout=False, buffer_size=4096):
        if not self.pending:
            raise TimeoutError("timed out")
        self.device.log.append(("read", long_timeout))
        return self.pending.pop(0)

    def send_file(self, handle):
        device = self.device
        device.log.append(("data", handle.read()))
        device.statuses = list(device.after_print) + [status()]
        self.pending.append(answer())

    def close(self):
        self.device.closed += 1
        self.device.log.append(("close",))


MAX_SLEEPS = 5000  # a polling loop that never ends fails instead of hanging


@pytest.fixture
def printer(monkeypatch):
    """A fake VC-500W behind vc500w._connect; no real sleeping: the fake
    clock only advances by the time vc500w sleeps."""
    device = FakeVC500W()
    calls = []
    waits = []
    sleeps = []
    now = [1000.0]

    def connect(host, port, wait=None):
        calls.append((host, port))
        waits.append(wait)
        return FakeConnection(device)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) > MAX_SLEEPS:
            raise AssertionError("endless polling")

    monkeypatch.setattr(vc500w, "_connect", connect)
    monkeypatch.setattr(vc500w, "_sleep", sleep)
    monkeypatch.setattr(vc500w, "_clock", lambda: now[0])
    return SimpleNamespace(device=device, calls=calls, waits=waits, sleeps=sleeps, now=now)


# --- GTK -------------------------------------------------------------------

_gtk_state: dict = {}


def gtk_available() -> bool:
    if "ok" not in _gtk_state:
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.overrides import Gtk as GtkOverride
            from gi.repository import Adw, Gtk

            # PyGObject's own init_check() at import time is what counts:
            # without a display widgets refuse to be created (or crash)
            ok = bool(getattr(GtkOverride, "initialized", True)) and Gtk.init_check()
            if ok:
                Adw.init()
            _gtk_state["ok"] = bool(ok)
        except (ImportError, ValueError):
            _gtk_state["ok"] = False
    return _gtk_state["ok"]


def pump(condition=None, timeout=3.0) -> bool:
    """Run the GLib main loop until condition() is true (or once if None)."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        while context.iteration(False):
            pass
        if condition is None or condition():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)


@pytest.fixture(scope="session")
def gtk_app():
    if not gtk_available():
        pytest.skip("no display for GTK")
    from gi.repository import Adw, Gio

    application = Adw.Application(
        application_id="xyz.joshuahirsig.Etiketto.Tests",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    application.register(None)
    return application


@pytest.fixture
def gui_env(monkeypatch, tmp_path):
    """The VC-500W backend with faked query/print/discover; config in tmp_path."""
    if not gtk_available():
        pytest.skip("no display for GTK")
    from etiketto.ui import settings as config

    backend = get_backend("vc500w")
    env = SimpleNamespace(
        queries=[],
        prints=[],
        discovers=0,
        query_result=PrinterInfo("Wedge", 25, 4000, 5000, "IDLE"),
        query_error=None,
        print_error=None,
        print_result=None,
        stages=[],  # printer stages reported while printing
        found=[],
        configured=None,
        tmp=tmp_path,
        backend=backend,
    )

    def query(host):
        env.queries.append(host)
        if env.query_error:
            raise env.query_error
        return env.query_result

    def print_label(image, host, options, progress=None):
        env.prints.append((image, host, options))
        for stage in [""] + env.stages:
            progress(Progress(1, options.copies, stage))
        if env.print_error:
            raise env.print_error
        return env.print_result

    def discover(timeout=3.0):
        env.discovers += 1
        return list(env.found)

    monkeypatch.setattr(backend, "query", query)
    monkeypatch.setattr(backend, "print_label", print_label)
    monkeypatch.setattr(backend, "discover", discover)
    monkeypatch.setattr(backend, "configured_host", lambda: env.configured)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "config" / "settings.json")
    monkeypatch.setattr(config, "TEMPLATE_DIR", tmp_path / "templates")
    monkeypatch.delenv("ETIKETTO_SCREENSHOT", raising=False)
    monkeypatch.setenv("LC_ALL", "de_CH.UTF-8")  # units: automatic = mm
    return env


@pytest.fixture
def make_window(gtk_app, gui_env):
    """Factory for windows with recorded toasts; settings can be preset."""
    from etiketto.ui import window as ui_window
    from etiketto.ui.settings import Settings

    windows = []

    def factory(settings=None, settle=True):
        if settings is not None:
            Settings(settings).save()
        win = ui_window.Window(gtk_app)
        win.toast_log = []
        win.toasts.add_toast = win.toast_log.append
        windows.append(win)
        if settle:  # let the startup printer search/query finish
            pump(lambda: not _busy(win))
            render_now(win)
        return win

    yield factory
    for win in windows:
        win.dirty = False
        win.print_flow.busy = False
        if win._render_source:
            from gi.repository import GLib

            GLib.source_remove(win._render_source)
            win._render_source = 0
        win.destroy()
    pump()


@pytest.fixture
def window(make_window):
    return make_window()


def _busy(win) -> bool:
    title = win.printer.row.get_title()
    return title in ("Searching…", "Connecting…", "Searching for printers…")


def render_now(win) -> None:
    """Run the debounced preview render immediately."""
    from gi.repository import GLib

    if win._render_source:
        GLib.source_remove(win._render_source)
        win._render_source = 0
    win.render_preview()


def toast_titles(win) -> list[str]:
    return [t.get_title() for t in win.toast_log]
