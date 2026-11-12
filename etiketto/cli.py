"""The command line: print label files without opening a window.

    etiketto                         the application
    etiketto print FILE [options]    print a label file
    etiketto print FILE --data ROWS  one label for each row of a data file
    etiketto list-printers           printers found on the network
    etiketto serve [options]         print server: REST API, MCP (server/)
    etiketto mcp [options]           MCP on stdin/stdout, for AI clients
    etiketto --version, --help

Printing needs no display: rendering uses Pango and Cairo, not GTK, and
nothing here imports GTK. Messages are one translated line on stderr; the
exit code tells scripts what went wrong (EXIT_*).

Two prints of the same label file run one after the other: the file is
locked (flock) from reading it until the next counter value is saved. The
window does not lock; it reads the saved counter again before printing and
saving and goes on from the larger value (see Window.sync_counter), so a
print from the command line is not repeated by the window. Printing from
both at the very same moment can still give a number twice.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import errno
import locale
import os
import re
import signal
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO, NamedTuple

from etiketto import __version__
from etiketto.backends import backends, get_backend
from etiketto.backends.base import Backend, PrinterError, PrinterInfo, PrintOptions
from etiketto.backends.cab import METHODS  # the choices of the printer setting "method"
from etiketto.job import Job, PrepareFailed
from etiketto.merge import MergeError, read_book
from etiketto.model import (
    PITCH_TOLERANCE_MM,
    CodeElement,
    Label,
    LabelError,
    LockTimeout,
    TextElement,
    data_inside,
    locked,
    writable,
)
from etiketto.serial import (
    Series,
    SeriesError,
    SeriesResult,
    print_series,
    row_span,
    unknown_columns,
    unprintable_rows,
)
from etiketto.sheets import find as find_sheet
from etiketto.sheets.layout import SheetJob
from etiketto.ui.i18n import _
from etiketto.ui.settings import PrinterSettings, Settings
from etiketto.ui.texts import (
    data_error_text,
    label_error_text,
    other_pitch_text,
    printed_remark,
    printer_error_text,
    save_error_text,
    series_stop_text,
    warning_text,
)
from etiketto.units import unit_for
from etiketto.variables import BUILTIN, Values, label_names

COMMANDS = ("print", "list-printers", "serve", "mcp", "token", "--version", "--help", "-h")
DEFAULT_PORT = 8790

EXIT_OK = 0
EXIT_PRINTER = 1  # the printer cannot be reached or reports a problem; none found
EXIT_USAGE = 2  # wrong arguments (argparse uses 2 as well)
EXIT_FILE = 3  # the label file cannot be read, or the output not written
EXIT_LABEL = 4  # the label cannot be printed as it is (empty, unknown variable)
EXIT_COUNTER = 5  # the next counter value cannot be saved (checked before printing)
EXIT_INTERRUPTED = 130  # Ctrl+C, SIGTERM or SIGHUP (128 + SIGINT, as shells report it)


class CliError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


# --- arguments --------------------------------------------------------------


class Assignment(NamedTuple):
    name: str
    value: str
    literal: bool = False  # --set-literal: braces in the value are no variables


def _assignment(text: str, literal: bool = False) -> Assignment:
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(_("expected NAME=VALUE: %s") % text)
    return Assignment(name.strip(), value, literal)


def _literal_assignment(text: str) -> Assignment:
    return _assignment(text, literal=True)


def _number(lower: int, upper: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = lower - 1
        if not lower <= value <= upper:
            raise argparse.ArgumentTypeError(
                _("expected a whole number from %(min)d to %(max)d: %(value)s")
                % {"min": lower, "max": upper, "value": text}
            )
        return value

    return parse


_ROWS = re.compile(r"\s*(\d*)\s*(-?)\s*(\d*)\s*\Z")
ALL_ROWS = 10**9


def _rows(text: str) -> tuple[int, int]:
    """ "3-10", "5", "7-" (to the last), "-4" (from the first): rows counted
    from 1, both included."""
    match = _ROWS.match(text)
    first, dash, last = match.groups() if match else ("", "", "")
    if not (first or last) or (not dash and not first):  # also no match at all
        raise _rows_error(text)
    start = int(first) if first else 1
    end = _row_end(last, dash, start)
    if start < 1 or end < start:
        raise _rows_error(text)
    return start, end


def _row_end(last: str, dash: str, start: int) -> int:
    if last:
        return int(last)
    return ALL_ROWS if dash else start


def _rows_error(text: str) -> argparse.ArgumentTypeError:
    return argparse.ArgumentTypeError(_("expected rows such as 3-10, 5 or 7-: %s") % text)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etiketto",
        description=_("Design and print labels"),
        epilog=_("Without a command the application starts; label files given open in it."),
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    print_ = commands.add_parser(
        "print",
        help=_("print a label file without opening a window"),
        description=_("Print a label file without opening a window."),
    )
    print_.add_argument("file", metavar="FILE", type=Path, help=_("the label (.etiketto)"))
    ids = ", ".join(b.id for b in backends())
    print_.add_argument(
        "--printer", metavar="ID", help=_("printer model, by default the label's: %s") % ids
    )
    print_.add_argument(
        "--host", metavar="ADDR", help=_("printer address, by default the one set in the app")
    )
    print_.add_argument(
        "--copies", metavar="N", type=_number(1, 9999), default=1, help=_("number of copies")
    )
    print_.add_argument(
        "--set",
        metavar="NAME=VALUE",
        type=_assignment,
        action="append",
        default=[],
        dest="assignments",
        help=_(
            "text of the element NAME, or value of the variable {=NAME}; variables in"
            " a text are filled in; repeatable"
        ),
    )
    print_.add_argument(
        "--set-literal",
        metavar="NAME=VALUE",
        type=_literal_assignment,
        action="append",
        dest="assignments",
        help=_("like --set, but the value is used as it is: braces are no variables"),
    )
    print_.add_argument(
        "--counter-start",
        metavar="N",
        type=_number(0, 10**15 - 1),
        help=_("counter of the first label, instead of the one saved in the label"),
    )
    print_.add_argument(
        "--printer-defaults",
        action="store_true",
        help=_(
            "print with the printer model's defaults instead of the cut, colours, quality, "
            "extras (chain printing, high resolution, tape feed), printer settings and label "
            "sheet outlines last chosen in the app; the address and the safety margin stay"
        ),
    )
    print_.add_argument(
        "--print-method",
        choices=METHODS,
        help=_(
            "print method of a thermal printer that has the setting: printer (as set on the "
            "printer), transfer (with a ribbon) or direct (thermal direct, no ribbon); by "
            "default the one chosen in the app, also with --printer-defaults"
        ),
    )
    print_.add_argument(
        "--no-query",
        action="store_true",
        help=_("do not ask the printer before rendering (its model, tape and resolution)"),
    )
    print_.add_argument(
        "--no-save-counter",
        action="store_true",
        help=_(
            "do not save the next counter value in the label (a read-only file);"
            " the next print starts from the same number"
        ),
    )
    print_.add_argument(
        "--data",
        metavar="FILE",
        type=Path,
        help=_(
            "serial labels: one label for each row of this CSV, ODS or XLSX file;"
            " by default the data file of the label"
        ),
    )
    print_.add_argument(
        "--sheet", metavar="NAME", help=_("the sheet of the data file, by default the first")
    )
    print_.add_argument(
        "--rows",
        metavar="FROM-TO",
        type=_rows,
        help=_("only these rows of the data file, e.g. 3-10, 5 or 7- (counted from 1)"),
    )
    print_.add_argument(
        "--dry-run", action="store_true", help=_("check and render only, do not print")
    )
    print_.add_argument(
        "--output",
        metavar="PNG",
        help=_(
            "with --dry-run: save each label as PNG as the window exports it, at most 600 dpi"
            " (- for stdout: the first only)"
        ),
    )
    listing = commands.add_parser(
        "list-printers",
        help=_("list the printers found on the network"),
        description=_("List the printers found on the network: model, address, name."),
    )
    listing.add_argument("--printer", metavar="ID", help=_("only this printer model: %s") % ids)
    _server_parsers(commands)
    return parser


def _network(text: str):
    import ipaddress

    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise argparse.ArgumentTypeError(
            _("expected an address or network such as 172.16.0.0/12: %s") % text
        ) from None


def _server_options(command: argparse.ArgumentParser) -> None:
    """What `serve` and `mcp` share: where the labels are, which printers."""
    command.add_argument(
        "--labels-dir",
        metavar="DIR",
        type=Path,
        help=_("the folder with the label files the server may use"),
    )
    command.add_argument(
        "--allow-printer",
        metavar="ADDR",
        action="append",
        default=[],
        help=_("only print on this printer address (* matches anything); repeatable"),
    )
    command.add_argument(
        "--printers",
        metavar="FILE",
        type=Path,
        help=_("printers.toml: the printers clients may use, by name"),
    )
    command.add_argument(
        "--max-labels",
        metavar="N",
        type=_number(1, 10**7),
        default=10_000,
        help=_("most labels in one print job, copies times rows (default 10000)"),
    )
    command.add_argument(
        "--discover",
        action="store_true",
        help=_("search the network for printers (off by default)"),
    )


def _server_parsers(commands) -> None:
    serve = commands.add_parser(
        "serve",
        help=_("run a print server with a REST API"),
        description=_("Run a print server: a REST API and, with --mcp, MCP for AI clients."),
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help=_("address to listen on (default 127.0.0.1)")
    )
    serve.add_argument(
        "--port",
        type=_number(0, 65535),
        default=DEFAULT_PORT,
        help=_("port to listen on (default %d)") % DEFAULT_PORT,
    )
    serve.add_argument(
        "--token-file",
        metavar="FILE",
        type=Path,
        help=_("file with the access token; else the environment variable ETIKETTO_TOKEN"),
    )
    serve.add_argument(
        "--tokens",
        metavar="FILE",
        type=Path,
        help=_("tokens.toml: named access tokens with roles"),
    )
    serve.add_argument("--mcp", action="store_true", help=_("also serve MCP at /mcp"))
    serve.add_argument(
        "--max-body",
        metavar="MB",
        type=_number(1, 64),
        default=4,
        help=_("largest request body in MB (default 4)"),
    )
    serve.add_argument(
        "--trusted-proxy",
        metavar="CIDR",
        type=_network,
        action="append",
        default=[],
        help=_(
            "a reverse proxy whose X-Forwarded-For header names the client, e.g. "
            "172.16.0.0/12; repeatable"
        ),
    )
    serve.add_argument(
        "--allow-origin",
        metavar="URL",
        action="append",
        default=[],
        help=_("allow requests from web pages of this origin; repeatable"),
    )
    _server_options(serve)
    mcp = commands.add_parser(
        "mcp",
        help=_("serve MCP on standard input and output"),
        description=_(
            "Serve MCP on standard input and output, for AI clients such as Claude Desktop."
        ),
    )
    _server_options(mcp)
    _token_parser(commands)


def _token_parser(commands) -> None:
    token = commands.add_parser(
        "token",
        help=_("manage the access tokens of the print server"),
        description=_("Manage the access tokens of the print server."),
    )
    actions = token.add_subparsers(dest="action", required=True, metavar="ACTION")
    new = actions.add_parser(
        "new",
        help=_("make a token and add its hash to tokens.toml"),
        description=_("Make a token, add its hash to tokens.toml and show the token once."),
    )
    new.add_argument("name", metavar="NAME", help=_("name of the token, e.g. the device"))
    new.add_argument(
        "--role",
        choices=("preview", "print", "admin"),
        default="print",
        help=_("preview: look only; print: also print; admin: also all jobs"),
    )
    new.add_argument("--tokens", metavar="FILE", type=Path, help=_("tokens.toml to add it to"))
    new.add_argument(
        "--printer",
        metavar="NAME",
        action="append",
        default=[],
        help=_("only this printer of printers.toml; repeatable"),
    )
    new.add_argument(
        "--printers", metavar="FILE", type=Path, help=_("printers.toml, to check --printer")
    )
    new.add_argument("--expires", metavar="DATE", help=_("valid until, e.g. 2027-12-31"))
    new.add_argument("--description", metavar="TEXT", help=_("a note for people"))
    new.add_argument(
        "--scrypt", action="store_true", help=_("store the hash with scrypt instead of SHA-256")
    )
    new.add_argument(
        "--from-stdin",
        action="store_true",
        help=_(
            "take a token you chose from standard input (stored with scrypt) instead of making one"
        ),
    )


# --- print ------------------------------------------------------------------


@dataclass
class Overrides:
    """What --set changes: element texts directly, the rest here."""

    custom: dict[str, str] = field(default_factory=dict)
    now: datetime | None = None


def _backend(backend_id: str) -> Backend:
    try:
        return get_backend(backend_id)
    except KeyError:
        raise CliError(_("Unknown printer model: %s") % backend_id, EXIT_USAGE) from None


def load_label(path: Path) -> Label:
    try:
        return Label.load(path)
    except OSError:
        raise CliError(_("Cannot open %s") % _name(path), EXIT_FILE) from None
    except LabelError as e:
        raise CliError(label_error_text(e, _name(path)), EXIT_FILE) from None


def _set_element(label: Label, name: str, value: str) -> bool:
    found = False
    for element in label.elements:
        if isinstance(element, TextElement) and element.name == name:
            element.text = value
            found = True
        elif isinstance(element, CodeElement) and element.name == name:
            element.data = value
            found = True
    return found


def _set_date(overrides: Overrides, value: str) -> None:
    try:
        overrides.now = datetime.fromisoformat(value.strip())
    except ValueError:
        raise CliError(
            _("%s is not a date, expected e.g. 2026-12-31 or 2026-12-31T08:30") % value,
            EXIT_USAGE,
        ) from None


def apply_assignments(
    label: Label, assignments: list[Assignment], serial: bool = False
) -> Overrides:
    """--set NAME=VALUE: the text of elements with that name, else the
    variable {=NAME}; "date" sets the moment {=date} and {=time} start from.
    The element texts are set first: a variable used only in a new text
    (--set title="Lot {=lot}" --set lot=7) can be given too."""
    overrides = Overrides()
    rest = [a for a in assignments if not _set_element(label, a.name, _escaped(a, serial))]
    used = label_names(label)
    for name, value, _literal in rest:
        _set_variable(overrides, used, name, value)
    return overrides


def _escaped(assignment: Assignment, serial: bool) -> str:
    """The value of --set-literal with its braces doubled where they would
    be read: "{=" as it is, in a text (a variable's value is never filled
    in). Only a text with "{=" is expanded at all, or every text of a
    serial label (`serial`): other braces ('{"a":1}' in a QR code) stay as
    they are and must not be doubled."""
    value = assignment.value
    braces = "{" in value or "}" in value
    if assignment.literal and ("{=" in value or (serial and braces)):
        return value.replace("{", "{{").replace("}", "}}")
    return value


def _set_variable(overrides: Overrides, used: set[str], name: str, value: str) -> None:
    if name == "date":
        _set_date(overrides, value)
    elif name in BUILTIN:
        raise CliError(_("{=%s} is filled in when printing and cannot be set") % name, EXIT_USAGE)
    elif name in used:
        overrides.custom[name] = value  # a value is never filled in itself
    else:
        raise CliError(_("The label has no element or variable named %s") % name, EXIT_USAGE)


# what would print a label other than meant: a variable as text, or without
# an element or the background that could not be drawn (nobody looks at a
# preview here). The other warnings (an EAN-13 with a wrong check digit, no
# room for the content) are told and the label is printed, like the window.
REFUSED = frozenset(
    {
        "unknown_variable",
        "unclosed_variable",
        "date_out_of_range",
        "element_failed",
        "image_unreadable",
        "background_unreadable",
        "code128_long",  # wider than any label; refused for serial labels too
    }
)


def _check(job: Job) -> None:
    """Refuse what would print wrongly; other problems become warnings."""
    label = job.label
    if not label.elements and not label.background_image:
        raise CliError(_("The label is empty"), EXIT_LABEL)
    try:
        warnings = job.check()
    except PrinterError as e:  # a page too large for the printer's resolution
        raise CliError(printer_error_text(e, unit_for("mm")), EXIT_LABEL) from None
    for warning in warnings:
        if warning.key in REFUSED:
            raise CliError(warning_text(warning), EXIT_LABEL)
        _say(warning_text(warning))


def _say(message: str) -> None:
    """One line on stderr; without stderr (closed, None) the message is
    dropped, never written to stdout: the exit code tells."""
    stderr = sys.stderr
    if stderr is None:
        return
    with contextlib.suppress(OSError, ValueError):
        stderr.write("etiketto: " + message + "\n")
        stderr.flush()


def _name(path: Path) -> str:
    """The file's name for messages; "." and "/" have none of their own."""
    return path.name or str(path)


# --- standard output ----------------------------------------------------------


def _output_failed() -> CliError:
    """stdout is gone (a closed pipe) or full: say so, and let the rest of
    the output (and Python's flush at the exit) go to /dev/null."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    return CliError(_("Cannot write the output"), EXIT_FILE)


def _out(line: str) -> None:
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):  # gone, full, closed; None if never open
        raise _output_failed() from None


def _stdout_bytes() -> IO[bytes]:
    try:
        return sys.stdout.buffer
    except AttributeError:  # no stdout at all (started with ">&-")
        raise _output_failed() from None


def _output_paths(output: str, count: int) -> Iterator[str]:
    """One file per render: out.png, or out-1.png, out-2.png … for labels
    that differ from copy to copy."""
    if count == 1:
        yield output
        return
    path = Path(output)
    for n in range(1, count + 1):
        yield str(path.with_name("%s-%d%s" % (path.stem, n, path.suffix)))


def _check_output(output: str) -> None:
    if output != "-" and Path(output).suffix.lower() != ".png":
        raise CliError(_("--output writes PNG images: give a name ending in .png"), EXIT_USAGE)


def _write_png(image, dpi: float, target: str) -> None:
    """The image as PNG with its resolution (what the window's export writes)."""
    if target == "-":
        stdout = _stdout_bytes()
        try:
            image.save(stdout, format="PNG", dpi=(dpi, dpi))
            stdout.flush()
        except (OSError, ValueError):
            raise _output_failed() from None
        return
    try:
        image.save(target, format="PNG", dpi=(dpi, dpi))
    except OSError as e:
        raise CliError(save_error_text(e, _name(Path(target))), EXIT_FILE) from None


def _prepare_failed(error: PrepareFailed, unit) -> CliError:
    """The label cannot be rendered: a code that does not fit at the
    printer's resolution says so (CodeTooLarge), anything else in general."""
    cause = error.__cause__
    if isinstance(cause, PrinterError):
        return CliError(printer_error_text(cause, unit), EXIT_LABEL)
    return CliError(_("The label cannot be prepared for printing."), EXIT_LABEL)


def _dry_run(job: Job, output: str | None, unit) -> int:
    """Fill in the labels of the job one at a time and list their texts.
    The first is rendered as the printer would get it, to find what would
    fail; with --output each label is also saved as the window exports it
    (Job.export: rendered once, on the whole tape, with its resolution).
    "-" writes the first image to stdout and nothing else: stdout holds
    one PNG."""
    try:
        if output == "-":
            _copy, label = next(job.labels())
            job.image(label)
            _write_png(*job.export(label), "-")
            return EXIT_OK
        targets = _output_paths(output, job.renders) if output else None
        for copy, label in job.labels():
            if copy == 1:
                job.image(label)
            if targets:
                _write_png(*job.export(label), next(targets))
            _out("\t".join([str(copy)] + [_one_line(t) for t in _contents(label)]))
    except PrepareFailed as e:
        raise _prepare_failed(e, unit) from None
    return EXIT_OK


def _dry_run_series(series: Series, output: str | None, unit) -> int:
    """As _dry_run(), for each row: the listing starts with the row number,
    --output saves one image per label in turn (name-1.png, name-2.png …).
    A job is made for one row at a time, never for all of them at once."""
    backend = series_backend(series)
    values = series.first_values()

    def job(position: int, index: int) -> Job:
        start = replace(values, counter=values.counter + values.step * position * values.copies)
        return Job(
            backend, series.label, series.options, series.safe_mm, start, series.rows[index]
        )

    try:
        if output == "-":
            first = job(0, series.indexes[0])
            label = next(first.labels())[1]
            first.image(label)
            _write_png(*first.export(label), "-")
            return EXIT_OK
        renders = job(0, series.indexes[0]).renders * len(series.indexes)  # the same for each row
        targets = _output_paths(output, renders) if output else None
        for position, index in enumerate(series.indexes):
            row_job = job(position, index)
            for copy, label in row_job.labels():
                if copy == 1 and position == 0:
                    row_job.image(label)
                if targets:
                    _write_png(*row_job.export(label), next(targets))
                fields = [str(index + 1), str(copy)] + [_one_line(t) for t in _contents(label)]
                _out("\t".join(fields))
    except PrepareFailed as e:
        raise _prepare_failed(e, unit) from None
    return EXIT_OK


def _one_line(text: str) -> str:
    """A field of the listing: no tab or line break of its own."""
    return re.sub(r"[\t\n\r]", " ", text)


def _contents(label: Label) -> list[str]:
    """Texts and code contents, in the order of the elements."""
    return [
        e.text if isinstance(e, TextElement) else e.data
        for e in label.elements
        if isinstance(e, (TextElement, CodeElement))
    ]


def _host(args, backend: Backend, settings: PrinterSettings) -> str:
    """--host, the address set in the app, or the only printer on the network."""
    host = args.host or settings.host
    if host:
        return host
    found = backend.discover()
    if len(found) == 1:
        return found[0].host
    if not found:
        raise CliError(_("No printer found – give its address with --host"), EXIT_PRINTER)
    raise CliError(
        _("Several printers found – choose one with --host (etiketto list-printers)"), EXIT_PRINTER
    )


def _save_counter(path: Path, counter: int | None, printed: int) -> int:
    if counter is None or not printed:
        return EXIT_OK
    try:
        Label.store_counter(path, counter)
    except (OSError, ValueError) as e:
        name = _name(path)
        reason = save_error_text(e, name) if isinstance(e, OSError) else _("Cannot save %s") % name
        _say(
            reason + " – " + _("the labels were printed, but the next counter value was not saved")
        )
        return EXIT_COUNTER
    return EXIT_OK


def _check_writable(path: Path, job: Job) -> None:
    """A label with a counter: refuse to print if the next counter value
    could not be saved afterwards (a read-only file or folder)."""
    if job.next_counter() is None or writable(path):
        return
    error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    raise CliError(
        save_error_text(error, _name(path))
        + " – "
        + _("nothing was printed: the next counter value could not be saved"),
        EXIT_COUNTER,
    )


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """SIGTERM (kill, systemd stop) and SIGHUP (the terminal closed) end
    the job like Ctrl+C does: as an exception, so the counter of the labels
    printed so far is saved."""

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        previous = {s: signal.signal(s, interrupt) for s in _ENDING}
    except ValueError:  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


_ENDING = (signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def _uninterrupted() -> Iterator[None]:
    """Ctrl+C, SIGTERM and SIGHUP wait until the counter is saved: noted while it is
    written, then raised. (Not a signal mask: that holds for one thread
    only, and a signal to the process may reach another one.)"""
    caught: list[int] = []

    def remember(signum, _frame):
        caught.append(signum)

    try:
        previous = {s: signal.signal(s, remember) for s in (signal.SIGINT, *_ENDING)}
    except ValueError:  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if caught:
            raise KeyboardInterrupt


def _send(args, job: Job, host: str, unit) -> int:
    """Print, and save the next counter value whatever happens: also after
    a printer error or Ctrl+C, the labels already printed used their numbers."""
    with _interruptible():
        try:
            result = job.run(host)
        except KeyboardInterrupt:
            raise CliError(_("Interrupted"), EXIT_INTERRUPTED) from None
        except PrepareFailed as e:
            raise _prepare_failed(e, unit) from None
        except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
            raise CliError(printer_error_text(e, unit), EXIT_PRINTER) from None
        finally:
            with _uninterrupted():
                saved = (
                    EXIT_OK
                    if args.no_save_counter
                    else _save_counter(args.file, job.next_counter(), job.printed)
                )
    remark = printed_remark(job.options, result)
    if remark:  # queued, or printed in another mode: say so
        _say(remark)
    return saved


def _send_series(args, series: Series, host: str, unit) -> int:
    """Print one label per row, and save the next counter value whatever
    happens, as _send() does."""
    outcome = SeriesResult()
    with _interruptible():
        try:
            # the codes were checked before the file was locked for printing
            print_series(series_backend(series), host, series, outcome=outcome, check=False)
        except KeyboardInterrupt:
            raise CliError(_("Interrupted"), EXIT_INTERRUPTED) from None
        except SeriesError as e:
            if e.preparing and isinstance(e.error, PrinterError):  # a code too large
                message, code = printer_error_text(e.error, unit), EXIT_LABEL
            elif e.preparing:
                message, code = _("The label cannot be prepared for printing."), EXIT_LABEL
            else:
                message, code = printer_error_text(e.error, unit), EXIT_PRINTER
            where = series_stop_text(e.row, e.copies_printed, e.position)
            raise CliError(message + " " + where, code) from None
        finally:
            with _uninterrupted():
                saved = (
                    EXIT_OK
                    if args.no_save_counter
                    else _save_counter(args.file, outcome.counter, outcome.labels)
                )
    remark = printed_remark(series.options, outcome.result)
    if remark:
        _say(remark)
    return saved


def series_backend(series: Series) -> Backend:
    return _backend(series.label.printer)


def _options(backend: Backend, printer: PrinterSettings, copies: int, gap_mm: float):
    """Cut, mode, quality, colours and the printer's extras (chain printing,
    high resolution, tape feed) as last chosen in the app, if the printer
    has them (PrinterSettings.options, as for the print dialog)."""
    return printer.options(None, copies, gap_mm)


def _job(args, settings: Settings | None, now: datetime | None):
    """The label as given, filled in: (job, printer settings, settings)."""
    label = load_label(args.file)
    if args.printer and args.printer != label.printer:
        _backend(args.printer)  # a clear message for unknown ids
        label.switch_printer(args.printer)
    backend = _backend(label.printer)
    _check_flags(args, backend)
    settings = settings or Settings.load()
    printer = settings.printer(backend)
    if args.counter_start is not None:
        label.counter_next = args.counter_start
    overrides = apply_assignments(label, args.assignments, bool(args.data or label.data_file))
    choices = _choices(args, settings, backend)
    options = _flagged(args, _options(backend, choices, args.copies, label.gap_mm))
    values = Values.for_label(
        label, overrides.now or now or datetime.now(), args.copies, custom=overrides.custom
    )
    rows, indexes = _data(args, label)
    columns = rows[indexes[0]] if rows else None
    sheet = _sheet_job(label, backend, choices, args.copies * max(1, len(indexes)))
    job = Job(backend, label, options, printer.safe_mm, values, columns, sheet)
    series = None
    if rows:
        series = Series(label, rows, indexes, options, printer.safe_mm, values, sheet)
    return job, printer, settings, series


def _choices(args, settings: Settings, backend: Backend) -> PrinterSettings:
    """Whose print choices (cut, mode, quality, the extras, the printer
    settings, a label sheet's outlines) a job takes: the ones last made in
    the app, or with --printer-defaults the printer model's defaults."""
    return Settings({}).printer(backend) if args.printer_defaults else settings.printer(backend)


def _check_flags(args, backend: Backend) -> None:
    """--print-method only for a printer that has the setting."""
    if args.print_method and not any(s.key == "method" for s in backend.settings(None)):
        raise CliError(
            _("--print-method: this printer model has no print method setting: %s") % backend.id,
            EXIT_USAGE,
        )


def _flagged(args, options: PrintOptions) -> PrintOptions:
    """The printer settings given on the command line on top of the
    choices (--print-method)."""
    if not args.print_method:
        return options
    return replace(options, settings={**options.settings, "method": args.print_method})


def _checked(job: Job, series: Series | None) -> None:
    """The job's problems, after the printer was asked: the warnings are
    those of the render for that printer (its resolution, tape)."""
    _check(job)
    if series:
        _check_rows(series)


def _sheet_job(label: Label, backend: Backend, choices: PrinterSettings, count: int):
    """The label sheet the label is printed on, as in the window: each copy
    (or each copy of each row) a position, from the chosen positions on,
    with the outlines as `choices` say (_choices()); None for a label of
    its own page or a sheet this version does not know."""
    choice = label.sheet
    sheet = find_sheet(choice.id) if choice and backend.capabilities.pages else None
    if sheet is None or choice is None:
        return None
    outlines = choices.get("outlines", False) is True
    return SheetJob(sheet, count, tuple(choice.positions), choice.rotated, outlines)


def _data(args, label: Label) -> tuple[list[dict[str, str]], list[int]]:
    """The rows of a serial label: --data, else the label's data file;
    [] for a label without one. A data file the label names is read only
    if it lies in the label's folder (as the window does without asking):
    a label from elsewhere could name any file."""
    path = _data_path(args, label)
    if path is None:
        if args.sheet or args.rows:
            raise CliError(_("--sheet and --rows need a data file (--data)"), EXIT_USAGE)
        return [], []
    own = label.data_file and Path(label.data_file).resolve() == path.resolve()
    # the label's sheet goes with its own data file, also given with --data
    sheet = args.sheet or (label.data_sheet if own else "")
    rows = _read_rows(path, sheet, bool(args.sheet))
    first, last = args.rows or (1, ALL_ROWS)
    indexes = list(row_span(first, last, len(rows)))
    if not indexes:
        raise CliError(_("No rows to print"), EXIT_USAGE)
    return rows, indexes


def _data_path(args, label: Label) -> Path | None:
    """--data, else the label's own data file if it lies in its folder."""
    if args.data is not None or not label.data_file:
        return args.data
    if not data_inside(label.data_file, args.file):
        raise CliError(
            _("The label uses a data file outside its folder: give it with --data"),
            EXIT_USAGE,
        )
    return Path(label.data_file)


def _read_rows(path: Path, sheet: str, asked: bool) -> list[dict[str, str]]:
    """The rows of the sheet; `asked`: the sheet was given with --sheet."""
    try:
        book = read_book(path, sheet)
    except (OSError, MergeError) as e:
        raise CliError(data_error_text(e, _name(path)), EXIT_FILE) from None
    # never another sheet than the one asked for; a CSV file has no sheets:
    # a sheet saved with the label (the file was a spreadsheet once) is
    # ignored, one given with --sheet is refused
    if sheet and not book.sheets and not asked:
        sheet = ""
    if sheet and book.sheet != sheet:
        raise CliError(data_error_text(MergeError("no_sheet", sheet), _name(path)), EXIT_USAGE)
    return book.table.rows


def _check_rows(series: Series) -> None:
    """Placeholders without a column and codes that cannot be drawn would
    print wrongly: refused, as variables are."""
    if unknown := unknown_columns(series.label, list(series.rows[0])):
        names = ", ".join("{%s}" % name for name in unknown)
        raise CliError(_("No such column: %s") % names, EXIT_LABEL)
    if bad := unprintable_rows(series.label, series.rows, series.indexes, series.values):
        shown = ", ".join(str(row) for row in bad[:20]) + (", …" if len(bad) > 20 else "")
        raise CliError(
            _(
                "The barcode or QR code of these rows cannot be made from their values, for "
                "example an EAN-13 with letters or an empty cell. Correct the data file and read "
                "it again, or print without these rows."
            )
            + " "
            + _("Rows: %s") % shown,
            EXIT_LABEL,
        )


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    def waiting():
        _say(_("Waiting for another print of %s to finish…") % _name(path))

    try:
        with locked(path, waiting):
            yield
    except LockTimeout:
        raise CliError(
            _("%s is still locked by another print – try again later") % _name(path), EXIT_FILE
        ) from None


def print_command(args, settings: Settings | None = None, now: datetime | None = None) -> int:
    if args.output and not args.dry_run:
        raise CliError(_("--output needs --dry-run"), EXIT_USAGE)
    if args.output:
        _check_output(args.output)
    if args.dry_run:  # writes nothing back: no lock
        job, printer, settings, series = _job(args, settings, now)
        # rendered as the print would be, if the printer is known; a dry run
        # waits for it only briefly (DRY_RUN_WAIT): it prints nothing
        host = args.host or printer.host or ""
        series = _for_the_printer(args, job, series, host, settings, DRY_RUN_WAIT, 0.0)
        _checked(job, series)
        unit = unit_for(settings.units)
        if series:
            return _dry_run_series(series, args.output, unit)
        return _dry_run(job, args.output, unit)
    if args.no_save_counter:  # nothing written back: no lock, no write check
        job, printer, settings, series = _job(args, settings, now)
        if job.next_counter() is not None:
            _say(_("The next counter value is not saved: the next print repeats the numbers"))
        return _print(args, job, series, _host(args, job.backend, printer), settings)
    with _locked(args.file):  # from reading the counter until the next one is saved
        job, printer, settings, series = _job(args, settings, now)
        _check_writable(args.file, job)
        host = _host(args, job.backend, printer)
        return _print(args, job, series, host, settings)


def _print(args, job: Job, series: Series | None, host: str, settings: Settings) -> int:
    unit = unit_for(settings.units)
    series = _for_the_printer(args, job, series, host, settings)
    _checked(job, series)
    return _send_series(args, series, host, unit) if series else _send(args, job, host, unit)


def _for_the_printer(
    args,
    job: Job,
    series: Series | None,
    host: str,
    settings: Settings,
    wait: float | None = None,
    finish: float | None = None,
) -> Series | None:
    """The job's options for what the printer reports (ask_printer(), unless
    --no-query): rendered for its model, tape and resolution, with the
    extras it has. The series, if any, with the same options."""
    if args.no_query or not host:
        return series
    info = ask_printer(
        job.backend,
        host,
        QUERY_WAIT if wait is None else wait,
        QUERY_FINISH if finish is None else finish,
    )
    if info is None:
        return series
    _check_pitch(job, info, settings)
    options = _flagged(
        args,
        _choices(args, settings, job.backend).options(
            info, job.options.copies, job.options.gap_mm, host=host
        ),
    )
    job.options = options
    return replace(series, options=options) if series else None


def _check_pitch(job: Job, info: PrinterInfo, settings: Settings) -> None:
    """Die-cut labels: a roll whose measured label distance is not the
    label's gets a hint, as the window warns; printed anyway."""
    caps, label = job.backend.capabilities, job.label
    if caps.continuous or caps.pages or not info.pitch_mm:
        return
    if abs(info.pitch_mm - label.pitch_mm()) > PITCH_TOLERANCE_MM:
        text = other_pitch_text(label, info.pitch_mm, unit_for(settings.units))
        _say(text.replace("\u2068", "").replace("\u2069", ""))  # no bidi marks


QUERY_WAIT = 5.0  # seconds the command line waits for the printer's answer
DRY_RUN_WAIT = 2.0  # a dry run prints nothing: it waits less
# a question still on its way after QUERY_WAIT is given this long to end
# before printing (a USB printer takes one session at a time)
QUERY_FINISH = 10.0


def ask_printer(
    backend: Backend, host: str, wait: float = QUERY_WAIT, finish: float = QUERY_FINISH
) -> PrinterInfo | None:
    """What the printer reports, for backends whose render depends on it
    (Backend.query_before_render); None if they do not, or the printer does
    not answer within `wait` seconds and the extra `finish` seconds a
    question still running gets (the user is told; an answer then is
    used); after that it is left to end by itself (the backend's timeouts
    end it; the print's own session waits for a busy USB printer). What it
    stores in the backend later (a raw printer's resolution) counts for
    the print's render. With finish 0 (a dry run) it asks a copy of the
    backend, so a late answer changes nothing."""
    if not backend.query_before_render:
        return None
    answer: list[PrinterInfo] = []
    # a question that may be left behind (finish 0: a dry run) asks through
    # a copy of the backend: what it stores when it answers late cannot
    # change the resolution between the check and the images
    asking = backend if finish > 0 else _isolated(backend)

    def query() -> None:
        with contextlib.suppress(Exception):
            answer.append(asking.query(host))

    thread = threading.Thread(target=query, name="etiketto-query", daemon=True)
    thread.start()
    thread.join(wait)
    if thread.is_alive() and finish > 0:
        _say(_("Waiting for the printer's answer…"))
        thread.join(finish)
    return answer[0] if answer else None  # also one that came in the extra time


def _isolated(backend: Backend) -> Backend:
    """A copy of the backend whose caches (the resolutions a raw printer
    reported, the model last asked) are its own."""
    twin = copy.copy(backend)
    for name, value in vars(backend).items():
        if isinstance(value, (dict, list, set)):
            setattr(twin, name, copy.copy(value))
    return twin


# --- list-printers ----------------------------------------------------------


def list_command(args) -> int:
    chosen = [_backend(args.printer)] if args.printer else backends()
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        results = list(pool.map(lambda b: (b, b.discover()), chosen))
    lines = [
        "\t".join(_one_line(field) for field in (backend.id, printer.host, printer.name))
        for backend, found in results
        for printer in found
    ]
    if not lines:
        _say(_("No printer found"))
        return EXIT_PRINTER
    _out("\n".join(lines))
    return EXIT_OK


# --- entry point ------------------------------------------------------------


def run(argv: list[str]) -> int:
    with contextlib.suppress(locale.Error):  # {=date} as the system language writes it
        locale.setlocale(locale.LC_ALL, "")
    try:
        args = parser().parse_args(argv)
    except SystemExit as e:  # --help, --version, wrong arguments
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    try:
        if args.command == "print":
            return print_command(args)
        if args.command in ("serve", "mcp", "token"):
            from etiketto.server import main as server

            run = {"serve": server.serve, "mcp": server.mcp, "token": server.token_new}
            return run[args.command](args)
        return list_command(args)
    except CliError as e:
        _say(str(e))
        return e.code
    except KeyboardInterrupt:  # Ctrl+C before anything was printed
        _say(_("Interrupted"))
        return EXIT_INTERRUPTED


def _gui() -> int:  # pragma: no cover - starts the application
    from etiketto.app import main as app_main

    return app_main()


def main(argv: list[str] | None = None) -> int:
    """The `etiketto` command: a command line command, else the application."""
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return run(argv)
    return _gui()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
