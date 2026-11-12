"""The review of release 1.1.0, the server and cab part: a test for each
finding."""

from __future__ import annotations

import base64
import io
import textwrap
import time

import pytest
from PIL import Image
from test_server import call, label_file, wait_for
from test_server_config import PRINTERS, write

from tavletta import cli, freelayout
from tavletta.backends import cab, get_backend, niimbot
from tavletta.backends.base import PrinterInfo, PrintOptions
from tavletta.job import preview_image
from tavletta.model import ImageElement, Label, PageElement, TextElement
from tavletta.render import _color
from tavletta.server import config as cfg
from tavletta.server import main as server_main
from tavletta.server import service as service_module
from tavletta.server.config import TokenStore, load_printers
from tavletta.server.requests import LabelRequest
from tavletta.server.service import Service
from tavletta.shipping import source
from tavletta.ui.settings import Settings


def _png(color, size=(40, 10)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _dark_label(labels) -> None:
    """Black text on a dark background: it does not show in black and white."""
    label_file(
        labels,
        "dark.tavletta",
        TextElement(text="x", color="#000000"),
        background_image=ImageElement(data=_png((15, 15, 15))),
    )


# --- M1: the default preview is the window's preview --------------------------------


def test_default_preview_is_the_one_render_path(make_server, server_env):
    _server, base = make_server()
    _dark_label(server_env.labels)
    status, _r, body = call(
        base, "POST", "/api/v1/preview", body={"label": "dark", "format": "json"}
    )
    assert status == 200
    # rendered in black and white, as printed: the text does not show
    assert "no_contrast" in [w["code"] for w in body["warnings"]]
    image = Image.open(io.BytesIO(base64.b64decode(body["png"])))
    label = Label.load(server_env.labels / "dark.tavletta")
    printer = server_env.printer
    options = Settings({}).printer(printer).options(None, 1, label.gap_mm)
    _rendered, expected, _dpi = preview_image(printer, label, options)
    assert image.size == expected.size
    assert image.convert("RGB").tobytes() == expected.convert("RGB").tobytes()


def test_printer_view_tells_what_the_print_render_finds(make_server, server_env):
    _server, base = make_server()
    _dark_label(server_env.labels)
    status, _r, body = call(
        base,
        "POST",
        "/api/v1/preview",
        body={"label": "dark", "format": "json", "view": "printer"},
    )
    assert status == 200 and "no_contrast" in [w["code"] for w in body["warnings"]]


def test_a_preview_that_cannot_be_drawn_is_a_422(make_server, server_env, monkeypatch):
    _server, base = make_server()

    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot draw")

    monkeypatch.setattr(server_env.printer, "render_label", broken)
    status, _r, body = call(base, "POST", "/api/v1/preview", body={"label": "box"})
    assert status == 422 and body["error"]["code"] == "prepare_failed"


# --- M2: printers.toml settings of the die-cut QL ---------------------------------


def test_printer_settings_count_for_the_die_cut_ql(server_env):
    path = write(
        server_env.tmp / "ql.toml",
        textwrap.dedent(
            """
            [printers.ql]
            backend = "brother_ql_diecut"
            host = "ql.local"
            safe_mm = 2.5
            cut = "none"

            [printers.ql.settings]
            quality = "high"
            """
        ),
    )
    label_file(
        server_env.labels, "dc.tavletta", TextElement(text="x"), printer="brother_ql_diecut"
    )
    service = Service(
        server_env.labels, printers=load_printers(path), settings=lambda: Settings({})
    )
    try:
        prepared = service.prepare(LabelRequest(label="dc", printer="ql"))
        assert prepared.printer.safe_mm == 2.5
        assert prepared.options.cut == "none" and prepared.options.quality == "high"
    finally:
        service.close(timeout=5)


# --- M4: the printer is asked before rendering -----------------------------------


@pytest.fixture
def cab_600(monkeypatch, server_env):
    """The cab backend as a 600 dpi printer that answers; what it printed."""
    backend = get_backend("cab")
    monkeypatch.setattr(backend, "reported", {})
    printed = []

    def query(host):
        return PrinterInfo("EOS2/600", None, None, None, "IDLE", dpi=cab.dpi_for(23.6))

    def print_label(image, host, options, progress=None):
        printed.append((image.info["dots_per_mm"], image.size, host, options))

    monkeypatch.setattr(backend, "query", query)
    monkeypatch.setattr(backend, "print_label", print_label)
    label = Label(printer="cab", auto_length=False, elements=[TextElement(text="x")])
    label.set_media_size(50, 25)
    label.save(server_env.labels / "cab.tavletta")
    return printed


def _print_and_wait(base, body):
    status, _r, job = call(base, "POST", "/api/v1/print", body=body)
    assert status == 202, job
    path = "/api/v1/jobs/" + job["id"]
    wait_for(lambda: call(base, "GET", path)[2]["state"] in ("done", "failed"))
    return call(base, "GET", path)[2]


def test_the_first_job_renders_for_the_printer_asked(make_server, cab_600):
    _server, base = make_server(settings={"printers": {"cab": {"host": "cab.local"}}})
    job = _print_and_wait(base, {"label": "cab"})
    assert job["state"] == "done", job
    dots, size, host, options = cab_600[0]
    assert dots == 23.6 and size == (1180, 590) and host == "cab.local"
    assert options.host == "cab.local" and options.info.model == "EOS2/600"


def test_a_named_printer_is_asked_through_its_probe(make_server, cab_600, server_env):
    path = write(
        server_env.tmp / "cab.toml", '[printers.c]\nbackend = "cab"\nhost = "cab.local"\n'
    )
    _server, base = make_server(printers=load_printers(path))
    job = _print_and_wait(base, {"label": "cab", "printer": "c"})
    assert job["state"] == "done", job
    assert cab_600[0][0] == 23.6


def test_a_printer_that_does_not_answer_prints_with_what_is_known(
    make_server, cab_600, monkeypatch
):
    def silent(host):
        raise OSError("no answer")

    monkeypatch.setattr(get_backend("cab"), "query", silent)
    _server, base = make_server(settings={"printers": {"cab": {"host": "cab.local"}}})
    assert _print_and_wait(base, {"label": "cab"})["state"] == "done"
    assert cab_600[0][0] == 12.0  # the default resolution


def test_cab_resolutions_are_kept_per_printer():
    backend = cab.Cab()
    backend._learn("a", 23.6)
    backend._learn("b", 8.0)
    assert backend.raster(25, PrintOptions(host="a")).dpi == pytest.approx(cab.dpi_for(23.6))
    assert backend.raster(25, PrintOptions(host="b")).dpi == pytest.approx(cab.dpi_for(8.0))
    assert backend.raster(25, PrintOptions(host="c")).dpi == pytest.approx(cab.dpi_for(12.0))
    assert backend.raster(25, PrintOptions()).dpi == pytest.approx(cab.dpi_for(8.0))  # last


def test_niimbot_models_are_kept_per_printer():
    backend = niimbot.Niimbot()
    models = list(niimbot.BY_NAME.values())
    first = next(m for m in models if m.dpi != models[-1].dpi)
    backend._learn("a", first)
    backend._learn("b", models[-1])
    assert backend.raster(12, PrintOptions(host="a")).dpi == first.dpi
    assert backend.raster(12, PrintOptions(host="b")).dpi == models[-1].dpi
    assert backend.raster(12, PrintOptions(host="c")).dpi == backend.capabilities.dpi
    assert backend.raster(12, PrintOptions()).dpi == models[-1].dpi  # the last asked


# --- M5: a page that cannot be drawn is not printed ----------------------------------


@pytest.mark.parametrize("missing", [False, True])
def test_unreadable_pages_are_refused(make_server, server_env, monkeypatch, missing):
    if missing:

        def no_pdf(*_args, **_kwargs):
            raise source.SourceError("no_pdf")

        monkeypatch.setattr(source, "draw", no_pdf)
    code = "pdf_missing" if missing else "page_unreadable"
    assert code in cli.REFUSED
    data = base64.b64encode(b"junk").decode()
    label_file(server_env.labels, "page.tavletta", PageElement(data=data))
    _server, base = make_server()
    status, _r, body = call(base, "POST", "/api/v1/print", body={"label": "page"})
    assert status == 422 and body["error"]["code"] == "label_unprintable"
    assert server_env.printer.jobs == []


# --- L1, L2: the render wait is read when used ----------------------------------------


def test_render_wait_is_read_when_a_slot_is_taken(server_env, monkeypatch):
    service = Service(server_env.labels, settings=lambda: Settings({}))
    monkeypatch.setattr(service_module, "RENDER_WAIT", 0.05)
    for _n in range(service_module.RENDERS):
        service._renders.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(service_module.ApiError), service.render_slot():
            pass
        assert time.monotonic() - started < 2
    finally:
        for _n in range(service_module.RENDERS):
            service._renders.release()
        service.close(timeout=5)


# --- L4: cab sends the label's own size --------------------------------------------


def test_cab_sends_the_label_size_in_mm():
    image = Image.new("RGB", (480, 223), "white")  # 40 x 18.6 mm at 12 dots/mm
    job = cab.build_job(image, 12.0, 4.0, 1, size_mm=(40, 18.6))
    assert b"S l1;0,0,18.6,22.6,40\r\n" in job
    # a size the image does not have (more than a dot off): the image's
    job = cab.build_job(image, 12.0, 4.0, 1, size_mm=(41, 18.6))
    assert b"S l1;0,0,18.6,22.6,40\r\n" in job


@pytest.mark.parametrize("orientation", ["across", "along"])
def test_cab_prepare_passes_the_label_size(orientation):
    label = Label(printer="cab", auto_length=False, orientation=orientation)
    label.set_media_size(40, 18.6)
    image = cab.Cab().prepare(label, PrintOptions())
    assert image.size == (480, 223) and image.info["size_mm"] == (40, 18.6)


# --- L5: the web interface is read once per job -------------------------------------


def test_cab_web_setup_is_kept_for_a_while(monkeypatch):
    asked = []
    now = [100.0]
    monkeypatch.setattr(cab, "_fetch_setup", lambda host: asked.append(host))
    monkeypatch.setattr(cab, "_clock", lambda: now[0])
    for _n in range(5):
        cab.web_setup("h")
    assert asked == ["h"]
    cab.web_setup("other")
    now[0] += cab.SETUP_SECONDS + 1
    cab.web_setup("h")
    cab.web_setup("h", fresh=True)
    assert asked == ["h", "other", "h", "h"]


# --- L8: token new beside tokens limited to printers ---------------------------------


def test_token_new_beside_limited_tokens(tmp_path, capsys, server_env):
    printers = write(tmp_path / "printers.toml", PRINTERS)
    path = tmp_path / "tokens.toml"

    def run(*argv):
        return cli.main(["token", "new", *map(str, argv), "--tokens", str(path)])

    assert run("gate", "--printer", "lager", "--printers", printers) == cli.EXIT_OK
    assert run("terminal") == cli.EXIT_OK  # no printers.toml: the others are not checked
    token = capsys.readouterr().out.split()[-1]
    store = TokenStore(path, load_printers(printers))
    assert store.find(token).name == "terminal"
    # a new entry's printers are checked: without printers.toml they cannot be
    assert run("x", "--printer", "lager") == cli.EXIT_FILE
    assert "--printers" in capsys.readouterr().err
    assert run("y", "--printer", "nope", "--printers", printers) == cli.EXIT_FILE
    assert "nope" in capsys.readouterr().err


# --- nits ----------------------------------------------------------------------------


def test_new_tokens_carry_the_tavletta_prefix():
    token, ident = cfg.new_token()
    assert token.startswith("tav_%s_" % ident) and cfg.TOKEN_ID.match(token)
    assert not cfg.TOKEN_ID.match("etk_%s_%s" % (ident, "x" * 43))
    assert server_main  # the module that checks chosen tokens against the prefix


def test_the_docstring_example_of_printers_toml_loads(tmp_path):
    doc = cfg.__doc__ or ""
    example = doc.split("can be printed on:\n")[1].split("tokens.toml names")[0]
    printers = load_printers(write(tmp_path / "printers.toml", textwrap.dedent(example)))
    printer = printers["lager-cab"]
    assert printer.default and printer.settings["method"] == "transfer"


def test_free_layout_red_background_defaults_to_white():
    label = Label(tape_mm=12, background="no colour", elements=[])
    inks = freelayout._inks(label, True, True)
    assert not inks.behind_red
    assert _color(label.background, "#ffffff") == (255, 255, 255)


def test_print_method_help_says_printer_defaults_resets_it():
    command = cli.parser()._subparsers._group_actions[0].choices["print"]
    action = next(a for a in command._actions if "--print-method" in a.option_strings)
    assert action.help.endswith("with --printer-defaults the one set on the printer")
