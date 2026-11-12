"""The sixth review of the print server: a test for each finding (the
renderer's are in test_render.py)."""

from __future__ import annotations

import io
import os
import sys

import pytest
from test_server_config import write

from etiketto import cli
from etiketto.server import config as cfg
from etiketto.server.config import TokenStore
from etiketto.server.mcp import Mcp
from etiketto.server.service import Service, pattern_problem
from etiketto.ui.settings import Settings


def run(*argv):
    return cli.main([str(a) for a in argv])


# --- N2: MCP tools run without the slot of the body ---------------------------------------


def _held(service):
    return getattr(service._held, "count", 0)


def test_mcp_tools_give_the_slot_of_a_large_body_back(server_env, monkeypatch):
    service = Service(server_env.labels)
    mcp = Mcp(service)
    seen = []
    monkeypatch.setattr(service, "printers", lambda *a: seen.append(_held(service)) or [])
    monkeypatch.setattr(service, "status", lambda *a: seen.append(_held(service)) or {})
    with service.render_slot():  # as for a body over 512 KB
        mcp.call({"name": "list_printers", "arguments": {}})
        mcp.call({"name": "printer_status", "arguments": {"printer": "fake-mono"}})
        assert _held(service) == 0
    assert seen == [0, 0]
    service.close(timeout=1)


def test_mcp_print_looks_up_the_printer_outside_the_slot(server_env, monkeypatch):
    settings = {"printers": {"fake-mono": {"host": "mono.local"}}}
    service = Service(server_env.labels, settings=lambda: Settings(dict(settings)))
    mcp = Mcp(service)
    held = []
    real = service.queue_print
    monkeypatch.setattr(service, "queue_print", lambda *a: held.append(_held(service)) or real(*a))
    answer = mcp.call(
        {"name": "print_label", "arguments": {"label": "box", "printer": "fake-mono", "wait": 10}}
    )
    assert held == [0] and not answer.get("isError"), answer
    service.close(timeout=1)


# --- N5: address patterns -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "bad"),
    [
        ("1.2.3", True),
        ("10", True),
        ("010.1.1.1", True),
        ("10*", True),
        ("**", True),
        ("999.1.1.*", True),
        ("1.2.3.4.5", True),
        ("10.*", False),
        ("*.5", False),
        ("*.*", False),
        ("*", False),
        ("192.168.1.5", False),
        ("0.0.0.0", False),
        ("printer.local", False),
        ("*.local", False),
    ],
)
def test_address_patterns_that_can_never_match_are_refused(pattern, bad):
    assert pattern_problem(pattern) is bad


# --- N6, N12: one dummy check of the kind the id entries have --------------------------------


def _store(tmp_path, kinds):
    text = ""
    for n, kind in enumerate(kinds):
        made, ident = cfg.new_token()
        text += cfg.token_entry("t%d" % n, cfg.hash_token(made, kind), "print", [], "", "", ident)
    text += cfg.token_entry(
        "chosen", cfg.hash_token("a-chosen-token-123", "scrypt"), "print", [], "", ""
    )
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    return TokenStore(path)


@pytest.mark.parametrize(
    ("kinds", "dummy"),
    [
        (["scrypt", "scrypt", "sha256"], "scrypt:"),
        (["sha256", "sha256", "scrypt"], "sha256:"),
        ([], "sha256:"),
    ],
)
def test_an_unknown_id_costs_one_check_of_the_usual_kind(tmp_path, monkeypatch, kinds, dummy):
    store = _store(tmp_path, kinds)
    checked = []
    real = cfg.token_matches
    monkeypatch.setattr(cfg, "token_matches", lambda t, h: checked.append(h) or real(t, h))
    made = []
    monkeypatch.setattr(cfg, "hash_token", lambda *a: made.append(a) or "")
    assert store.find("etk_%s_%s" % ("a" * 12, "z" * 43)) is None
    assert len(checked) == 1 and checked[0].startswith(dummy)
    assert made == []  # the dummy was made when the file was read, not now


# --- chosen tokens -----------------------------------------------------------------------------


def test_a_chosen_token_must_not_look_like_a_made_one(tmp_path, capsys, monkeypatch):
    path = tmp_path / "tokens.toml"
    chosen = "etk_%s_%s" % ("1" * 12, "q" * 43)
    monkeypatch.setattr(sys, "stdin", io.StringIO(chosen + "\n"))
    assert run("token", "new", "a", "--tokens", path, "--from-stdin") == cli.EXIT_USAGE
    assert "must not look like a made one" in capsys.readouterr().err
    assert not path.exists() or chosen not in path.read_text()
