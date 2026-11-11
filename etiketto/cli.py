"""The command line: print label files without opening a window.

    etiketto                         the application
    etiketto print FILE [options]    print a label file
    etiketto list-printers           printers found on the network
    etiketto --version

Printing needs no display: rendering uses Pango and Cairo, not GTK, and
nothing here imports GTK. Messages are one translated line on stderr; the
exit code tells scripts what went wrong (EXIT_*).
"""

from __future__ import annotations

import argparse
import contextlib
import locale
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

from etiketto import __version__
from etiketto.backends import backends, get_backend
from etiketto.backends.base import Backend, PrintOptions
from etiketto.job import Job, PrepareFailed
from etiketto.model import CodeElement, Label, LabelError, TextElement
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

COMMANDS = ("print", "list-printers", "--version")

EXIT_OK = 0
EXIT_PRINTER = 1  # the printer cannot be reached or reports a problem; none found
EXIT_USAGE = 2  # wrong arguments (argparse uses 2 as well)
EXIT_FILE = 3  # the label file cannot be read, or the output not written
EXIT_LABEL = 4  # the label cannot be printed as it is (empty, unknown variable)
EXIT_COUNTER = 5  # printed, but the next counter value could not be saved


class CliError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


# --- arguments --------------------------------------------------------------


def _assignment(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(_("expected NAME=VALUE: %s") % text)
    return name.strip(), value


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
    parser = argparse.ArgumentParser(prog="etiketto", description=_("Design and print labels"))
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
        help=_("text of the element NAME, or value of the variable {=NAME}; repeatable"),
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
        help=_("with --dry-run: save the image as the printer would get it (- for stdout)"),
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
        raise CliError(_("Cannot open %s") % path.name, EXIT_FILE) from None
    except LabelError as e:
        raise CliError(label_error_text(e, path.name), EXIT_FILE) from None


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


def apply_assignments(label: Label, assignments: list[tuple[str, str]]) -> Overrides:
    """--set NAME=VALUE: the text of elements with that name, else the
    variable {=NAME}; "date" sets the moment {=date} and {=time} start from."""
    overrides = Overrides()
    used = label_names(label)
    for name, value in assignments:
        if _set_element(label, name, value):
            continue
        if name == "date":
            _set_date(overrides, value)
        elif name in BUILTIN:
            raise CliError(
                _("{=%s} is filled in when printing and cannot be set") % name, EXIT_USAGE
            )
        elif name in used:
            overrides.custom[name] = value
        else:
            raise CliError(_("The label has no element or variable named %s") % name, EXIT_USAGE)
    return overrides


def _check(job: Job) -> None:
    """Refuse what would print wrongly; other problems become warnings."""
    label = job.label
    if not label.elements and not label.background_image:
        raise CliError(_("The label is empty"), EXIT_LABEL)
    for warning in job.check():
        if warning.key == "unknown_variable":
            raise CliError(warning_text(warning), EXIT_LABEL)
        _say(warning_text(warning))


def _say(message: str) -> None:
    print("etiketto: " + message, file=sys.stderr)


def _output_paths(output: str, count: int) -> list[str]:
    """One file per render: out.png, or out-1.png, out-2.png … for labels
    that differ from copy to copy; "-" (stdout) takes the first only."""
    if output == "-" or count == 1:
        return [output]
    path = Path(output)
    return [
        str(path.with_name("%s-%d%s" % (path.stem, n, path.suffix))) for n in range(1, count + 1)
    ]


def _write_png(image, target: str, stdout: IO[bytes]) -> None:
    if target == "-":
        image.save(stdout, format="PNG")
        stdout.flush()
        return
    try:
        image.save(target, format="PNG")
    except OSError as e:
        raise CliError(save_error_text(e, Path(target).name), EXIT_FILE) from None


def _dry_run(job: Job, output: str | None) -> int:
    """Render every label of the job; list its texts, or save the images."""
    try:
        images = list(job.images())
    except PrepareFailed:
        raise CliError(_("The label cannot be prepared for printing."), EXIT_LABEL) from None
    if output:
        stdout = sys.stdout.buffer
        for (_copy, image), target in zip(
            images, _output_paths(output, len(images)), strict=False
        ):
            _write_png(image, target, stdout)
    if output != "-":  # stdout is free: what each label says
        for copy, label in job.labels():
            print("\t".join([str(copy)] + [t.replace("\n", " ") for t in _contents(label)]))
    return EXIT_OK


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
        name = path.name
        reason = save_error_text(e, name) if isinstance(e, OSError) else _("Cannot save %s") % name
        _say(
            reason + " – " + _("the labels were printed, but the next counter value was not saved")
        )
        return EXIT_COUNTER
    return EXIT_OK


def _send(args, job: Job, host: str, unit) -> int:
    try:
        result = job.run(host)
    except PrepareFailed:
        _save_counter(args.file, job)
        raise CliError(_("The label cannot be prepared for printing."), EXIT_LABEL) from None
    except Exception as e:  # noqa: BLE001 - the backend's errors, like in the window
        _save_counter(args.file, job)
        raise CliError(printer_error_text(e, unit), EXIT_PRINTER) from None
    text = printed_text(job.options, result)
    if text != _("Printed"):  # queued, or printed in another mode: say so
        _say(text)
    return _save_counter(args.file, job)


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


def print_command(args, settings: Settings | None = None, now: datetime | None = None) -> int:
    label = load_label(args.file)
    if args.printer and args.printer != label.printer:
        _backend(args.printer)  # a clear message for unknown ids
        label.switch_printer(args.printer)
    backend = _backend(label.printer)
    settings = settings or Settings.load()
    printer = settings.printer(backend)
    if args.output and not args.dry_run:
        raise CliError(_("--output needs --dry-run"), EXIT_USAGE)
    if args.counter_start is not None:
        label.counter_next = args.counter_start
    overrides = apply_assignments(label, args.assignments)
    options = _options(backend, printer, args.copies, label.gap_mm)
    values = Values.for_label(
        label, overrides.now or now or datetime.now(), args.copies, custom=overrides.custom
    )
    job = Job(backend, label, options, printer.safe_mm, values)
    _check(job)
    if args.dry_run:
        return _dry_run(job, args.output)
    return _send(args, job, _host(args, backend, printer), unit_for(settings.units))


# --- list-printers ----------------------------------------------------------


def list_command(args) -> int:
    chosen = [_backend(args.printer)] if args.printer else backends()
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        results = list(pool.map(lambda b: (b, b.discover()), chosen))
    lines = [
        "%s\t%s\t%s" % (backend.id, printer.host, printer.name)
        for backend, found in results
        for printer in found
    ]
    if not lines:
        _say(_("No printer found"))
        return EXIT_PRINTER
    print("\n".join(lines))
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
