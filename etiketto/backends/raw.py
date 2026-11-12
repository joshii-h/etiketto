"""What the raw label printer languages share (ZPL, EPL2, TSPL, ESC/POS,
DYMO): the label as 1-bit rows, the connection (TCP port 9100 or USB),
short status questions that may stay unanswered, and finding the printers.

These printers take a job as a stream of commands with the label as a
bitmap; most of them answer few or no questions. A question without an
answer is not an error here: the printer may simply not know it, so the
state is "UNKNOWN" and the job is sent anyway. See docs/zpl.md and the other
language notes; the byte sequences follow LPrint by Michael R Sweet
(Apache-2.0), written anew.
"""

from __future__ import annotations

import socket
import time
from abc import abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

from PIL import Image

from etiketto.backends import mdns, usb
from etiketto.backends.base import (
    Backend,
    FoundPrinter,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    Raster,
    check_length,
    check_width,
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

DEFAULT_PORT = 9100
SOCKET_TIMEOUT = 10.0  # connecting and sending; on USB also for each write
ANSWER_TIMEOUT = 1.0  # a status answer: silence after this means "unknown"
SERVICE = "_pdl-datastream._tcp"  # raw printing on port 9100, announced by mDNS
DPI_INFO = "etiketto_dpi"  # image.info: the resolution prepare() rendered for

# the resolutions of the dpi settings: the print heads have whole dots per
# mm (8, 12, 24), so "203 dpi" is 203.2 and a 100 mm label 800 dots
DPI = {"180": 180.0, "203": 8 * 25.4, "300": 12 * 25.4, "600": 24 * 25.4}

_clock = time.monotonic  # replaced in tests
_browse = mdns.browse_services  # replaced in tests: no search in the real network


# --- the label as 1-bit rows ------------------------------------------------------


@dataclass(frozen=True)
class Bitmap:
    """The label in printer orientation, one bit per dot, rows MSB first
    (the leftmost dot is the highest bit), padded to whole bytes."""

    width: int  # dots, before padding
    rows: tuple[bytes, ...]
    black_is_one: bool

    @property
    def bpr(self) -> int:
        """Bytes per row."""
        return (self.width + 7) // 8

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def blank(self) -> bytes:
        """A row without ink."""
        return (b"\x00" if self.black_is_one else b"\xff") * self.bpr

    def data(self) -> bytes:
        return b"".join(self.rows)


_INVERT = bytes(255 - b for b in range(256))


def bitmap(image: Image.Image, black_is_one: bool, width: int | None = None) -> Bitmap:
    """The image as 1-bit rows: a threshold, no dithering, so barcodes and
    text keep their edges (photos were dithered by prepare() already, the
    preview shows them so). `width` crops or pads (white) to that many dots;
    padding bits are white too. ZPL, ESC/POS and DYMO want 1 = black, EPL2
    and TSPL 1 = white."""
    width = image.width if width is None else width
    bpr = (width + 7) // 8
    page = Image.new("L", (bpr * 8, image.height), 255)
    page.paste(image.convert("L").crop((0, 0, min(width, image.width), image.height)))
    # PIL's mode "1" is 1 = white, rows of whole bytes; the padding and a
    # crop wider than the image are white already
    data = page.convert("1", dither=Image.Dither.NONE).tobytes()
    if black_is_one:
        data = data.translate(_INVERT)
    rows = tuple(data[i : i + bpr] for i in range(0, len(data), bpr))
    return Bitmap(width, rows, black_is_one)


def blank_runs(rows: tuple[bytes, ...], blank: bytes) -> list[tuple[int, int, bool]]:
    """(first row, number of rows, blank?) for each run of blank or inked
    rows: the languages skip blank rows by feeding paper instead."""
    runs: list[tuple[int, int, bool]] = []
    for index, row in enumerate(rows):
        empty = row == blank
        if runs and runs[-1][2] == empty:
            first, count, _empty = runs[-1]
            runs[-1] = (first, count + 1, empty)
        else:
            runs.append((index, 1, empty))
    return runs


def chunks(count: int, limit: int) -> list[int]:
    """count as pieces of at most limit: a feed of 600 dots as 255, 255, 90."""
    return [limit] * (count // limit) + ([count % limit] if count % limit else [])


def percent_to(value: int, top: int) -> int:
    """A percentage as a printer level from 0 to top (rounded down, like LPrint)."""
    return top * max(0, min(100, value)) // 100


def mm_text(value: float) -> str:
    """12.5 -> "12.5", 50.0 -> "50": millimetres in commands."""
    return ("%.2f" % value).rstrip("0").rstrip(".")


# --- the connection -----------------------------------------------------------------


def _connect(host: str, port: int, wait: float = usb.BUSY_WAIT):
    """TCP to the raw port, or the printer's USB device: the same language.
    `wait`: on USB, how long to wait while another session uses it."""
    if usb.is_usb(host):
        return usb.connect(host, SOCKET_TIMEOUT, wait)
    return socket.create_connection((mdns.resolve_host(host), port), SOCKET_TIMEOUT)


def map_error(error: Exception) -> PrinterError:
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    # mdns.resolve_host() fails with "cannot resolve", any other OSError
    # means the network is unreachable
    if isinstance(error, OSError) or "resolve" in text.lower():
        return PrinterNotFound(text)
    return PrinterError(text)


class Session:
    """A connection to the printer: send a job, ask short questions."""

    def __init__(self, connection):
        self.connection = connection

    def send(self, data: bytes) -> None:
        self.connection.sendall(data)

    def ask(
        self,
        request: bytes,
        done: Callable[[bytes], bool],
        timeout: float | None = None,
    ) -> bytes:
        """Send a question, read the answer until done(answer) or the
        timeout (ANSWER_TIMEOUT). What came by then is returned, b"" if
        nothing: many of these printers do not answer every question, which
        is no error. On USB a late answer could come before the next one;
        each session starts by dropping what is left (usb.connect)."""
        self.send(request)
        answer = b""
        deadline = _clock() + (ANSWER_TIMEOUT if timeout is None else timeout)
        try:
            while not done(answer):
                remaining = deadline - _clock()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(4096)
                if not chunk:  # TCP: the printer closed the connection
                    break
                answer += chunk
        except (TimeoutError, PrinterTimeout):
            pass
        finally:
            self.connection.settimeout(SOCKET_TIMEOUT)
        return answer


def session(
    host: str, port: int = DEFAULT_PORT, wait: float = usb.BUSY_WAIT
) -> AbstractContextManager[Session]:
    return open_session(lambda: _connect(host, port, wait), Session, map_error)


def ends_with(terminator: bytes) -> Callable[[bytes], bool]:
    return lambda answer: terminator in answer


def has_bytes(count: int) -> Callable[[bytes], bool]:
    return lambda answer: len(answer) >= count


# --- the backends -------------------------------------------------------------------


def state(answered: bool, error: PrinterError | None, busy: bool = False) -> str:
    """PrinterInfo.state from a status: UNKNOWN without an answer."""
    if not answered:
        return "UNKNOWN"
    if error:
        return "PAUSED" if isinstance(error, PrinterPaused) else "ERROR"
    return "BUSY" if busy else "IDLE"


def usb_model(host: str) -> str:
    """The model in a usb:// address ("LabelWriter 450"); "" for others."""
    if not usb.is_usb(host):
        return ""
    try:
        return unquote(urlsplit(host).path.lstrip("/"))
    except ValueError:
        return ""


class RawBackend(Backend):
    """A printer that takes the label as a bitmap in its own command
    language, on port 9100 or USB. Subclasses build the job and, where the
    language has one, ask for the status."""

    port = DEFAULT_PORT
    # the language's names in the IEEE 1284 command set (CMD:ZPL) and in the
    # usb_CMD of an mDNS announcement
    command_sets: tuple[str, ...] = ()
    # makers whose printers speak the language, for printers that name no
    # command set; empty: only the command set counts
    makers: tuple[str, ...] = ()
    default_dpi = "203"

    def __init__(self, port: int | None = None):
        if port is not None:
            self.port = port
        # the resolution the printer reported last (where it can), for "auto"
        self.reported_dpi: float | None = None

    # --- finding the printer

    def speaks(self, maker: str, model: str, commands: tuple[str, ...]) -> bool:
        """A printer of this language: its command set names it, or it names
        none and comes from one of the makers."""
        if commands:
            return any(c in commands for c in self.command_sets)
        return any(maker.casefold().startswith(m.casefold()) for m in self.makers)

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """Printers of this language on USB, then on the network."""
        plugged = [
            FoundPrinter("%s %s (USB)" % (p.manufacturer, p.model), p.address, str(p.device))
            for p in usb.printers()
            if self.speaks(p.manufacturer, p.model, p.commands)
        ]
        return plugged + [
            FoundPrinter(s.name, s.host, s.address)
            for s in _browse(SERVICE, lambda _name: True, timeout)
            if self._announced(s.name, s.txt)
        ]

    def _announced(self, name: str, txt: dict[str, str]) -> bool:
        commands = tuple(c.strip().upper() for c in txt.get("usb_cmd", "").split(",") if c.strip())
        return self.speaks(txt.get("usb_mfg", "") or name, txt.get("usb_mdl", ""), commands)

    # --- settings

    def choice(self, options: PrintOptions, key: str) -> str:
        return str(self.setting(options, key))

    def level(self, options: PrintOptions, key: str) -> int | None:
        value = self.setting(options, key)
        return value if isinstance(value, int) else None

    # --- rendering

    def dpi(self, options: PrintOptions) -> float:
        """The resolution of a job: the setting, or what the printer
        reported ("auto"), else the default. Without a dpi setting the
        printer has one resolution, capabilities.dpi."""
        if not any(setting.key == "dpi" for setting in self.capabilities.settings):
            return self.capabilities.dpi
        choice = self.setting(options, "dpi")
        if choice == "auto":
            return self.reported_dpi or DPI[self.default_dpi]
        return DPI.get(str(choice), DPI[self.default_dpi])

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        return Raster(self.dpi(options))

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """Die-cut labels in printer orientation: x across the print head,
        y along the feed, the first row leaves the printer first. The
        resolution it was made for travels with the image (info)."""
        dpi = self.dpi(options)
        image = super().prepare(replace(label, auto_length=False), options, safe_mm)
        if label.orientation == "along":
            image = image.rotate(-90, expand=True)
        image.info[DPI_INFO] = dpi
        return image

    # --- status and printing

    def query(self, host: str) -> PrinterInfo:
        """The printer's state as far as it tells; reachable is the least."""
        with session(host, self.port) as s:
            info = self.ask_status(s)
        if not info.model:
            info.model = usb_model(host)
        return info

    def ask_status(self, s: Session) -> PrinterInfo:
        """What the printer says about itself; languages without status
        questions only know that it is reachable."""
        return PrinterInfo("", None, None, None, "UNKNOWN")

    def problem(self, s: Session) -> PrinterError | None:
        """A problem the printer reports before a job, None if none or unknown."""
        return None

    def check(self, image: Image.Image, dpi: float, host: str) -> None:
        """Refuse a label the printer cannot take, before connecting."""
        check_width(image.width / dpi * 25.4, self.capabilities)
        check_length(image.height / dpi * 25.4, self.capabilities)

    @abstractmethod
    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        """The whole job, all copies, in the printer's language."""

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        """Send the job once the printer reports no problem. These printers
        do not report when a job is done: progress ends when it is sent."""
        dpi = float(image.info.get(DPI_INFO) or self.dpi(options))
        self.check(image, dpi, host)
        copies = max(1, options.copies)
        job = self.build_job(image, dpi, replace(options, copies=copies), host)
        with session(host, self.port, usb.PRINT_WAIT) as s:
            report_progress(progress, 1, copies)
            problem = self.problem(s)
            if problem:
                raise problem
            s.send(job)
            report_progress(progress, copies, copies)
        return None
