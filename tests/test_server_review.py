"""The second security review of the print server: one test (or more) for
each finding."""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import subprocess  # nosec B404 - two `etiketto token new` at once
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_server import ASK, call, wait_for
from test_server_config import PRINTERS, TOKENS, token, tokens_text, write
from test_server_mcp import ACCEPT, request

from etiketto import cli
from etiketto.server import config as cfg
from etiketto.server import http as server_http
from etiketto.server import jobs as jobs_module
from etiketto.server import main as server_main
from etiketto.server import mcp as mcp_module
from etiketto.server import service as service_module
from etiketto.server.config import Caller, ConfigError, TokenStore, load_printers, load_tokens
from etiketto.server.service import Service, host_allowed, queue_key

ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base)


# --- 2: worker threads -----------------------------------------------------------------------


def test_idle_workers_end_and_their_queue_goes(monkeypatch):
    monkeypatch.setattr(jobs_module, "IDLE_SECONDS", 0.1)
    queue = jobs_module.JobQueue(lambda job, error: None)
    for n in range(5):
        job = jobs_module.PrintJob("p", "h%d" % n, "", 1, lambda job: None, key="h%d" % n)
        queue.submit(job)
        assert job.done.wait(5)
    wait_for(lambda: not queue._queues)
    wait_for(lambda: not any(w.is_alive() for w in queue._workers))
    again = queue.submit(jobs_module.PrintJob("p", "h0", "", 1, lambda job: None, key="h0"))
    assert again.done.wait(5)  # a new worker for a printer whose worker ended
    queue.close(timeout=5)


def test_queues_are_capped(monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_QUEUES", 2)
    gate = threading.Event()
    queue = jobs_module.JobQueue(lambda job, error: None)
    for n in range(2):
        queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: gate.wait(5), key=str(n)))
    with pytest.raises(jobs_module.QueueFull):
        queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: None, key="third"))
    gate.set()
    queue.close(timeout=5)


def test_no_thread_to_be_had(monkeypatch):
    queue = jobs_module.JobQueue(lambda job, error: None)

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(jobs_module.QueueFull):
        queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: None))
    assert queue._queues == {} and queue._jobs == {}


# --- 3: slow clients ---------------------------------------------------------------------------


def trickle(base, head: bytes, byte: bytes, seconds: float) -> tuple[bool, bytes]:
    """Send `head`, then `byte` every 0.1 s for `seconds`: (still open, answer)."""
    port = int(base.rsplit(":", 1)[1])
    answer = b""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head)
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds:
                time.sleep(0.1)
                sock.sendall(byte)
            sock.settimeout(0.5)
            answer = sock.recv(1024)
            return answer != b"" and not answer.startswith(b"HTTP/1.0 408"), answer
        except OSError:
            return False, answer


@pytest.mark.parametrize(
    ("head", "byte"),
    [
        (b"GET /api/v1/hea", b"a"),  # inside the request line
        (b"GET /api/v1/health HTTP/1.1\r\nX-A: ", b"a"),  # inside a header
        (
            b"POST /api/v1/preview HTTP/1.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: 100000\r\n\r\n{",
            b" ",
        ),  # inside the body
    ],
)
def test_trickling_is_cut_off(api, monkeypatch, head, byte):
    monkeypatch.setattr(server_http, "REQUEST_SECONDS", 0.6)
    still_open, _answer = trickle(api.base, head, byte, 2.0)
    assert not still_open


def test_the_answer_has_its_own_time(api, monkeypatch):
    monkeypatch.setattr(server_http, "REQUEST_SECONDS", 0.6)
    status, _r, png = call(api.base, "POST", "/api/v1/preview", body={"label": "box"})
    assert status == 200 and png.startswith(b"\x89PNG")


# --- 5: the rate limit ---------------------------------------------------------------------------


@pytest.fixture
def secured(make_server, server_env, tmp_path):
    printers = load_printers(write(tmp_path / "printers.toml", PRINTERS))
    path = write(tmp_path / "tokens.toml", tokens_text(**TOKENS))
    os.chmod(path, 0o600)
    store = TokenStore(path, printers)
    server, base = make_server(printers=printers, tokens=store)
    return SimpleNamespace(server=server, base=base, store=store, path=path)


def test_valid_tokens_work_from_a_blocked_address(secured):
    for _n in range(12):
        call(secured.base, "GET", "/api/v1/labels", token="etk_wrong-token-0123456789")
    assert call(secured.base, "GET", "/api/v1/labels", token="nope")[0] == 429
    assert call(secured.base, "GET", "/api/v1/labels", token=token("viewer"))[0] == 200
    failures = secured.server.rate_limit._failures["127.0.0.1"]
    assert len(failures) == server_http.AUTH_FAILURES  # 429s are not counted, never more kept


def test_rate_key_and_trusted_proxies(make_server, server_env):
    assert server_http.rate_key("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert server_http.rate_key("::ffff:192.0.2.1") == "192.0.2.1"
    assert server_http.rate_key("192.0.2.1") == "192.0.2.1"
    assert server_http.rate_key("not-an-ip") == "not-an-ip"
    import ipaddress

    server, base = make_server(
        token="single-token-for-tests", trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),)
    )
    for _n in range(10):
        call(base, "GET", "/api/v1/labels", headers={"X-Forwarded-For": "198.51.100.7"})
    assert "198.51.100.7" in server.rate_limit._failures
    # another client behind the same proxy is not blocked
    status, _r, _b = call(
        base, "GET", "/api/v1/labels", headers={"X-Forwarded-For": "198.51.100.8"}
    )
    assert status == 401
    headers = {"X-Forwarded-For": "198.51.100.9, 127.0.0.1"}  # the last untrusted hop counts
    call(base, "GET", "/api/v1/labels", headers=headers)
    assert "198.51.100.9" in server.rate_limit._failures
    _server, plain = make_server(token="single-token-for-tests")
    call(plain, "GET", "/api/v1/labels", headers={"X-Forwarded-For": "198.51.100.10"})
    assert "198.51.100.10" not in _server.rate_limit._failures  # not a trusted proxy


def test_rate_limit_stays_bounded():
    limit = server_http.RateLimit(limit=3)
    limit.MAX_CLIENTS = 5
    for n in range(20):
        limit.failed(str(n))
    assert len(limit._failures) == 5 and "19" in limit._failures


# --- 6: --allow-printer and addresses ----------------------------------------------------------


def test_address_patterns_match_addresses(monkeypatch):
    monkeypatch.setattr(service_module, "_resolved", lambda name: [])
    assert host_allowed("192.0.2.5", ("192.0.2.*",))
    assert host_allowed("192.0.2.5", ("192.0.2.0/24",))
    assert not host_allowed("192.0.2.evil.example", ("192.0.2.*",))
    assert not host_allowed("192.168.130.5", ("192.0.2.0/24",))
    assert host_allowed("2001:db8::5", ("2001:db8::/32",))
    assert host_allowed("cab.local", ("*.local",))
    assert not host_allowed("192.168.1.5", ("*",)) or host_allowed("192.168.1.5", ("*",))
    monkeypatch.setattr(service_module, "_resolved", lambda name: ["192.0.2.7"])
    assert host_allowed("printer.lan", ("192.0.2.0/24",))  # by what the name resolves to
    monkeypatch.setattr(service_module, "_resolved", lambda name: ["192.0.2.7", "10.0.0.1"])
    assert not host_allowed("printer.lan", ("192.0.2.0/24",))  # every address must match
    assert not host_allowed("10.0.0.1", ("10.0.0.0/33",))  # no network: nothing


def test_queue_keys_are_normalised():
    assert queue_key("ipp://Host.Local.:631/ipp/print") == queue_key("ipp://host.local/ipp/print")
    assert queue_key("https://h/x") == queue_key("https://h:443/x")
    assert queue_key("h.local") != queue_key("h.local:9100")


# --- 7 and 8: memory -----------------------------------------------------------------------------


def test_bodies_are_limited_to_4_mb_by_default(make_server):
    assert server_http.Config().max_body == 4 * 1024 * 1024
    _server, base = make_server()
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(
            b"POST /api/v1/preview HTTP/1.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % (4 * 1024 * 1024 + 1)
        )
        assert sock.recv(1024).startswith(b"HTTP/1.0 413")


def test_preview_bodies_are_parsed_in_a_render_slot(make_server, monkeypatch):
    server, base = make_server()
    service = server.service
    parsed_in_slot = []
    real_parse = server_http._parse

    def parse(body):
        parsed_in_slot.append(getattr(service._held, "count", 0) > 0)
        return real_parse(body)

    monkeypatch.setattr(server_http, "_parse", parse)
    assert call(base, "POST", "/api/v1/preview", body={"label": "box"})[0] == 200
    assert call(base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})[0] == 200
    assert parsed_in_slot == [True, True]


def test_pending_jobs_per_owner_and_by_size(make_server, secured, server_env, monkeypatch):
    printer = server_env.printer
    printer.gate = threading.Event()
    monkeypatch.setattr(jobs_module, "MAX_PER_OWNER", 1)
    body = {"label": "box", "printer": "office", "no_save_counter": True}
    other = token("other")
    call(secured.base, "POST", "/api/v1/print", body=body, token=other)
    status, _r, answer = call(secured.base, "POST", "/api/v1/print", body=body, token=other)
    assert status == 429 and answer["error"]["code"] == "queue_full"
    # a server without named tokens: everyone is one caller, only the overall limit
    _server, plain = make_server()
    for _n in range(2):
        assert call(
            plain, "POST", "/api/v1/print", body={"label": "box", "no_save_counter": True}
        )[0] in (200, 202)
    monkeypatch.setattr(jobs_module, "MAX_PENDING_BYTES", 100)
    status, _r, _b = call(plain, "POST", "/api/v1/print", body={"label": "box"})
    assert status == 429
    printer.gate.set()


def test_labels_per_job(make_server, server_env):
    server, base = make_server()
    server.service.max_labels = 5
    rows = [{"x": str(n)} for n in range(3)]
    status, _r, body = call(
        base, "POST", "/api/v1/print", body={"label": "box", "copies": 2, "data_rows": rows}
    )
    assert status == 413 and body["error"]["code"] == "too_many_labels"
    status, _r, body = call(base, "POST", "/api/v1/print", body={"label": "box", "copies": 6})
    assert status == 413


# --- 9, 10: SIGHUP -------------------------------------------------------------------------


def test_sighup_only_sets_a_flag(secured):
    store = secured.store
    server_main._reload(None)  # without tokens.toml: nothing to do, no crash
    rest = {k: v for k, v in TOKENS.items() if k != "boss"}
    stamp = secured.path.stat()
    write(secured.path, tokens_text(**rest))
    os.utime(secured.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    store._stamp = store._stat()  # as if the change went unnoticed
    server_main._reload(store)
    assert store._reload.is_set()
    assert store.find(token("boss")) is None and not store._reload.is_set()


def test_serve_always_handles_sighup(monkeypatch, tmp_path, server_env):
    handlers = {}

    def serve_forever(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(server_http.Server, "serve_forever", serve_forever)
    monkeypatch.setattr(server_main.signal, "signal", lambda n, h: handlers.setdefault(n, h))
    labels = tmp_path / "served"
    labels.mkdir()
    assert run("serve", "--port", "0", "--labels-dir", labels) == cli.EXIT_OK
    handlers[server_main.signal.SIGHUP](1, None)


# --- 11, 32: packaging -------------------------------------------------------------------


def test_systemd_unit_is_sandboxed():
    unit = (ROOT / "packaging/systemd/etiketto.service").read_text()
    for line in (
        "ProtectHome=read-only",
        "InaccessiblePaths=-%h/.ssh -%h/.gnupg",
        "CapabilityBoundingSet=\n",
        "SystemCallFilter=@system-service",
        "ProtectKernelLogs=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "MALLOC_ARENA_MAX=2",
        "MemoryMax=",
        "ExecReload=",
    ):
        assert line in unit, line


def test_container_files():
    dockerfile = (ROOT / "packaging/docker/Dockerfile").read_text()
    assert "VOLUME" not in dockerfile and "XDG_CONFIG_HOME=/config" in dockerfile
    assert "MALLOC_ARENA_MAX=2" in dockerfile
    host = (ROOT / "packaging/docker/compose.host.yaml").read_text()
    assert "group_add:" in host and "- lp" in host and "- dialout" in host


# --- 12, 21: the token cache and changes ------------------------------------------------


def test_cache_survives_a_reload_that_keeps_the_entry(tmp_path):
    chosen = "a-token-people-chose-1234"
    text = '[tokens.chosen]\nrole = "print"\nhash = "%s"\n' % cfg.hash_token(chosen, "scrypt")
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    store = TokenStore(path)
    assert store.find(chosen).name == "chosen"
    write(
        path, text + '[tokens.other]\nrole = "preview"\nhash = "%s"\n' % cfg.hash_token("x" * 20)
    )
    store.reload()
    assert store.find(chosen, cheap_only=True).name == "chosen"  # still known, no scrypt
    write(path, text.replace('"print"', '"admin"'))
    store.reload()
    assert store.find(chosen, cheap_only=True) is None  # the entry changed: checked again
    assert store.find(chosen).role == "admin"


def test_a_replaced_file_is_noticed_even_with_the_same_size_and_time(tmp_path):
    path = write(tmp_path / "t.toml", tokens_text(viewer=TOKENS["viewer"]))
    os.chmod(path, 0o600)
    store = TokenStore(path)
    assert store.find(token("viewer"))
    info = path.stat()
    other = tokens_text(viewer=("etk_viewer-token-9876543210", "preview", ""))
    replacement = tmp_path / "new.toml"
    write(replacement, other)
    os.chmod(replacement, 0o600)
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.replace(replacement, path)
    assert store.find(token("viewer")) is None


# --- 13: preview tokens and addresses ---------------------------------------------------


def test_preview_tokens_give_no_address_without_a_printer_list(make_server, server_env, tmp_path):
    path = write(tmp_path / "t.toml", tokens_text(viewer=TOKENS["viewer"], boss=TOKENS["boss"]))
    os.chmod(path, 0o600)
    _server, base = make_server(tokens=TokenStore(path))
    url = "/api/v1/printers/fake-mono/status?host=10.0.0.1"
    status, _r, body = call(base, "GET", url, token=token("viewer"))
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert call(base, "GET", "/api/v1/printers/fake-mono/status", token=token("viewer"))[0] == 200
    assert call(base, "GET", url.replace("10.0.0.1", "mono.local"), token=token("boss"))[0] == 200


def test_warnings_at_startup(monkeypatch, tmp_path, server_env, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        server_http.Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    monkeypatch.setattr(server_main.signal, "signal", lambda n, h: None)
    monkeypatch.setenv("ETIKETTO_TOKEN", "an-ignored-token-0123")
    path = write(tmp_path / "t.toml", tokens_text(boss=TOKENS["boss"]))
    os.chmod(path, 0o600)
    labels = tmp_path / "served"
    labels.mkdir()
    assert run("serve", "--port", "0", "--labels-dir", labels, "--tokens", path) == cli.EXIT_OK
    assert "ETIKETTO_TOKEN is ignored" in caplog.text
    assert "--tokens without --printers" in caplog.text


# --- 14, 33, 34: requests, logs, answers ---------------------------------------------


def test_a_broken_request_line_is_answered_without_a_traceback(api, caplog):
    caplog.set_level(logging.DEBUG)
    port = int(api.base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GARBAGE\r\n\r\n")
        answer = sock.recv(1024)
    assert answer.startswith(b"HTTP/1.0 400")
    wait_for(lambda: "GARBAGE" in caplog.text or "400" in caplog.text)
    assert "Traceback" not in caplog.text


def test_content_length_must_be_digits(api):
    port = int(api.base.rsplit(":", 1)[1])
    for value in (b"+2", b"-0", "\u0662".encode()):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /api/v1/preview HTTP/1.1\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + value + b"\r\n\r\n{}"
            )
            assert sock.recv(1024).startswith(b"HTTP/1.0 411"), value


def test_refused_requests_are_logged_without_a_name(make_server, caplog):
    caplog.set_level(logging.INFO)
    _server, base = make_server(token="single-token-for-tests")
    call(base, "GET", "/api/v1/labels")
    assert " - GET /api/v1/labels 401" in caplog.text
    call(base, "GET", "/api/v1/labels", token="single-token-for-tests")
    assert " token GET /api/v1/labels 200" in caplog.text


def test_listening_line_brackets_ipv6(caplog, server_env):
    caplog.set_level(logging.INFO)

    class Fake:
        server_address = ("::1", 8790, 0, 0)
        mcp = None

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    service = Service(server_env.labels)
    original = server_main.signal.signal
    try:
        server_main.signal.signal = lambda n, h: None
        server_main._run(Fake(), service)
    finally:
        server_main.signal.signal = original
    assert "http://[::1]:8790" in caplog.text


def test_printer_states_are_remembered_briefly(make_server, server_env, tmp_path):
    printers = load_printers(write(tmp_path / "p.toml", PRINTERS))
    _server, base = make_server(printers=printers)
    call(base, "GET", "/api/v1/printers", headers=ASK)
    call(base, "GET", "/api/v1/printers", headers=ASK)
    assert len(server_env.printer.queries) == 2  # two printers, asked once each


# --- 15, 16, 20, 26, 27: tokens.toml --------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "scrypt:1048576:32:16:AAAAAAAAAAA=:" + "A" * 44,  # far more memory than allowed
        "scrypt:16384:8:1:AAAA:" + "A" * 44,  # salt too short
        "scrypt:16384:8:1:AAAAAAAAAAA=:AAAA",  # hash too short
        "scrypt:+16384:8:1:AAAAAAAAAAA=:" + "A" * 44,  # not only digits
        "scrypt:16385:8:1:AAAAAAAAAAA=:" + "A" * 44,  # no power of two
    ],
)
def test_scrypt_parameters_are_checked(tmp_path, stored):
    path = write(tmp_path / "t.toml", '[tokens.a]\nrole = "print"\nhash = "%s"\n' % stored)
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="not a token hash"):
        load_tokens(path, None)
    assert not cfg.token_matches("x", stored)


def test_tokens_with_an_id_are_found_without_trying_the_others(tmp_path, monkeypatch):
    tokens = [cfg.new_token() for _n in range(3)]
    text = "".join(
        cfg.token_entry("t%d" % n, cfg.hash_token(t, "scrypt"), "print", [], "", "", ident)
        for n, (t, ident) in enumerate(tokens)
    )
    path = write(tmp_path / "t.toml", text)
    os.chmod(path, 0o600)
    store = TokenStore(path)
    checked = []
    real = cfg.token_matches
    monkeypatch.setattr(cfg, "token_matches", lambda t, h: checked.append(h) or real(t, h))
    assert store.find(tokens[2][0]).name == "t2"
    assert len(checked) == 1


def test_tokens_file_permissions(tmp_path, caplog):
    path = write(tmp_path / "t.toml", tokens_text(viewer=TOKENS["viewer"]))
    os.chmod(path, 0o666)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_tokens(path, None)
    os.chmod(path, 0o640)
    load_tokens(path, None)
    assert "chmod 600" in caplog.text


def test_role_is_required_and_tokens_are_unique(tmp_path):
    path = write(tmp_path / "t.toml", '[tokens.a]\nhash = "%s"\n' % cfg.hash_token("x"))
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="role: missing"):
        load_tokens(path, None)
    twice = "".join(
        '[tokens.%s]\nrole = "%s"\nhash = "%s"\n' % (name, role, cfg.hash_token("same"))
        for name, role in (("a", "preview"), ("b", "admin"))
    )
    write(path, twice)
    with pytest.raises(ConfigError, match="the same token twice"):
        load_tokens(path, None)
    ids = '[tokens.a]\nrole = "print"\nhash = "%s"\nid = "0123456789ab"\n' % cfg.hash_token("a")
    ids += '[tokens.b]\nrole = "print"\nhash = "%s"\nid = "0123456789ab"\n' % cfg.hash_token("b")
    write(path, ids)
    with pytest.raises(ConfigError, match="the same token twice"):
        load_tokens(path, None)
    write(path, '[tokens.a]\nrole = "print"\nhash = "%s"\nid = "XYZ"\n' % cfg.hash_token("a"))
    assert load_tokens(path, None)[0].id == ""  # a wrong id is ignored (and warned about)
    scrypt = cfg.hash_token("chosen-token-12345", "scrypt")
    write(path, "".join('[tokens.%s]\nrole = "print"\nhash = "%s"\n' % (n, scrypt) for n in "ab"))
    with pytest.raises(ConfigError, match="the same token twice"):
        load_tokens(path, None)


# --- 17, 18, 19, 35: etiketto token new -----------------------------------------------------


def test_token_new_twice_at_once_keeps_both(tmp_path):
    path = tmp_path / "tokens.toml"
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "config"), PYTHONPATH=str(ROOT))
    command = [sys.executable, "-m", "etiketto.cli", "token", "new"]
    started = [
        subprocess.Popen(  # nosec B603 - this checkout, fixed arguments
            command + ["device%d" % n, "--tokens", str(path)],
            env=env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for n in range(4)
    ]
    for process in started:
        assert process.wait(60) == 0, process.stderr.read()
    names = {e.caller.name for e in load_tokens(path, None)}
    assert names == {"device0", "device1", "device2", "device3"}


def test_token_names_with_dots(tmp_path, capsys):
    path = tmp_path / "tokens.toml"
    assert run("token", "new", "gate.2", "--tokens", path) == cli.EXIT_OK
    made = capsys.readouterr().out.strip()
    assert '[tokens."gate.2"]' in path.read_text()
    assert TokenStore(path).find(made).name == "gate.2"


def test_token_new_reports_files_it_cannot_use(tmp_path, capsys):
    folder = tmp_path / "folder.toml"
    folder.mkdir()
    assert run("token", "new", "a", "--tokens", folder) == cli.EXIT_FILE
    broken = tmp_path / "broken.toml"
    broken.write_bytes(b"\xff\xfe[tokens]")
    os.chmod(broken, 0o600)
    assert run("token", "new", "a", "--tokens", broken) == cli.EXIT_FILE
    assert "Cannot read" in capsys.readouterr().err
    readonly = tmp_path / "ro" / "tokens.toml"
    readonly.parent.mkdir()
    os.chmod(readonly.parent, 0o500)
    try:
        if not os.access(readonly.parent, os.W_OK):
            assert run("token", "new", "a", "--tokens", readonly) == cli.EXIT_FILE
    finally:
        os.chmod(readonly.parent, 0o700)


def test_token_from_stdin(tmp_path, capsys, monkeypatch):
    path = tmp_path / "tokens.toml"
    monkeypatch.setattr(sys, "stdin", io.StringIO("a-token-people-chose-1234\n"))
    assert run("token", "new", "chosen", "--tokens", path, "--from-stdin") == cli.EXIT_OK
    assert capsys.readouterr().out == ""  # not shown: whoever chose it knows it
    assert "scrypt:" in path.read_text()
    assert TokenStore(path).find("a-token-people-chose-1234").name == "chosen"
    for bad in ("short\n", "with spaces in the token\n", "\n"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(bad))
        assert run("token", "new", "x", "--tokens", path, "--from-stdin") == cli.EXIT_USAGE


def test_token_file_locked_elsewhere(tmp_path, monkeypatch, capsys):
    from etiketto import model

    def busy(path, waiting=None, timeout=60.0):
        raise model.LockTimeout(11, "locked")

    monkeypatch.setattr(model, "locked", busy)
    path = tmp_path / "tokens.toml"
    assert run("token", "new", "a", "--tokens", path) == cli.EXIT_FILE
    assert "in use" in capsys.readouterr().err
    assert not path.exists()  # the empty file made for it is gone again


# --- 22, 23: printers a token may use ------------------------------------------------------


def test_default_printer_among_the_tokens_own(make_server, server_env, tmp_path):
    printers = load_printers(write(tmp_path / "p.toml", PRINTERS))
    entry = ("etk_office-token-0123456789", "print", 'printers = ["office"]')
    path = write(tmp_path / "t.toml", tokens_text(office=entry))
    os.chmod(path, 0o600)
    _server, base = make_server(printers=printers, tokens=TokenStore(path, printers))
    body = {"label": "box", "wait": 10}
    status, _r, job = call(base, "POST", "/api/v1/print", body=body, token=entry[0])
    assert status == 200 and job["printer_name"] == "office"  # not the default "lager"


# --- 24, 25: MCP -------------------------------------------------------------------------------


def test_sessions_per_owner(monkeypatch, server_env):
    monkeypatch.setattr(mcp_module, "MAX_SESSIONS_PER_OWNER", 2)
    monkeypatch.setattr(mcp_module, "MAX_SESSIONS", 4)
    transport = mcp_module.HttpTransport(mcp_module.Mcp(Service(server_env.labels)))
    init = json.dumps(request("initialize", {})).encode()
    a, b, c = (Caller(name, named=True) for name in "abc")
    other = transport.post({}, init, b).headers["Mcp-Session-Id"]
    mine = [transport.post({}, init, a).headers["Mcp-Session-Id"] for _n in range(3)]
    assert transport._session(mine[0], a) is None  # its own oldest went
    assert transport._session(mine[2], a) is not None
    assert transport._session(other, b) is not None  # the other token's stays
    transport.post({}, init, b)
    reply = transport.post({}, init, c)  # full of others' sessions: refused
    assert reply.status == 503 and transport._session(other, b) is not None
    # one shared token (not named): only the overall limit, its own oldest goes
    shared = Caller("token")
    first = transport.post({}, init, shared)
    assert first.status == 503  # the table is full of a's and b's


def test_cancelled_with_an_odd_id_is_ignored(server_env):
    mcp = mcp_module.Mcp(Service(server_env.labels))
    for ident in ([1], {"a": 1}, True, None):
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": ident},
        }
        assert mcp.handle(note) is None


def test_openapi_has_the_remaining_errors():
    from etiketto.server.openapi import spec

    answers = spec()["paths"]["/labels"]["get"]["responses"]
    for code in ("405", "500", "501"):
        assert code in answers


def test_mcp_post_over_http_keeps_working(make_server):
    _server, base = make_server()
    status, _r, answer = call(base, "POST", "/mcp", body=request("ping"), headers=ACCEPT)
    assert status == 400  # no session: the transport still answers as before


# --- 1, 4: rendering over HTTP --------------------------------------------------------------


def test_many_small_images_are_refused(api):
    import base64

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("1", (4000, 10), 1).save(buffer, "PNG", optimize=True)
    image = {"kind": "image", "data": base64.b64encode(buffer.getvalue()).decode()}
    content = {"printer": "vc500w", "tape_mm": 25, "elements": [image] * 50}
    started = time.monotonic()
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label_content": content})
    assert status == 413 and body["error"]["code"] == "label_too_large"
    assert time.monotonic() - started < 10


def test_a_page_with_a_fitted_text_renders(api):
    content = {
        "printer": "system",
        "tape_mm": 297,
        "length_mm": 210,
        "auto_length": False,
        "orientation": "across",
        "elements": [{"kind": "text", "text": "Hello World"}],
    }
    body = {"label_content": content, "format": "json"}
    status, _r, answer = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status == 200 and answer["warnings"] == []
