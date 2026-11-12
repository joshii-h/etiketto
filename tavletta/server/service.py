"""What the server can do, for HTTP and MCP alike.

Labels come from the labels folder or as content in the request; values,
data rows and checks work as on the command line (cli.py), whose pieces
are used here: a label prints the same from the window, the command line
and the server. Messages are translated in the language of the request
(i18n.language()), errors are ApiError.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fnmatch
import functools
import io
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from tavletta import __version__
from tavletta.backends import backends, get_backend
from tavletta.backends.base import Backend, PrinterError, PrinterInfo, PrintOptions
from tavletta.cli import (
    EXIT_COUNTER,
    EXIT_FILE,
    EXIT_LABEL,
    REFUSED,
    CliError,
    _check_rows,
    _options,
    _sheet_job,
    apply_assignments,
)
from tavletta.job import Job, PrepareFailed
from tavletta.merge import MergeError, read_book
from tavletta.model import (
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    LockTimeout,
    PageElement,
    TextElement,
    data_inside,
    locked,
    writable,
)
from tavletta.render import RenderTooLarge, RenderWarning, limited, render
from tavletta.serial import (
    Series,
    SeriesError,
    SeriesProgress,
    SeriesResult,
    print_series,
    row_span,
    unknown_columns,
    used_columns,
)
from tavletta.server.config import LOCAL, Caller, NamedPrinter
from tavletta.server.errors import ApiError, bad_request, invalid_field, not_found
from tavletta.server.jobs import PRINTING, WAITING, Failure, JobQueue, PrintJob, QueueFull
from tavletta.server.paths import LabelsDir
from tavletta.server.requests import LabelRequest, PreviewRequest, PrintRequest
from tavletta.sheets.layout import SheetJob
from tavletta.ui.i18n import _
from tavletta.ui.settings import PrinterSettings, Settings
from tavletta.ui.texts import (
    backend_name,
    code_text,
    data_error_text,
    label_error_text,
    printed_remark,
    printer_error_text,
    save_error_text,
    series_stop_text,
    warning_text,
)
from tavletta.units import unit_for
from tavletta.variables import BUILTIN, Values, label_names, uses_counter

log = logging.getLogger(__name__)
DISCOVERY_SECONDS = 30.0  # printers found are remembered this long
STATUS_SECONDS = 5.0  # the state of printers in the list, as long
MAX_HOST = 512
RENDERS = 2  # renders for previews and checks at once: a large label takes much memory
RENDER_WAIT = 20.0  # seconds a request waits for one, then 503
MAX_WAITERS = 8  # requests waiting for one; more get 503 at once
MAX_PICTURES = 50  # image and code elements of a label: each takes time to draw
# what one label of a request may be: a text of thousands of lines, or
# thousands of elements, would take gigabytes and minutes to render
MAX_ELEMENTS = 200
MAX_TEXT = 5000  # characters of one text or code, filled in
MAX_LINES = 100
# of any one image made while rendering, and of all elements of a label
# drawn together; image files and QR codes each get half of it (render.Limits)
MAX_PIXELS = 40_000_000


@dataclass
class Prepared:
    """A label filled in and checked, ready to be previewed or printed."""

    label: Label
    path: Path | None  # the label file; None for a label sent as content
    backend: Backend
    settings: Settings
    printer: PrinterSettings
    options: PrintOptions
    values: Values
    rows: list[dict[str, str]]  # data rows; [] for a label without
    indexes: list[int]  # the rows to print
    fixed_time: bool  # "date" was set: the time is not that of printing
    fixed_counter: bool  # counter_start was given
    named: NamedPrinter | None = None  # the printer of printers.toml it is for
    # a label sheet (page printers): every label a position, as in the app
    sheet: SheetJob | None = None

    def job(self, values: Values | None = None, index: int | None = None) -> Job:
        columns = self.rows[self.indexes[0] if index is None else index] if self.rows else None
        return Job(
            self.backend,
            self.label,
            self.options,
            self.printer.safe_mm,
            values or self.values,
            columns,
            self.sheet,
        )


@dataclass
class Checked:
    """A print request filled in and checked, not queued yet."""

    request: PrintRequest
    prepared: Prepared
    warnings: list[RenderWarning]
    save: bool  # the next counter value goes into the label file
    total: int  # labels


@dataclass
class Preview:
    png: bytes
    width: int
    height: int
    warnings: list[dict[str, str]]

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "warnings": self.warnings,
            "png": base64.b64encode(self.png).decode(),
        }


def _warning(key: str, message: str) -> dict[str, str]:
    return {"code": key, "message": message}


def _cli_error(error: CliError) -> ApiError:
    """The command line's errors (already translated) as API errors."""
    status, code = {
        EXIT_LABEL: (422, "label_unprintable"),
        EXIT_FILE: (422, "file_problem"),
        EXIT_COUNTER: (409, "counter_not_saved"),
    }.get(error.code, (400, "bad_request"))
    return ApiError(status, code, str(error))


def host_name(host: str) -> str | None:
    """The host a printer address names, lower case: of "ipp://Cab.local:631/
    ipp/print" "cab.local", of "192.0.2.5" itself, of a CUPS queue name the
    name. None for no address: not printable ASCII, spaces, a user name or
    password ("user@host", which would also hide the real host from a
    pattern), or no host at all."""
    if not 0 < len(host) <= MAX_HOST or not all(33 <= ord(c) < 127 for c in host):
        return None
    if "@" in host or "\\" in host:
        return None
    try:
        parts = urlsplit(host if "://" in host else "//" + host)
        parts.port  # noqa: B018 - raises for a port that is no number
    except ValueError:
        return None
    return parts.hostname or None


def host_ok(host: str) -> bool:
    """An address as a printer has one: host name, IP address, printer URI
    or CUPS queue name."""
    return host_name(host) is not None


def _ip_pattern(pattern: str) -> bool:
    """An address pattern: an IP address or network as ipaddress reads it
    (192.168.1.5, 192.168.1.0/24, 2001:db8::/32), or digits, dots and *
    only (192.168.1.*, 10.*, *.5). Anything with a letter, a hyphen or
    another sign is a pattern for host names (*.local, printer-?)."""
    try:
        ipaddress.ip_network(pattern, strict=False)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[0-9.*]+", pattern))


_OCTET = re.compile(r"\*|0|[1-9]\d{0,2}")


def pattern_problem(pattern: str) -> bool:
    """Whether an --allow-printer pattern cannot be meant: a network that
    is none ("10.0.0.0/33"), an empty one, or an address pattern that can
    never match an address: a part over 255 or with a leading zero
    ("010"), a * that is not a whole part ("10*", "**"), more than four
    parts, or fewer than four without a * ("1.2.3", "10")."""
    if not pattern:
        return True
    if "/" in pattern or ":" in pattern:
        return not _network_ok(pattern)
    if not re.fullmatch(r"[0-9.*]+", pattern):
        return False  # a pattern for host names
    return _address_pattern_problem(pattern.split("."))


def _address_pattern_problem(parts: list[str]) -> bool:
    if len(parts) > 4 or not all(_OCTET.fullmatch(part) for part in parts):
        return True
    if "*" not in parts and len(parts) != 4:
        return True
    return any(part.isdigit() and int(part) > 255 for part in parts)


def _network_ok(pattern: str) -> bool:
    try:
        ipaddress.ip_network(pattern, strict=False)
    except ValueError:
        return False
    return True


def _ip_matches(address: str, pattern: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if "/" in pattern:
        try:
            return ip in ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            return False
    return fnmatch.fnmatchcase(str(ip), pattern.lower())


def _resolved(name: str) -> list[str]:
    """The addresses of a name in the resolver's order (the first is the
    one printed on), each once."""
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def host_allowed(name: str, patterns: tuple[str, ...]) -> str | None:
    """The host to print on if it matches --allow-printer, else None.
    Patterns are compared with the host, never the whole address (a path
    or a user name cannot make "evil/x.printers.lan" pass "*.printers.lan").
    "*" allows everything (no lookup). Name patterns match names. Address
    patterns (192.168.1.0/24, 192.168.1.*) match IP addresses; a name only
    if every address it resolves to matches one of them, and then the
    address checked is returned, to be printed on: a name looked up again
    could point elsewhere by then (DNS rebinding)."""
    if "*" in patterns:
        return name
    is_ip = bool(_ip_matches(name, "*"))
    names = [p for p in patterns if not _ip_pattern(p)]
    if not is_ip and any(fnmatch.fnmatchcase(name, p.lower()) for p in names):
        return name
    return _address_allowed(name, is_ip, [p for p in patterns if _ip_pattern(p)])


def _address_allowed(name: str, is_ip: bool, patterns: list[str]) -> str | None:
    """The address of `name` if every address it has matches a pattern."""
    if not patterns:
        return None
    resolved = [name] if is_ip else _resolved(name)
    if resolved and all(any(_ip_matches(a, p) for p in patterns) for a in resolved):
        return resolved[0]
    return None


def with_address(host: str, address: str) -> str:
    """The printer address with its host replaced by an IP address."""
    scheme = "://" in host
    parts = urlsplit(host if scheme else "//" + host)
    netloc = "[%s]" % address if ":" in address else address
    if parts.port:
        netloc += ":%d" % parts.port
    text = urlunsplit(parts._replace(netloc=netloc))
    return text if scheme else text[2:]


DEFAULT_PORTS = {"ipp": 631, "ipps": 631, "http": 80, "https": 443}


def queue_key(host: str) -> str:
    """The same printer however its address is written: the case of the
    name, a dot at its end, the default port given or not."""
    parts = urlsplit(host if "://" in host else "//" + host)
    name = (parts.hostname or "").rstrip(".")
    port = parts.port or DEFAULT_PORTS.get(parts.scheme.lower()) or ""
    return "%s:%s%s" % (name, port, parts.path)


def _cost(prepared: Prepared) -> int:
    """About the bytes a waiting job holds: the label (images in it) and
    the data rows."""
    label = prepared.label
    size = sum(len(getattr(e, "data", "") or getattr(e, "text", "")) for e in label.elements)
    size += len(label.background_image.data) if label.background_image else 0
    rows = sum(len(k) + len(v) + 100 for row in prepared.rows for k, v in row.items())
    return 2 * size + rows + 4096


def redacted(host: str | None) -> str | None:
    """An address without a user name and password, for answers."""
    if not host or "@" not in host:
        return host
    scheme, sep, rest = host.rpartition("://")
    return scheme + sep + rest.rpartition("@")[2]


class Service:
    def __init__(
        self,
        labels_dir: str | Path,
        allowed_hosts: tuple[str, ...] = (),
        settings: Callable[[], Settings] = Settings.load,
        clock: Callable[[], datetime] = datetime.now,
        printers: Mapping[str, NamedPrinter] | None = None,
        discover: bool = False,
        max_labels: int = 10_000,
        allow_pdf_pages: bool = False,
    ):
        self.labels = LabelsDir(labels_dir)
        # fnmatch patterns, spaces around them dropped; () allows any
        self.allowed_hosts = tuple(p.strip() for p in allowed_hosts)
        # printers.toml: only these printers, chosen by name
        self.named = dict(printers) if printers is not None else None
        self.discovery = discover  # search the network (off unless --discover)
        self.max_labels = max_labels  # copies times rows of one job
        # pages (PDFs, images) in label_content: off unless --allow-pdf-pages
        self.allow_pdf_pages = allow_pdf_pages
        self._settings = settings
        self._clock = clock
        self.jobs = JobQueue(self._failed)
        self._found: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._found_lock = threading.Lock()
        self._label_locks: dict[str, threading.Lock] = {}
        self._searches: dict[str, threading.Lock] = {}
        self._states: dict[str, tuple[float, PrinterInfo | Exception]] = {}
        self._asking: dict[str, threading.Lock] = {}
        self._renders = threading.BoundedSemaphore(RENDERS)
        self._held = threading.local()  # render slots this thread holds
        self._waiting = 0  # requests waiting for a render slot
        self._waiting_lock = threading.Lock()

    def close(self, timeout: float = 30.0) -> None:
        """Cancel the waiting jobs; printing ones stop after their label."""
        self.jobs.close(timeout)

    # --- printers -------------------------------------------------------------

    def health(self) -> dict:
        return {"status": "ok", "version": __version__}

    def printers(
        self, discover: bool = True, status: bool = True, caller: Caller = LOCAL
    ) -> list[dict]:
        """The printers of printers.toml (with their state), else the
        printer models with the address set in the app and those found."""
        if self.named is not None:
            return self._named_printers(status, caller)
        chosen = backends()
        settings = self._settings()
        found: dict[str, list[dict[str, str]]] = {}
        if discover and self.discovery:
            with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                found = dict(
                    zip((b.id for b in chosen), pool.map(self._discover, chosen), strict=True)
                )
        return [self._printer(b, settings, found.get(b.id, [])) for b in chosen]

    def _named_printers(self, status: bool, caller: Caller) -> list[dict]:
        assert self.named is not None
        chosen = [p for p in self.named.values() if caller.may_use(p.name)]
        if not chosen:
            return []
        states: list[dict | None] = [None] * len(chosen)
        if status:
            with ThreadPoolExecutor(max_workers=min(len(chosen), 16)) as pool:
                states = list(pool.map(self._state, chosen))
        return [
            dict(p.to_dict(), model=backend_name(get_backend(p.backend)), status=state)
            for p, state in zip(chosen, states, strict=True)
        ]

    def _probe(self, printer: NamedPrinter) -> PrinterInfo | Exception:
        """What the printer answered (or the error), remembered a few
        seconds and asked for once at a time per printer: a list asked for
        often must not keep every printer busy. Kept raw, so each answer is
        in the language of its request."""
        with self._found_lock:
            asking = self._asking.setdefault(printer.name, threading.Lock())
        with asking:
            with self._found_lock:
                cached = self._states.get(printer.name)
            if cached and time.monotonic() - cached[0] < STATUS_SECONDS:
                return cached[1]
            try:
                outcome: PrinterInfo | Exception = get_backend(printer.backend).query(printer.host)
            except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
                _detach(e)
                outcome = e
            with self._found_lock:
                self._states[printer.name] = (time.monotonic(), outcome)
            return outcome

    def _named_state(self, printer: NamedPrinter) -> dict:
        """The state of a printer of printers.toml; ApiError if it fails."""
        settings = self._named_settings(printer)
        outcome = self._probe(printer)
        if isinstance(outcome, Exception):
            raise _printer_failed(outcome, settings)
        return self._info(get_backend(printer.backend), printer.host, outcome, settings)

    def _state(self, printer: NamedPrinter) -> dict:
        """A printer's state for the list; a problem is part of the state."""
        try:
            return self._named_state(printer)
        except ApiError as e:
            return {"state": "UNREACHABLE", "error": e.to_dict()}

    def _named_settings(self, printer: NamedPrinter) -> Settings:
        """The app's settings, with this printer's own for its model."""
        base = self._settings()
        return Settings(
            {"units": base.units, "printers": {printer.backend: dict(printer.settings)}}
        )

    def _named(self, name: object, caller: Caller) -> NamedPrinter:
        assert self.named is not None
        printer = self.named.get(name) if isinstance(name, str) else None
        # a printer the token may not use is as unknown as one that is not
        # there: the names of the others are none of its business
        if printer is None or not caller.may_use(printer.name):
            raise not_found("printer_not_found", _("Unknown printer: %s") % name)
        return printer

    def _choose(self, name: str | None, caller: Caller, required: bool) -> NamedPrinter | None:
        """The printer a request names, else the default one, else the only one."""
        assert self.named is not None
        if name is not None:
            return self._named(name, caller)
        chosen = _default_printer([p for p in self.named.values() if caller.may_use(p.name)])
        if chosen is not None:
            return chosen
        if required:
            raise bad_request(_("Choose a printer by its name"), "no_printer")
        return None

    def _printer(self, backend: Backend, settings: Settings, found: list[dict]) -> dict:
        caps = backend.capabilities
        kind = "tape" if caps.continuous else ("pages" if caps.pages else "labels")
        return {
            "id": backend.id,
            "name": backend_name(backend),
            "kind": kind,
            "dpi": caps.dpi,
            "color_model": caps.color_model,
            "tape_widths_mm": list(caps.tape_widths_mm),
            "label_sizes_mm": [[s.width_mm, s.height_mm] for s in caps.label_sizes],
            "configured_host": redacted(settings.printer(backend).host),
            "found": found,
        }

    def _discover(self, backend: Backend) -> list[dict[str, str]]:
        """The printers found, remembered for a while; one search at a time
        per model, the others wait for its result."""
        with self._found_lock:
            searching = self._searches.setdefault(backend.id, threading.Lock())
        with searching:
            with self._found_lock:
                cached = self._found.get(backend.id)
            if cached and time.monotonic() - cached[0] < DISCOVERY_SECONDS:
                return cached[1]
            return self._search(backend)

    def _search(self, backend: Backend) -> list[dict[str, str]]:
        try:
            found = [
                {"name": p.name, "host": redacted(p.host) or "", "address": p.address}
                for p in backend.discover()
            ]
        except Exception:  # noqa: BLE001 - a broken plugin must not break the list
            found = []
        with self._found_lock:
            self._found[backend.id] = (time.monotonic(), found)
        return found

    def _backend(self, backend_id: object) -> Backend:
        if not isinstance(backend_id, str):
            raise invalid_field("printer")
        try:
            return get_backend(backend_id)
        except KeyError:
            raise not_found(
                "printer_not_found", _("Unknown printer model: %s") % backend_id
            ) from None

    def status(self, printer: object, host: object = None, caller: Caller = LOCAL) -> dict:
        """The state of a printer: by name with printers.toml, else a
        printer model and an address."""
        if host is not None and not isinstance(host, str):
            raise invalid_field("host")
        if self.named is not None:
            if host is not None:
                raise _host_given()
            named = self._named(printer, caller)
            return dict(self._named_state(named), name=named.name)
        if host is not None and not caller.may("print"):
            # a preview token must not make the server contact any address
            raise ApiError(403, "forbidden", _("This access token may not do this"))
        backend = self._backend(printer)
        settings = self._settings()
        return self._query(backend, self._host(host or settings.printer(backend).host), settings)

    def _query(self, backend: Backend, host: str, settings: Settings) -> dict:
        try:
            info = backend.query(host)
        except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
            raise _printer_failed(e, settings) from None
        return self._info(backend, host, info, settings)

    def _info(self, backend: Backend, host: str, info: PrinterInfo, settings: Settings) -> dict:
        unit = unit_for(settings.units)
        cassette = info.cassette
        return {
            "printer": backend.id,
            "host": host,
            "model": info.model or backend.display_name(info),
            "state": info.state,
            "error": (
                {"code": info.error, "message": code_text(info.error, unit)}
                if info.error
                else None
            ),
            "dpi": backend.capabilities.dpi,
            "firmware": info.firmware or None,
            "modes": [m.id for m in backend.modes(info)],
            "media": {
                "tape_mm": info.tape_mm,
                "remaining_mm": info.remaining_mm,
                "total_mm": info.total_mm,
                "pitch_mm": info.pitch_mm,
                "cassette": (
                    {
                        "lot": cassette.lot,
                        "serial": cassette.serial,
                        "made": cassette.made.isoformat() if cassette.made else None,
                        "printable_mm": cassette.printable_mm,
                    }
                    if cassette
                    else None
                ),
            },
        }

    def _host(self, host: str | None) -> str:
        if not host:
            raise bad_request(_("No printer address: give it as host"), "no_host")
        name = host_name(host)
        if name is None:
            raise invalid_field("host")
        if not self.allowed_hosts:
            return host
        checked = host_allowed(name, self.allowed_hosts)
        if checked is None:
            raise ApiError(
                403, "host_not_allowed", _("This printer address is not allowed: %s") % host
            )
        return host if checked == name else with_address(host, checked)

    def _print_host(self, request: PrintRequest, prepared: Prepared) -> str:
        """The address asked for, the one set in the app, or the only
        printer found."""
        if prepared.named is not None:
            if request.host is not None:
                raise _host_given()
            return prepared.named.host
        host = request.host or prepared.printer.host
        if not host and self.discovery:
            found = self._discover(prepared.backend)
            host = found[0]["host"] if len(found) == 1 else None
        return self._host(host)

    # --- labels ---------------------------------------------------------------

    def list_labels(self) -> list[dict]:
        result = []
        for path in self.labels.labels():
            with contextlib.suppress(OSError):
                info = path.stat()
                result.append(
                    {
                        "name": self.labels.name_of(path),
                        "bytes": info.st_size,
                        "modified": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(
                            timespec="seconds"
                        ),
                    }
                )
        return result

    def describe(self, name: object) -> dict:
        path = self.labels.label(name)
        label = self._load(path)
        names = label_names(label)
        width, height = label.media_size()
        data_file = label.data_file
        own_data = (
            bool(data_file) and self.labels.contains(data_file) and data_inside(data_file, path)
        )
        return {
            "name": self.labels.name_of(path),
            "printer": {"id": label.printer, "name": backend_name(self._backend(label.printer))},
            "size": {
                "tape_mm": label.tape_mm,
                "length_mm": None if label.auto_length else label.length_mm,
                "auto_length": label.auto_length,
                "orientation": label.orientation,
                "width_mm": width,
                "height_mm": height,
                "gap_mm": label.gap_mm,
            },
            "elements": [_element(n, e) for n, e in enumerate(label.elements, 1)],
            "variables": sorted(names - BUILTIN),
            "builtin_variables": sorted(names & BUILTIN),
            "columns": used_columns(label),
            "data_file": self.labels.name_of(data_file) if own_data else None,
            "data_file_unusable": bool(data_file) and not own_data,
            "data_sheet": label.data_sheet or None,
            "counter": {
                "used": uses_counter(label),
                "next": label.counter_next,
                "step": label.counter_step,
            },
            "features": label.features(),
        }

    def _load(self, path: Path) -> Label:
        name = self.labels.name_of(path)
        try:
            return Label.load(path)
        except OSError:
            raise ApiError(422, "file_problem", _("Cannot open %s") % name) from None
        except LabelError as e:
            raise ApiError(422, "label_" + e.code, label_error_text(e, name)) from None

    def _content(self, content: dict) -> Label:
        try:
            label = Label.from_dict(content)
        except LabelError as e:
            raise ApiError(422, "label_" + e.code, label_error_text(e, "label_content")) from None
        except (TypeError, ValueError, RecursionError):  # e.g. a field of the wrong kind
            raise ApiError(
                422, "label_damaged", _("The file is damaged: %s") % "label_content"
            ) from None
        # a label sent along may name any file: its data come with the request
        label.data_file, label.data_sheet = "", ""
        if not self.allow_pdf_pages and any(isinstance(e, PageElement) for e in label.elements):
            raise ApiError(
                422,
                "pages_not_allowed",
                _("Document pages in label_content are not allowed (--allow-pdf-pages)"),
            )
        return label

    # --- filling in -----------------------------------------------------------

    def prepare(
        self,
        request: LabelRequest,
        copies: int = 1,
        rows=None,
        caller: Caller = LOCAL,
        printing: bool = False,
    ) -> Prepared:
        """The label of a request with its values and data rows, checked
        as the command line checks before printing. With printers.toml
        `printer` names one of its printers (printing needs one)."""
        named = None
        if self.named is not None:
            named = self._choose(request.printer, caller, printing)
        path = self.labels.label(request.label) if request.label is not None else None
        label = self._label(request, path, named.backend if named else request.printer)
        backend = self._backend(label.printer)
        settings = self._named_settings(named) if named else self._settings()
        printer = settings.printer(backend)
        if request.counter_start is not None:
            label.counter_next = request.counter_start
        table, indexes = self._rows(request, label, path, rows)
        try:
            overrides = apply_assignments(label, request.assignments, bool(table))
        except CliError as e:
            raise _cli_error(e) from None
        _check_limits(label, table)
        options = _options(backend, printer, copies, label.gap_mm)
        values = Values.for_label(
            label, overrides.now or self._clock(), copies, custom=overrides.custom
        )
        sheet = _sheet_job(label, backend, printer, copies * max(1, len(indexes)))
        return Prepared(
            label,
            path,
            backend,
            settings,
            printer,
            options,
            values,
            table,
            indexes,
            overrides.now is not None,
            request.counter_start is not None,
            named,
            sheet,
        )

    def _label(self, request: LabelRequest, path: Path | None, model: str | None) -> Label:
        """The label of the request, moved to the printer model asked for."""
        label = self._load(path) if path else self._content(request.content or {})
        if model and model != label.printer:
            self._backend(model)
            label.switch_printer(model)
        return label

    def _rows(self, request: LabelRequest, label: Label, path: Path | None, span):
        """The data rows: given inline, a data file of the labels folder,
        or the label's own data file if it lies beside it in the folder."""
        if request.data_rows is not None:
            table = request.data_rows
        else:
            data = self._data_path(request, label, path)
            if data is None:
                return [], []
            own = bool(label.data_file) and Path(label.data_file).resolve() == data.resolve()
            table = self._read(data, request.sheet or (label.data_sheet if own else ""))
        first, last = span or (1, len(table))
        indexes = list(row_span(first, last, len(table)))
        if not indexes:
            raise bad_request(_("No rows to print"), "no_rows")
        return table, indexes

    def _data_path(self, request: LabelRequest, label: Label, path: Path | None) -> Path | None:
        if request.data_file is not None:
            return self.labels.data(request.data_file)
        if not label.data_file or path is None:
            if request.sheet:
                raise bad_request(_("A sheet needs a data file"), "no_data")
            return None
        if not (self.labels.contains(label.data_file) and data_inside(label.data_file, path)):
            raise ApiError(
                422,
                "data_outside",
                _("The label uses a data file outside the labels folder: send its rows along"),
            )
        return Path(label.data_file)

    def _read(self, path: Path, sheet: str) -> list[dict[str, str]]:
        name = path.name
        try:
            book = read_book(path, sheet)
        except (OSError, MergeError) as e:
            raise ApiError(422, "data_problem", data_error_text(e, name)) from None
        if sheet and book.sheet != sheet:
            error = MergeError("no_sheet", sheet)
            raise ApiError(422, "data_problem", data_error_text(error, name))
        return book.table.rows

    # --- preview --------------------------------------------------------------

    def preview(self, request: PreviewRequest, caller: Caller = LOCAL) -> Preview:
        prepared = self.prepare(request, caller=caller)
        index = request.row - 1
        if prepared.rows and index >= len(prepared.rows):
            raise invalid_field("row")
        job = prepared.job(index=index if prepared.rows else None)
        with self._rendering(prepared):
            label, warnings = job.expanded(1)
            if request.view == "printer":
                image = self._printer_image(job, label)
            else:
                caps = prepared.backend.capabilities
                rendered = render(
                    label, caps.dpi, safe_mm=job.safe_mm, min_length_mm=caps.min_length_mm
                )
                warnings = warnings + rendered.warnings
                image = prepared.backend.preview(rendered.image, prepared.options.mode)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        texts = [_warning(w.key, warning_text(w)) for w in warnings]
        if prepared.rows and (unknown := unknown_columns(prepared.label, list(prepared.rows[0]))):
            names = ", ".join("{%s}" % name for name in unknown)
            texts.append(_warning("unknown_column", _("No such column: %s") % names))
        return Preview(buffer.getvalue(), image.width, image.height, texts)

    @contextlib.contextmanager
    def _rendering(self, prepared: Prepared) -> Iterator[None]:
        """Render for a request: in a render slot, within the limits."""
        caps = prepared.backend.capabilities
        with self.render_slot(RENDER_WAIT):
            try:
                with limited(MAX_PIXELS, caps.max_length_mm):
                    yield
            except RenderTooLarge:
                raise _too_large() from None

    @contextlib.contextmanager
    def render_slot(self, timeout: float | None = RENDER_WAIT) -> Iterator[None]:
        """One of the RENDERS slots every render takes: previews, checks and
        print jobs alike. Held by this thread already: nothing more to take
        (the HTTP server takes it before it parses a request). With
        MAX_WAITERS requests waiting already, the next one gets 503 at once,
        as one that waits `timeout` seconds; print jobs (timeout None) wait
        their turn."""
        if getattr(self._held, "count", 0):
            self._held.count += 1
            try:
                yield
            finally:
                if self._held.count:  # 0: given back meanwhile (slot_released)
                    self._held.count -= 1
            return
        self._take(timeout)
        self._held.count = 1
        try:
            yield
        finally:
            if self._held.count:  # not given back meanwhile
                self._held.count = 0
                self._renders.release()

    @contextlib.contextmanager
    def slot_released(self) -> Iterator[None]:
        """Give back the render slot this thread holds, for good: what
        follows needs none (it waits for a job or asks a printer). A render
        afterwards takes a slot anew, with the same timeout and waiting
        limit as any; the render_slot() that took this one does not give it
        back a second time."""
        if getattr(self._held, "count", 0):
            self._held.count = 0
            self._renders.release()
        yield

    def _take(self, timeout: float | None) -> None:
        if timeout is None:
            self._renders.acquire()
            return
        with self._waiting_lock:
            if self._waiting >= MAX_WAITERS:
                raise _busy()
            self._waiting += 1
        try:
            if not self._renders.acquire(timeout=timeout):
                raise _busy()
        finally:
            with self._waiting_lock:
                self._waiting -= 1

    @staticmethod
    def _printer_image(job: Job, label: Label):
        try:
            return job.image(label)
        except PrepareFailed as e:
            if isinstance(e.__cause__, RenderTooLarge):
                raise _too_large() from None
            raise ApiError(
                422, "prepare_failed", _("The label cannot be prepared for printing.")
            ) from None

    # --- printing -------------------------------------------------------------

    def submit(self, request: PrintRequest, caller: Caller = LOCAL) -> PrintJob:
        """Check the request and queue the job; ApiError if it cannot print."""
        return self.queue_print(self.check_print(request, caller), caller)

    def check_print(self, request: PrintRequest, caller: Caller = LOCAL) -> Checked:
        """Fill in the label and check it (the part that renders: in a
        render slot); queue_print() then finds the printer and queues it,
        outside the slot (a name lookup or a search can take seconds)."""
        _require(caller, "print")
        prepared = self.prepare(request, request.copies, request.rows, caller, printing=True)
        _check_copies(prepared, request.copies)
        total = request.copies * (len(prepared.indexes) if prepared.rows else 1)
        if total > self.max_labels:
            raise ApiError(
                413,
                "too_many_labels",
                _("At most %d labels in one print job") % self.max_labels,
            )
        with self._rendering(prepared):
            warnings = self._check(prepared)
        save = self._saves(prepared, request.no_save_counter)
        return Checked(request, prepared, warnings, save, total)

    def queue_print(self, checked: Checked, caller: Caller = LOCAL) -> PrintJob:
        prepared, save = checked.prepared, checked.save
        host = self._print_host(checked.request, prepared)
        name = self.labels.name_of(prepared.path) if prepared.path else ""

        def run(job: PrintJob):
            return self._print(job, prepared, save)

        job = PrintJob(
            prepared.backend.id, host, name, checked.total, run, warnings=checked.warnings
        )
        job.owner = caller.name
        job.limited = caller.named
        job.printer_name = prepared.named.name if prepared.named else ""
        job.key = queue_key(host)
        job.cost = _cost(prepared)
        try:
            self.jobs.submit(job)
        except QueueFull:
            raise ApiError(
                429, "queue_full", _("Too many print jobs are waiting – try again later")
            ) from None
        log.info(
            "job %s by %s: %s on %s (%s), %d labels",
            job.id,
            caller.name,
            name or "label_content",
            job.printer_name or job.printer,
            host,
            checked.total,
        )
        return job

    def _saves(self, prepared: Prepared, no_save: bool) -> bool:
        """Whether the job saves the next counter value into the label
        file; refused if it would have to and cannot."""
        save = prepared.path is not None and not no_save
        if save and prepared.path and prepared.job().next_counter() is not None:
            self._check_writable(prepared.path)
        return save

    def _check(self, prepared: Prepared) -> list[RenderWarning]:
        label = prepared.label
        if not label.elements and not label.background_image:
            raise ApiError(422, "label_unprintable", _("The label is empty"))
        warnings = prepared.job().check()
        for warning in warnings:
            if warning.key in REFUSED:
                raise ApiError(422, "label_unprintable", warning_text(warning))
        if prepared.rows:
            series = Series(
                label,
                prepared.rows,
                prepared.indexes,
                prepared.options,
                values=prepared.values,
                sheet=prepared.sheet,
            )
            try:
                _check_rows(series)
            except CliError as e:
                raise _cli_error(e) from None
        return warnings

    @staticmethod
    def _check_writable(path: Path) -> None:
        if not writable(path):
            error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            raise ApiError(
                409,
                "counter_not_saved",
                save_error_text(error, path.name)
                + " – "
                + _("nothing was printed: the next counter value could not be saved"),
            )

    @contextlib.contextmanager
    def _exclusive(self, path: Path | None) -> Iterator[None]:
        """The label file for one job at a time, from reading its counter
        until the next one is saved: other jobs of this server wait here,
        other programs (the command line) through the file lock."""
        if path is None:
            yield
            return
        key = os.path.realpath(path)
        with self._found_lock:
            lock = self._label_locks.setdefault(key, threading.Lock())
        with lock, locked(path):
            yield

    def _print(self, job: PrintJob, prepared: Prepared, save: bool):
        caps = prepared.backend.capabilities
        if save:
            job.state = WAITING  # until the label file is free
        # every render of the job takes a render slot (waits for it): print
        # jobs share the two with previews, the memory stays bounded
        slot = functools.partial(self.render_slot, None)
        with (
            limited(MAX_PIXELS, caps.max_length_mm, slot),
            self._exclusive(prepared.path if save else None),
        ):
            job.state = PRINTING
            if job.stop.is_set():  # canceled while it waited for the file
                return None
            values = self._fresh_values(prepared, save)
            if prepared.rows:
                return self._print_series(job, prepared, values, save)
            return self._print_one(job, prepared, values, save)

    def _fresh_values(self, prepared: Prepared, save: bool) -> Values:
        """The values when the job prints: the time of printing (unless
        set) and the counter as saved now, after the jobs before it."""
        values = prepared.values
        if not prepared.fixed_time:
            values = replace(values, now=self._clock())
        if save and prepared.path and not prepared.fixed_counter:
            saved = Label.saved_counter(prepared.path)
            if saved is not None:
                values = replace(values, counter=saved)
        return values

    def _print_one(self, job: PrintJob, prepared: Prepared, values: Values, save: bool):
        print_job = prepared.job(values)

        def progress(_event) -> None:
            job.printed = print_job.printed

        printed = False
        try:
            result = print_job.run(job.host, progress, job.stop)
            printed = True
        finally:
            job.printed = print_job.printed
            if save:
                counter = print_job.next_counter()
                self._save_counter(prepared, counter, print_job.printed, printed)
        options = prepared.options  # not `prepared`: the finished job keeps this
        return lambda: printed_remark(options, result)

    def _print_series(self, job: PrintJob, prepared: Prepared, values: Values, save: bool):
        outcome = SeriesResult()
        series = Series(
            prepared.label,
            prepared.rows,
            prepared.indexes,
            prepared.options,
            prepared.printer.safe_mm,
            values,
            prepared.sheet,
        )

        def progress(_event: SeriesProgress) -> None:
            job.printed = outcome.labels
            job.rows_printed = len(outcome.printed)

        printed = False
        try:
            print_series(
                prepared.backend, job.host, series, progress, job.stop, outcome, check=False
            )
            printed = True
        finally:
            job.printed = outcome.labels
            job.rows_printed = len(outcome.printed)
            if save:
                self._save_counter(prepared, outcome.counter, outcome.labels, printed)
        options, result = prepared.options, outcome.result
        return lambda: printed_remark(options, result)

    @staticmethod
    def _save_counter(prepared: Prepared, counter: int | None, labels: int, fail: bool) -> None:
        """Save the next counter value, also after a printer problem: the
        labels printed used their numbers. A problem saving it fails the
        job, unless the job failed already (that problem is told then)."""
        if counter is None or not labels or prepared.path is None:
            return
        try:
            Label.store_counter(prepared.path, counter)
        except (OSError, ValueError) as e:
            log.warning("cannot save the counter of %s: %s", prepared.path.name, e)
            if fail:
                raise CounterNotSaved(prepared.path.name, e) from e

    def _failed(self, job: PrintJob, error: Exception) -> None:
        """What a failed job tells: kept as a function, translated when the
        job is asked for."""
        units = self._settings().units
        _detach(error)
        if isinstance(error, CounterNotSaved):
            job.failure = Failure("counter_not_saved", error.text)
        elif isinstance(error, LockTimeout):
            job.failure = Failure(
                "label_locked",
                lambda: _("%s is still locked by another print – try again later") % job.label,
            )
        elif isinstance(error, SeriesError):
            job.failure = Failure(*_series_failure(error, units))
        elif isinstance(error, PrepareFailed):
            job.failure = Failure(
                "prepare_failed", lambda: _("The label cannot be prepared for printing.")
            )
        else:
            code = error.code if isinstance(error, PrinterError) else "generic"
            job.failure = Failure(
                "printer_" + code, lambda: printer_error_text(error, unit_for(units))
            )

    def job(self, job_id: object, caller: Caller = LOCAL) -> PrintJob:
        """A job; others' jobs only for admin tokens (for the rest they are
        not there)."""
        _require(caller, "print")
        job = self.jobs.get(job_id) if isinstance(job_id, str) else None
        if job is not None and not caller.may("admin") and job.owner != caller.name:
            job = None
        if job is None:
            raise not_found("job_not_found", _("No such print job: %s") % job_id)
        return job

    @staticmethod
    def job_dict(job: PrintJob) -> dict:
        data = job.to_dict()
        data["warnings"] = [_warning(w.key, warning_text(w)) for w in job.warnings]
        return data

    def cancel(self, job_id: object, caller: Caller = LOCAL) -> PrintJob:
        job = self.job(job_id, caller)
        if job.done.is_set():
            raise ApiError(409, "job_finished", _("The print job has already finished"))
        self.jobs.cancel(job)
        return job


def _require(caller: Caller, role: str) -> None:
    if not caller.may(role):
        raise ApiError(403, "forbidden", _("This access token may not do this"))


def _detach(error: BaseException | None) -> None:
    """Drop the traceback (and what it holds: the request, the label, the
    rows) from an error a finished job keeps."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        inner = getattr(error, "error", None)
        error.__traceback__ = None
        error.__context__ = error.__cause__ = None
        error = inner if isinstance(inner, BaseException) else None


def _printer_failed(error: Exception, settings: Settings) -> ApiError:
    code = error.code if isinstance(error, PrinterError) else "generic"
    return ApiError(502, "printer_" + code, printer_error_text(error, unit_for(settings.units)))


def _busy() -> ApiError:
    return ApiError(503, "busy", _("The server is busy – try again later"))


def _too_large() -> ApiError:
    return ApiError(413, "label_too_large", _("The label is too large to render"))


def _too_long(text: str) -> bool:
    return len(text) > MAX_TEXT or text.count("\n") >= MAX_LINES  # lines: one more


def _check_pictures(label: Label) -> None:
    """Images, codes and pages take long to draw: at most MAX_PICTURES of them."""
    pictures = sum(isinstance(e, (ImageElement, CodeElement, PageElement)) for e in label.elements)
    if pictures > MAX_PICTURES:
        raise ApiError(
            413,
            "label_too_large",
            _("The label is too large: at most %d images and codes") % MAX_PICTURES,
        )


def _check_limits(label: Label, rows: list[dict[str, str]]) -> None:
    """Refuse what would take too long or too much memory to render: many
    elements, long texts, long cells of the columns the label uses."""
    contents = [
        e.text if isinstance(e, TextElement) else e.data
        for e in label.elements
        if isinstance(e, (TextElement, CodeElement))
    ]
    columns = used_columns(label) if rows else []
    cells = (row.get(name, "") for row in rows for name in columns)
    if (
        len(label.elements) > MAX_ELEMENTS
        or any(map(_too_long, contents))
        or any(map(_too_long, cells))
    ):
        raise ApiError(
            413,
            "label_too_large",
            _(
                "The label is too large: at most %(elements)d elements and %(chars)d characters in a text"
            )
            % {"elements": MAX_ELEMENTS, "chars": MAX_TEXT},
        )
    _check_pictures(label)  # after the general limits: those are told first


def _default_printer(usable: list[NamedPrinter]) -> NamedPrinter | None:
    """The default among the printers a token may use, else the only one."""
    defaults = [p for p in usable if p.default]
    if defaults:
        return defaults[0]
    return usable[0] if len(usable) == 1 else None


def _check_copies(prepared: Prepared, copies: int) -> None:
    named = prepared.named
    if named and copies > named.max_copies:
        raise bad_request(
            _("At most %d copies on this printer") % named.max_copies, "too_many_copies"
        )


def _host_given() -> ApiError:
    return bad_request(_("Printers are chosen by name: leave out host"), "host_given")


class CounterNotSaved(Exception):
    def __init__(self, name: str, error: Exception):
        super().__init__(str(error))
        self.name = name
        self.error = error

    def text(self) -> str:
        error = self.error
        reason = (
            save_error_text(error, self.name)
            if isinstance(error, OSError)
            else _("Cannot save %s") % self.name
        )
        return (
            reason + " – " + _("the labels were printed, but the next counter value was not saved")
        )


def _series_failure(error: SeriesError, units: str | None) -> tuple[str, Callable[[], str]]:
    def message() -> str:
        if error.preparing:
            text = _("The label cannot be prepared for printing.")
        else:
            text = printer_error_text(error.error, unit_for(units))
        return text + " " + series_stop_text(error.row, error.copies_printed)

    if error.preparing:
        return "prepare_failed", message
    code = error.error.code if isinstance(error.error, PrinterError) else "generic"
    return "printer_" + code, message


def _element(index: int, element) -> dict:
    """An element as the API shows it; `name` is what "set" fills in."""
    data: dict = {"index": index, "kind": element.kind}
    if isinstance(element, TextElement):
        data.update(name=element.name or None, text=element.text)
    elif isinstance(element, CodeElement):
        data.update(name=element.name or None, data=element.data, symbology=element.symbology)
    elif isinstance(element, ImageElement):
        data["file"] = element.name or None  # the image's file name, for display only
    return data
