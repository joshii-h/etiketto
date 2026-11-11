"""The cab backend against FakeCab, on the byte level: the JScript job and the
ESC status requests are checked exactly. Answers are the ones an EOS2/300
with firmware 5.47.7 gave."""

from __future__ import annotations

import base64
import io
import re
import socket
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from PIL import Image

from etiketto.backends import cab, mdns
from etiketto.backends.base import (
    FoundPrinter,
    HeadOpen,
    LabelNotFound,
    LabelTooWide,
    OutOfLabels,
    PortClosed,
    PrinterBusy,
    PrinterError,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintOptions,
    RibbonError,
)
from etiketto.backends.cab import Cab
from etiketto.model import Label, TextElement
from etiketto.render import mm_to_px

REAL_CONNECT = cab._connect  # before the autouse fixture replaces it
MAX_SLEEPS = 5000


class FakeCab:
    """Answers ESC s, ESC l and qv; counts the labels of a job down, one per
    status request, like a printer that prints one label per poll."""

    def __init__(self):
        self.version = "5.47.7 Jan 22, 2026 (EOS2/300)"
        self.sync = "Y0020"
        self.error = "-"
        self.online = True
        self.busy_polls = 0  # status requests that still show another job
        self.jobs: list[bytes] = []
        self.requests: list[bytes] = []
        self.pending = 0
        self.closed = 0
        self.cancelled = 0
        self.silent = False  # never answer

    def status(self) -> bytes:
        if self.busy_polls:
            self.busy_polls -= 1
            return b"Y-000001Y"
        running = self.pending > 0
        answer = "%s%s%06d%s" % (
            "Y" if self.online else "N",
            self.error,
            self.pending,
            "Y" if running else "N",
        )
        if running:
            self.pending -= 1
        return answer.encode()

    def printed(self) -> int:
        return sum(int(re.search(rb"\nA (\d+)", job).group(1)) for job in self.jobs)


class FakeSocket:
    def __init__(self, device: FakeCab):
        self.device = device
        self.out = b""

    def sendall(self, data: bytes) -> None:
        device = self.device
        device.requests.append(data)
        if data == b"\x1bs":
            self.out += device.status()
        elif data == b"\x1bl":
            self.out += device.sync.encode()
        elif data == b"qv\r\n":
            self.out += device.version.encode() + b"\r"
        elif data == b"\x1bc":
            device.cancelled += 1
            device.pending = 0
        else:
            assert data.startswith(b"d PNG;"), data[:40]
            device.jobs.append(data)
            device.pending = int(re.search(rb"\nA (\d+)", data).group(1))

    def recv(self, size: int) -> bytes:
        if self.device.silent:
            raise TimeoutError("timed out")
        chunk, self.out = self.out[:size], self.out[size:]
        return chunk

    def close(self) -> None:
        self.device.closed += 1


@pytest.fixture
def printer(monkeypatch):
    device = FakeCab()
    calls = []
    sleeps = []
    now = [1000.0]

    def connect(host, port):
        calls.append((host, port))
        return FakeSocket(device)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) > MAX_SLEEPS:
            raise AssertionError("endless polling")

    monkeypatch.setattr(cab, "_connect", connect)
    monkeypatch.setattr(cab, "_sleep", sleep)
    monkeypatch.setattr(cab, "_clock", lambda: now[0])
    return SimpleNamespace(device=device, calls=calls, sleeps=sleeps, now=now)


@pytest.fixture
def backend():
    return Cab()


def _label(width=50, height=25, orientation="across", **kwargs) -> Label:
    label = Label(printer="cab", auto_length=False, orientation=orientation, **kwargs)
    label.set_media_size(width, height)
    return label


def _job_parts(job: bytes) -> tuple[Image.Image, list[str]]:
    """The downloaded image and the label commands of a job."""
    text = job.decode()
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")[:-1]
    assert lines[:2] == ["d PNG;ETIKETTO", "<BASE64>"] and lines[3] == "</BASE64>"
    image = Image.open(io.BytesIO(base64.b64decode(lines[2])))
    image.load()
    return image, lines[4:]


# --- answers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Y-000000N", (True, "-", 0, False, "IDLE")),
        ("Y-000003Y", (True, "-", 3, True, "BUSY")),
        ("Y-000000Y", (True, "-", 0, True, "IDLE")),  # interpreter still active, no job
        ("YP000000N", (True, "P", 0, False, "ERROR")),
        ("N-000000N", (False, "-", 0, False, "PAUSED")),
        ("Y-000000N\r", (True, "-", 0, False, "IDLE")),
    ],
)
def test_parse_status(raw, expected):
    status = cab.parse_status(raw)
    assert (status.online, status.error, status.pending, status.running, status.state) == expected
    assert status.idle == (not status.pending and not status.running)


@pytest.mark.parametrize("raw", ["", "Y-00000N", "X-000000N", "Y-0000000N", "hello"])
def test_parse_status_refuses_other_answers(raw):
    with pytest.raises(PrinterError, match="unexpected status"):
        cab.parse_status(raw)


@pytest.mark.parametrize(
    "error, cls",
    [
        ("P", OutOfLabels),
        ("D", HeadOpen),
        ("E", LabelNotFound),
        ("F", RibbonError),
        ("R", RibbonError),
        ("W", PrinterError),
        ("q", PrinterError),  # unknown: still an error
    ],
)
def test_status_errors(error, cls):
    found = cab.status_error(cab.parse_status("Y%s000000N" % error))
    assert type(found) is cls
    assert found.details.startswith("ESC s Y%s000000N" % error)


def test_status_without_error():
    assert cab.status_error(cab.parse_status("Y-000002Y")) is None
    paused = cab.status_error(cab.parse_status("N-000000N"))
    assert isinstance(paused, PrinterPaused) and paused.code == "paused"


@pytest.mark.parametrize(
    "raw, pitch",
    [("Y0020", 20.0), ("Y0152", 152.0), ("Y0000", None), ("N0020", None), ("junk", None)],
)
def test_parse_pitch(raw, pitch):
    assert cab.parse_pitch(raw) == pitch


@pytest.mark.parametrize(
    "raw, firmware, model, dots",
    [
        ("5.47.7 Jan 22, 2026 (EOS2/300)\r", "5.47.7 Jan 22, 2026", "EOS2/300", 12.0),
        ("5.44.2 Mar 04, 2024 (SQUIX 4/300P)", "5.44.2 Mar 04, 2024", "SQUIX 4/300P", 12.0),
        ("5.40 (MACH 4S/200)", "5.40", "MACH 4S/200", 8.0),
        ("5.40 (SQUIX 4/600)", "5.40", "SQUIX 4/600", 23.6),
        ("5.40 (EOS5/400)", "5.40", "EOS5/400", 12.0),  # unknown resolution
        ("5.40 (EOS5)", "5.40", "EOS5", 12.0),
        ("5.40", "5.40", "", 12.0),
    ],
)
def test_parse_version(raw, firmware, model, dots):
    version = cab.parse_version(raw)
    assert (version.firmware, version.model, version.dots_per_mm) == (firmware, model, dots)


# --- the job ---------------------------------------------------------------------


def test_job_places_the_image_on_a_die_cut_label():
    image = Image.new("RGB", (600, 300), "white")
    image.paste((0, 0, 0), (0, 0, 10, 10))
    job = cab.build_job(image, 12.0, 3.0, 2)
    png, commands = _job_parts(job)
    assert commands == [
        "m m",
        "J",
        "S l1;0,0,25,28,50",  # height, height + gap, width
        "I:Label;0,0,0;ETIKETTO",
        "A 2",
    ]
    assert png.mode == "1" and png.size == (600, 300)
    assert png.getpixel((5, 5)) == 0 and png.getpixel((50, 50)) == 255


def test_job_sizes_with_decimals():
    job = cab.build_job(Image.new("RGB", (610, 301), "white"), 12.0, 2.5, 0)
    _png, commands = _job_parts(job)
    assert commands[2] == "S l1;0,0,25.08,27.58,50.83"
    assert commands[-1] == "A 1"  # at least one label


def test_mm_formatting():
    assert [cab._mm(v) for v in (25, 25.0, 25.5, 25.125, 0.004)] == [
        "25",
        "25",
        "25.5",
        "25.12",
        "0",
    ]


# --- backend ---------------------------------------------------------------------


def test_capabilities(backend):
    caps = backend.capabilities
    assert not caps.continuous and caps.tape_widths_mm == ()
    assert caps.color_model == "mono" and caps.cut_modes == ()
    assert caps.label_sizes[0] == cab.LabelSize(50, 25)
    assert caps.max_width_mm == 105.6
    assert backend.raster(25, PrintOptions()).dpi == pytest.approx(304.8)
    assert cab.dpi_for(8.0) == pytest.approx(203.2)


def test_query(backend, printer):
    info = backend.query("printer04.local")
    assert printer.calls == [("printer04.local", 9100)]
    assert printer.device.requests == [b"\x1bs", b"qv\r\n", b"\x1bl"]
    assert (info.model, info.firmware, info.state, info.error) == (
        "EOS2/300",
        "5.47.7 Jan 22, 2026",
        "IDLE",
        "",
    )
    assert info.tape_mm is None and info.pitch_mm == 20.0
    assert printer.device.closed == 1
    assert backend.display_name(info) == "cab EOS2/300"
    assert backend.display_name(None) == backend.name


def test_query_reports_printer_problems(backend, printer):
    printer.device.error = "P"
    info = backend.query("printer04.local")
    assert (info.state, info.error) == ("ERROR", "out_of_labels")


def test_query_learns_the_resolution(backend, printer):
    printer.device.version = "5.40 (MACH 4S/200)"
    printer.device.sync = "N0000"
    info = backend.query("printer04.local")
    assert info.pitch_mm is None
    assert backend.dots_per_mm == 8.0
    assert backend.raster(25, PrintOptions()).dpi == pytest.approx(203.2)


def test_prepare_across_the_roll(backend):
    label = _label(50, 25, elements=[TextElement(text="A")])
    image = backend.prepare(label, PrintOptions())
    assert image.size == (600, 300)  # 12 dots per mm, x across the print head
    assert {c for _n, c in image.getcolors()} <= {(0, 0, 0), (255, 255, 255)}


def test_prepare_along_the_roll_turns_the_content(backend, png_b64):
    """Content along the roll: the start of the label comes first off the
    roll (top of the printed image), like on tape."""
    from conftest import ink_bbox

    from etiketto.model import ImageElement

    def label(orientation):
        lab = _label(50, 25, orientation=orientation, align="start", margin_mm=0)
        lab.elements = [ImageElement(data=png_b64((10, 10), "black"))]
        return lab

    across = backend.prepare(label("across"), PrintOptions())
    along = backend.prepare(label("along"), PrintOptions())
    assert along.size == across.size == (600, 300)
    safe = mm_to_px(1.5, 305)
    x0, y0, _x1, _y1 = ink_bbox(across, "white")
    assert (x0, y0) == (safe, safe)  # starts at the left edge
    x0, y0, x1, _y1 = ink_bbox(along, "white")
    assert y0 == safe and x0 > safe and 600 - x1 > safe  # at the top, centred across


def test_prepare_ignores_automatic_length(backend):
    label = _label(50, 25)
    label.auto_length = True  # never set for die-cut labels, but harmless
    assert backend.prepare(label, PrintOptions()).size == (600, 300)
    assert label.auto_length  # the label itself is not changed


def test_print(backend, printer):
    image = backend.prepare(_label(50, 25), PrintOptions())
    events = []
    result = backend.print_label(
        image, "printer04.local", PrintOptions(copies=3, gap_mm=2), events.append
    )
    assert result is None
    (job,) = printer.device.jobs
    _png, commands = _job_parts(job)
    assert commands[2:] == ["S l1;0,0,25,27,50", "I:Label;0,0,0;ETIKETTO", "A 3"]
    assert [e.copy for e in events] == [1, 1, 2, 3, 3] and {e.copies for e in events} == {3}
    assert printer.device.closed == 1
    assert all(s == cab.POLL_INTERVAL for s in printer.sleeps)


def test_print_waits_for_a_running_job(backend, printer):
    printer.device.busy_polls = 3
    backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert printer.device.printed() == 1
    sent = printer.device.requests.index(printer.device.jobs[0])
    assert printer.device.requests[:sent] == [b"qv\r\n"] + [b"\x1bs"] * 4


def test_print_waits_while_the_interpreter_is_still_active(backend, printer, monkeypatch):
    """After a text command (qv) the printer reports "interpreter active" with
    no labels pending for about a second (EOS2/300, firmware 5.47.7)."""
    states = iter([b"Y-000000Y", b"Y-000000Y", b"Y-000000N", b"Y-000001Y", b"Y-000000N"])
    monkeypatch.setattr(FakeCab, "status", lambda self: next(states))
    backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    sent = printer.device.requests.index(printer.device.jobs[0])
    assert sent == 4  # after qv and three status requests


def test_print_gives_up_on_a_printer_that_stays_busy(backend, printer):
    printer.device.busy_polls = 10_000
    with pytest.raises(PrinterBusy):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert printer.device.jobs == []


def test_print_job_that_never_shows_up(backend, printer, monkeypatch):
    """A job printed between two polls: done after the grace time."""
    monkeypatch.setattr(FakeCab, "status", lambda self: b"Y-000000N")
    backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert len(printer.device.jobs) == 1
    assert sum(printer.sleeps) >= cab.START_GRACE


def test_print_stuck_job_times_out(backend, printer, monkeypatch):
    def status(self):
        return b"Y-000001Y" if self.jobs else b"Y-000000N"

    monkeypatch.setattr(FakeCab, "status", status)
    with pytest.raises(PrinterTimeout, match="stayed busy"):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert printer.device.cancelled == 1  # not printed later behind the user's back


def test_print_error_while_printing(backend, printer, monkeypatch):
    states = iter([b"Y-000000N", b"Y-000002Y", b"YP000001N"])
    monkeypatch.setattr(FakeCab, "status", lambda self: next(states))
    with pytest.raises(OutOfLabels):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions(copies=2))
    assert printer.device.requests[-1] == b"\x1bc"


def test_print_job_not_done_while_the_image_is_read(backend, printer, monkeypatch):
    """Interpreter active, nothing pending, idle once: the job has not run
    yet; it is done only after its labels were counted down."""
    states = iter(
        [b"Y-000000N", b"Y-000000Y", b"Y-000000N", b"Y-000000Y", b"Y-000002Y", b"Y-000001Y"]
        + [b"Y-000000N"]
    )
    monkeypatch.setattr(FakeCab, "status", lambda self: next(states))
    events = []
    backend.print_label(
        Image.new("RGB", (600, 300), "white"), "h", PrintOptions(copies=2), events.append
    )
    assert [e.copy for e in events] == [1, 1, 2, 2]
    assert printer.device.cancelled == 0


def test_print_cancel_survives_a_closed_connection(backend, printer, monkeypatch):
    states = iter([b"Y-000000N", b"YP000001N"])
    monkeypatch.setattr(FakeCab, "status", lambda self: next(states))
    real = FakeSocket.sendall

    def sendall(self, data):
        if data == b"\x1bc":
            raise BrokenPipeError("gone")
        real(self, data)

    monkeypatch.setattr(FakeSocket, "sendall", sendall)
    with pytest.raises(OutOfLabels):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())


def test_print_learns_the_resolution_and_rescales(backend, printer):
    """Rendered for 12 dots/mm, but the printer turns out to be a /200."""
    image = backend.prepare(_label(50, 25), PrintOptions())
    assert image.info["dots_per_mm"] == 12.0 and image.size == (600, 300)
    printer.device.version = "5.40 (MACH 4S/200)"
    backend.print_label(image, "h", PrintOptions())
    png, commands = _job_parts(printer.device.jobs[0])
    assert png.size == (400, 200) and commands[2] == "S l1;0,0,25,28,50"
    assert backend.dots_per_mm == 8.0


def test_print_keeps_the_resolution_if_the_printer_does_not_say(backend, printer):
    printer.device.version = "5.40"
    image = backend.prepare(_label(105.6, 25), PrintOptions())
    assert image.width == 1267  # the widest label, not rounded up beyond it
    backend.print_label(image, "h", PrintOptions())
    png, commands = _job_parts(printer.device.jobs[0])
    assert png.size == (1267, 300) and commands[2] == "S l1;0,0,25,28,105.58"


def test_query_keeps_the_status_if_text_commands_time_out(backend, printer, monkeypatch):
    def version(self):
        raise PrinterTimeout("timed out")

    monkeypatch.setattr(cab._Session, "version", version)
    printer.device.error = "D"
    info = backend.query("h")
    assert (info.error, info.model, info.pitch_mm) == ("head_open", "cab", None)
    assert backend.dots_per_mm == 12.0


def test_print_refused_while_head_is_open(backend, printer):
    printer.device.error = "D"
    with pytest.raises(HeadOpen):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert printer.device.jobs == []


def test_print_refuses_labels_wider_than_the_head(backend, printer):
    with pytest.raises(LabelTooWide) as caught:
        backend.print_label(Image.new("RGB", (1300, 300), "white"), "h", PrintOptions())
    assert caught.value.params == {"max_mm": 105.6}
    assert printer.calls == []


# --- connection errors --------------------------------------------------------------


@pytest.mark.parametrize(
    "error, cls",
    [
        (socket.gaierror("Name or service not known"), PrinterNotFound),
        (OSError("cannot resolve x.local (mDNS)"), PrinterNotFound),
        (ConnectionRefusedError("refused"), PortClosed),
        (TimeoutError("timed out"), PrinterTimeout),
        (OSError("No route to host"), PrinterNotFound),
        (ValueError(""), PrinterError),
    ],
)
def test_connection_errors(backend, monkeypatch, error, cls):
    def connect(host, port):
        raise error

    monkeypatch.setattr(cab, "_connect", connect)
    with pytest.raises(cls) as caught:
        backend.query("h")
    assert type(caught.value) is cls
    assert caught.value.details


def test_printer_that_does_not_answer(backend, printer):
    printer.device.silent = True
    with pytest.raises(PrinterTimeout):
        backend.query("h")
    assert printer.device.closed == 1


def test_connection_closed_mid_answer(backend, printer, monkeypatch):
    monkeypatch.setattr(FakeSocket, "recv", lambda self, size: b"")
    with pytest.raises(PrinterTimeout, match="connection closed"):
        backend.query("h")


def test_answer_with_garbage(backend, printer, monkeypatch):
    monkeypatch.setattr(FakeCab, "status", lambda self: b"?????????")
    with pytest.raises(PrinterError, match="unexpected status"):
        backend.query("h")


def test_real_connect_resolves_through_mdns(monkeypatch):
    seen = []
    monkeypatch.setattr(mdns, "resolve_host", lambda host: "192.0.2.21")
    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout: seen.append((address, timeout))
    )
    REAL_CONNECT("printer04.local", 9100)
    assert seen == [(("192.0.2.21", 9100), cab.SOCKET_TIMEOUT)]


# --- discovery --------------------------------------------------------------------


def test_discover(backend, monkeypatch):
    calls = []
    found = [FoundPrinter("cab EOS2/300 (aa:bb:cc)", "printer04.local", "192.0.2.21")]

    def browse(service_type, match, timeout):
        calls.append((service_type, timeout))
        assert match("cab EOS2/300 (aa:bb:cc)") and match("CAB SQUIX 4/300")
        assert not match("HP LaserJet") and not match("Brother VC-500W")
        return found

    monkeypatch.setattr(mdns, "browse", browse)
    assert backend.discover(timeout=1.5) == found
    assert calls == [("_pdl-datastream._tcp", 1.5)]


class TestContract(BackendContract):
    backend_id = "cab"

    @pytest.fixture
    def harness(self, printer, monkeypatch):
        found = [FoundPrinter("cab EOS2/300 (aa:bb:cc)", "printer04.local", "192.0.2.21")]
        monkeypatch.setattr(mdns, "browse", lambda service_type, match, timeout: found)

        def fail():
            printer.device.error = "E"

        return SimpleNamespace(backend=Cab(), fail=fail, printed=printer.device.printed)
