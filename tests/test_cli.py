"""The command line: arguments, exit codes, messages, a dry run that renders
without a display, printing on a fake printer and the counter in the file."""

from __future__ import annotations

import io
import json
import subprocess  # nosec B404 - runs this checkout's CLI without a display
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_backends import FakeMono
from PIL import Image

from etiketto import __version__, cli
from etiketto.backends import register, unregister
from etiketto.backends.base import (
    FoundPrinter,
    PaperJam,
    PrinterInfo,
    PrinterTimeout,
    PrintOptions,
    PrintResult,
)
from etiketto.model import CodeElement, Label, TextElement
from etiketto.ui import settings as config
from etiketto.ui.settings import Settings

NOW = datetime(2026, 3, 1, 9, 30)


class Recording(FakeMono):
    def __init__(self):
        super().__init__()
        self.prepared: list[Label] = []
        self.result: PrintResult | None = None

    def prepare(self, label, options, safe_mm=1.5):
        self.prepared.append(label)
        return super().prepare(label, options, safe_mm)

    def print_label(self, image, host, options, progress=None):
        super().print_label(image, host, options, progress)
        return self.result


@pytest.fixture
def mono(monkeypatch, tmp_path):
    """A registered fake printer; settings in tmp_path; a fixed clock."""
    backend = Recording()
    register(backend)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "config" / "settings.json")
    real = cli.print_command
    monkeypatch.setattr(cli, "print_command", lambda args: real(args, now=NOW))
    yield backend
    unregister(backend.id)


@pytest.fixture
def label_file(tmp_path):
    def write(*elements, **fields) -> Path:
        fields.setdefault("printer", "fake-mono")
        fields.setdefault("tape_mm", 12)
        path = tmp_path / "label.etiketto"
        Label(elements=list(elements), **fields).save(path)
        return path

    return write


def run(*argv) -> int:
    return cli.main([str(a) for a in argv])


def err(capsys) -> str:
    return capsys.readouterr().err


def contents(backend) -> list[list[str]]:
    return [
        [getattr(e, "text", "") or getattr(e, "data", "") for e in label.elements]
        for label in backend.prepared
    ]


# --- dispatch and arguments ----------------------------------------------------------


def test_without_a_command_the_application_starts(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_gui", lambda: started.append(True) or 0)
    assert cli.main([]) == 0
    assert cli.main(["label.etiketto"]) == 0  # files open in the window
    monkeypatch.setattr(sys, "argv", ["etiketto"])
    assert cli.main() == 0
    assert started == [True, True, True]


def test_version(capsys):
    assert run("--version") == 0
    assert capsys.readouterr().out == "etiketto %s\n" % __version__


@pytest.mark.parametrize(
    "argv",
    [
        ["print"],  # no file
        ["print", "a.etiketto", "--copies", "0"],
        ["print", "a.etiketto", "--copies", "x"],
        ["print", "a.etiketto", "--copies", "10000"],
        ["print", "a.etiketto", "--counter-start", "-1"],
        ["print", "a.etiketto", "--set", "novalue"],
        ["print", "a.etiketto", "--set", "=x"],
        ["print", "a.etiketto", "--bogus"],
    ],
)
def test_wrong_arguments(argv, capsys):
    assert cli.main(argv) == cli.EXIT_USAGE
    assert "usage: etiketto" in err(capsys)


def test_arguments_are_parsed():
    args = cli.parser().parse_args(
        [
            "print",
            "x.etiketto",
            "--printer",
            "cab",
            "--host",
            "EOS2.local",
            "--copies",
            "3",
            "--set",
            "title=Hello=World",
            "--set",
            " lot =A1",
            "--counter-start",
            "17",
            "--dry-run",
            "--output",
            "-",
        ]
    )
    assert args.file == Path("x.etiketto")
    assert (args.printer, args.host, args.copies) == ("cab", "EOS2.local", 3)
    assert args.assignments == [("title", "Hello=World", False), ("lot", "A1", False)]
    assert (args.counter_start, args.dry_run, args.output) == (17, True, "-")
    help_text = cli.parser().format_help()
    assert "list-printers" in help_text


def test_help_exits_cleanly(capsys):
    assert run("print", "--help") == 0
    assert "--counter-start" in capsys.readouterr().out


# --- dry run -------------------------------------------------------------------------


def test_dry_run_lists_every_label_and_saves_the_images(mono, label_file, tmp_path, capsys):
    path = label_file(
        TextElement(text="No. {=counter:000}\n{=date+30d:%d.%m.%Y}"),
        CodeElement(data="{=copy}/{=copies}"),
        counter_next=8,
    )
    out = tmp_path / "out.png"
    assert run("print", path, "--copies", "3", "--dry-run", "--output", out) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "1\tNo. 008 31.03.2026\t1/3",
        "2\tNo. 009 31.03.2026\t2/3",
        "3\tNo. 010 31.03.2026\t3/3",
    ]
    for n in (1, 2, 3):
        with Image.open(tmp_path / ("out-%d.png" % n)) as image:
            assert image.height == round(12 / 25.4 * 203)  # the fake's 12 mm at 203 dpi
    assert mono.jobs == []  # nothing printed
    assert json.loads(path.read_text())["counter_next"] == 8  # nothing used up


def test_dry_run_to_stdout(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="{=counter}"))
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=buffer, write=sys.stdout.write))
    assert run("print", path, "--copies", "2", "--dry-run", "--output", "-") == 0
    buffer.seek(0)
    with Image.open(buffer) as image:  # the first label only
        assert image.format == "PNG"


def test_dry_run_of_a_label_without_changing_variables_is_one_image(
    mono, label_file, tmp_path, capsys
):
    path = label_file(TextElement(text="Hello"))
    assert run("print", path, "--copies", "4", "--dry-run", "--output", tmp_path / "o.png") == 0
    assert (tmp_path / "o.png").exists() and not (tmp_path / "o-1.png").exists()
    assert capsys.readouterr().out == "1\tHello\n"


def test_dry_run_cannot_write_the_output(mono, label_file, tmp_path, capsys):
    path = label_file(TextElement(text="x"))
    target = tmp_path / "missing" / "o.png"
    assert run("print", path, "--dry-run", "--output", target) == cli.EXIT_FILE
    assert err(capsys) == "etiketto: Cannot save o.png\n"


def test_output_needs_a_dry_run(mono, label_file, capsys):
    path = label_file(TextElement(text="x"))
    assert run("print", path, "--output", "o.png") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: --output needs --dry-run\n"


def test_a_render_that_fails(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="x"))

    def broken(*_args, **_kwargs):
        raise RuntimeError("no fonts")

    monkeypatch.setattr(mono, "prepare", broken)
    assert run("print", path, "--dry-run") == cli.EXIT_LABEL
    assert err(capsys) == "etiketto: The label cannot be prepared for printing.\n"
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL


def test_dry_run_without_a_display(tmp_path):
    """Printing never needs GTK: the render path runs without any display."""
    path = tmp_path / "plain.etiketto"
    Label(tape_mm=25, elements=[TextElement(text="Lot {=counter:0001}")], counter_next=3).save(
        path
    )
    out = tmp_path / "plain.png"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}
    code = (
        "import sys; from etiketto import cli; rc = cli.main(sys.argv[1:]); "
        "assert 'gi.repository.Gtk' not in sys.modules; sys.exit(rc)"
    )
    done = subprocess.run(  # nosec B603 - our own interpreter and module
        [sys.executable, "-c", code, "print", str(path), "--dry-run", "--output", str(out)],
        env=env,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "1\tLot 0003\n"
    with Image.open(out) as image:
        assert image.height > 300  # 25 mm at the VC-500W's resolution


# --- printing ------------------------------------------------------------------------


def test_print_moves_the_counter_on_in_the_file(mono, label_file, capsys):
    path = label_file(
        TextElement(text="{=counter}", name="serial"), counter_next=5, counter_step=10
    )
    assert run("print", path, "--host", "mono.local", "--copies", "3") == 0
    assert contents(mono) == [["5"], ["15"], ["25"]]
    assert {host for _image, host, _options in mono.jobs} == {"mono.local"}
    assert Label.load(path).counter_next == 35
    assert capsys.readouterr() == ("", "")  # quiet on success
    assert run("print", path, "--host", "mono.local") == 0
    assert contents(mono)[-1] == ["35"]
    assert run("print", path, "--host", "mono.local", "--counter-start", "100") == 0
    assert contents(mono)[-1] == ["100"]
    assert Label.load(path).counter_next == 110


def test_set_replaces_named_elements_and_custom_variables(mono, label_file):
    path = label_file(
        TextElement(text="old", name="title"),
        TextElement(text="Lot {=lot}, {=date:%d.%m.%Y} {=time:%H:%M}"),
        CodeElement(data="old", name="link"),
    )
    assert (
        run(
            "print",
            path,
            "--host",
            "h",
            "--set",
            "title=New {=copies}",  # a new text may use variables
            "--set",
            "lot=A12",
            "--set",
            "date=2027-05-04T08:15",
            "--set",
            "link=https://example.org",
        )
        == 0
    )
    assert contents(mono) == [["New 1", "Lot A12, 04.05.2027 08:15", "https://example.org"]]
    saved = Label.load(path)
    assert saved.elements[0].text == "old"  # overrides are never saved


@pytest.mark.parametrize(
    "assignment, message",
    [
        ("nothing=1", "The label has no element or variable named nothing"),
        ("counter=5", "{=counter} is filled in when printing and cannot be set"),
        ("date=tomorrow", "tomorrow is not a date, expected e.g. 2026-12-31 or 2026-12-31T08:30"),
    ],
)
def test_wrong_assignments(mono, label_file, capsys, assignment, message):
    path = label_file(TextElement(text="{=counter} {=date}"))
    assert run("print", path, "--host", "h", "--set", assignment) == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: %s\n" % message
    assert mono.jobs == []


def test_unknown_variables_are_refused(mono, label_file, capsys):
    path = label_file(TextElement(text="Lot {=lot}"))
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert err(capsys) == "etiketto: Element 1: {=lot} is not a known variable\n"
    assert mono.jobs == []


def test_other_problems_are_warnings(mono, label_file, capsys):
    path = label_file(CodeElement(data="12", symbology="ean13"))
    assert run("print", path, "--host", "h") == 0
    assert err(capsys) == "etiketto: Element 1: EAN-13 needs exactly 12 digits\n"


def test_an_empty_label(mono, label_file, capsys):
    assert run("print", label_file(), "--host", "h") == cli.EXIT_LABEL
    assert err(capsys) == "etiketto: The label is empty\n"


def test_printer_errors(mono, label_file, capsys):
    path = label_file(TextElement(text="{=counter}"), counter_next=1)
    mono.error = PaperJam("jam")
    assert run("print", path, "--host", "h") == cli.EXIT_PRINTER
    assert err(capsys) == (
        "etiketto: The label is jammed. Open the cover, remove it and try again.\n"
    )
    assert Label.load(path).counter_next == 1


def test_a_failure_after_some_copies_saves_the_used_numbers(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="{=counter}"), counter_next=1)
    calls = []
    real = Recording.print_label

    def second_fails(image, host, options, progress=None):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("connection reset")
        return real(mono, image, host, options, progress)

    monkeypatch.setattr(mono, "print_label", second_fails)
    assert run("print", path, "--host", "h", "--copies", "3") == cli.EXIT_PRINTER
    assert Label.load(path).counter_next == 2
    assert err(capsys).startswith("etiketto: The printer reported a problem.")


def test_the_counter_cannot_be_saved(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="{=counter}"))

    def refuse(*_args):
        raise PermissionError("read-only")

    monkeypatch.setattr(Label, "store_counter", staticmethod(refuse))
    assert run("print", path, "--host", "h") == cli.EXIT_COUNTER
    assert err(capsys) == (
        "etiketto: Cannot save label.etiketto: no permission to write there – the labels "
        "were printed, but the next counter value was not saved\n"
    )
    monkeypatch.setattr(Label, "store_counter", staticmethod(lambda *_a: json.loads("[")))
    assert run("print", path, "--host", "h") == cli.EXIT_COUNTER
    assert err(capsys).startswith("etiketto: Cannot save label.etiketto – ")


def test_queued_and_other_mode_are_told(mono, label_file, capsys):
    path = label_file(TextElement(text="x"))
    mono.result = PrintResult(queued=True)
    assert run("print", path, "--host", "h") == 0
    assert err(capsys) == "etiketto: Sent to the printer – it prints when its turn comes\n"


def test_host_from_the_settings_or_the_network(mono, label_file, capsys):
    path = label_file(TextElement(text="x"))
    Settings({"printers": {"fake-mono": {"host": "saved.local", "safe_mm": 0}}}).save()
    assert run("print", path) == 0
    assert mono.jobs[-1][1] == "saved.local"
    Settings({}).save()
    assert run("print", path) == 0
    assert mono.jobs[-1][1] == "mono.local"  # the only printer found
    mono.found = []
    assert run("print", path) == cli.EXIT_PRINTER
    assert err(capsys) == "etiketto: No printer found – give its address with --host\n"
    mono.found = [FoundPrinter("A", "a.local", "192.0.2.1"), FoundPrinter("B", "b", "192.0.2.2")]
    assert run("print", path) == cli.EXIT_PRINTER
    assert "Several printers found" in err(capsys)


def test_another_printer_model(mono, label_file, tmp_path, capsys):
    path = label_file(TextElement(text="x"), printer="vc500w", tape_mm=25)
    out = tmp_path / "o.png"
    assert run("print", path, "--printer", "fake-mono", "--dry-run", "--output", out) == 0
    with Image.open(out) as image:
        assert image.height == round(24 / 25.4 * 203)  # the nearest tape of the fake
    assert run("print", path, "--printer", "nope", "--dry-run") == cli.EXIT_USAGE
    assert err(capsys).endswith("etiketto: Unknown printer model: nope\n")


@pytest.mark.parametrize(
    "content, message",
    [
        (None, "Cannot open missing.etiketto"),
        ("no json", "missing.etiketto is not an Etiketto label"),
        ('{"version": 99}', "This label needs a newer version of Etiketto"),
    ],
)
def test_files_that_cannot_be_printed(mono, tmp_path, capsys, content, message):
    path = tmp_path / "missing.etiketto"
    if content is not None:
        path.write_text(content)
    assert run("print", path, "--dry-run") == cli.EXIT_FILE
    assert err(capsys) == "etiketto: %s\n" % message


def test_messages_are_translated(mono, tmp_path, monkeypatch, capsys):
    from etiketto.ui import i18n

    german = {"Cannot open %s": "%s lässt sich nicht öffnen"}
    monkeypatch.setattr(cli, "_", lambda text: german.get(text, text))
    assert i18n  # the real catalogue is used in the app; here a stand-in
    assert run("print", tmp_path / "x.etiketto", "--dry-run") == cli.EXIT_FILE
    assert err(capsys) == "etiketto: x.etiketto lässt sich nicht öffnen\n"


# --- list-printers -------------------------------------------------------------------


def test_list_printers(mono, capsys, monkeypatch):
    from etiketto import backends

    for backend in backends.backends():
        if backend is not mono:
            monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    assert run("list-printers") == 0
    assert capsys.readouterr().out == "fake-mono\tmono.local\tFake Mono\n"
    assert run("list-printers", "--printer", "fake-mono") == 0
    mono.found = []
    assert run("list-printers") == cli.EXIT_PRINTER
    assert err(capsys) == "etiketto: No printer found\n"
    assert run("list-printers", "--printer", "nope") == cli.EXIT_USAGE


def test_print_options_from_the_app(mono, label_file, monkeypatch):
    from etiketto.backends.base import PrintMode

    monkeypatch.setattr(
        mono,
        "capabilities",
        replace(
            mono.capabilities,
            cut_modes=("full", "half"),
            modes=(PrintMode("standard", 203), PrintMode("fine", 406)),
        ),
    )
    path = label_file(TextElement(text="x"), gap_mm=4)
    Settings({"printers": {"fake-mono": {"cut": "half", "mode": "fine"}}}).save()
    assert run("print", path, "--host", "h") == 0
    options = mono.jobs[-1][2]
    assert (options.cut, options.mode, options.gap_mm) == ("half", "fine", 4)
    Settings({"printers": {"fake-mono": {"cut": "bogus", "mode": "gone"}}}).save()
    assert run("print", path, "--host", "h") == 0
    options = mono.jobs[-1][2]
    assert (options.cut, options.mode) == ("full", "standard")


# --- review fixes --------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_the_application_help_names_the_commands(flag, capsys):
    assert cli.main([flag]) == 0
    out = capsys.readouterr().out
    assert "print" in out and "list-printers" in out
    assert "Without a command the application starts" in out


def interrupt_at(mono, monkeypatch, copy, how):
    real = Recording.print_label
    calls = []

    def send(image, host, options, progress=None):
        calls.append(1)
        if len(calls) == copy:
            how()
        return real(mono, image, host, options, progress)

    monkeypatch.setattr(mono, "print_label", send)


def test_ctrl_c_while_printing_saves_the_counter(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="{=counter}"), counter_next=1)

    def ctrl_c():
        raise KeyboardInterrupt

    interrupt_at(mono, monkeypatch, 3, ctrl_c)
    assert run("print", path, "--host", "h", "--copies", "5") == cli.EXIT_INTERRUPTED == 130
    assert err(capsys) == "etiketto: Interrupted\n"  # no traceback
    assert Label.load(path).counter_next == 4  # two printed, the third maybe


def test_sigterm_while_printing_saves_the_counter(mono, label_file, monkeypatch, capsys):
    import os
    import signal

    path = label_file(TextElement(text="{=counter}"), counter_next=1)
    interrupt_at(mono, monkeypatch, 2, lambda: os.kill(os.getpid(), signal.SIGTERM))
    before = signal.getsignal(signal.SIGTERM)
    assert run("print", path, "--host", "h", "--copies", "5") == cli.EXIT_INTERRUPTED
    assert err(capsys) == "etiketto: Interrupted\n"
    assert Label.load(path).counter_next == 3
    assert signal.getsignal(signal.SIGTERM) is before  # handler given back


def test_signals_wait_until_the_counter_is_saved(mono, label_file, monkeypatch, capsys):
    import os
    import signal

    path = label_file(TextElement(text="{=counter}"), counter_next=1)
    real = Label.store_counter

    def store(p, counter):
        os.kill(os.getpid(), signal.SIGINT)  # Ctrl+C while saving: waits
        real(p, counter)

    monkeypatch.setattr(Label, "store_counter", staticmethod(store))
    assert run("print", path, "--host", "h", "--copies", "2") == cli.EXIT_INTERRUPTED
    assert Label.load(path).counter_next == 3


def test_ctrl_c_before_printing(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="x"))

    def ctrl_c(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mono, "discover", ctrl_c)
    assert run("print", path) == cli.EXIT_INTERRUPTED
    assert err(capsys) == "etiketto: Interrupted\n"


def test_the_label_file_is_locked_while_printing(mono, label_file, monkeypatch, capsys):
    import fcntl
    import os

    path = label_file(TextElement(text="{=counter}"))
    held = []

    def check(image, host, options, progress=None):
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)

    monkeypatch.setattr(mono, "print_label", check)
    assert run("print", path, "--host", "h") == 0
    assert held == [True]


def test_waiting_for_another_print(mono, label_file, monkeypatch, capsys):
    from etiketto import model

    path = label_file(TextElement(text="x"))
    real = model.fcntl.flock
    calls = []

    def busy_once(fd, operation):
        calls.append(operation)
        if len(calls) == 1:
            raise BlockingIOError
        real(fd, operation)

    monkeypatch.setattr(model.fcntl, "flock", busy_once)
    assert run("print", path, "--host", "h") == 0
    assert err(capsys) == "etiketto: Waiting for another print of label.etiketto to finish…\n"


def test_nothing_is_printed_if_the_counter_cannot_be_saved(mono, label_file, tmp_path, capsys):
    path = label_file(TextElement(text="{=counter}"))
    path.chmod(0o444)
    try:
        assert run("print", path, "--host", "h") == cli.EXIT_COUNTER
    finally:
        path.chmod(0o644)
    assert err(capsys) == (
        "etiketto: Cannot save label.etiketto: no permission to write there – nothing was "
        "printed: the next counter value could not be saved\n"
    )
    assert mono.jobs == []
    plain = label_file(TextElement(text="no counter"))
    plain.chmod(0o444)
    try:
        assert run("print", plain, "--host", "h") == 0  # nothing to save
    finally:
        plain.chmod(0o644)


def test_set_literal_and_set_order(mono, label_file):
    path = label_file(TextElement(text="old", name="title"), TextElement(text="x", name="note"))
    assert (
        run(
            "print",
            path,
            "--host",
            "h",
            "--set",
            "lot=7",  # only used in the new title: set after all element texts
            "--set",
            "title=Lot {=lot}",
            "--set-literal",
            "note={=date} {x}",
        )
        == 0
    )
    assert contents(mono) == [["Lot 7", "{=date} {x}"]]


def test_set_literal_value_of_a_variable_is_as_given(mono, label_file):
    path = label_file(TextElement(text="{=lot}"))
    assert run("print", path, "--host", "h", "--set-literal", "lot={a}") == 0
    assert contents(mono) == [["{a}"]]


def test_an_unclosed_variable_is_refused(mono, label_file, capsys):
    path = label_file(TextElement(text="Lot {=lot"))
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert err(capsys) == (
        "etiketto: Element 1: the variable {=lot… is not closed – end it with }\n"
    )


def test_a_dry_run_renders_one_label_without_output(mono, label_file, capsys):
    path = label_file(TextElement(text="{=counter}\tx"))
    assert run("print", path, "--copies", "3", "--dry-run") == 0
    assert len(mono.prepared) == 1  # the first, to find what would fail
    assert capsys.readouterr().out == "1\t1 x\n2\t2 x\n3\t3 x\n"  # the tab is no column


def test_output_must_be_png(mono, label_file, tmp_path, capsys):
    path = label_file(TextElement(text="x"))
    assert run("print", path, "--dry-run", "--output", tmp_path / "o.jpg") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: --output writes PNG images: give a name ending in .png\n"
    assert run("print", path, "--dry-run", "--output", tmp_path / "o.PNG") == 0


def test_a_broken_pipe_is_no_traceback(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="x"))

    class Closed(io.StringIO):
        buffer = None

        def write(self, _text):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Closed())
    assert run("print", path, "--dry-run") == cli.EXIT_FILE
    assert err(capsys) == "etiketto: Cannot write the output\n"

    class ClosedBuffer(io.BytesIO):
        def write(self, _data):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=ClosedBuffer()))
    assert run("print", path, "--dry-run", "--output", "-") == cli.EXIT_FILE


def test_a_full_disk_on_stdout(tmp_path):
    path = tmp_path / "plain.etiketto"
    Label(tape_mm=25, elements=[TextElement(text="x")]).save(path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}
    with open("/dev/full", "w") as full:
        done = subprocess.run(  # nosec B603 - our own interpreter and module
            [sys.executable, "-m", "etiketto.cli", "print", str(path), "--dry-run"],
            env=env,
            stdout=full,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
            check=False,
        )
    assert done.returncode == cli.EXIT_FILE
    assert done.stderr == "etiketto: Cannot write the output\n"


def test_a_folder_has_a_name_in_messages(mono, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run("print", ".", "--dry-run") == cli.EXIT_FILE
    assert err(capsys) == "etiketto: . is not an Etiketto label\n"


def test_list_printers_keeps_its_columns(mono, capsys, monkeypatch):
    from etiketto import backends

    for backend in backends.backends():
        if backend is not mono:
            monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    mono.found = [FoundPrinter("Tab\tName", "a.local", "192.0.2.1")]
    assert run("list-printers") == 0
    assert capsys.readouterr().out == "fake-mono\ta.local\tTab Name\n"


def test_set_literal_keeps_other_braces(mono, label_file):
    path = label_file(CodeElement(data="x", name="payload"))
    assert run("print", path, "--host", "h", "--set-literal", 'payload={"a":1,"b":{}}') == 0
    assert contents(mono) == [['{"a":1,"b":{}}']]


def test_no_save_counter_for_read_only_labels(mono, label_file, capsys):
    path = label_file(TextElement(text="{=counter}"), counter_next=7)
    path.chmod(0o444)
    try:
        argv = ["print", path, "--host", "h", "--copies", "2", "--no-save-counter"]
        assert run(*argv) == 0
        assert err(capsys) == (
            "etiketto: The next counter value is not saved: the next print repeats the numbers\n"
        )
        assert run(*argv, "--counter-start", "20") == 0
        err(capsys)
    finally:
        path.chmod(0o644)
    assert contents(mono) == [["7"], ["8"], ["20"], ["21"]]
    assert Label.load(path).counter_next == 7
    plain = label_file(TextElement(text="x"))
    assert run("print", plain, "--host", "h", "--no-save-counter") == 0
    assert err(capsys) == ""  # no counter: nothing to warn about


def test_sighup_while_printing_saves_the_counter(mono, label_file, monkeypatch, capsys):
    import os
    import signal

    path = label_file(TextElement(text="{=counter}"), counter_next=1)
    interrupt_at(mono, monkeypatch, 2, lambda: os.kill(os.getpid(), signal.SIGHUP))
    assert run("print", path, "--host", "h", "--copies", "3") == cli.EXIT_INTERRUPTED
    assert Label.load(path).counter_next == 3


def test_no_stdout_at_all(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="x"))
    monkeypatch.setattr(sys, "stdout", None)
    assert run("print", path, "--dry-run") == cli.EXIT_FILE
    assert run("print", path, "--dry-run", "--output", "-") == cli.EXIT_FILE
    assert err(capsys) == "etiketto: Cannot write the output\n" * 2


def test_a_lock_held_too_long(mono, label_file, monkeypatch, capsys):
    from etiketto import model

    path = label_file(TextElement(text="{=counter}"))

    def held(*_args, **_kwargs):
        raise model.LockTimeout(11, "locked")

    monkeypatch.setattr(model, "_lock", held)
    assert run("print", path, "--host", "h") == cli.EXIT_FILE
    assert err(capsys) == (
        "etiketto: label.etiketto is still locked by another print – try again later\n"
    )
    assert mono.jobs == []


def test_dates_out_of_range_are_refused(mono, label_file, capsys):
    path = label_file(TextElement(text="{=date+9000y}"))
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert err(capsys) == (
        "etiketto: Element 1: the date of {=date+9000y} is before the year 1 or after 9999\n"
    )


def test_messages_are_dropped_without_stderr(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="Lot {=lot}"))
    monkeypatch.setattr(sys, "stderr", None)
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert capsys.readouterr().out == ""  # not on stdout instead


def test_an_element_that_cannot_be_drawn_is_not_printed(mono, label_file, monkeypatch, capsys):
    from etiketto.render import RenderWarning

    path = label_file(TextElement(text="x"))
    monkeypatch.setattr(cli.Job, "check", lambda self: [RenderWarning("element_failed", {"n": 1})])
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert err(capsys) == "etiketto: Element 1 cannot be drawn\n"
    assert mono.jobs == []


def test_a_code_too_large_for_the_printer_says_so(mono, label_file, monkeypatch, capsys):
    from etiketto.backends.base import CodeTooLarge

    path = label_file(TextElement(text="x"))

    def too_large(*_args, **_kwargs):
        raise CodeTooLarge("element 1", n=1)

    monkeypatch.setattr(mono, "prepare", too_large)
    assert run("print", path, "--dry-run") == cli.EXIT_LABEL
    assert err(capsys).startswith("etiketto: A code does not fit on the label")
    assert run("print", path, "--host", "h") == cli.EXIT_LABEL
    assert err(capsys).startswith("etiketto: A code does not fit on the label")


# --- serial labels -------------------------------------------------------------


@pytest.fixture
def rows_csv(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("Name;EAN\nScrews;4006381333931\nWashers;400638133393\nFuses;12x\n")
    return path


def test_rows_argument():
    assert cli._rows("3-10") == (3, 10)
    assert cli._rows("5") == (5, 5)
    assert cli._rows(" 7- ") == (7, cli.ALL_ROWS)
    assert cli._rows("-4") == (1, 4)
    for bad in ("", "-", "0", "4-2", "a-b", "1-2-3"):
        with pytest.raises(Exception, match="expected rows"):
            cli._rows(bad)


def test_serial_dry_run_fills_rows_and_variables_in_one_pass(
    mono, label_file, rows_csv, tmp_path, capsys
):
    rows_csv.write_text("Name\n{=counter}\nB\nC\n")  # a value is never filled in
    path = label_file(TextElement(text="{Name} {=counter} {{x}}"), counter_next=5)
    assert (
        run("print", path, "--data", rows_csv, "--rows", "1-2", "--copies", "2", "--dry-run") == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        "1\t1\t{=counter} 5 {x}",
        "1\t2\t{=counter} 6 {x}",
        "2\t1\tB 7 {x}",
        "2\t2\tB 8 {x}",
    ]
    out = tmp_path / "o.png"
    assert run("print", path, "--data", rows_csv, "--rows", "3", "--dry-run", "--output", out) == 0
    assert out.exists()


def test_serial_print_continues_the_counter_across_rows(mono, label_file, rows_csv, capsys):
    rows_csv.write_text("Name\nA\nB\nC\n")
    path = label_file(TextElement(text="{=counter} {Name}"), counter_next=1)
    assert run("print", path, "--host", "h", "--data", rows_csv, "--copies", "2") == 0
    assert contents(mono) == [["1 A"], ["2 A"], ["3 B"], ["4 B"], ["5 C"], ["6 C"]]
    assert Label.load(path).counter_next == 7


def test_serial_print_without_variables_sends_copies_per_row(mono, label_file, rows_csv):
    rows_csv.write_text("Name\nA\nB\n")
    path = label_file(TextElement(text="{Name}"))
    assert run("print", path, "--host", "h", "--data", rows_csv, "--copies", "3") == 0
    assert contents(mono) == [["A"], ["B"]]  # one render per row, the printer copies it
    assert len(mono.jobs) == 6


def test_serial_printer_problem_names_the_row_and_saves_the_numbers(
    mono, label_file, rows_csv, monkeypatch, capsys
):
    rows_csv.write_text("Name\nA\nB\nC\n")
    path = label_file(TextElement(text="{=counter} {Name}"), counter_next=1)
    real = Recording.print_label
    calls = []

    def third_fails(image, host, options, progress=None):
        calls.append(1)
        if len(calls) == 3:
            raise PaperJam("jam")
        return real(mono, image, host, options, progress)

    monkeypatch.setattr(mono, "print_label", third_fails)
    assert run("print", path, "--host", "h", "--data", rows_csv) == cli.EXIT_PRINTER
    assert err(capsys).endswith(
        "Printing stopped at row 3. To continue, print again from row 3.\n"
    )
    assert Label.load(path).counter_next == 3


def test_serial_ctrl_c_saves_the_counter(mono, label_file, rows_csv, monkeypatch, capsys):
    rows_csv.write_text("Name\nA\nB\nC\n")
    path = label_file(TextElement(text="{=counter}{Name}"), counter_next=1)

    def ctrl_c():
        raise KeyboardInterrupt

    interrupt_at(mono, monkeypatch, 2, ctrl_c)
    assert run("print", path, "--host", "h", "--data", rows_csv) == cli.EXIT_INTERRUPTED
    assert Label.load(path).counter_next == 3  # one printed, the second maybe


def test_serial_problems_are_refused(mono, label_file, rows_csv, tmp_path, capsys):
    path = label_file(CodeElement(data="{EAN}", symbology="ean13"), TextElement(text="{Name}"))
    assert run("print", path, "--host", "h", "--data", rows_csv) == cli.EXIT_LABEL
    assert err(capsys).endswith("Rows: 3\n")
    assert run("print", path, "--host", "h", "--data", rows_csv, "--rows", "1-2") == 0
    capsys.readouterr()  # the fake's low resolution: a warning that the EAN may not scan
    path = label_file(TextElement(text="{Price}"))
    assert run("print", path, "--host", "h", "--data", rows_csv) == cli.EXIT_LABEL
    assert err(capsys) == "etiketto: No such column: {Price}\n"
    assert run("print", path, "--data", tmp_path / "gone.csv") == cli.EXIT_FILE
    assert "gone.csv is not there any more" in err(capsys)
    assert run("print", path, "--data", rows_csv, "--rows", "9-") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: No rows to print\n"
    assert run("print", path, "--rows", "1") == cli.EXIT_USAGE
    assert "--sheet and --rows need a data file" in err(capsys)
    assert run("print", path, "--data", rows_csv, "--sheet", "Mai") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: rows.csv has no sheet named Mai\n"


def test_serial_label_with_its_own_data_file(mono, label_file, rows_csv, tmp_path, capsys):
    path = label_file(TextElement(text="{Name}"), data_file=str(rows_csv))
    assert run("print", path, "--dry-run") == 0
    assert capsys.readouterr().out.splitlines()[0] == "1\t1\tScrews"
    (tmp_path / "labels").mkdir()
    elsewhere = tmp_path / "labels" / "x.etiketto"
    Label.load(path).save(elsewhere)  # the data file is now outside its folder
    assert run("print", elsewhere, "--dry-run") == cli.EXIT_USAGE
    assert "outside its folder" in err(capsys)
    assert run("print", elsewhere, "--dry-run", "--data", rows_csv, "--rows", "2") == 0
    assert capsys.readouterr().out == "2\t1\tWashers\n"


def test_serial_sheets(mono, label_file, tmp_path, capsys):
    from test_merge import make_ods, ods_table, row, text

    book = make_ods(
        tmp_path,
        ods_table("Mai", row(text("Name")), row(text("Anna"))),
        ods_table("Juni", row(text("Name")), row(text("Cleo"))),
        name="book.ods",
    )
    path = label_file(TextElement(text="{Name}"), data_file=str(book), data_sheet="Juni")
    assert run("print", path, "--dry-run") == 0
    assert capsys.readouterr().out == "1\t1\tCleo\n"
    # the label's own data file given with --data keeps the label's sheet
    assert run("print", path, "--dry-run", "--data", book) == 0
    assert capsys.readouterr().out == "1\t1\tCleo\n"
    assert run("print", path, "--dry-run", "--data", book, "--sheet", "Mai") == 0
    assert capsys.readouterr().out == "1\t1\tAnna\n"
    path = label_file(TextElement(text="{Name}"), data_file=str(book), data_sheet="August")
    assert run("print", path, "--dry-run") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: book.ods has no sheet named August\n"


def test_set_literal_with_a_data_file(mono, label_file, rows_csv, capsys):
    """Every text of a serial label follows the placeholder syntax: a
    literal value is escaped whatever braces it has."""
    rows_csv.write_text("Name\nA\n")
    path = label_file(TextElement(text="x", name="t"), TextElement(text="{Name}"))
    argv = ["print", path, "--dry-run", "--data", rows_csv, "--set-literal", 't={"a":1} {Name}']
    assert run(*argv) == 0
    assert capsys.readouterr().out == '1\t1\t{"a":1} {Name}\tA\n'


def test_serial_stop_names_the_copies_of_the_row_printed(
    mono, label_file, rows_csv, monkeypatch, capsys
):
    rows_csv.write_text("Name\nA\nB\n")
    path = label_file(TextElement(text="{=counter} {Name}"))

    def jam():
        raise PaperJam("jam")

    interrupt_at(mono, monkeypatch, 4, jam)  # row 2, its second copy
    assert run("print", path, "--host", "h", "--data", rows_csv, "--copies", "2") == 1
    assert err(capsys).endswith(
        "Printing stopped at row 2. To continue, print again from row 2."
        " Copies of row 2 already printed: 1.\n"
    )
    assert Label.load(path).counter_next == 4


def test_a_csv_file_with_a_saved_sheet_prints(mono, label_file, rows_csv, capsys):
    """The label's own data file, once a spreadsheet, now a CSV file: the
    saved sheet is ignored; one given with --sheet is still refused."""
    path = label_file(TextElement(text="{Name}"), data_file=str(rows_csv), data_sheet="Mai")
    assert run("print", path, "--dry-run") == 0
    assert capsys.readouterr().out.splitlines()[0] == "1\t1\tScrews"
    assert run("print", path, "--dry-run", "--sheet", "Mai") == cli.EXIT_USAGE
    assert err(capsys) == "etiketto: rows.csv has no sheet named Mai\n"


def test_the_printer_is_asked_before_rendering_where_it_matters(mono, label_file, monkeypatch):
    """A backend whose render depends on the printer (Backend.
    query_before_render) is asked first; its answer goes with the job.
    --no-query and a printer that does not answer: rendered without."""
    path = label_file(TextElement(text="x"))
    assert run("print", path, "--host", "h") == 0
    assert mono.queries == []  # the fake renders the same for every printer
    monkeypatch.setattr(mono, "query_before_render", True, raising=False)
    assert run("print", path, "--host", "h") == 0
    assert mono.queries == ["h"] and mono.jobs[-1][2].info is mono.info
    assert mono.jobs[-1][2].host == "h"
    assert run("print", path, "--host", "h", "--no-query") == 0
    assert mono.queries == ["h"] and mono.jobs[-1][2].info is None
    rows = path.parent / "rows.csv"
    rows.write_text("Name\nA\nB\n")
    assert run("print", path, "--host", "h", "--data", rows) == 0  # serial labels too
    assert mono.jobs[-1][2].info is mono.info
    mono.error = PrinterTimeout("no answer")
    assert cli.ask_printer(mono, "h") is None


def test_an_answer_in_the_extra_time_is_used_a_later_one_is_left(mono, monkeypatch, capsys):
    """A question still running after `wait` gets `finish` seconds more
    (the user is told; an answer then is used), then it is left behind."""
    import threading

    monkeypatch.setattr(mono, "query_before_render", True, raising=False)
    release = threading.Event()
    done = []
    monkeypatch.setattr(
        mono, "query", lambda host: (release.wait(5), done.append(host), mono.info)[2]
    )
    threading.Timer(0.2, release.set).start()
    assert cli.ask_printer(mono, "h", wait=0.05) is mono.info  # in the extra time: used
    assert done == ["h"]  # and it ended before the print
    assert "Waiting for the printer's answer" in capsys.readouterr().err
    release.clear()
    done.clear()
    started = time.monotonic()
    assert cli.ask_printer(mono, "h", wait=0.05, finish=0.1) is None
    assert time.monotonic() - started < 1 and done == []  # bounded: left behind
    release.set()


def test_the_check_runs_for_the_printer_that_answered(mono, label_file, monkeypatch):
    """The warnings are the render's for the printer that was asked (a
    P-touch PT-2300 renders for its own head): the check comes after."""
    path = label_file(TextElement(text="x"))
    monkeypatch.setattr(mono, "query_before_render", True, raising=False)
    checked = []
    real = cli._check
    monkeypatch.setattr(cli, "_check", lambda job: checked.append(job.options.info) or real(job))
    assert run("print", path, "--host", "h") == 0
    assert checked == [mono.info]
    assert run("print", path, "--host", "h", "--dry-run") == 0
    assert checked == [mono.info, mono.info]


def test_printer_defaults_instead_of_the_apps_choices(tmp_path, monkeypatch):
    from etiketto.backends import get_backend
    from etiketto.ui.settings import Settings

    ptouch = get_backend("ptouch")
    settings = Settings({"printers": {"ptouch": {"cut": "half", "chain": True}}})
    args = cli.parser().parse_args(["print", "x.etiketto"])
    assert cli._choices(args, settings, ptouch).options().chain is True
    args = cli.parser().parse_args(["print", "x.etiketto", "--printer-defaults"])
    options = cli._choices(args, settings, ptouch).options()
    assert (options.chain, options.cut) == (False, "full")


def test_label_sheets_on_the_command_line(tmp_path, monkeypatch):
    """A label for a sheet prints on the sheet as in the window: each copy
    (or copy of a row) a position from the chosen ones on."""
    from etiketto.backends import get_backend
    from etiketto.model import SheetChoice
    from etiketto.ui.settings import Settings

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    system = get_backend("system")
    printer = Settings({"printers": {"system": {"outlines": True}}}).printer(system)
    label = Label(printer="system", orientation="across", elements=[TextElement(text="x")])
    label.sheet = SheetChoice("Avery L7160", [3, 4])
    job = cli._sheet_job(label, system, printer, 5)
    assert job is not None and job.count == 5 and job.positions == (3, 4) and job.outlines
    assert [len(page) for page in job.slots()] == [2, 3]
    label.sheet = SheetChoice("Future 1")
    assert cli._sheet_job(label, system, printer, 5) is None  # unknown to this version
    label.sheet = None
    assert cli._sheet_job(label, system, printer, 5) is None


def test_the_command_line_prints_with_the_extras_last_chosen(tmp_path, monkeypatch):
    """Chain printing, high resolution and tape feed as chosen in the app's
    print dialog (one helper for both: PrinterSettings.options)."""
    from etiketto.backends import get_backend
    from etiketto.ui.settings import Settings

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    ptouch = get_backend("ptouch")
    stored = {"chain": True, "high_res": True, "feed_mm": 999, "cut": "half", "quality": "x"}
    printer = Settings({"printers": {"ptouch": stored}}).printer(ptouch)
    options = cli._options(ptouch, printer, 2, 3.0)
    low, high, _default = ptouch.feed_mm(None)
    assert (options.chain, options.high_res, options.feed_mm) == (True, True, high)
    assert (options.cut, options.copies, options.quality) == ("half", 2, "")
    vc500w = get_backend("vc500w")
    plain = cli._options(vc500w, Settings({}).printer(vc500w), 1, 3.0)
    assert (plain.chain, plain.high_res, plain.feed_mm) == (False, False, None)


def test_a_dry_run_asks_the_printer_too(mono, label_file, monkeypatch, capsys):
    path = label_file(TextElement(text="x"))
    monkeypatch.setattr(mono, "query_before_render", True, raising=False)
    checked = []
    real = cli._check
    monkeypatch.setattr(cli, "_check", lambda job: checked.append(job.options.info) or real(job))
    assert run("print", path, "--dry-run", "--host", "h") == 0
    assert checked == [mono.info]  # asked (through a copy of the backend)
    assert run("print", path, "--dry-run", "--host", "h", "--no-query") == 0
    assert run("print", path, "--dry-run") == 0  # no printer known: nothing to ask
    assert checked == [mono.info, None, None]
    assert mono.queries == []  # the real backend's caches are untouched


def test_a_dry_run_waits_briefly_and_never_blocks(mono, label_file, monkeypatch):
    path = label_file(TextElement(text="x"))
    monkeypatch.setattr(mono, "query_before_render", True, raising=False)
    asked = []
    monkeypatch.setattr(
        cli, "ask_printer", lambda backend, host, wait, finish: asked.append((wait, finish))
    )
    assert run("print", path, "--dry-run", "--host", "h") == 0
    assert run("print", path, "--host", "h") == 0
    assert asked == [(cli.DRY_RUN_WAIT, 0.0), (cli.QUERY_WAIT, cli.QUERY_FINISH)]


def test_a_dry_runs_late_answer_changes_nothing(monkeypatch):
    """A dry run asks a copy of the backend: what a late answer stores (a
    raw printer's resolution) does not reach the backend that renders."""
    from etiketto.backends import get_backend

    zpl = get_backend("zpl")
    monkeypatch.setattr(zpl, "reported", {})

    def query(self, host):
        self.reported[host] = 600.0
        return

    monkeypatch.setattr(type(zpl), "query", query)
    cli.ask_printer(zpl, "zebra.local", wait=1.0, finish=0.0)
    assert zpl.reported == {}


def test_a_page_too_large_for_the_check_is_refused(mono, label_file, monkeypatch, capsys):
    from etiketto.backends.base import PageTooLarge

    path = label_file(TextElement(text="x"))

    def too_large(*_args, **_kwargs):
        raise PageTooLarge("210 x 297 mm at 1200 dpi")

    monkeypatch.setattr(mono, "render_label", too_large)
    assert run("print", path, "--dry-run") == cli.EXIT_LABEL
    assert err(capsys).startswith("etiketto: The page is too large")


def test_dry_run_output_is_the_windows_export(mono, label_file, tmp_path):
    """--output saves the label as the window exports it: label space, the
    whole tape, with its resolution in the PNG."""
    from PIL import Image

    path = label_file(TextElement(text="x"))
    out = tmp_path / "o.png"
    assert run("print", path, "--dry-run", "--output", out) == 0
    label = Label.load(path)
    dpi = mono.render_dpi(label.tape_mm, PrintOptions())
    with Image.open(out) as png:
        assert round(png.info["dpi"][0]) == round(dpi)
        if mono.capabilities.continuous:
            assert png.height == round(label.tape_mm / 25.4 * dpi)


def test_printer_defaults_also_reset_the_sheet_outlines(tmp_path):
    """Outlines on a label sheet are one of the print choices of the app:
    --printer-defaults prints without them, as the model's default."""
    from etiketto.model import SheetChoice

    label = Label(printer="system", orientation="across", elements=[TextElement(text="x")])
    label.sheet = SheetChoice("Avery L7160", [1])
    path = tmp_path / "sheet.etiketto"
    label.save(path)
    settings = Settings({"printers": {"system": {"outlines": True, "host": "office"}}})
    args = cli.parser().parse_args(["print", str(path)])
    job = cli._job(args, settings, NOW)[0]
    assert job.sheet is not None and job.sheet.outlines
    args = cli.parser().parse_args(["print", str(path), "--printer-defaults"])
    job, printer, _settings, _series = cli._job(args, settings, NOW)
    assert job.sheet is not None and not job.sheet.outlines
    assert printer.host == "office"  # the address stays


def test_a_dry_run_output_renders_each_label_once(mono, label_file, tmp_path, monkeypatch):
    """--output: the check and the first label as the printer gets it (what
    would fail) render once each, then every label once for its PNG; not
    every label twice."""
    path = label_file(TextElement(text="{=copy}"))
    renders = []
    real = mono.render_label
    monkeypatch.setattr(mono, "render_label", lambda *a, **k: renders.append(1) or real(*a, **k))
    assert run("print", path, "--copies", "4", "--dry-run", "--output", tmp_path / "o.png") == 0
    assert len(renders) == 1 + 1 + 4
    assert len(mono.prepared) == 1
    assert sorted(p.name for p in tmp_path.glob("o-*.png")) == [
        "o-%d.png" % n for n in range(1, 5)
    ]


def test_a_dry_run_series_output_renders_each_label_once(mono, tmp_path, monkeypatch):
    rows = tmp_path / "rows.csv"
    rows.write_text("name\nA\nB\nC\n", encoding="utf-8")
    path = tmp_path / "label.etiketto"
    Label(printer="fake-mono", tape_mm=12, elements=[TextElement(text="{name}")]).save(path)
    renders = []
    real = mono.render_label
    monkeypatch.setattr(mono, "render_label", lambda *a, **k: renders.append(1) or real(*a, **k))
    out = tmp_path / "o.png"
    assert run("print", path, "--data", rows, "--dry-run", "--output", out) == 0
    assert len(mono.prepared) == 1  # the first label, as the printer gets it
    assert len(list(tmp_path.glob("o-*.png"))) == 3
    assert len(renders) == 1 + 1 + 3  # the check, the first as printed, one per PNG
    renders.clear()
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=buffer, write=sys.stdout.write))
    assert run("print", path, "--data", rows, "--dry-run", "--output", "-") == 0
    assert len(mono.prepared) == 2 and len(renders) == 1 + 1 + 1  # the first row only
    buffer.seek(0)
    with Image.open(buffer) as image:
        assert image.format == "PNG"


def test_dry_run_output_of_a_page_printer_is_capped_as_the_window(tmp_path, monkeypatch):
    """A printer reporting 1200 dpi: the window's export is drawn at most
    at 600 dpi, and so is --output."""
    from etiketto.backends import get_backend
    from etiketto.backends.base import PrinterInfo
    from etiketto.job import PREVIEW_MAX_DPI

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    system = get_backend("system")
    info = PrinterInfo("Office", None, None, None, "IDLE", dpi=1200)
    monkeypatch.setattr(system, "query", lambda host: info)
    label = Label(printer="system", orientation="across", auto_length=False)
    label.set_media_size(100, 50)
    label.elements = [TextElement(text="x")]
    path = tmp_path / "page.etiketto"
    label.save(path)
    out = tmp_path / "o.png"
    assert run("print", path, "--host", "office", "--dry-run", "--output", out) == 0
    with Image.open(out) as png:
        assert round(png.info["dpi"][0]) == PREVIEW_MAX_DPI
        assert png.width == round(100 / 25.4 * PREVIEW_MAX_DPI)


def test_a_dry_run_output_stops_at_a_row_whose_code_does_not_fit(mono, tmp_path, capsys):
    """Each label --output saves is checked like a print: a row whose QR
    code does not fit the tape stops the dry run (exit 4, the printer's
    message), no PNG of a code cut off. Without --output only the first
    label is drawn (documented): the rows' codes are checked to be makeable."""
    rows = tmp_path / "rows.csv"
    rows.write_text("data\nA1\nB2\n" + "x" * 600 + "\n", encoding="utf-8")
    path = tmp_path / "label.etiketto"
    code = CodeElement(data="{data}", symbology="qr")
    Label(printer="fake-mono", tape_mm=12, elements=[code]).save(path)
    out = tmp_path / "o.png"
    assert run("print", path, "--data", rows, "--dry-run", "--output", out) == cli.EXIT_LABEL
    assert err(capsys).startswith("etiketto: A code does not fit")
    assert sorted(p.name for p in tmp_path.glob("o-*.png")) == ["o-1.png", "o-2.png"]
    assert run("print", path, "--data", rows, "--dry-run") == cli.EXIT_OK


# --- print method (thermal printers) ---------------------------------------------------


@pytest.fixture
def cab_cli(monkeypatch, mono, tmp_path):
    """The cab backend with faked query and print; a 50 × 25 mm label for it,
    the app's choice "thermal direct" in settings.json."""
    from etiketto.backends import get_backend

    backend = get_backend("cab")
    printed: list = []
    info = PrinterInfo("EOS2/300", None, None, None, "IDLE", dpi=304.8, pitch_mm=28.0)
    monkeypatch.setattr(backend, "query", lambda host: info)
    monkeypatch.setattr(
        backend,
        "print_label",
        lambda image, host, options, progress=None: printed.append(options),
    )
    Settings({"printers": {"cab": {"host": "h", "method": "direct"}}}).save()
    label = Label(printer="cab", auto_length=False, orientation="across")
    label.set_media_size(50, 25)
    label.elements = [TextElement(text="x")]
    path = tmp_path / "cab.etiketto"
    label.save(path)
    return SimpleNamespace(path=path, printed=printed, info=info)


@pytest.mark.parametrize(
    "flags, settings",
    [
        ([], {"method": "direct"}),  # as chosen in the app
        (["--print-method", "transfer"], {"method": "transfer"}),
        (["--print-method", "printer"], {"method": "printer"}),
        (["--printer-defaults"], {}),  # the default: as set on the printer
        (["--printer-defaults", "--print-method", "transfer"], {"method": "transfer"}),
        (["--no-query", "--print-method", "transfer"], {"method": "transfer"}),
        (["--no-query", "--printer-defaults"], {}),
    ],
)
def test_print_method_on_the_command_line(cab_cli, flags, settings):
    assert run("print", cab_cli.path, *flags) == 0
    (options,) = cab_cli.printed
    assert dict(options.settings) == settings


def test_print_method_only_for_printers_that_have_it(mono, label_file, capsys):
    path = label_file(TextElement(text="x"))
    assert run("print", path, "--host", "h", "--print-method", "direct") == cli.EXIT_USAGE
    assert "has no print method setting: fake-mono" in err(capsys)
    assert mono.prepared == []


def test_print_method_must_be_one_of_the_choices(capsys):
    assert cli.main(["print", "x.etiketto", "--print-method", "ribbon"]) == cli.EXIT_USAGE
    assert "invalid choice" in err(capsys)


def test_a_roll_with_another_label_distance_gets_a_hint(cab_cli, capsys):
    """The label starts a new one every 28 mm (25 + 3 mm gap); the printer
    measured 20 mm: said, as the window warns, and printed anyway."""
    assert run("print", cab_cli.path) == 0
    assert "measured" not in err(capsys)  # 28 mm: the same roll
    cab_cli.info.pitch_mm = 28.9  # the sensor measures in whole mm
    assert run("print", cab_cli.path, "--dry-run") == 0
    assert "measured" not in err(capsys)
    cab_cli.info.pitch_mm = 20.0
    assert run("print", cab_cli.path) == 0
    assert err(capsys) == (
        "etiketto: The label is 50 × 25 mm with a gap of 3.0 mm, so a new one starts "
        "every 28.0 mm. The printer measured a label every 20.0 mm. Check the label "
        "size and the gap.\n"
    )
    assert len(cab_cli.printed) == 2
    cab_cli.info.pitch_mm = None  # not synchronised: nothing to compare
    assert run("print", cab_cli.path) == 0
    assert "measured" not in err(capsys)


def test_print_refuses_a_code_128_too_long(mono, label_file, capsys):
    from etiketto.render import CODE128_MAX

    path = label_file(CodeElement(data="A" * (CODE128_MAX + 1), symbology="code128"))
    assert run("print", path, "--host", "mono.local") == cli.EXIT_LABEL
    assert "Code 128" in err(capsys)
    assert mono.jobs == []
