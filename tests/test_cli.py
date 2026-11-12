"""The command line: arguments, exit codes, messages, a dry run that renders
without a display, printing on a fake printer and the counter in the file."""

from __future__ import annotations

import io
import json
import subprocess  # nosec B404 - runs this checkout's CLI without a display
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_backends import FakeMono
from PIL import Image

from etiketto import __version__, cli
from etiketto.backends import register, unregister
from etiketto.backends.base import FoundPrinter, PaperJam, PrintResult
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
    assert err(capsys) == "etiketto: Cannot open .\n"


def test_list_printers_keeps_its_columns(mono, capsys, monkeypatch):
    from etiketto import backends

    for backend in backends.backends():
        if backend is not mono:
            monkeypatch.setattr(backend, "discover", lambda timeout=3.0: [])
    mono.found = [FoundPrinter("Tab\tName", "a.local", "192.0.2.1")]
    assert run("list-printers") == 0
    assert capsys.readouterr().out == "fake-mono\ta.local\tTab Name\n"
