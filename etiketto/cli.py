"""The command line: print label files without opening a window.

    etiketto                         the application
    etiketto print FILE [options]    print a label file
    etiketto list-printers           printers found on the network
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
import errno
import locale
import os
import re
import signal
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, NamedTuple

from etiketto import __version__
from etiketto.backends import backends, get_backend
from etiketto.backends.base import Backend, PrintOptions
from etiketto.job import Job, PrepareFailed
from etiketto.model import CodeElement, Label, LabelError, TextElement, locked, writable
from etiketto.ui.i18n import _
from etiketto.ui.settings import PrinterSettings, Settings
from etiketto.ui.texts import (
    label_error_text,
    printed_text,
    printer_error_text,
    save_error_text,
    warning_text,
)
from etiketto.units import unit_for
from etiketto.variables import BUILTIN, Values, label_names

COMMANDS = ("print", "list-printers", "--version", "--help", "-h")

EXIT_OK = 0
EXIT_PRINTER = 1  # the printer cannot be reached or reports a problem; none found
EXIT_USAGE = 2  # wrong arguments (argparse uses 2 as well)
EXIT_FILE = 3  # the label file cannot be read, or the output not written
EXIT_LABEL = 4  # the label cannot be printed as it is (empty, unknown variable)
EXIT_COUNTER = 5  # the next counter value cannot be saved (checked before printing)
EXIT_INTERRUPTED = 130  # Ctrl+C or SIGTERM (128 + SIGINT, as shells report it)


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
        "--dry-run", action="store_true", help=_("check and render only, do not print")
    )
    print_.add_argument(
        "--output",
        metavar="PNG",
        help=_(
            "with --dry-run: save the images as the printer would get them, as PNG"
            " (- for stdout: the first only)"
        ),
    )
    listing = commands.add_parser(
        "list-printers",
        help=_("list the printers found on the network"),
        description=_("List the printers found on the network: model, address, name."),
    )
    listing.add_argument("--printer", metavar="ID", help=_("only this printer model: %s") % ids)
    return parser


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


def apply_assignments(label: Label, assignments: list[Assignment]) -> Overrides:
    """--set NAME=VALUE: the text of elements with that name, else the
    variable {=NAME}; "date" sets the moment {=date} and {=time} start from.
    The element texts are set first: a variable used only in a new text
    (--set title="Lot {=lot}" --set lot=7) can be given too."""
    overrides = Overrides()
    rest = []
    for assignment in assignments:
        name, value, literal = assignment
        if literal:  # "{" and "}" as they are (in a text, not in a variable's value)
            value = value.replace("{", "{{").replace("}", "}}")
        if not _set_element(label, name, value):
            rest.append(assignment)
    used = label_names(label)
    for name, value, _literal in rest:
        if name == "date":
            _set_date(overrides, value)
        elif name in BUILTIN:
            raise CliError(
                _("{=%s} is filled in when printing and cannot be set") % name, EXIT_USAGE
            )
        elif name in used:
            overrides.custom[name] = value  # a value is never filled in itself
        else:
            raise CliError(_("The label has no element or variable named %s") % name, EXIT_USAGE)
    return overrides


def _check(job: Job) -> None:
    """Refuse what would print wrongly; other problems become warnings."""
    label = job.label
    if not label.elements and not label.background_image:
        raise CliError(_("The label is empty"), EXIT_LABEL)
    for warning in job.check():
        if warning.key in ("unknown_variable", "unclosed_variable"):
            raise CliError(warning_text(warning), EXIT_LABEL)
        _say(warning_text(warning))


def _say(message: str) -> None:
    with contextlib.suppress(OSError):  # no stderr either: the exit code tells
        print("etiketto: " + message, file=sys.stderr)


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
    except OSError:
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


def _write_png(image, target: str, stdout: IO[bytes]) -> None:
    if target == "-":
        try:
            image.save(stdout, format="PNG")
            stdout.flush()
        except OSError:
            raise _output_failed() from None
        return
    try:
        image.save(target, format="PNG")
    except OSError as e:
        raise CliError(save_error_text(e, _name(Path(target))), EXIT_FILE) from None


def _dry_run(job: Job, output: str | None) -> int:
    """Fill in the labels of the job one at a time and list their texts.
    The first is rendered as the printer would get it, to find what would
    fail; the others only with --output, which saves each image. "-"
    writes the first image to stdout and nothing else: stdout holds one PNG."""
    try:
        if output == "-":
            _copy, label = next(job.labels())
            _write_png(job.image(label), "-", sys.stdout.buffer)
            return EXIT_OK
        targets = _output_paths(output, job.renders) if output else None
        for copy, label in job.labels():
            if targets:
                _write_png(job.image(label), next(targets), sys.stdout.buffer)
            elif copy == 1:
                job.image(label)
            _out("\t".join([str(copy)] + [_one_line(t) for t in _contents(label)]))
    except PrepareFailed:
        raise CliError(_("The label cannot be prepared for printing."), EXIT_LABEL) from None
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


def _save_counter(path: Path, job: Job) -> int:
    counter = job.next_counter()
    if counter is None or not job.printed:
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
    """SIGTERM (kill, systemd stop) ends the job like Ctrl+C does: as an
    exception, so the counter of the labels printed so far is saved."""

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def _uninterrupted() -> Iterator[None]:
    """Ctrl+C and SIGTERM wait until the counter is saved: noted while it is
    written, then raised. (Not a signal mask: that holds for one thread
    only, and a signal to the process may reach another one.)"""
    caught: list[int] = []

    def remember(signum, _frame):
        caught.append(signum)

    try:
        previous = {s: signal.signal(s, remember) for s in (signal.SIGINT, signal.SIGTERM)}
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
        except PrepareFailed:
            raise CliError(_("The label cannot be prepared for printing."), EXIT_LABEL) from None
        except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
            raise CliError(printer_error_text(e, unit), EXIT_PRINTER) from None
        finally:
            with _uninterrupted():
                saved = _save_counter(args.file, job)
    text = printed_text(job.options, result)
    if text != _("Printed"):  # queued, or printed in another mode: say so
        _say(text)
    return saved


def _options(backend: Backend, printer: PrinterSettings, copies: int, gap_mm: float):
    """Cut, mode and colours as last chosen in the app, if the printer has them."""
    cut_modes = backend.capabilities.cut_modes
    modes = [m.id for m in backend.modes(None)]
    cut, mode = printer.get("cut", "full"), printer.get("mode")
    return PrintOptions(
        copies=copies,
        cut=cut if cut in cut_modes else (cut_modes or ("",))[0],
        mode=mode if mode in modes else modes[0],
        color=printer.color_adjust,
        data_format=printer.data_format,
        gap_mm=gap_mm,
    )


def _job(args, settings: Settings | None, now: datetime | None):
    """The label as given, filled in: (job, printer settings, settings)."""
    label = load_label(args.file)
    if args.printer and args.printer != label.printer:
        _backend(args.printer)  # a clear message for unknown ids
        label.switch_printer(args.printer)
    backend = _backend(label.printer)
    settings = settings or Settings.load()
    printer = settings.printer(backend)
    if args.counter_start is not None:
        label.counter_next = args.counter_start
    overrides = apply_assignments(label, args.assignments)
    options = _options(backend, printer, args.copies, label.gap_mm)
    values = Values.for_label(
        label, overrides.now or now or datetime.now(), args.copies, custom=overrides.custom
    )
    job = Job(backend, label, options, printer.safe_mm, values)
    _check(job)
    return job, printer, settings


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    def waiting():
        _say(_("Waiting for another print of %s to finish…") % _name(path))

    with locked(path, waiting):
        yield


def print_command(args, settings: Settings | None = None, now: datetime | None = None) -> int:
    if args.output and not args.dry_run:
        raise CliError(_("--output needs --dry-run"), EXIT_USAGE)
    if args.output:
        _check_output(args.output)
    if args.dry_run:  # writes nothing back: no lock
        job, _printer, _settings = _job(args, settings, now)
        return _dry_run(job, args.output)
    with _locked(args.file):  # from reading the counter until the next one is saved
        job, printer, settings = _job(args, settings, now)
        _check_writable(args.file, job)
        host = _host(args, job.backend, printer)
        return _send(args, job, host, unit_for(settings.units))


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
