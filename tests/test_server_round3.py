"""The third security review of the print server: a test for each finding
(the renderer's are in test_render.py)."""

from __future__ import annotations

import io
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from test_server import ASK, call
from test_server_config import PRINTERS, TOKENS, tokens_text, write
from test_server_mcp import ACCEPT, request

from etiketto import cli
from etiketto.backends.base import PaperJam
from etiketto.server import config as cfg
from etiketto.server import http as server_http
from etiketto.server import jobs as jobs_module
from etiketto.server import main as server_main
from etiketto.server import service as service_module
from etiketto.server.config import TokenStore, load_printers
from etiketto.server.service import host_allowed, with_address
from etiketto.ui import i18n


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base, port=int(base.rsplit(":", 1)[1]))


def raw(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        return sock.recv(4096)


# --- 4, 5: busy servers -------------------------------------------------------------------


def test_render_slots_time_out_with_503(api, monkeypatch):
    monkeypatch.setattr(service_module, "RENDER_WAIT", 0.1)
    renders = api.server.service._renders
    renders.acquire()
    renders.acquire()
    try:
        status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label": "box"})
        assert status == 503 and body["error"]["code"] == "busy"
    finally:
        renders.release()
        renders.release()


def test_health_is_answered_when_every_request_slot_is_taken(api):
    taken = 0
    while api.server.requests.acquire(blocking=False):
        taken += 1
    try:
        status, _r, body = call(api.base, "GET", "/api/v1/labels")
        assert status == 503 and body["error"]["code"] == "busy"
        assert call(api.base, "GET", "/api/v1/health")[0] == 200
    finally:
        for _n in range(taken):
            api.server.requests.release()


def test_no_thread_for_a_connection_is_a_503(api, monkeypatch):
    real = threading.Thread.start

    def start(self):
        if self.name.startswith("Thread"):  # the connection's thread
            raise RuntimeError("can't start new thread")
        return real(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    answer = raw(api.port, b"GET /api/v1/health HTTP/1.1\r\n\r\n")
    monkeypatch.setattr(threading.Thread, "start", real)
    assert answer.startswith(b"HTTP/1.0 503")
    assert jobs_module.MAX_QUEUES == 16


# --- 7, 14, 15, 18, 21: proxies and the rate limit --------------------------------------


def test_forwarded_for_is_read_carefully(make_server):
    server, base = make_server(
        token="single-token-for-tests", trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),)
    )
    port = int(base.rsplit(":", 1)[1])

    def client_of(*lines: str) -> str:
        headers = "".join("X-Forwarded-For: %s\r\n" % line for line in lines)
        raw(port, ("GET /api/v1/labels HTTP/1.1\r\n%s\r\n" % headers).encode())
        time.sleep(0.05)
        return next(reversed(server.rate_limit._failures))

    assert client_of("198.51.100.1", "198.51.100.2") == "198.51.100.2"  # lines joined
    assert client_of("198.51.100.3:5000") == "198.51.100.3"  # a port taken off
    assert client_of("[2001:db8::7]:443") == "2001:db8::/64"
    assert client_of("unknown, 198.51.100.4") == "198.51.100.4"
    assert client_of("198.51.100.5, garbage") == "127.0.0.1"  # nothing behind junk is trusted


def test_trusted_proxies_also_as_ipv4_mapped_addresses(make_server):
    server, _base = make_server(trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),))
    assert server.trusted("::ffff:127.0.0.1") and not server.trusted("::ffff:10.0.0.1")


def test_clients_behind_a_proxy_are_counted_one_by_one(make_server, monkeypatch):
    monkeypatch.setattr(server_http, "MAX_PER_CLIENT", 1)
    server, _base = make_server(trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),))
    with server.forwarded("198.51.100.1") as first, server.forwarded("198.51.100.1") as second:
        assert first and not second
        with server.forwarded("198.51.100.2") as other:
            assert other


def test_the_block_ends_a_minute_after_the_tenth(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server_http.time, "monotonic", lambda: clock[0])
    limit = server_http.RateLimit(limit=3, window=60)
    for _n in range(3):
        limit.failed("a")
        clock[0] += 20  # the third 40 s after the first
    clock[0] -= 20
    assert limit.blocked("a")
    clock[0] += 59  # a minute after the first, not yet after the third
    assert limit.blocked("a")
    clock[0] += 2
    assert not limit.blocked("a")
    for _n in range(3):  # slowly: never three within a minute
        limit.failed("b")
        clock[0] += 61
    assert not limit.blocked("b")


def test_link_local_addresses_are_counted_each():
    assert server_http.rate_key("fe80::1") == "fe80::1"
    assert server_http.rate_key("fe80::2") != server_http.rate_key("fe80::1")


def test_warning_for_trusting_everyone(monkeypatch, tmp_path, server_env, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        server_http.Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    monkeypatch.setattr(server_main.signal, "signal", lambda n, h: None)
    labels = tmp_path / "served"
    labels.mkdir()
    code = run("serve", "--port", "0", "--labels-dir", labels, "--trusted-proxy", "0.0.0.0/0")
    assert code == cli.EXIT_OK and "trusts every address" in caplog.text


# --- 9, 12, 13: --allow-printer -------------------------------------------------------------


def test_the_checked_address_is_printed_on(make_server, server_env, monkeypatch):
    monkeypatch.setattr(service_module, "_resolved", lambda name: ["192.0.2.9"])
    _server, base = make_server(allowed=("192.0.2.0/24",))
    body = {"label": "box", "host": "ipp://printer.lan:631/ipp/print", "wait": 10}
    status, _r, job = call(base, "POST", "/api/v1/print", body=body)
    assert status == 200
    assert server_env.printer.jobs[-1][1] == "ipp://192.0.2.9:631/ipp/print"
    assert with_address("printer.lan", "2001:db8::1") == "[2001:db8::1]"
    assert with_address("printer.lan:9100", "192.0.2.1") == "192.0.2.1:9100"


def test_every_address_must_match_some_pattern(monkeypatch):
    monkeypatch.setattr(service_module, "_resolved", lambda name: ["10.0.0.1", "192.168.1.1"])
    assert host_allowed("p.lan", ("10.0.0.0/8", "192.168.0.0/16")) == "10.0.0.1"
    assert host_allowed("p.lan", ("10.0.0.0/8",)) is None


def test_star_allows_everything_without_a_lookup(monkeypatch):
    def refuse(name):
        raise AssertionError("looked up")

    monkeypatch.setattr(service_module, "_resolved", refuse)
    assert host_allowed("anything.example", ("*",)) == "anything.example"


# --- 10, 11, 17, 23, 24, 29: tokens -----------------------------------------------------------


def test_entries_with_an_id_are_only_tried_for_their_token(tmp_path, monkeypatch):
    made, ident = cfg.new_token()
    text = cfg.token_entry("made", cfg.hash_token(made, "scrypt"), "print", [], "", "", ident)
    text += cfg.token_entry(
        "chosen", cfg.hash_token("a-chosen-token-123", "scrypt"), "print", [], "", ""
    )
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    store = TokenStore(path)
    checked = []
    real = cfg.token_matches
    monkeypatch.setattr(cfg, "token_matches", lambda t, h: checked.append(h) or real(t, h))
    assert store.find("a-chosen-token-123").name == "chosen"
    assert len(checked) == 1  # the entry with an id was not tried
    checked.clear()
    unknown = "etk_%s_%s" % ("0" * 12, "x" * 43)  # an id no entry has
    assert store.find(unknown) is None
    # a dummy as slow as the id entries (scrypt here), then the id-less one
    assert checked[0].startswith("scrypt:") and checked[0] not in (made, "") and len(checked) == 2
    checked.clear()
    assert store.find(made).name == "made" and len(checked) == 1


def test_a_stale_cache_entry_is_looked_up_again(tmp_path):
    path = write(tmp_path / "t.toml", tokens_text(viewer=TOKENS["viewer"]))
    os.chmod(path, 0o600)
    store = TokenStore(path)
    token = TOKENS["viewer"][0]
    assert store.find(token).role == "preview"
    write(path, tokens_text(viewer=(token, "admin", "")))
    store.reload()
    assert store.find(token, cheap_only=True).role == "admin"


def test_token_new_checks_the_file_mode_also_of_an_empty_file(tmp_path, capsys):
    path = tmp_path / "tokens.toml"
    path.write_text("")
    os.chmod(path, 0o666)
    assert run("token", "new", "a", "--tokens", path) == cli.EXIT_FILE
    assert "chmod 600" in capsys.readouterr().err


def test_a_chosen_token_already_in_the_file_is_refused(tmp_path, capsys, monkeypatch):
    path = tmp_path / "tokens.toml"
    for name in ("first", "second"):
        monkeypatch.setattr(sys, "stdin", io.StringIO("the-same-chosen-token-1\n"))
        code = run("token", "new", name, "--tokens", path, "--from-stdin")
    assert code == cli.EXIT_USAGE
    assert "in the file already, as first" in capsys.readouterr().err


def test_a_wrong_id_is_warned_about_and_the_token_still_works(tmp_path, caplog):
    text = '[tokens.a]\nrole = "print"\nhash = "%s"\nid = "XYZ"\n' % cfg.hash_token("a-token-123")
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    assert TokenStore(path).find("a-token-123").name == "a"
    assert "12 hexadecimal digits" in caplog.text


def test_a_chosen_token_is_asked_for_without_echo(tmp_path, capsys, monkeypatch):
    import getpass

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "typed-without-echo-123")
    path = tmp_path / "tokens.toml"
    assert run("token", "new", "typed", "--tokens", path, "--from-stdin") == cli.EXIT_OK
    assert "typed was added" in capsys.readouterr().err
    assert TokenStore(path).find("typed-without-echo-123").name == "typed"


# --- 19, 22: printer states ---------------------------------------------------------------


def test_status_of_one_printer_is_remembered_and_asked_once(
    make_server, server_env, tmp_path, monkeypatch
):
    printers = load_printers(write(tmp_path / "p.toml", PRINTERS))
    _server, base = make_server(printers=printers)
    real = server_env.printer.query

    def slow(host):
        time.sleep(0.2)
        return real(host)

    monkeypatch.setattr(server_env.printer, "query", slow)
    threads = [
        threading.Thread(
            target=call,
            args=(base, "GET", "/api/v1/printers/lager/status"),
            kwargs={"headers": ASK},
        )
        for _n in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    call(base, "GET", "/api/v1/printers", headers=ASK)
    assert server_env.printer.queries.count("lager.local") == 1


def test_remembered_printer_errors_speak_the_language_of_each_request(
    make_server, server_env, tmp_path, monkeypatch
):
    printers = load_printers(write(tmp_path / "p.toml", PRINTERS))
    _server, base = make_server(printers=printers)
    server_env.printer.error = PaperJam("jam")
    _s, _r, english = call(base, "GET", "/api/v1/printers/lager/status", headers=ASK)

    class Catalog(i18n.gettext.NullTranslations):
        def gettext(self, message):
            return "DE:" + message

    monkeypatch.setattr(i18n, "translation_for", lambda languages: Catalog())
    headers = dict(ASK, **{"Accept-Language": "de"})
    _s, _r, german = call(base, "GET", "/api/v1/printers/lager/status", headers=headers)
    assert german["error"]["message"].startswith("DE:")
    assert not english["error"]["message"].startswith("DE:")


# --- 20, 25, 26, 27: requests --------------------------------------------------------------


def test_large_mcp_bodies_are_parsed_in_a_render_slot(make_server, monkeypatch):
    server, base = make_server()
    monkeypatch.setattr(server_http, "LARGE_BODY", 10)
    held = []
    from etiketto.server import mcp as mcp_module

    real = mcp_module.HttpTransport.post

    def post(self, headers, body, caller):
        held.append(getattr(server.service._held, "count", 0))
        return real(self, headers, body, caller)

    monkeypatch.setattr(mcp_module.HttpTransport, "post", post)
    call(base, "POST", "/mcp", body=request("initialize", {}), headers=ACCEPT)
    assert held == [1]
    monkeypatch.setattr(server_http, "LARGE_BODY", 10**6)  # a small one: no slot to parse
    call(base, "POST", "/mcp", body=request("initialize", {}), headers=ACCEPT)
    assert held == [1, 0]


def test_a_body_the_client_stops_sending_is_a_400(api):
    with socket.create_connection(("127.0.0.1", api.port), timeout=5) as sock:
        sock.sendall(
            b"POST /api/v1/preview HTTP/1.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n{"
        )
        sock.shutdown(socket.SHUT_WR)
        answer = sock.recv(4096)
    assert answer.startswith(b"HTTP/1.0 400")


def test_two_content_lengths_are_refused(api):
    answer = raw(
        api.port,
        b"POST /api/v1/preview HTTP/1.1\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\nContent-Length: 20\r\n\r\n{}",
    )
    assert answer.startswith(b"HTTP/1.0 400")


def test_a_broken_request_line_is_logged_with_dashes(api, caplog):
    caplog.set_level(logging.INFO)
    raw(api.port, b"GARBAGE\r\n\r\n")
    time.sleep(0.1)
    assert "local - - 400" in caplog.text or "- - - 400" in caplog.text


# --- 32: SIGHUP --------------------------------------------------------------------------


def test_sighup_is_handled_before_the_server_starts(monkeypatch, tmp_path, server_env):
    order = []
    real_init = server_http.Server.__init__

    def init(self, *args, **kwargs):
        order.append("server")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(server_http.Server, "__init__", init)
    monkeypatch.setattr(
        server_http.Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    monkeypatch.setattr(
        server_main.signal,
        "signal",
        lambda number, handler: (
            order.append(number) if number == server_main.signal.SIGHUP else None
        ),
    )
    labels = tmp_path / "served"
    labels.mkdir()
    assert run("serve", "--port", "0", "--labels-dir", labels) == cli.EXIT_OK
    assert order.index(server_main.signal.SIGHUP) < order.index("server")
