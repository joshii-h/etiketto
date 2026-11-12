"""The cab backend against FakeCab, on the byte level: the JScript job and the
ESC status requests are checked exactly. Answers are the ones an EOS2/300
with firmware 5.47.7 gave."""

from __future__ import annotations

import base64
import http.server
import io
import json
import re
import socket
import threading
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from PIL import Image

from tavletta.backends import cab, mdns, net, usb
from tavletta.backends.base import (
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
    PrinterStalled,
    PrinterTimeout,
    PrintOptions,
    RibbonError,
    RibbonInDirectMode,
    RibbonOut,
)
from tavletta.backends.cab import Cab
from tavletta.model import Label, TextElement
from tavletta.render import mm_to_px

REAL_CONNECT = cab._connect  # before the autouse fixture replaces it
REAL_FETCH_SETUP = cab._fetch_setup
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
        self.setup: bytes | None = None  # the web interface's setup tree; None: no answer
        self.web: list[str] = []  # hosts whose web interface was asked

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
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

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
    waits = []
    sleeps = []
    now = [1000.0]

    sockets = []

    def connect(host, port, wait=None):
        calls.append((host, port))
        waits.append(wait)
        sockets.append(FakeSocket(device))
        return sockets[-1]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) > MAX_SLEEPS:
            raise AssertionError("endless polling")

    def fetch_setup(host):
        device.web.append(host)
        return device.setup

    monkeypatch.setattr(cab, "_connect", connect)
    monkeypatch.setattr(cab, "_fetch_setup", fetch_setup)
    monkeypatch.setattr(cab, "_sleep", sleep)
    monkeypatch.setattr(cab, "_clock", lambda: now[0])
    return SimpleNamespace(
        device=device, calls=calls, waits=waits, sleeps=sleeps, now=now, sockets=sockets
    )


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
    assert lines[:2] == ["d PNG;TAVLETTA", "<BASE64>"] and lines[3] == "</BASE64>"
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
        ("F", RibbonOut),
        ("R", RibbonInDirectMode),
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
        "I:Label;0,0,0;TAVLETTA",
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
    assert info.dpi == pytest.approx(203.2)  # the preview and its warnings at it


def test_prepare_across_the_roll(backend):
    label = _label(50, 25, elements=[TextElement(text="A")])
    image = backend.prepare(label, PrintOptions())
    assert image.size == (600, 300)  # 12 dots per mm, x across the print head
    assert {c for _n, c in image.getcolors()} <= {(0, 0, 0), (255, 255, 255)}


def test_prepare_along_the_roll_turns_the_content(backend, png_b64):
    """Content along the roll: the start of the label comes first off the
    roll (top of the printed image), like on tape."""
    from conftest import ink_bbox

    from tavletta.model import ImageElement

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
    assert commands[2:] == ["S l1;0,0,25,27,50", "I:Label;0,0,0;TAVLETTA", "A 3"]
    assert [e.copy for e in events] == [1, 1, 2, 3, 3] and {e.copies for e in events} == {3}
    assert printer.device.closed == 1
    assert all(s == cab.POLL_INTERVAL for s in printer.sleeps)
    # on USB a print waits longer than a status query for the device
    assert printer.waits == [usb.PRINT_WAIT]
    backend.query("printer04.local")
    assert printer.waits[-1] == usb.BUSY_WAIT


@pytest.mark.parametrize(
    "error, cls",
    [
        (PrinterStalled("USB printer stopped taking data"), PrinterStalled),
        (BrokenPipeError(), PrinterError),
    ],
)
def test_print_cancels_a_job_cut_off_while_sending(backend, printer, monkeypatch, error, cls):
    """Part of the image download reached the printer: ESC c, or the rest
    of it would spoil the next job (on USB nothing closes)."""
    real = FakeSocket.sendall

    def sendall(self, data):
        if data.startswith(b"d PNG;"):
            raise error
        real(self, data)

    monkeypatch.setattr(FakeSocket, "sendall", sendall)
    with pytest.raises(cls):
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    assert printer.device.requests[-1] == b"\x1bc" and printer.device.cancelled == 1
    # a printer that stalled takes no ESC c either: a short try, a short close
    assert printer.sockets[-1].timeouts == [cab.CANCEL_TIMEOUT]


def test_print_send_failure_survives_a_cancel_that_fails_too(backend, printer, monkeypatch):
    def sendall(self, data):
        if data.startswith(b"d PNG;") or data == b"\x1bc":
            raise PrinterStalled("USB printer stopped taking data")
        FakeSocket.real(self, data)

    monkeypatch.setattr(FakeSocket, "real", FakeSocket.sendall, raising=False)
    monkeypatch.setattr(FakeSocket, "sendall", sendall)
    with pytest.raises(PrinterStalled):  # the send's error, not the cancel's
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())


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
    def connect(host, port, wait=None):
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


def test_a_job_on_tcp_goes_through_net_send_all(monkeypatch):
    """A job larger than the socket buffer arrives whole; the timeout counts
    from the last progress (net.send_all), not for the whole job."""
    calls = []
    real = net.send_all
    monkeypatch.setattr(
        net,
        "send_all",
        lambda sock, data, timeout, rate: (
            calls.append(len(data)) or real(sock, data, timeout, rate)
        ),
    )
    ours, theirs = socket.socketpair()
    got = bytearray()

    def drain():
        while chunk := theirs.recv(65536):
            got.extend(chunk)

    reader = threading.Thread(target=drain)
    reader.start()
    job = bytes(range(256)) * 4000
    cab._Session(ours).send(job)
    ours.shutdown(socket.SHUT_WR)
    reader.join(5)
    ours.close()
    theirs.close()
    assert calls == [len(job)] and bytes(got) == job


# --- print method ------------------------------------------------------------------


def _setup_tree(transfer="Off", speed="50 mm/s", options=("Off", "On")) -> dict:
    """The parts of /cgi-bin/get?cmd=1 that matter, as an EOS2/300 with
    firmware 5.47.7 sends them (radio buttons and lists: the shown texts,
    the values and the selected text)."""

    def radio(selection):
        return {"type": "radio", "selection": selection, "options": list(options)}

    speeds = ["30 mm/s", "40 mm/s", "50 mm/s", "75 mm/s", "100 mm/s", "125 mm/s", "150 mm/s"]
    return {
        "id": "ID_SETUP",
        "submenu": [
            {
                "id": "ID_PRINTING",
                "submenu": [
                    {"id": "ID_HEAT_LEVEL", "editor": {"type": "slider", "value": 0}},
                    {
                        "id": "ID_PRINT_SPEED",
                        "editor": {
                            "type": "selectbox",
                            "selection": speed,
                            "list": speeds,
                            "values": speeds,
                        },
                    },
                ],
            },
            {
                "id": "ID_RIBBON",
                "submenu": [
                    {
                        "id": "ID_TRANSFER_PRINT",
                        "editor": dict(radio(transfer), values=["false", "true"]),
                    }
                ],
            },
        ],
    }


def _setup(**kwargs) -> bytes:
    return json.dumps(_setup_tree(**kwargs)).encode()


def test_print_method_is_a_printer_setting(backend):
    setting, upside_down = backend.settings(None)
    assert setting.key == "method" and setting.choices == ("printer", "transfer", "direct")
    assert upside_down.key == "upside_down" and upside_down.value(None) is False
    # the default keeps the printer's own setting, as before the setting existed
    assert backend.setting(PrintOptions(), "method") == "printer"
    assert backend.setting(PrintOptions(settings={"method": "ribbon"}), "method") == "printer"
    assert backend.setting(PrintOptions(settings={"method": "direct"}), "method") == "direct"


@pytest.mark.parametrize(
    "method, speed, line",
    [("transfer", 50, "H 50,0,T"), ("direct", 100, "H 100,0,D"), ("transfer", 30, "H 30,0,T")],
)
def test_job_with_a_print_method(method, speed, line):
    """H after J, before S (as the manual's examples); heat 0 adds nothing
    to the heat set on the printer."""
    job = cab.build_job(Image.new("RGB", (600, 300), "white"), 12.0, 3.0, 1, method, speed)
    _png, commands = _job_parts(job)
    assert commands == [
        "m m",
        "J",
        line,
        "S l1;0,0,25,28,50",
        "I:Label;0,0,0;TAVLETTA",
        "A 1",
    ]


def test_job_as_set_on_the_printer_sends_no_h():
    image = Image.new("RGB", (600, 300), "white")
    assert cab.build_job(image, 12.0, 3.0, 1, "printer") == cab.build_job(image, 12.0, 3.0, 1)
    _png, commands = _job_parts(cab.build_job(image, 12.0, 3.0, 1, "junk"))
    assert not any(c.startswith("H") for c in commands)


@pytest.mark.parametrize(
    "data, expected",
    [
        (_setup(), cab.Setup("direct", 50)),
        (_setup(transfer="On", speed="125 mm/s"), cab.Setup("transfer", 125)),
        # a printer set to German: the texts differ, the values do not
        (_setup(transfer="Ein", options=("Aus", "Ein")), cab.Setup("transfer", 50)),
        (_setup(speed="2 in/s"), cab.Setup("direct", None)),  # inches: not used
        (_setup(transfer="Maybe"), cab.Setup("", 50)),  # not one of the options
        (None, cab.Setup()),
        (b"", cab.Setup()),
        (b"{", cab.Setup()),
        (b"<html>login</html>", cab.Setup()),
        (b"[1, 2]", cab.Setup()),
        (b'{"submenu": [{"id": "ID_PRINT_SPEED", "editor": 3}, 5]}', cab.Setup()),
        (b'{"submenu": {"id": "x"}}', cab.Setup()),
    ],
)
def test_parse_setup(data, expected):
    assert cab.parse_setup(data) == expected


def test_parse_setup_ignores_broken_entries():
    tree = _setup_tree()
    speed = tree["submenu"][0]["submenu"][1]["editor"]
    speed["values"] = speed["values"][:2]  # the selected one has no value
    radio = tree["submenu"][1]["submenu"][0]["editor"]
    radio["options"] = "Off"
    assert cab.parse_setup(json.dumps(tree).encode()) == cab.Setup()


def test_parse_setup_of_a_tree_nested_too_deep():
    tree: dict = {"id": "ID_TRANSFER_PRINT", "editor": _setup_tree()["submenu"][1]}
    for _ in range(30):
        tree = {"submenu": [tree]}
    assert cab.parse_setup(json.dumps(tree).encode()) == cab.Setup()


def test_query_reports_the_print_method_set_on_the_printer(backend, printer):
    assert backend.query("printer04.local").print_method == ""  # no web interface
    printer.device.setup = _setup()
    assert backend.query("printer04.local").print_method == "direct"
    printer.device.setup = _setup(transfer="On")
    assert backend.query("printer04.local").print_method == "transfer"
    assert printer.device.web == ["printer04.local"] * 3


@pytest.mark.parametrize(
    "setup, line",
    [
        (None, "H 50,0,T"),  # web interface off: cab's default speed
        (_setup(speed="100 mm/s"), "H 100,0,T"),  # the printer's own speed
        (_setup(speed="2 in/s"), "H 50,0,T"),
    ],
)
def test_print_with_thermal_transfer(backend, printer, setup, line):
    printer.device.setup = setup
    image = backend.prepare(_label(50, 25), PrintOptions())
    backend.print_label(image, "h", PrintOptions(settings={"method": "transfer"}))
    _png, commands = _job_parts(printer.device.jobs[0])
    assert commands[2:4] == [line, "S l1;0,0,25,28,50"]
    assert printer.device.web == ["h"]


def test_print_thermal_direct(backend, printer):
    printer.device.setup = _setup(transfer="On", speed="75 mm/s")
    backend.print_label(
        Image.new("RGB", (600, 300), "white"), "h", PrintOptions(settings={"method": "direct"})
    )
    _png, commands = _job_parts(printer.device.jobs[0])
    assert commands[2] == "H 75,0,D"


def test_print_as_set_on_the_printer_asks_no_web_interface(backend, printer):
    printer.device.setup = _setup(transfer="On")
    backend.print_label(Image.new("RGB", (600, 300), "white"), "h", PrintOptions())
    _png, commands = _job_parts(printer.device.jobs[0])
    assert commands[2] == "S l1;0,0,25,28,50"
    assert printer.device.web == []


@pytest.mark.parametrize(
    "error, cls, code",
    [("F", RibbonOut, "ribbon_out"), ("R", RibbonInDirectMode, "ribbon_direct")],
)
def test_ribbon_errors_while_printing(backend, printer, monkeypatch, error, cls, code):
    """Thermal transfer without a ribbon, or thermal direct with one: the
    printer stops, the job is cancelled, the error names the problem."""
    states = iter([b"Y-000000N", b"Y%s000001N" % error.encode()])
    monkeypatch.setattr(FakeCab, "status", lambda self: next(states))
    options = PrintOptions(settings={"method": "transfer"})
    with pytest.raises(cls) as raised:
        backend.print_label(Image.new("RGB", (600, 300), "white"), "h", options)
    assert isinstance(raised.value, RibbonError) and raised.value.code == code
    assert printer.device.requests[-1] == b"\x1bc"


class _SetupHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b""
    paths: list[str] = []

    def do_GET(self):  # noqa: N802 - the name http.server calls
        type(self).paths.append(self.path)
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def web(monkeypatch):
    """A web interface on 127.0.0.1 that answers with _SetupHandler.body."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SetupHandler)
    monkeypatch.setattr(cab, "WEB_PORT", server.server_address[1])
    monkeypatch.setattr(_SetupHandler, "paths", [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _SetupHandler
    server.shutdown()
    server.server_close()


def test_fetch_setup_reads_the_web_interface(web, monkeypatch):
    monkeypatch.setattr(web, "body", _setup())
    assert REAL_FETCH_SETUP("127.0.0.1") == _setup()
    assert web.paths == ["/cgi-bin/get?cmd=1"]


def test_fetch_setup_behind_a_password(web, monkeypatch):
    monkeypatch.setattr(web, "status", 401)
    monkeypatch.setattr(web, "body", b"login")
    assert REAL_FETCH_SETUP("127.0.0.1") is None


def test_fetch_setup_without_a_web_interface(monkeypatch):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    monkeypatch.setattr(cab, "WEB_PORT", port)  # nothing listens: refused
    assert REAL_FETCH_SETUP("127.0.0.1") is None
    assert REAL_FETCH_SETUP("bad\x00host") is None  # cannot be resolved


def test_fetch_setup_not_on_usb(monkeypatch):
    def resolve(host):
        raise AssertionError("resolved %s" % host)

    monkeypatch.setattr(mdns, "resolve_host", resolve)
    assert REAL_FETCH_SETUP("usb://cab/EOS2%2F300?serial=1") is None


def test_job_upside_down():
    """O R turns the whole label by 180° (as cablabel sends it for an
    EOS2); O directly after S, as the manual asks."""
    image = Image.new("RGB", (480, 223), "white")
    job = cab.build_job(image, 12.0, 4.0, 1, "transfer", 50, upside_down=True)
    _png, commands = _job_parts(job)
    assert commands == [
        "m m",
        "J",
        "H 50,0,T",
        "S l1;0,0,18.58,22.58,40",
        "O R",
        "I:Label;0,0,0;TAVLETTA",
        "A 1",
    ]


@pytest.mark.parametrize("value, sent", [(True, True), (False, False), ("yes", False)])
def test_print_upside_down(backend, printer, value, sent):
    options = PrintOptions(settings={"upside_down": value})
    backend.print_label(Image.new("RGB", (600, 300), "white"), "h", options)
    _png, commands = _job_parts(printer.device.jobs[0])
    assert ("O R" in commands) is sent
    if sent:
        assert commands[commands.index("O R") - 1].startswith("S l1;")
