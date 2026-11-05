"""Shared fixtures: images, labels, a fake label printer and a GTK window.

The printer is never contacted: device._connect (and for the GUI tests
device.query/print_label/discover) are always replaced by fakes.
"""

from __future__ import annotations

import base64
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from etiketto import device
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

    monkeypatch.setattr(device, "_connect", refuse)


# --- fake labelprinter -----------------------------------------------------


class FakeConnection:
    def __init__(self, log):
        self.log = log
        self.closed = False

    def close(self):
        self.closed = True
        self.log.append(("close",))


class FakeLabelPrinter:
    """Records every call; states/config/status are set by the test."""

    def __init__(self, log):
        self.log = log
        self.states = ["IDLE"]  # print_state returned by successive get_status() calls
        self.config = SimpleNamespace(model="Wedge", tape_width=2.0, tape_length_initial=100.0)
        self.remaining = 40.0
        self.fail_on_print = None  # exception raised by print_jpeg
        self.jpeg_paths = []
        self.jpeg_sizes = []

    def get_configuration(self):
        self.log.append(("get_configuration",))
        return self.config

    def get_status(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        self.log.append(("get_status",))
        return SimpleNamespace(print_state=state, tape_length_remaining=self.remaining)

    def wait_to_turn_idle(self, timeout):
        self.log.append(("wait", timeout))

    def print_jpeg(self, handle, mode, cut):
        self.jpeg_paths.append(handle.name)
        data = handle.read()
        with Image.open(io.BytesIO(data)) as img:
            self.jpeg_sizes.append((img.format, img.size, img.info.get("dpi")))
        self.log.append(("print_jpeg", mode, cut))
        if self.fail_on_print:
            raise self.fail_on_print


@pytest.fixture
def fake_printer(monkeypatch):
    """Replace device._connect; returns (printer, connection, log, connect_calls)."""
    log: list = []
    connection = FakeConnection(log)
    printer = FakeLabelPrinter(log)
    calls = []

    def connect(host, port):
        calls.append((host, port))
        return connection, printer

    monkeypatch.setattr(device, "_connect", connect)
    return SimpleNamespace(printer=printer, connection=connection, log=log, calls=calls)


# --- GTK -------------------------------------------------------------------

_gtk_state: dict = {}


def gtk_available() -> bool:
    if "ok" not in _gtk_state:
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.overrides import Gtk as gtk_override
            from gi.repository import Adw, Gtk

            # PyGObject's own init_check() at import time is what counts:
            # without a display widgets refuse to be created (or crash)
            ok = bool(getattr(gtk_override, "initialized", True)) and Gtk.init_check()
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
    """Mocked device functions and config paths in tmp_path."""
    if not gtk_available():
        pytest.skip("no display for GTK")
    from etiketto import app

    env = SimpleNamespace(
        queries=[],
        prints=[],
        discovers=0,
        query_result=device.PrinterInfo("Wedge", 25, 400, 500, "IDLE"),
        query_error=None,
        print_error=None,
        found=[],
        configured=None,
        tmp=tmp_path,
    )

    def query(host, port=device.DEFAULT_PORT):
        env.queries.append(host)
        if env.query_error:
            raise env.query_error
        return env.query_result

    def print_label(image, host, **kwargs):
        env.prints.append((image, host, kwargs))
        if kwargs.get("progress"):
            kwargs["progress"](1, kwargs.get("copies", 1))
        if env.print_error:
            raise env.print_error

    def discover(timeout=3.0):
        env.discovers += 1
        return list(env.found)

    monkeypatch.setattr(device, "query", query)
    monkeypatch.setattr(device, "print_label", print_label)
    monkeypatch.setattr(device, "discover", discover)
    monkeypatch.setattr(device, "configured_host", lambda: env.configured)
    monkeypatch.setattr(app, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "config" / "settings.json")
    monkeypatch.setattr(app, "TEMPLATE_DIR", tmp_path / "templates")
    monkeypatch.delenv("ETIKETTO_SCREENSHOT", raising=False)
    monkeypatch.setenv("LC_ALL", "de_CH.UTF-8")  # units: automatic = mm
    return env


@pytest.fixture
def make_window(gtk_app, gui_env):
    """Factory for windows with recorded toasts; settings can be preset."""
    from etiketto import app

    windows = []

    def factory(settings=None, settle=True):
        if settings is not None:
            app.save_settings(settings)
        win = app.Window(gtk_app)
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
        win.printing = False
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
    title = win.printer_row.get_title()
    return title in ("Searching…", "Connecting…", "Searching for printers…")


def render_now(win) -> None:
    """Run the debounced preview render immediately."""
    from gi.repository import GLib

    if win._render_source:
        GLib.source_remove(win._render_source)
        win._render_source = 0
    win._do_render()


def toast_titles(win) -> list[str]:
    return [t.get_title() for t in win.toast_log]
