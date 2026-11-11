"""Brother VC-500W: native protocol on TCP port 9100 (see docs/vc500w.md).

The connection and the answer parsing come from labelprinter; the print
request is built here, so colour correction, the "bw" mode and the exact
rawrgb geometry work without changing labelprinter.
"""

from __future__ import annotations

import io
import json
import math
import re
import socket
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image

from etiketto.backends import mdns, usb
from etiketto.backends.base import (
    Backend,
    BadCassette,
    Capabilities,
    CassetteInfo,
    ColorAdjust,
    CoverOpen,
    FoundPrinter,
    NoCassette,
    PaperJam,
    PortClosed,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    ProgressCallback,
    Raster,
    check_length,
    error_text,
    network_error,
    open_session,
    report_progress,
)
from etiketto.render import PRINT_DPI

DEFAULT_PORT = 9100
PRINT_TIMEOUT = 180  # seconds without any change of the printer's stage
POLL_INTERVAL = 0.5  # like Brother's app
SEND_RETRIES = 10  # answer code 1 (busy): try again, like Brother's app
START_GRACE = 3.0  # seconds: a job that never shows BUSY counts as done after this
MIN_LENGTH_MM = 15.0  # the cutter needs at least this much
# stages in which a new job may be sent (print_state IDLE); "" = not reported
READY_STAGES = frozenset({"SUCCESS", "READY FOR PRINT", "IDLE", ""})
# half cut / no cut: the label is printed and the printer waits for the panel swipe
CUT_STAGE = "READY_FOR_CUT"
CROSSWEB_DPI = 320  # resolution across the tape (config.xml crossweb_dpi)
BLEED_IN = 0.02  # extra width on both tape edges for rawrgb (9-25 mm)
MAX_LENGTH_IN = 17.0
TAPE_INCHES = {9: 0.375, 12: 0.5, 19: 0.75, 25: 1.0, 50: 1.968504}
CASSETTE_TYPES = {"3/8": 9, "1/2": 12, "3/4": 19, "1": 25, "2": 50}

# mode/speed/lpi triples as config.xml reports them
VIVID = PrintMode("vivid", 317, 0)
COLOR = PrintMode("color", 264, 1)  # Brother calls it "Standard"
BW = PrintMode("bw", 400, 2)
KNOWN_MODES = {mode.id: mode for mode in (VIVID, COLOR, BW)}

JOB_ERRORS: dict[str, type[PrinterError]] = {
    "NO MEDIA": NoCassette,
    "JAM": PaperJam,
    "EJECT JAM": PaperJam,
    "DOOR OPEN": CoverOpen,
    "BAD CASSETTE": BadCassette,
}

# cassette widths of the VC-500W (CZ-1001 ... CZ-1005)
TAPE_WIDTHS_MM = (9, 12, 19, 25, 50)
_sleep = time.sleep  # replaced in tests
_clock = time.monotonic


def nominal_width(width_mm: float) -> int:
    return VC500W.capabilities.nearest_tape(width_mm)


def configured_host() -> str | None:
    """Host from labelprinter's config (~/.config/labelprinter/config.json).

    Not visible inside the Flatpak sandbox; there the host is entered in the app.
    """
    config = Path.home() / ".config" / "labelprinter" / "config.json"
    try:
        return json.loads(config.read_text(encoding="utf-8")).get("host") or None
    except (OSError, ValueError, AttributeError):
        return None


# --- config.xml and status.xml ----------------------------------------------


def _value(xml: str, name: str) -> str:
    match = re.search(r"<%s>\s*([^<]*?)\s*</%s>" % (name, name), xml)
    return match.group(1) if match else ""


def _number(xml: str, name: str) -> float | None:
    try:
        return float(_value(xml, name))
    except ValueError:
        return None


@dataclass
class DeviceConfig:
    model: str = ""
    firmware: str = ""
    crossweb_dpi: int = CROSSWEB_DPI
    tape_mm: int | None = None
    length_in: float | None = None
    printable_in: float | None = None
    media_id: str = ""
    cassette_sn: str = ""
    modes: tuple[PrintMode, ...] = ()
    no_cassette: bool = False  # cassette_type present but empty


@dataclass
class DeviceStatus:
    state: str = ""
    stage: str = ""
    error: str = ""
    remain_in: float | None = None


def parse_modes(xml: str) -> tuple[PrintMode, ...]:
    """Print modes from config.xml: <mode>, <speed> and <lpi> elements in
    document order (each <mode> starts a new entry), or "name speed lpi"
    triples in <media_mode>. Unknown mode names are skipped."""
    entries: list[dict] = []
    for tag, value in re.findall(r"<(mode|speed|lpi)>\s*([^<]*?)\s*</\1>", xml):
        if tag == "mode":
            entries.append({"mode": value.lower()})
        elif entries:
            entries[-1][tag] = value
    for triple in re.findall(r"<media_mode>\s*([^<]*?)\s*</media_mode>", xml):
        parts = triple.split()
        if len(parts) == 3:
            entries.append({"mode": parts[0].lower(), "speed": parts[1], "lpi": parts[2]})
    modes: dict[str, PrintMode] = {}
    for entry in entries:
        mode = _mode_from_entry(entry)
        if mode and mode.id not in modes:
            modes[mode.id] = mode
    return tuple(modes.values())


def _mode_from_entry(entry: dict) -> PrintMode | None:
    known = KNOWN_MODES.get(entry["mode"])
    if not known:
        return None
    try:
        lpi = int(entry.get("lpi", known.lpi))
        speed = int(entry.get("speed", known.speed))
    except ValueError:
        return None
    return PrintMode(known.id, lpi, speed) if lpi > 0 else None


def parse_config(xml: str) -> DeviceConfig:
    width = _number(xml, "width_inches")
    cassette = _value(xml, "cassette_type")
    # empty = no cassette; a firmware without the field at all: go by the width
    no_cassette = bool(re.search(r"<cassette_type\s*/>|<cassette_type>\s*</", xml))
    tape = CASSETTE_TYPES.get(cassette)
    if tape is None and not no_cassette and width:
        tape = nominal_width(width * 25.4)
    length = _number(xml, "media_length_initial")
    dpi = _number(xml, "crossweb_dpi")
    return DeviceConfig(
        model=_value(xml, "model_name"),
        firmware=_value(xml, "fw_version_number"),
        crossweb_dpi=int(dpi) if dpi and dpi > 0 else CROSSWEB_DPI,
        tape_mm=tape,
        length_in=length if length and length > 0 else None,
        printable_in=_number(xml, "width_inches_media"),
        media_id=_value(xml, "media_id"),
        cassette_sn=_value(xml, "cassette_sn"),
        modes=parse_modes(xml),
        no_cassette=no_cassette,
    )


def parse_status(xml: str) -> DeviceStatus:
    remain = _number(xml, "remain")
    return DeviceStatus(
        state=_value(xml, "print_state").upper(),
        stage=_value(xml, "print_job_stage").upper(),
        error=_value(xml, "print_job_error").upper(),
        remain_in=remain if remain is not None and remain >= 0 else None,
    )


def cassette_date(serial: str, today: date | None = None) -> date | None:
    """Production date from a cassette serial like "180529-0116G…" (YYMMDD).

    An assumption (docs/vc500w.md): only dates that make sense are returned."""
    match = re.match(r"(\d{2})(\d{2})(\d{2})(?!\d)", serial or "")
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        made = date(2000 + year, month, day)
    except ValueError:
        return None
    today = today or date.today()
    return made if date(2010, 1, 1) <= made <= today else None


def cassette_info(config: DeviceConfig) -> CassetteInfo | None:
    if config.tape_mm is None:
        return None
    return CassetteInfo(
        lot=config.media_id[:9],
        serial=config.cassette_sn,
        made=cassette_date(config.cassette_sn),
        printable_mm=round(config.printable_in * 25.4, 1) if config.printable_in else None,
    )


def status_error(status: DeviceStatus) -> PrinterError | None:
    if status.state != "ERROR":
        return None
    cls = JOB_ERRORS.get(status.error, PrinterError)
    return cls(
        "print_state=ERROR print_job_error=%s print_job_stage=%s" % (status.error, status.stage)
    )


# --- errors from labelprinter and sockets -------------------------------------


def map_error(error: Exception) -> PrinterError:
    """labelprinter reports everything as ValueError with English advice;
    turn it into a PrinterError, keeping the original text as details."""
    mapped = network_error(error)
    if mapped:
        return mapped
    text = error_text(error)
    first = text.splitlines()[0]
    if "resolve" in first.lower() or "Cannot connect" in first:
        return PrinterNotFound(text)
    if "refused" in first:
        return PortClosed(text)
    if "timed out" in first:
        return PrinterTimeout(text)
    return PrinterError(text)


# --- print request ------------------------------------------------------------


def print_xml(
    mode: PrintMode,
    data_format: str,
    datasize: int,
    cut: str,
    color: ColorAdjust,
    width: int = 0,
    height: int = 0,
) -> str:
    lines = [
        "<print>",
        "<mode>%s</mode>" % mode.id,
        "<speed>%d</speed>" % mode.speed,
        "<lpi>%d</lpi>" % mode.lpi,
        "<width>%d</width>" % width,
        "<height>%d</height>" % height,
        "<dataformat>%s</dataformat>" % data_format,
    ]
    if data_format == "jpeg":
        lines.append("<autofit>1</autofit>")  # the printer scales to the tape
    lines += ["<datasize>%d</datasize>" % datasize, "<cutmode>%s</cutmode>" % cut]
    if not color.is_neutral():
        # Brother's sliders: -3..+3 divided by 6, two decimals
        steps = color.to_dict()
        for name, key in (
            ("brightness", "brightness"),
            ("color_balance_red", "red"),
            ("color_balance_green", "green"),
            ("color_balance_blue", "blue"),
        ):
            lines.append("<%s>%.2f</%s>" % (name, steps[key] / 6, name))
    lines.append("</print>")
    return "\n".join(lines)


def cross_geometry(tape_mm: int, dpi: int) -> tuple[int, int]:
    """(tape pixels, image width including bleed) across the tape for rawrgb."""
    inches = TAPE_INCHES[tape_mm]
    tape_px = round(inches * dpi)
    if tape_mm == 50:
        return tape_px, round(2.0 * dpi)
    return tape_px, round((inches + 2 * BLEED_IN) * dpi)


def data_format(options: PrintOptions) -> str:
    """rawrgb unless jpeg is asked for explicitly (the fallback setting)."""
    return "jpeg" if options.data_format == "jpeg" else "rawrgb"


def raster_dpi(mode_id: str) -> int:
    return max(CROSSWEB_DPI, KNOWN_MODES.get(mode_id, VIVID).lpi)


def encode_rawrgb(
    image: Image.Image, mode: PrintMode, rendered_dpi: int, crossweb_dpi: int = CROSSWEB_DPI
) -> tuple[bytes, int, int]:
    """Raw RGB rows across the tape; returns (data, width, height).

    The image comes from prepare() at rendered_dpi (raster_dpi()) in label
    space; the printer's own lpi and crossweb dpi decide the final size."""
    length_in = image.width / rendered_dpi
    dpi = max(crossweb_dpi, mode.lpi)
    height = max(1, round(length_in * mode.lpi))
    width = max(1, round(image.height * dpi / rendered_dpi))
    if image.size != (height, width):
        image = image.resize((height, width), Image.Resampling.LANCZOS)
    rows = image.convert("RGB").rotate(-90, expand=True)
    return rows.tobytes(), width, height


def encode_jpeg(image: Image.Image, mode: PrintMode) -> bytes:
    buf = io.BytesIO()
    # the printer scales the image to the tape (autofit), the DPI tag is
    # informative only; it matches the mode's line density
    image.convert("RGB").rotate(-90, expand=True).save(
        buf, format="JPEG", quality=95, dpi=(mode.lpi, mode.lpi)
    )
    return buf.getvalue()


# --- connection -----------------------------------------------------------------


def _connect(host: str, port: int):
    """TCP through labelprinter, or the printer's USB device file."""
    if usb.is_usb(host):
        return _UsbLink(usb.connect(host, STANDARD_TIMEOUT))
    from labelprinter.connection import Connection

    # labelprinter opens an IPv4 socket
    return Connection(mdns.resolve_host(host, socket.AF_INET), port, PRINT_TIMEOUT)


STANDARD_TIMEOUT = 5.0  # an answer to a question, as labelprinter waits on TCP
QUIET = 0.3  # an answer has ended when nothing more comes for this long
ANSWER_LIMIT = 10.0  # seconds after the first piece of an answer, at most
ANSWER_MAX = 1 << 20  # bytes of one answer, at most (config.xml is a few kB)


class _UsbLink:
    """The interface labelprinter's TCP Connection offers (send_message,
    send_file, get_message, close), on the USB device file: the same XML
    protocol (barnguard found it on USB as on TCP, see docs/vc500w.md).
    The job states its size (datasize), so no end-of-job marker is sent
    (assumed, see docs/vc500w.md)."""

    def __init__(self, device):
        self.device = device  # usb.connect() dropped what an earlier session left

    def send_message(self, message) -> None:
        self.device.sendall(message.get_data().encode())

    def send_file(self, handle) -> None:
        self.device.sendall(handle.read())

    def get_message(self, long_timeout: bool = False, buffer_size: int = 4096) -> str:
        """One answer. It may arrive in pieces on USB: read until the
        printer is quiet; the first piece may take long (vivid prints)."""
        device = self.device
        device.settimeout(PRINT_TIMEOUT if long_timeout else STANDARD_TIMEOUT)
        data = device.recv(buffer_size)
        device.settimeout(QUIET)
        # a printer that keeps talking must not hold the session forever
        end = time.monotonic() + ANSWER_LIMIT
        try:
            while len(data) < ANSWER_MAX and time.monotonic() < end:
                piece = device.recv(buffer_size)
                if not piece:  # usb.Connection never says b"", other devices might
                    break
                data += piece
        except PrinterTimeout:
            pass
        finally:
            device.settimeout(STANDARD_TIMEOUT)
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        self.device.close()


class _Busy(ValueError):
    """The printer answered a print request with code 1 (busy)."""


def _answer_code(data: str) -> int:
    match = re.search(r"<code>\s*(-?\d+)\s*</code>", data or "")
    return int(match.group(1)) if match else -1


def _raw_answer(base: type) -> type:
    """labelprinter's answer class, keeping the XML instead of parsing it."""

    class Answer(base):  # a class from labelprinter
        xml = ""  # set by labelprinter through _process_data()

        def _process_data(self, data):
            self.xml = data

    return Answer


class _Session:
    """Requests on one open connection, answers parsed by labelprinter."""

    def __init__(self, connection):
        self.connection = connection

    def _ask(self, question, answer_class, long_timeout: bool = False):
        self.connection.send_message(question)
        data = self.connection.get_message(long_timeout=long_timeout)
        return answer_class(data, self.connection.get_message)

    def config(self) -> DeviceConfig:
        from labelprinter.printer import Config, GetConfig

        return parse_config(self._ask(GetConfig(), _raw_answer(Config)).xml)

    def status(self) -> DeviceStatus:
        from labelprinter.printer import GetStatus, Status

        return parse_status(self._ask(GetStatus(), _raw_answer(Status)).xml)

    def print(self, xml: str, data: bytes) -> None:
        from labelprinter.printer import PrintAnswer, Question

        self.connection.send_message(Question(xml))
        answer = self.connection.get_message()
        if _answer_code(answer) == 1:
            comment = _value(answer, "comment")
            raise _Busy('The XML status code is not OK: "%s"' % comment if comment else "busy")
        PrintAnswer(answer, self.connection.get_message)
        self.connection.send_file(io.BytesIO(data))
        # vivid prints take well over 30 s before the printer answers
        PrintAnswer(self.connection.get_message(long_timeout=True), self.connection.get_message)


def session(host: str, port: int = DEFAULT_PORT) -> AbstractContextManager[_Session]:
    return open_session(lambda: _connect(host, port), _Session, map_error)


# --- the backend --------------------------------------------------------------------


class VC500W(Backend):
    id = "vc500w"
    name = "Brother VC-500W"
    capabilities = Capabilities(
        tape_widths_mm=TAPE_WIDTHS_MM,
        dpi=PRINT_DPI,
        modes=(VIVID, COLOR),
        reports_remaining=True,
        supports_color_adjust=True,
        data_formats=("rawrgb", "jpeg"),
        min_length_mm=MIN_LENGTH_MM,
        max_length_mm=MAX_LENGTH_IN * 25.4,
    )

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port

    def configured_host(self) -> str | None:
        return configured_host()

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        """The VC-500W on USB, then on the network."""
        plugged = [
            FoundPrinter("Brother VC-500W (USB)", p.address, str(p.device))
            for p in usb.printers()
            if _is_vc500w(p.model)
        ]
        return plugged + mdns.browse("_ipp._tcp", _is_vc500w, timeout)

    def query(self, host: str) -> PrinterInfo:
        with session(host, self.port) as s:
            config = s.config()
            status = s.status()
        return self.info(config, status)

    @staticmethod
    def info(config: DeviceConfig, status: DeviceStatus) -> PrinterInfo:
        remaining = total = None
        if config.tape_mm and config.length_in and status.remain_in is not None:
            total = round(config.length_in * 25.4)
            remaining = round(status.remain_in * 25.4)
        # the printer says print_job_error=NONE when all is well
        problem = status_error(status)
        return PrinterInfo(
            model=config.model or "VC-500W",
            tape_mm=config.tape_mm,
            remaining_mm=remaining,
            total_mm=total,
            state=status.state,
            error=problem.code if problem else "",
            modes=config.modes,
            firmware=config.firmware,
            cassette=cassette_info(config),
        )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        if data_format(options) == "jpeg":
            return Raster(PRINT_DPI)
        dpi = raster_dpi(options.mode)
        tape_px, width = cross_geometry(nominal_width(tape_mm), dpi)
        return Raster(dpi, tape_px, bleed_px=math.ceil((width - tape_px) / 2), width_px=width)

    def preview(self, image: Image.Image, mode: str = "") -> Image.Image:
        if mode == "bw":
            return image.convert("L").convert("RGB")
        return super().preview(image, mode)

    def print_label(
        self,
        image: Image.Image,
        host: str,
        options: PrintOptions,
        progress: ProgressCallback | None = None,
    ) -> PrintResult | None:
        fmt = data_format(options)
        cut = options.cut if options.cut in self.capabilities.cut_modes else "full"
        # prepare() rendered the image at this resolution along the tape
        rendered_dpi = raster_dpi(options.mode) if fmt == "rawrgb" else PRINT_DPI
        check_length(image.width / rendered_dpi * 25.4, self.capabilities)
        with session(host, self.port) as s:
            config = s.config()
            if config.no_cassette:
                raise NoCassette("config.xml: empty cassette_type")
            mode = _pick_mode(config.modes or self.capabilities.modes, options.mode)
            if mode.id == "bw":
                image = image.convert("L")
            if fmt == "rawrgb":
                data, width, height = encode_rawrgb(image, mode, rendered_dpi, config.crossweb_dpi)
            else:
                data, width, height = encode_jpeg(image, mode), 0, 0
            xml = print_xml(mode, fmt, len(data), cut, options.color, width, height)
            for n in range(1, options.copies + 1):
                report_progress(progress, n, options.copies)
                _wait_ready(s, progress, n, options.copies)
                _send(s, xml, data)
                _wait_done(s, progress, n, options.copies)
        if options.mode and mode.id != options.mode:
            return PrintResult(mode=mode.id)
        return None


def _is_vc500w(name: str) -> bool:
    upper = name.upper()
    return "VC-500W" in upper.replace(" ", "-") or "VC500W" in upper


def _pick_mode(modes: tuple[PrintMode, ...], wanted: str) -> PrintMode:
    return next((m for m in modes if m.id == wanted), modes[0])


def _is_ready(status: DeviceStatus) -> bool:
    """Ready for a new job, as Brother's app checks it."""
    return status.state == "IDLE" and status.stage in READY_STAGES


def _poll(s: _Session, progress, n: int, copies: int, done) -> None:
    """Poll the status until done(status); stop at once on an error. The
    timeout restarts whenever the stage changes (preheating can take a while)."""
    stage = None
    deadline = _clock() + PRINT_TIMEOUT
    while True:
        status = s.status()
        error = status_error(status)
        if error:
            raise error
        if not _is_ready(status) and status.stage != stage:
            stage = status.stage
            deadline = _clock() + PRINT_TIMEOUT
            report_progress(progress, n, copies, stage)
        if done(status):
            return
        if status.stage == CUT_STAGE:
            # waiting for someone to swipe the panel: no time limit
            deadline = _clock() + PRINT_TIMEOUT
        if _clock() > deadline:
            raise PrinterTimeout("printer stayed in %s / %s" % (status.state, status.stage))
        _sleep(POLL_INTERVAL)


def _wait_ready(s: _Session, progress, n: int, copies: int) -> None:
    _poll(s, progress, n, copies, _is_ready)


def _wait_done(s: _Session, progress, n: int, copies: int) -> None:
    """After sending, the printer still reports the old, ready state for a
    moment. Done once it was busy and is ready again, or if it showed no job
    within START_GRACE (a job finished between two polls)."""
    sent = _clock()
    started = False

    def done(status: DeviceStatus) -> bool:
        nonlocal started
        if status.stage == CUT_STAGE:
            return True  # printed; only the cut (by hand) is left
        if not _is_ready(status):
            started = True
            return False
        return started or _clock() - sent >= START_GRACE

    _poll(s, progress, n, copies, done)


def _send(s: _Session, xml: str, data: bytes) -> None:
    """Send one job; answer code 1 (busy) is retried SEND_RETRIES times."""
    for attempt in range(SEND_RETRIES + 1):
        try:
            s.print(xml, data)
        except _Busy as e:
            if attempt == SEND_RETRIES:
                raise _rejected(s, e) from e
            _sleep(POLL_INTERVAL)
        except ValueError as e:
            if "status code is not OK" not in str(e):
                raise
            raise _rejected(s, e) from e
        else:
            return


def _rejected(s: _Session, error: Exception) -> PrinterError:
    """The printer refused the job: its status tells why."""
    try:
        status = s.status()
    except Exception:  # noqa: BLE001
        return PrinterError(str(error))
    found = status_error(status)
    if found:
        found.details = "%s; %s" % (error, found.details)
        return found
    if status.state == "BUSY":
        return PrinterBusy(str(error))
    # The printer rejects the job with comment "no media" when no cassette is
    # inserted; status.xml then reads IDLE/IDLE without an error (seen on a
    # VC-500W with firmware 2026031504)
    if "no media" in str(error).lower() or (status.state == "IDLE" and status.stage == "IDLE"):
        return NoCassette(str(error))
    return PrinterError(str(error))
