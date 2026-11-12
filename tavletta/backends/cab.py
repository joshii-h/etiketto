"""cab label printers with a 4 inch print head (EOS2, EOS5, SQUIX 4, MACH 4S):
JScript on TCP port 9100 or USB (see docs/cab.md).

JScript is cab's own, publicly documented printer language ("JScript
Programming Manual"). A job downloads the rendered label as a PNG in base 64
and places it on a die-cut label of the given size. The print method
(thermal transfer with a ribbon, or thermal direct) is the printer's own
setting unless the "method" setting chooses one for each job;
"upside_down" turns the label by 180° (O R); heat, backfeed and tear-off
position stay as set on the printer. Status comes from the ESC
commands, which the printer answers on the same connection; the setup of
the web interface, if it answers, tells the print method and speed set on
the printer.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import math
import re
import socket
import threading
import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, replace

from PIL import Image

from tavletta.backends import mdns, net, usb
from tavletta.backends.base import (
    Backend,
    Capabilities,
    Choice,
    FoundPrinter,
    HeadOpen,
    LabelNotFound,
    LabelSize,
    OutOfLabels,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterPaused,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    Raster,
    RibbonInDirectMode,
    RibbonOut,
    Toggle,
    check_width,
    map_error,
    open_session,
    report_progress,
)
from tavletta.model import Label
from tavletta.render import DEFAULT_SAFE_MM

DEFAULT_PORT = 9100
# seconds for connecting and for each answer; also without progress while
# sending a job (net.send_all, usb.Connection.sendall)
SOCKET_TIMEOUT = 10.0
CANCEL_TIMEOUT = 1.0  # seconds for ESC c after a problem, and for closing then
MIN_RATE = 8_000  # bytes a second: a job that trickles in slower counts as stalled
PRINT_TIMEOUT = 180.0  # seconds without progress while waiting or printing
POLL_INTERVAL = 0.5  # the manual: status requests not more often than every 0.5 s
START_GRACE = 3.0  # a job that never shows up as running counts as done after this
IMAGE_NAME = "TAVLETTA"  # at most 8 characters; each job replaces the previous image
CR = b"\r\n"
ESC = b"\x1b"

# dots per mm by the resolution in the model name ("EOS2/300"); the print head
# of an EOS2/300 reports itself as "106 mm 12 dots/mm"
DOTS_PER_MM = {200: 8.0, 300: 12.0, 600: 23.6}
DEFAULT_DOTS_PER_MM = DOTS_PER_MM[300]
# widest label: the print width of a 4 inch head (manual); the EOS2/300's head
# reports "106 mm", but the printer refuses S ...,106 as a syntax error
MAX_WIDTH_MM = 105.6

# the "method" setting: as set on the printer (no H command), thermal
# transfer with a ribbon (H ...,T) or thermal direct (H ...,D); the first is
# the default
METHODS = ("printer", "transfer", "direct")
METHOD_CODES = {"transfer": "T", "direct": "D"}
# H needs a speed. Sent as set on the printer if its web interface tells it,
# else this one: the default of cab's setup, and the slower the cleaner the
# print (docs/cab.md)
DEFAULT_SPEED = 50  # mm/s
WEB_PORT = 80
WEB_TIMEOUT = 2.0  # seconds for the web interface's setup, an optional hint
SETUP_PATH = "/cgi-bin/get?cmd=1"  # the setup as JSON, read without a password
MAX_SETUP_BYTES = 1 << 20
# the setup read is kept this long per printer: a job of many labels, each
# sent on its own (a counter), asks once, not once per label (up to
# WEB_TIMEOUT each where port 80 is filtered)
SETUP_SECONDS = 30.0

# common die-cut sizes (width × height); the first one is the default
LABEL_SIZES = (
    LabelSize(50, 25),
    LabelSize(38, 25),
    LabelSize(57, 32),
    LabelSize(76, 51),
    LabelSize(100, 50),
    LabelSize(100, 100),
    LabelSize(100, 150),
)

# error character of ESC s (position 2) -> error class; text for bug reports
STATUS_ERRORS: dict[str, tuple[type[PrinterError], str]] = {
    "P": (OutOfLabels, "out of paper"),
    "D": (HeadOpen, "printhead or pinch roller open"),
    "E": (LabelNotFound, "synchronization error (no label found)"),
    "F": (RibbonOut, "out of ribbon"),
    "R": (RibbonInDirectMode, "ribbon detected in thermal direct mode"),
    "B": (PrinterError, "protocol error / invalid barcode data"),
    "H": (PrinterError, "heating voltage problem"),
    "O": (PrinterError, "out of memory"),
    "V": (PrinterError, "input buffer overflow"),
    "W": (PrinterError, "print head overheated"),
    "Y": (PrinterError, "printhead error"),
    "Z": (PrinterError, "printhead damaged"),
}

_sleep = time.sleep  # replaced in tests
_clock = time.monotonic


def dpi_for(dots_per_mm: float) -> float:
    """Exact, not rounded: 12 dots/mm is 304.8 dpi, and a 100 mm label must
    come out as 1200 dots, not 1201."""
    return dots_per_mm * 25.4


# --- answers ----------------------------------------------------------------------


@dataclass
class Status:
    """ESC s: "XYNNNNNNZ" - online, error, labels still to print, job running."""

    online: bool = True
    error: str = "-"  # "-" = no error
    pending: int = 0
    running: bool = False
    raw: str = ""

    @property
    def state(self) -> str:
        """BUSY only with labels to print: after a text command such as qv the
        interpreter stays active for about a second without a job."""
        if self.error != "-":
            return "ERROR"
        if not self.online:
            return "PAUSED"
        return "BUSY" if self.pending else "IDLE"

    @property
    def idle(self) -> bool:
        """Ready for a new job: nothing to print, interpreter done."""
        return not self.pending and not self.running


def parse_status(raw: str) -> Status:
    """PrinterError for an answer that is not a status at all."""
    match = re.fullmatch(r"([YN])(.)(\d{6})([YN])", raw.strip())
    if not match:
        raise PrinterError("unexpected status answer %r" % raw)
    online, error, pending, running = match.groups()
    return Status(online == "Y", error, int(pending), running == "Y", raw.strip())


def status_error(status: Status) -> PrinterError | None:
    """What keeps the printer from printing, or None."""
    if status.error != "-":
        cls, text = STATUS_ERRORS.get(status.error, (PrinterError, "error"))
        return cls("ESC s %s: %s" % (status.raw, text))
    if not status.online:
        return PrinterPaused("ESC s %s: printer is offline (paused)" % status.raw)
    return None


def parse_pitch(raw: str) -> float | None:
    """ESC l: "YNNNN" - synchronised, label distance in mm (0000 = unknown)."""
    match = re.fullmatch(r"([YN])(\d{4})", raw.strip())
    if not match or match.group(1) != "Y" or not int(match.group(2)):
        return None
    return float(int(match.group(2)))


@dataclass
class Version:
    firmware: str = ""
    model: str = ""

    @property
    def dots_per_mm(self) -> float:
        match = re.search(r"/(\d+)", self.model)
        return DOTS_PER_MM.get(int(match.group(1)) if match else 0, DEFAULT_DOTS_PER_MM)


def parse_version(raw: str) -> Version:
    """qv: "5.47.7 Jan 22, 2026 (EOS2/300)"."""
    text = raw.strip()
    match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)", text)
    if not match:
        return Version(firmware=text)
    return Version(firmware=match.group(1), model=match.group(2))


@dataclass(frozen=True)
class Setup:
    """What the web interface's setup tells: the print method set on the
    printer ("transfer", "direct", "" = unknown) and its print speed."""

    method: str = ""
    speed: int | None = None  # mm/s


def parse_setup(data: bytes | None) -> Setup:
    """The setup tree of /cgi-bin/get?cmd=1 (firmware 5.47); anything else
    tells nothing. The shown texts depend on the printer's language: the
    choice is read by its position in the list of values."""
    try:
        tree = json.loads(data or b"")
    except ValueError:
        return Setup()
    editors = dict(_editors(tree))
    method = {"true": "transfer", "false": "direct"}.get(
        _selected(editors.get("ID_TRANSFER_PRINT"), "options") or "", ""
    )
    speed = None
    match = re.fullmatch(r"(\d+) ?mm/s", str(_selected(editors.get("ID_PRINT_SPEED"), "list")))
    if match:
        speed = int(match.group(1))
    return Setup(method, speed)


def _editors(node, depth: int = 0):
    """(id, editor) of every entry of the setup tree."""
    if not isinstance(node, dict) or depth > 20:
        return
    if isinstance(node.get("editor"), dict):
        yield node.get("id"), node["editor"]
    for child in node.get("submenu") or ():
        yield from _editors(child, depth + 1)


def _selected(editor, shown: str) -> str | None:
    """The value of a radio button or list: the one at the position of the
    selected text."""
    if not isinstance(editor, dict):
        return None
    texts, values = editor.get(shown), editor.get("values")
    selection = editor.get("selection")
    if not isinstance(texts, list) or not isinstance(values, list) or selection not in texts:
        return None
    index = texts.index(selection)
    return str(values[index]) if index < len(values) else None


def _fetch_setup(host: str) -> bytes | None:
    """The setup tree from the printer's web interface; None on USB or if
    it does not answer (switched off, behind a password). Replaced in tests."""
    if usb.is_usb(host):
        return None
    try:
        connection = http.client.HTTPConnection(
            mdns.resolve_host(host), WEB_PORT, timeout=WEB_TIMEOUT
        )
        try:
            connection.request("GET", SETUP_PATH)
            response = connection.getresponse()
            return response.read(MAX_SETUP_BYTES) if response.status == 200 else None
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - only a hint: any failure tells nothing
        return None


_setups: dict[str, tuple[float, Setup]] = {}  # host -> (when read, setup)
_setups_lock = threading.Lock()


def web_setup(host: str, fresh: bool = False) -> Setup:
    """The setup of the printer's web interface, read again after
    SETUP_SECONDS (fresh: now); what told nothing (no answer) is kept as
    long."""
    now = _clock()
    with _setups_lock:
        cached = _setups.get(host)
    if not fresh and cached and 0 <= now - cached[0] < SETUP_SECONDS:
        return cached[1]
    setup = parse_setup(_fetch_setup(host))
    with _setups_lock:
        _setups[host] = (now, setup)
    return setup


# --- the job --------------------------------------------------------------------


def _mm(value: float) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".")


def encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("1", dither=Image.Dither.NONE).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_job(
    image: Image.Image,
    dots_per_mm: float,
    gap_mm: float,
    copies: int,
    method: str = "printer",
    speed: int = DEFAULT_SPEED,
    upside_down: bool = False,
    size_mm: tuple[float, float] | None = None,
) -> bytes:
    """Download the image and print it on die-cut labels of its size.

    The image is in printer orientation: x across the print head, y along the
    feed, one pixel per dot. size_mm: the label's size (width across, height
    along), sent as it is where the image agrees with it within a dot (an
    18.6 mm label is 18.6, not the 18.58 of its 223 dots), else the image's.
    A print method other than "printer" is sent with the speed (H needs
    one) and heat 0, which adds nothing to the heat set on the printer.
    upside_down: O R, the whole label turned by 180° (O must directly
    follow S)."""
    width = _size(image.width, dots_per_mm, size_mm[0] if size_mm else None)
    height = _size(image.height, dots_per_mm, size_mm[1] if size_mm else None)
    data = base64.b64encode(encode_png(image))
    lines = [
        b"d PNG;" + IMAGE_NAME.encode(),
        b"<BASE64>",
        data,
        b"</BASE64>",
        b"m m",
        b"J",
    ]
    if method in METHOD_CODES:
        lines.append(("H %d,0,%s" % (speed, METHOD_CODES[method])).encode())
    lines += [
        # l1: die-cut labels, the gap sensor finds each one
        ("S l1;0,0,%s,%s,%s" % (_mm(height), _mm(height + gap_mm), _mm(width))).encode(),
    ]
    if upside_down:
        lines.append(b"O R")
    lines += [
        ("I:Label;0,0,0;%s" % IMAGE_NAME).encode(),
        b"A %d" % max(1, copies),
    ]
    return CR.join(lines) + CR


def _size(pixels: int, dots_per_mm: float, mm: float | None) -> float:
    """The label's size in mm if the pixels make it within a dot, else
    the pixels' size."""
    if mm is not None and abs(mm * dots_per_mm - pixels) <= 1:
        return mm
    return pixels / dots_per_mm


# --- connection -----------------------------------------------------------------


def _connect(host: str, port: int, wait: float = usb.BUSY_WAIT):
    """TCP to the printer's port, or its USB device: the same language
    (net.connect). Replaced in tests."""
    return net.connect(host, port, SOCKET_TIMEOUT, wait, MIN_RATE)


class _Session:
    def __init__(self, sock):
        self.sock = sock

    def _read(self, length: int | None = None) -> str:
        """An answer: `length` bytes, or up to a carriage return."""
        data = b""
        while True:
            chunk = self.sock.recv(1 if length is None else length - len(data))
            if not chunk:
                raise PrinterTimeout("connection closed after %r" % data)
            data += chunk
            if length is not None and len(data) >= length:
                return data.decode("ascii", "replace")
            if length is None and data.endswith(b"\r"):
                return data.decode("ascii", "replace")

    def status(self) -> Status:
        self.sock.sendall(ESC + b"s")
        return parse_status(self._read(9))

    def pitch(self) -> float | None:
        self.sock.sendall(ESC + b"l")
        return parse_pitch(self._read(5))

    def version(self) -> Version:
        self.sock.sendall(b"qv" + CR)
        return parse_version(self._read())

    def send(self, job: bytes) -> None:
        """The job; on TCP the timeout restarts with each bit of progress
        (net.send_all), usb.Connection.sendall does the same on USB."""
        if isinstance(self.sock, socket.socket):
            net.send_all(self.sock, job, SOCKET_TIMEOUT, MIN_RATE)
        else:
            self.sock.sendall(job)

    def cancel(self) -> None:
        """ESC c: drop the running job, so that "try again" does not print
        its remaining labels a second time once the problem is solved."""
        # the connection is gone or the printer takes nothing: nothing more to
        # do. A printer that stalled will not take it either: a short try,
        # and closing (which waits for the last write on USB) is short too
        with suppress(OSError, PrinterError):
            self.sock.settimeout(CANCEL_TIMEOUT)
            self.sock.sendall(ESC + b"c")


def session(
    host: str, port: int = DEFAULT_PORT, wait: float = usb.BUSY_WAIT
) -> AbstractContextManager[_Session]:
    return open_session(lambda: _connect(host, port, wait), _Session, map_error)


# --- the backend ------------------------------------------------------------------


class Cab(Backend):
    query_before_render = True  # the resolution (dots per mm) the printer reports
    id = "cab"
    name = "cab EOS2, EOS5, SQUIX 4, MACH 4S"
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=dpi_for(DEFAULT_DOTS_PER_MM),
        modes=(PrintMode("standard", round(dpi_for(DEFAULT_DOTS_PER_MM))),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=MAX_WIDTH_MM,
        data_formats=("png",),
        settings=(Choice("method", METHODS), Toggle("upside_down")),
    )

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        # the resolutions the printers reported, by address: prepare()
        # renders for the job's printer (PrintOptions.host); one backend
        # serves every cab printer of a print server
        self.reported: dict[str, float] = {}
        # the printer last asked: for a job whose printer is not known
        self.dots_per_mm = DEFAULT_DOTS_PER_MM

    def _learn(self, host: str, dots_per_mm: float) -> None:
        self.reported[host] = self.dots_per_mm = dots_per_mm

    def _dots(self, host: str) -> float:
        """The resolution of this printer as reported, else the default;
        without an address the one of the printer last asked."""
        if not host:
            return self.dots_per_mm
        return self.reported.get(host, DEFAULT_DOTS_PER_MM)

    def display_name(self, info: PrinterInfo | None) -> str:
        """ "cab EOS2/300": the printer names its model and resolution."""
        if info and info.model and info.model.casefold() != "cab":
            return _cab_name(info.model)
        return self.name

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """cab printers on USB, then on the network."""
        plugged = [
            FoundPrinter("%s (USB)" % _cab_name(p.model), p.address, str(p.device))
            for p in usb.printers()
            if p.manufacturer.casefold().startswith("cab")
        ]
        return plugged + mdns.browse("_pdl-datastream._tcp", _is_cab, timeout)

    def query(self, host: str) -> PrinterInfo:
        """The status, model and label distance; the print method set on the
        printer if its web interface tells it."""
        with session(host, self.port) as s:
            status = s.status()
            try:
                version = s.version()
                pitch = s.pitch()
            except PrinterTimeout:
                # a printer with a problem may not answer text commands:
                # the status is what matters
                version, pitch = Version(), None
        dots = self._dots(host)
        if version.model:
            dots = version.dots_per_mm
            self._learn(host, dots)
        error = status_error(status)
        return PrinterInfo(
            model=version.model or "cab",
            tape_mm=None,
            remaining_mm=None,
            total_mm=None,
            state=status.state,
            error=error.code if error else "",
            firmware=version.firmware,
            pitch_mm=pitch,
            dpi=dpi_for(dots),
            # asked for the printer's state: read now, kept for the print
            print_method=web_setup(host, fresh=True).method,
        )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        info = options.info  # this job's answer, else the printer as last asked
        return Raster(info.dpi if info and info.dpi else dpi_for(self._dots(options.host)))

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """In printer orientation: x across the print head, y along the feed.
        The resolution it was made for travels with the image (info)."""
        # what it is rendered at (12 dots/mm, not 11.999… from 304.8 dpi)
        dots = round(self.raster(label.tape_mm, options).dpi / 25.4, 6)
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info["dots_per_mm"] = dots
        # in printer orientation too: x across the print head (build_job)
        image.info["size_mm"] = label.media_size()
        return image

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        rendered = float(image.info.get("dots_per_mm", self._dots(host)))
        check_width(image.width / rendered, self.capabilities)
        copies = max(1, options.copies)
        method = str(self.setting(options, "method"))
        # H needs a speed: the printer's own, if its web interface tells it
        speed = (web_setup(host).speed if method in METHOD_CODES else None) or DEFAULT_SPEED
        with session(host, self.port, usb.PRINT_WAIT) as s:
            report_progress(progress, 1, copies)
            # the printer may not have been asked yet (switched on later)
            version = s.version()
            dots = version.dots_per_mm if version.model else rendered
            self._learn(host, dots)
            size_mm = image.info.get("size_mm")
            if not math.isclose(dots, rendered):
                image = _rescale(image, dots / rendered)
            upside_down = self.setting(options, "upside_down") is True
            job = build_job(
                image, dots, options.gap_mm, copies, method, speed, upside_down, size_mm
            )
            _wait_idle(s)
            try:
                # a job cut off while sending would leave a partial image
                # download in the printer, which spoils the next job
                s.send(job)
                _wait_done(s, progress, copies)
            except (PrinterError, OSError):
                s.cancel()
                raise
        return None


def _rescale(image: Image.Image, factor: float) -> Image.Image:
    """Made for another resolution: scale the (already black and white)
    image instead of failing; prepare() renders right the next time."""
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, Image.Resampling.NEAREST)


def _cab_name(model: str) -> str:
    """ "cab EOS2/300", also for a model that already names the maker."""
    return model if model.casefold().startswith("cab ") else "cab " + model


def _is_cab(name: str) -> bool:
    return name.lower().startswith("cab ")


def _checked(s: _Session) -> Status:
    status = s.status()
    error = status_error(status)
    if error:
        raise error
    return status


def _wait_idle(s: _Session) -> None:
    """Another job may still be printing: wait for it."""
    deadline = _clock() + PRINT_TIMEOUT
    while not _checked(s).idle:
        if _clock() > deadline:
            raise PrinterBusy("printer still busy after %.0f s" % PRINT_TIMEOUT)
        _sleep(POLL_INTERVAL)


def _wait_done(s: _Session, progress: ProgressCallback | None, copies: int) -> None:
    """Until the job has run: the printer counted its labels down to 0, or
    stayed idle for START_GRACE (a job printed between two polls). While the
    image is read the interpreter is active without labels pending, which is
    not yet the job. The timeout restarts with every printed label."""
    started = False
    last = None
    idle_since = None
    deadline = _clock() + PRINT_TIMEOUT
    while True:
        status = _checked(s)
        if status.pending:
            started, idle_since = True, None
            done = max(0, copies - status.pending)
            if done != last:
                last = done
                deadline = _clock() + PRINT_TIMEOUT
                report_progress(progress, min(copies, done + 1), copies)
        elif status.running:
            idle_since = None
        else:
            idle_since = _clock() if idle_since is None else idle_since
            if started or _clock() - idle_since >= START_GRACE:
                report_progress(progress, copies, copies)
                return
        if _clock() > deadline:
            raise PrinterTimeout("printer stayed busy: %s" % status.raw)
        _sleep(POLL_INTERVAL)
