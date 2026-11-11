"""cab label printers with a 4 inch print head (EOS2, EOS5, SQUIX 4, MACH 4S):
JScript on TCP port 9100 (see docs/cab.md).

JScript is cab's own, publicly documented printer language ("JScript
Programming Manual"). A job downloads the rendered label as a PNG in base 64
and places it on a die-cut label of the given size; the printer's own setup
(print method, heat, speed, tear-off position) stays in charge. Status comes
from the ESC commands, which the printer answers on the same connection.
"""

from __future__ import annotations

import base64
import io
import re
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from PIL import Image

from etiketto.backends import mdns
from etiketto.backends.base import (
    Backend,
    Capabilities,
    FoundPrinter,
    HeadOpen,
    LabelNotFound,
    LabelSize,
    OutOfLabels,
    PortClosed,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    Progress,
    ProgressCallback,
    Raster,
    RibbonError,
    check_width,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

DEFAULT_PORT = 9100
SOCKET_TIMEOUT = 10.0  # seconds for connecting and for each answer
PRINT_TIMEOUT = 180.0  # seconds without progress while waiting or printing
POLL_INTERVAL = 0.5  # the manual: status requests not more often than every 0.5 s
START_GRACE = 3.0  # a job that never shows up as running counts as done after this
IMAGE_NAME = "ETIKETTO"  # at most 8 characters; each job replaces the previous image
CR = b"\r\n"
ESC = b"\x1b"

# dots per mm by the resolution in the model name ("EOS2/300"); the print head
# of an EOS2/300 reports itself as "106 mm 12 dots/mm"
DOTS_PER_MM = {200: 8.0, 300: 12.0, 600: 23.6}
DEFAULT_DOTS_PER_MM = DOTS_PER_MM[300]
# widest label: the print width of a 4 inch head (manual); the EOS2/300's head
# reports "106 mm", but the printer refuses S ...,106 as a syntax error
MAX_WIDTH_MM = 105.6

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
    "F": (RibbonError, "out of ribbon"),
    "R": (RibbonError, "ribbon detected in thermal direct mode"),
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
        return (
            DOTS_PER_MM.get(int(match.group(1)), DEFAULT_DOTS_PER_MM)
            if match
            else (DEFAULT_DOTS_PER_MM)
        )


def parse_version(raw: str) -> Version:
    """qv: "5.47.7 Jan 22, 2026 (EOS2/300)"."""
    text = raw.strip()
    match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)", text)
    if not match:
        return Version(firmware=text)
    return Version(firmware=match.group(1), model=match.group(2))


# --- the job --------------------------------------------------------------------


def _mm(value: float) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".")


def encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("1", dither=Image.Dither.NONE).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_job(image: Image.Image, dots_per_mm: float, gap_mm: float, copies: int) -> bytes:
    """Download the image and print it on die-cut labels of its size.

    The image is in printer orientation: x across the print head, y along the
    feed, one pixel per dot."""
    width = image.width / dots_per_mm
    height = image.height / dots_per_mm
    data = base64.b64encode(encode_png(image))
    lines = [
        b"d PNG;" + IMAGE_NAME.encode(),
        b"<BASE64>",
        data,
        b"</BASE64>",
        b"m m",
        b"J",
        # l1: die-cut labels, the gap sensor finds each one
        ("S l1;0,0,%s,%s,%s" % (_mm(height), _mm(height + gap_mm), _mm(width))).encode(),
        ("I:Label;0,0,0;%s" % IMAGE_NAME).encode(),
        b"A %d" % max(1, copies),
    ]
    return CR.join(lines) + CR


# --- connection -----------------------------------------------------------------


def _connect(host: str, port: int):
    return socket.create_connection((mdns.resolve_host(host), port), SOCKET_TIMEOUT)


def map_error(error: Exception) -> PrinterError:
    if isinstance(error, PrinterError):
        return error
    text = str(error) or type(error).__name__
    if isinstance(error, socket.gaierror) or "resolve" in text.lower():
        return PrinterNotFound(text)
    if isinstance(error, ConnectionRefusedError):
        return PortClosed(text)
    if isinstance(error, (TimeoutError, socket.timeout)):
        return PrinterTimeout(text)
    if isinstance(error, OSError):
        return PrinterNotFound(text)
    return PrinterError(text)


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
        self.sock.sendall(job)

    def cancel(self) -> None:
        """ESC c: drop the running job, so that "try again" does not print
        its remaining labels a second time once the problem is solved."""
        try:
            self.sock.sendall(ESC + b"c")
        except OSError:
            pass  # the connection is gone; nothing more to do


@contextmanager
def session(host: str, port: int = DEFAULT_PORT) -> Iterator[_Session]:
    try:
        sock = _connect(host, port)
    except Exception as e:  # noqa: BLE001
        raise map_error(e) from e
    try:
        yield _Session(sock)
    except PrinterError:
        raise
    except Exception as e:  # noqa: BLE001
        raise map_error(e) from e
    finally:
        sock.close()


# --- the backend ------------------------------------------------------------------


class Cab(Backend):
    id = "cab"
    name = "cab EOS2, EOS5, SQUIX 4, MACH 4S"
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=dpi_for(DEFAULT_DOTS_PER_MM),
        modes=(PrintMode("standard", round(dpi_for(DEFAULT_DOTS_PER_MM))),),
        color_model="mono",
        cut_modes=(),
        reports_remaining=False,
        supports_color_adjust=False,
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=MAX_WIDTH_MM,
        data_formats=("png",),
    )

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        # the resolution of the printer last asked; prepare() renders for it
        self.dots_per_mm = DEFAULT_DOTS_PER_MM

    def display_name(self, info: PrinterInfo | None) -> str:
        """ "cab EOS2/300": the printer names its model and resolution."""
        if info and info.model and info.model != "cab":
            return "cab " + info.model
        return self.name

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        return mdns.browse("_pdl-datastream._tcp", _is_cab, timeout)

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            status = s.status()
            try:
                version = s.version()
                pitch = s.pitch()
            except PrinterTimeout:
                # a printer with a problem may not answer text commands:
                # the status is what matters
                version, pitch = Version(), None
        if version.model:
            self.dots_per_mm = version.dots_per_mm
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
        )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(dpi_for(self.dots_per_mm))

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """In printer orientation: x across the print head, y along the feed.
        The resolution it was made for travels with the image (info)."""
        dots = self.dots_per_mm
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info["dots_per_mm"] = dots
        return image

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        rendered = float(image.info.get("dots_per_mm", self.dots_per_mm))
        check_width(image.width / rendered, self.capabilities)
        copies = max(1, options.copies)
        with session(host, self.port) as s:
            _report(progress, 1, copies)
            # the printer may not have been asked yet (switched on later)
            version = s.version()
            dots = version.dots_per_mm if version.model else rendered
            self.dots_per_mm = dots
            if abs(dots - rendered) > 0.01:
                image = _rescale(image, dots / rendered)
            job = build_job(image, dots, options.gap_mm, copies)
            _wait_idle(s)
            s.send(job)
            try:
                _wait_done(s, progress, copies)
            except PrinterError:
                s.cancel()
                raise
        return None


def _rescale(image: Image.Image, factor: float) -> Image.Image:
    """Made for another resolution: scale the (already black and white)
    image instead of failing; prepare() renders right the next time."""
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, Image.Resampling.NEAREST)


def _is_cab(name: str) -> bool:
    return name.lower().startswith("cab ")


def _report(progress: ProgressCallback | None, n: int, copies: int) -> None:
    if progress:
        progress(Progress(n, copies))


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
                _report(progress, min(copies, done + 1), copies)
        elif status.running:
            idle_since = None
        else:
            idle_since = _clock() if idle_since is None else idle_since
            if started or _clock() - idle_since >= START_GRACE:
                _report(progress, copies, copies)
                return
        if _clock() > deadline:
            raise PrinterTimeout("printer stayed busy: %s" % status.raw)
        _sleep(POLL_INTERVAL)
