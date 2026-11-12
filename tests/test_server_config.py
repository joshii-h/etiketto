"""printers.toml and tokens.toml: strict reading, roles, printers per token,
revocation, `etiketto token new`, and printers chosen by name."""

from __future__ import annotations

import datetime as dt
import os
import stat
import time

import pytest
from test_server import call, label_file, wait_for
from test_server_mcp import ACCEPT, request

from etiketto import cli
from etiketto.backends.base import PaperJam, PrinterInfo
from etiketto.model import TextElement
from etiketto.server import config as cfg
from etiketto.server import main as server_main
from etiketto.server.config import Caller, ConfigError, TokenStore, load_printers, load_tokens

PRINTERS = """
[printers.lager]
backend = "fake-mono"
host = "lager.local"
description = "Warehouse"
location = "Gate 2"
media = "12 mm tape"
default = true
max_copies = 3
mode = "standard"
safe_mm = 2.0
data_format = "jpeg"

[printers.office]
backend = "fake-mono"
host = "office.local"
"""


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def printers_file(tmp_path, server_env):
    return write(tmp_path / "printers.toml", PRINTERS)


def tokens_text(**entries):
    """entries: name -> (token, role, extra lines)."""
    parts = []
    for name, (token, role, extra) in entries.items():
        parts.append(
            '[tokens.%s]\nhash = "%s"\nrole = "%s"\n%s\n'
            % (name, cfg.hash_token(token), role, extra)
        )
    return "\n".join(parts)


TOKENS = {
    "viewer": ("etk_viewer-token-0123456789", "preview", ""),
    "terminal": ("etk_terminal-token-0123456789", "print", 'printers = ["lager"]'),
    "other": ("etk_other-token-0123456789", "print", ""),
    "boss": ("etk_boss-token-0123456789", "admin", ""),
}


def token(name):
    return TOKENS[name][0]


# --- printers.toml -----------------------------------------------------------------


def test_printers_file(printers_file):
    printers = load_printers(printers_file)
    lager = printers["lager"]
    assert lager.backend == "fake-mono" and lager.host == "lager.local" and lager.default
    assert lager.max_copies == 3 and lager.media == "12 mm tape"
    assert lager.settings == {
        "host": "lager.local",
        "mode": "standard",
        "safe_mm": 2.0,
        "data_format": "jpeg",
    }
    assert printers["office"].max_copies == 9999 and not printers["office"].default
    assert lager.to_dict()["location"] == "Gate 2"


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("", "printers: nothing defined"),
        ("[other]\n", "other: unknown key"),
        ("printers = 5\n", "printers: nothing defined"),
        ("[printers]\nx = 1\n", "printers.x: expected a table"),
        ('[printers."bad name"]\nbackend = "x"\n', "not a valid name"),
        ('[printers.a]\nhost = "h"\n', "printers.a.backend: missing"),
        ('[printers.a]\nbackend = "nope"\nhost = "h"\n', "Unknown printer model: nope"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "a b"\n', "not a valid printer address"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = 5\n', "printers.a.host: expected text"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\ncut = "full"\n', "does not cut"),
        ('[printers.a]\nbackend = "vc500w"\nhost = "h"\ncut = "x"\n', "full, half, none"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\ncolor = { red = 1 }\n', "no colour"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\nlocation = "a\\nb"\n', "control"),
        (
            '[printers.a]\nbackend = "fake-mono"\nhost = "h"\nmode = "x"\n',
            "expected one of: standard",
        ),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\nsafe_mm = 99\n', "expected a number"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\nmax_copies = 1.5\n', "max_copies"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\ndefault = "yes"\n', "true or false"),
        ('[printers.a]\nbackend = "vc500w"\nhost = "h"\ncolor = { red = 9 }\n', "color.red"),
        ('[printers.a]\nbackend = "vc500w"\nhost = "h"\ncolor = { pink = 1 }\n', "color.pink"),
        ('[printers.a]\nbackend = "fake-mono"\nhost = "h"\nspeed = 1\n', "a.speed: unknown key"),
        (
            '[printers.a]\nbackend = "fake-mono"\nhost = "h"\ndefault = true\n'
            '[printers.b]\nbackend = "fake-mono"\nhost = "h"\ndefault = true\n',
            "printers.b.default: only one",
        ),
        ("[printers.a\n", "Cannot read"),
    ],
)
def test_printers_file_errors(tmp_path, server_env, text, where):
    path = write(tmp_path / "printers.toml", text)
    with pytest.raises(ConfigError) as error:
        load_printers(path)
    assert where in str(error.value) and str(path) in str(error.value)


def test_unreadable_and_large_files(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_printers(tmp_path / "missing.toml")
    monkeypatch.setattr(cfg, "MAX_FILE", 10)
    with pytest.raises(ConfigError, match="1 MB"):
        load_printers(write(tmp_path / "big.toml", "#" * 20))
    with pytest.raises(ConfigError, match="Cannot read"):
        (tmp_path / "bin.toml").write_bytes(b"\xff\xfe")
        monkeypatch.setattr(cfg, "MAX_FILE", 100)
        load_printers(tmp_path / "bin.toml")


# --- tokens.toml ---------------------------------------------------------------------


def test_hashes():
    sha = cfg.hash_token("secret")
    assert sha.startswith("sha256:") and cfg.token_matches("secret", sha)
    assert not cfg.token_matches("other", sha)
    scrypt = cfg.hash_token("secret", "scrypt")
    assert scrypt.startswith("scrypt:16384:8:1:") and cfg.token_matches("secret", scrypt)
    assert not cfg.token_matches("other", scrypt)
    for bad in ("plain", "sha256:xyz", "scrypt:3:8:1:AA==:AA==", "scrypt:a:b:c:d:e", "md5:00"):
        assert not cfg.token_matches("secret", bad)
    token, ident = cfg.new_token()
    assert token.startswith("etk_%s_" % ident) and len(token) > 50
    assert cfg.TOKEN_ID.match(token).group(1) == ident


def test_tokens_file(tmp_path, printers_file):
    printers = load_printers(printers_file)
    text = tokens_text(**TOKENS) + (
        '\n[tokens.until]\nhash = "%s"\nrole = "print"\nexpires = 2030-01-02\ndescription = "x"\n'
        '\n[tokens.moment]\nhash = "%s"\nrole = "admin"\nexpires = 2030-01-02T08:00:00Z\n'
        % (cfg.hash_token("a"), cfg.hash_token("b", "scrypt"))
    )
    entries = {e.caller.name: e for e in load_tokens(write(tmp_path / "t.toml", text), printers)}
    assert entries["terminal"].caller == Caller("terminal", "print", frozenset({"lager"}))
    assert entries["until"].caller.role == "print"
    assert entries["until"].expires.date() == dt.date(2030, 1, 3)  # through the whole day
    assert entries["moment"].expires.tzinfo is not None


@pytest.mark.parametrize(
    ("entry", "where"),
    [
        ('role = "print"\n', "hash: missing"),
        ('hash = "plaintext"\n', "not a token hash"),
        ('hash = "%s"\nrole = "root"\n', "expected one of: preview, print, admin"),
        ('hash = "%s"\nprinters = "lager"\n', "expected a list of printer names"),
        ('hash = "%s"\nprinters = ["nope"]\n', "Unknown printer: nope"),
        ('hash = "%s"\nexpires = "soon"\n', "expected a date"),
        ('hash = "%s"\ntoken = "x"\n', "unknown key"),
    ],
)
def test_tokens_file_errors(tmp_path, printers_file, entry, where):
    text = "[tokens.a]\n" + ("" if "role" in entry else 'role = "print"\n')
    text += entry % cfg.hash_token("x") if "%s" in entry else entry
    with pytest.raises(ConfigError, match=where):
        load_tokens(write(tmp_path / "t.toml", text), load_printers(printers_file))


def test_printers_of_a_token_need_a_printers_file(tmp_path):
    text = '[tokens.a]\nrole = "print"\nhash = "%s"\nprinters = ["x"]\n' % cfg.hash_token("x")
    with pytest.raises(ConfigError, match="--printers"):
        load_tokens(write(tmp_path / "t.toml", text), None)


def test_roles():
    viewer, printer, admin = (Caller("x", role) for role in ("preview", "print", "admin"))
    assert viewer.may("preview") and not viewer.may("print")
    assert printer.may("print") and not printer.may("admin") and admin.may("admin")
    assert Caller("x", printers=frozenset({"a"})).may_use("a")
    assert not Caller("x", printers=frozenset({"a"})).may_use("b")


def test_token_store_expiry_revocation_and_broken_files(tmp_path, caplog, printers_file):
    path = write(tmp_path / "t.toml", tokens_text(**TOKENS))
    store = TokenStore(path, load_printers(printers_file))
    assert store.find(token("boss")).role == "admin"
    assert store.find(token("boss")).role == "admin"  # remembered
    assert store.find("etk_wrong") is None
    # revoking: remove the entry; the file is read again when it changes
    rest = {k: v for k, v in TOKENS.items() if k != "boss"}
    write(path, tokens_text(**rest))
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert store.find(token("boss")) is None
    assert store.find(token("viewer")).name == "viewer"
    # a broken file lets nobody in until it is fixed
    write(path, "[tokens.x\n")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
    assert store.find(token("viewer")) is None
    assert "no token is valid" in caplog.text
    write(path, tokens_text(**rest))
    store.reload()
    assert store.find(token("viewer")) is not None
    # expired
    late = '[tokens.late]\nrole = "print"\nhash = "%s"\nexpires = 2020-01-01\n' % cfg.hash_token(
        "late"
    )
    write(path, late)
    store.reload()
    assert store.find("late") is None
    now = dt.datetime(2019, 6, 1).astimezone()
    assert store.find("late", now).name == "late"
    path.unlink()
    assert store.find("late") is None


# --- the HTTP API with tokens and printers -----------------------------------------------


@pytest.fixture
def secured(make_server, server_env, tmp_path, printers_file):
    printers = load_printers(printers_file)
    tokens_file = write(tmp_path / "tokens.toml", tokens_text(**TOKENS))
    store = TokenStore(tokens_file, printers)
    server, base = make_server(printers=printers, tokens=store)
    return base


def test_roles_over_http(secured, server_env):
    viewer, terminal, other, boss = (token(n) for n in ("viewer", "terminal", "other", "boss"))
    assert call(secured, "GET", "/api/v1/labels", token=viewer)[0] == 200
    assert call(secured, "POST", "/api/v1/preview", body={"label": "box"}, token=viewer)[0] == 200
    status, _r, body = call(secured, "POST", "/api/v1/print", body={"label": "box"}, token=viewer)
    assert status == 403 and body["error"]["code"] == "forbidden"
    status, _r, job = call(
        secured, "POST", "/api/v1/print", body={"label": "box", "wait": 10}, token=terminal
    )
    assert status == 200 and job["owner"] == "terminal" and job["printer_name"] == "lager"
    path = "/api/v1/jobs/" + job["id"]
    assert call(secured, "GET", path, token=terminal)[0] == 200
    assert call(secured, "GET", path, token=other)[0] == 404  # not theirs
    assert call(secured, "GET", path, token=viewer)[0] == 403
    assert call(secured, "GET", path, token=boss)[2]["owner"] == "terminal"
    assert call(secured, "GET", "/api/v1/labels", token="etk_nope")[0] == 401
    assert call(secured, "GET", "/api/v1/labels")[0] == 401


def test_admin_cancels_any_job(secured, server_env):
    printer = server_env.printer
    printer.gate = __import__("threading").Event()
    body = {"label": "box", "printer": "office"}
    _s, _r, first = call(secured, "POST", "/api/v1/print", body=body, token=token("other"))
    assert printer.started.wait(10)
    _s, _r, second = call(secured, "POST", "/api/v1/print", body=body, token=token("other"))
    path = "/api/v1/jobs/" + second["id"]
    assert call(secured, "DELETE", path, token=token("terminal"))[0] == 404
    status, _r, canceled = call(secured, "DELETE", path, token=token("boss"))
    assert status == 202 and canceled["state"] == "canceled"
    printer.gate.set()
    wait_for(
        lambda: (
            call(secured, "GET", "/api/v1/jobs/" + first["id"], token=token("boss"))[2]["state"]
            == "done"
        )
    )


def test_printers_of_a_token(secured, server_env):
    terminal = token("terminal")
    body = {"label": "box", "printer": "office"}
    status, _r, answer = call(secured, "POST", "/api/v1/print", body=body, token=terminal)
    assert status == 404 and answer["error"]["code"] == "printer_not_found"  # not even named
    status, _r, answer = call(secured, "GET", "/api/v1/printers", token=terminal)
    assert [p["name"] for p in answer["printers"]] == ["lager"]
    assert call(secured, "GET", "/api/v1/printers/office/status", token=terminal)[0] == 404
    assert server_env.printer.jobs == []


def test_named_printers_list_and_status(secured, server_env, monkeypatch):
    from etiketto.server import service as service_module

    monkeypatch.setattr(service_module, "STATUS_SECONDS", 0)  # asked anew every time
    boss = token("boss")
    server_env.printer.info = PrinterInfo("Fake", 12, 900, 1000, "IDLE")
    status, _r, body = call(secured, "GET", "/api/v1/printers", token=boss)
    assert status == 200
    lager = body["printers"][0]
    assert lager["name"] == "lager" and lager["printer"] == "fake-mono"
    assert lager["host"] == "lager.local" and lager["location"] == "Gate 2"
    assert lager["status"]["state"] == "IDLE" and lager["model"] == "Fake Mono 200"
    assert sorted(server_env.printer.queries) == ["lager.local", "office.local"]
    _s, _r, body = call(secured, "GET", "/api/v1/printers?status=false", token=boss)
    assert body["printers"][0]["status"] is None
    server_env.printer.error = PaperJam("jam")
    _s, _r, body = call(secured, "GET", "/api/v1/printers", token=boss)
    assert body["printers"][0]["status"]["state"] == "UNREACHABLE"
    assert body["printers"][0]["status"]["error"]["code"] == "printer_jam"
    server_env.printer.error = None
    status, _r, body = call(secured, "GET", "/api/v1/printers/office/status", token=boss)
    assert status == 200 and body["name"] == "office" and body["host"] == "office.local"
    status, _r, body = call(
        secured, "GET", "/api/v1/printers/office/status?host=x.local", token=boss
    )
    assert status == 400 and body["error"]["code"] == "host_given"
    status, _r, body = call(secured, "GET", "/api/v1/printers/nope/status", token=boss)
    assert status == 404 and body["error"]["code"] == "printer_not_found"


def test_print_by_name(secured, server_env):
    boss = token("boss")
    status, _r, job = call(
        secured, "POST", "/api/v1/print", body={"label": "box", "wait": 10}, token=boss
    )
    assert job["printer_name"] == "lager" and job["host"] == "lager.local"
    _image, host, options = server_env.printer.jobs[-1]
    assert host == "lager.local" and options.mode == "standard"
    assert options.mode == "standard"  # the printer's own settings
    body = {"label": "box", "printer": "office", "wait": 10}
    _s, _r, job = call(secured, "POST", "/api/v1/print", body=body, token=boss)
    assert job["host"] == "office.local"
    body = {"label": "box", "copies": 4}
    status, _r, answer = call(secured, "POST", "/api/v1/print", body=body, token=boss)
    assert status == 400 and answer["error"]["code"] == "too_many_copies"
    body = {"label": "box", "host": "elsewhere.local"}
    status, _r, answer = call(secured, "POST", "/api/v1/print", body=body, token=boss)
    assert status == 400 and answer["error"]["code"] == "host_given"
    body = {"label": "box", "printer": "fake-mono"}
    status, _r, answer = call(secured, "POST", "/api/v1/print", body=body, token=boss)
    assert status == 404 and answer["error"]["code"] == "printer_not_found"
    status, _r, _png = call(
        secured, "POST", "/api/v1/preview", body={"label": "box", "printer": "office"}, token=boss
    )
    assert status == 200


def test_printing_needs_a_chosen_printer(make_server, server_env, tmp_path):
    two = PRINTERS.replace("default = true\n", "")
    printers = load_printers(write(tmp_path / "p.toml", two))
    _server, base = make_server(printers=printers)
    status, _r, body = call(base, "POST", "/api/v1/print", body={"label": "box"})
    assert status == 400 and body["error"]["code"] == "no_printer"
    assert call(base, "POST", "/api/v1/preview", body={"label": "box"})[0] == 200
    one = load_printers(
        write(tmp_path / "one.toml", '[printers.only]\nbackend = "fake-mono"\nhost = "o.local"\n')
    )
    _server, base = make_server(printers=one)
    _s, _r, job = call(base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["printer_name"] == "only"


def test_single_token_is_admin(make_server, server_env):
    _server, base = make_server(token="single-token-for-tests")
    status, _r, job = call(
        base,
        "POST",
        "/api/v1/print",
        body={"label": "box", "wait": 10},
        token="single-token-for-tests",
    )
    assert job["owner"] == "token"


# --- MCP with tokens ----------------------------------------------------------------


def post(base, message, token_value, headers=None):
    return call(
        base,
        "POST",
        "/mcp",
        body=message,
        headers=dict(ACCEPT, **(headers or {})),
        token=token_value,
    )


def test_mcp_tools_follow_the_role(secured):
    init = request("initialize", {"protocolVersion": "2025-11-25"})
    status, response, _a = post(secured, init, token("viewer"))
    session = {"Mcp-Session-Id": response.getheader("Mcp-Session-Id")}
    _s, _r, answer = post(secured, request("tools/list", ident=2), token("viewer"), session)
    names = {t["name"] for t in answer["result"]["tools"]}
    assert "print_label" not in names and "preview_label" in names
    call_print = request("tools/call", {"name": "print_label", "arguments": {"label": "box"}}, 3)
    _s, _r, answer = post(secured, call_print, token("viewer"), session)
    assert answer["result"]["isError"]
    assert answer["result"]["structuredContent"]["error"]["code"] == "forbidden"
    # a session belongs to the token that opened it
    assert post(secured, request("ping", ident=4), token("boss"), session)[0] == 404
    status, _r, _b = call(secured, "DELETE", "/mcp", headers=session, token=token("boss"))
    assert status == 404
    assert call(secured, "DELETE", "/mcp", headers=session, token=token("viewer"))[0] == 204


def test_mcp_print_by_name(secured, server_env):
    init = request("initialize", {"protocolVersion": "2025-11-25"})
    _s, response, _a = post(secured, init, token("terminal"))
    session = {"Mcp-Session-Id": response.getheader("Mcp-Session-Id")}
    arguments = {"label": "box", "printer": "lager"}
    _s, _r, answer = post(
        secured,
        request("tools/call", {"name": "print_label", "arguments": arguments}, 2),
        token("terminal"),
        session,
    )
    job = answer["result"]["structuredContent"]
    assert job["state"] == "done" and job["owner"] == "terminal"
    listing = request("tools/call", {"name": "list_printers", "arguments": {"status": False}}, 3)
    _s, _r, answer = post(secured, listing, token("terminal"), session)
    assert [p["name"] for p in answer["result"]["structuredContent"]["printers"]] == ["lager"]


# --- the commands ---------------------------------------------------------------------


def run(*argv):
    return cli.main([str(a) for a in argv])


def test_token_new(tmp_path, capsys, printers_file):
    path = tmp_path / "conf" / "tokens.toml"
    assert run("token", "new", "terminal", "--tokens", path) == cli.EXIT_OK
    out = capsys.readouterr()
    first = out.out.strip()
    assert first.startswith("etk_") and "only this once" in out.err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert first not in path.read_text()
    assert TokenStore(path).find(first).role == "print"
    code = run(
        "token",
        "new",
        "boss",
        "--tokens",
        path,
        "--role",
        "admin",
        "--expires",
        "2030-01-01",
        "--description",
        'Chef "B"',
        "--scrypt",
    )
    assert code == cli.EXIT_OK
    second = capsys.readouterr().out.strip()
    store = TokenStore(path)
    assert store.find(second).role == "admin" and store.find(first).name == "terminal"
    assert "scrypt:" in path.read_text()
    assert run("token", "new", "boss", "--tokens", path) == cli.EXIT_USAGE
    assert "exists already" in capsys.readouterr().err
    assert run("token", "new", "bad name", "--tokens", path) == cli.EXIT_USAGE
    assert run("token", "new", "x", "--tokens", path, "--expires", "soon") == cli.EXIT_USAGE
    assert run("token", "new", "x", "--tokens", path, "--printer", "lager") == cli.EXIT_FILE
    assert "--printers" in capsys.readouterr().err
    code = run(
        "token",
        "new",
        "gate",
        "--tokens",
        path,
        "--printer",
        "lager",
        "--printers",
        printers_file,
        "--expires",
        "2030-01-01T08:00",
    )
    assert code == cli.EXIT_OK
    gate = capsys.readouterr().out.strip()
    assert TokenStore(path, load_printers(printers_file)).find(gate).printers == {"lager"}
    assert (
        run("token", "new", "y", "--tokens", path, "--printers", tmp_path / "none")
        == cli.EXIT_FILE
    )


def test_token_new_refuses_a_broken_file(tmp_path, capsys):
    path = write(tmp_path / "tokens.toml", "[tokens.x\n")
    assert run("token", "new", "a", "--tokens", path) == cli.EXIT_FILE
    assert path.read_text() == "[tokens.x\n"


def test_default_tokens_file():
    assert server_main.default_tokens_file().parts[-2:] == ("etiketto", "tokens.toml")


def test_serve_options(monkeypatch, tmp_path, capsys, server_env, printers_file):
    started = {}

    def serve_forever(self):
        started["server"] = self
        raise KeyboardInterrupt

    from etiketto.server import http as server_http

    handlers = {}
    monkeypatch.setattr(server_http.Server, "serve_forever", serve_forever)
    monkeypatch.setattr(
        server_main.signal, "signal", lambda number, handler: handlers.setdefault(number, handler)
    )
    monkeypatch.delenv("ETIKETTO_TOKEN", raising=False)
    tokens = write(tmp_path / "tokens.toml", tokens_text(**TOKENS))
    labels = tmp_path / "labels"
    labels.mkdir(exist_ok=True)
    base = ["serve", "--port", "0", "--labels-dir", labels]
    assert run(*base, "--printers", printers_file, "--allow-printer", "*") == cli.EXIT_USAGE
    assert run(*base, "--tokens", tokens, "--token-file", tokens) == cli.EXIT_USAGE
    assert run(*base, "--printers", write(tmp_path / "bad.toml", "x = 1")) == cli.EXIT_FILE
    assert "unknown key" in capsys.readouterr().err
    # named tokens allow other addresses; the server here binds 127.0.0.1 anyway
    code = run(*base, "--printers", printers_file, "--tokens", tokens, "--discover")
    assert code == cli.EXIT_OK
    server = started["server"]
    assert server.config.tokens is not None and server.service.named and server.service.discovery
    handlers[server_main.signal.SIGHUP](1, None)  # reads tokens.toml again


def test_mcp_command_takes_a_printers_file(monkeypatch, tmp_path, server_env, printers_file):
    seen = {}
    from etiketto.server import mcp as mcp_module

    monkeypatch.setattr(
        mcp_module, "run_stdio", lambda service: seen.setdefault("s", service) and 0
    )
    labels = tmp_path / "labels"
    labels.mkdir(exist_ok=True)
    assert run("mcp", "--labels-dir", labels, "--printers", printers_file) == 0
    assert set(seen["s"].named) == {"lager", "office"}


def test_jobs_are_logged_with_the_token_name(secured, caplog, server_env):
    import logging

    caplog.set_level(logging.INFO)
    label_file(server_env.labels, "named.etiketto", TextElement(text="x"))
    call(
        secured,
        "POST",
        "/api/v1/print",
        body={"label": "named", "wait": 10},
        token=token("terminal"),
    )
    assert "by terminal" in caplog.text
    assert token("terminal") not in caplog.text


def test_example_printers_file_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "packaging/docker/printers.example.toml"
    printers = load_printers(example)
    assert set(printers) == {"shipping", "office", "kitchen"}
    assert printers["shipping"].default and printers["kitchen"].settings["mode"] == "vivid"
