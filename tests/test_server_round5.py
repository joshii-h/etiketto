"""The fifth review of the print server: a test for each finding (the
renderer's are in test_render.py, the window's in test_app.py)."""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from types import SimpleNamespace

import pytest
from test_server import call
from test_server_config import write

from etiketto import cli
from etiketto.model import ImageElement, Label
from etiketto.server import config as cfg
from etiketto.server import http as server_http
from etiketto.server import main as server_main
from etiketto.server import service as service_module
from etiketto.server.config import TokenStore
from etiketto.server.service import host_allowed, pattern_problem


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base, service=server.service)


# --- F3: address patterns -------------------------------------------------------------------


def test_prefix_wildcards_are_address_patterns(monkeypatch):
    monkeypatch.setattr(service_module, "_resolved", lambda name: [])
    assert host_allowed("10.1.2.3", ("10.*",)) == "10.1.2.3"
    assert host_allowed("192.168.7.9", ("192.168.*",)) == "192.168.7.9"
    assert host_allowed("10.evil.example", ("10.*",)) is None  # a name: by its addresses
    monkeypatch.setattr(service_module, "_resolved", lambda name: ["10.0.0.4"])
    assert host_allowed("printer.lan", ("10.*",)) == "10.0.0.4"


@pytest.mark.parametrize(
    ("pattern", "bad"),
    [
        ("999.1.1.*", True),
        ("1.2.3.4.5", True),
        ("10..1", True),
        ("10.0.0.0/33", True),
        ("2001:db8::/129", True),
        ("", True),
        ("10.*", False),
        ("10.0.0.0/8", False),
        ("*.local", False),
    ],
)
def test_patterns_that_cannot_be_meant(pattern, bad):
    assert pattern_problem(pattern) is bad


def test_serve_refuses_bad_patterns_and_strips_spaces(monkeypatch, tmp_path, server_env, capsys):
    labels = tmp_path / "served"
    labels.mkdir()
    code = run("serve", "--port", "0", "--labels-dir", labels, "--allow-printer", "999.1.1.*")
    assert code == cli.EXIT_USAGE and "999.1.1.*" in capsys.readouterr().err
    service = service_module.Service(labels, (" 10.0.0.0/8 ",))
    assert service.allowed_hosts == ("10.0.0.0/8",)
    service.close(timeout=1)


# --- F6, F15: tokens ---------------------------------------------------------------------------


def test_unknown_ids_cost_as_much_as_scrypt_ids(tmp_path, monkeypatch):
    made, ident = cfg.new_token()
    text = cfg.token_entry("made", cfg.hash_token(made, "scrypt"), "print", [], "", "", ident)
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    store = TokenStore(path)
    checked = []
    real = cfg.token_matches
    monkeypatch.setattr(cfg, "token_matches", lambda t, h: checked.append(h) or real(t, h))
    unknown = "etk_%s_%s" % ("f" * 12, "y" * 43)
    assert store.find(unknown) is None
    assert checked and checked[0].startswith("scrypt:")  # as slow as the known one
    checked.clear()
    assert store.find(unknown, cheap_only=True) is None
    assert checked == []  # a blocked address: no scrypt at all


def test_an_id_of_another_type_is_warned_about_too(tmp_path, caplog):
    text = '[tokens.a]\nrole = "print"\nhash = "%s"\nid = 5\n' % cfg.hash_token("a-token-123456")
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    assert TokenStore(path).find("a-token-123456").name == "a"
    assert "12 hexadecimal digits" in caplog.text


# --- F7: SIGHUP at once, readiness ------------------------------------------------------------


def test_the_launcher_ignores_sighup_before_anything_is_imported(monkeypatch):
    from etiketto import launch

    seen = []
    monkeypatch.setattr(
        launch.signal, "signal", lambda number, handler: seen.append((number, handler))
    )
    monkeypatch.setattr(sys, "argv", ["etiketto", "serve", "--help"])
    assert launch.main() == 0
    assert seen[0] == (signal.SIGHUP, signal.SIG_IGN)
    seen.clear()
    monkeypatch.setattr(sys, "argv", ["etiketto", "--version"])
    assert launch.main() == 0
    assert seen == []


def test_readiness_is_told_to_systemd(tmp_path, monkeypatch):
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        server_main.notify("READY=1")
        assert sock.recv(64) == b"READY=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    server_main.notify("READY=1")  # not under systemd: nothing
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone"))
    server_main.notify("READY=1")  # a socket that is gone: only a warning


def test_the_unit_waits_for_readiness():
    from pathlib import Path

    unit = (
        Path(__file__).resolve().parent.parent / "packaging/systemd/etiketto.service"
    ).read_text()
    assert "Type=notify" in unit and "NotifyAccess=main" in unit


# --- F8, F13: render slots -----------------------------------------------------------------


def test_the_printer_is_looked_up_outside_the_render_slot(api, monkeypatch):
    held = []
    real = api.service._print_host

    def print_host(request, prepared):
        held.append(getattr(api.service._held, "count", 0))
        return real(request, prepared)

    monkeypatch.setattr(api.service, "_print_host", print_host)
    status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert status == 200 and held == [0]


def test_mcp_waits_without_a_render_slot(api, server_env):
    service = api.service
    with service.render_slot():
        assert service._held.count == 1
        with service.slot_released():
            assert getattr(service._held, "count", 0) == 0
            other = threading.Thread(target=lambda: service.render_slot().__enter__())
            other.start()
            other.join(5)
        assert service._held.count == 1


# --- F11, F16, F19 --------------------------------------------------------------------------


def test_health_reserve():
    assert server_http.RESERVED >= 4 and server_http.RESERVED_SECONDS <= 1.0


def test_peers_are_logged_as_plain_ipv4():
    assert server_http._peer("::ffff:192.0.2.7") == "192.0.2.7"
    assert server_http._peer("not-an-address") == "not-an-address"


def test_the_general_limits_are_told_first(api, png_b64):
    images = [ImageElement(data=png_b64((4, 4))) for _n in range(service_module.MAX_ELEMENTS + 1)]
    content = Label(printer="fake-mono", tape_mm=12, elements=images).to_dict()
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label_content": content})
    assert status == 413 and "elements" in body["error"]["message"]
