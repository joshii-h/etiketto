"""The fourth security review of the print server: a test for each finding
(the renderer's are in test_render.py, the window's and the command
line's Code 128 refusal in test_app.py and test_cli.py)."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from test_server import call

from etiketto import cli
from etiketto.model import CodeElement, ImageElement, Label
from etiketto.render import CODE128_MAX
from etiketto.server import http as server_http
from etiketto.server import jobs as jobs_module
from etiketto.server import main as server_main
from etiketto.server import service as service_module
from etiketto.server.service import _ip_pattern, _resolved


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base, service=server.service)


# --- 2, 3: render slots ------------------------------------------------------------------


def test_print_jobs_render_in_the_slots(api, server_env, monkeypatch):
    service = api.service
    held = []
    real = service.render_slot

    def slot(timeout=service_module.RENDER_WAIT):
        held.append((threading.current_thread().name, timeout))
        return real(timeout)

    monkeypatch.setattr(service, "render_slot", slot)
    _s, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["state"] == "done"
    assert ("print-job", None) in held  # the worker waited for a slot too


def test_too_many_waiting_requests_get_503_at_once(api, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_WAITERS", 0)
    renders = api.service._renders
    renders.acquire()
    renders.acquire()
    started = time.monotonic()
    try:
        status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label": "box"})
    finally:
        renders.release()
        renders.release()
    assert status == 503 and body["error"]["code"] == "busy"
    assert time.monotonic() - started < 2


# --- 6, 21: what a label may hold ------------------------------------------------------------


def test_images_and_codes_are_counted(api, png_b64):
    images = [ImageElement(data=png_b64((4, 4))) for _n in range(service_module.MAX_PICTURES + 1)]
    content = Label(printer="fake-mono", tape_mm=12, elements=images).to_dict()
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label_content": content})
    assert status == 413 and "images and codes" in body["error"]["message"]


def test_a_code_128_too_long_is_not_printed(api, server_env):
    code = CodeElement(data="A" * (CODE128_MAX + 1), symbology="code128")
    content = Label(printer="fake-mono", tape_mm=12, elements=[code]).to_dict()
    status, _r, body = call(api.base, "POST", "/api/v1/print", body={"label_content": content})
    assert status == 422 and "Code 128" in body["error"]["message"]
    assert server_env.printer.jobs == []


# --- 7, 15: startup order -------------------------------------------------------------------


def test_startup_order(monkeypatch, tmp_path, server_env):
    order = []
    monkeypatch.setattr(
        server_main.signal,
        "signal",
        lambda number, handler: (
            order.append("sighup") if number == server_main.signal.SIGHUP else None
        ),
    )
    real_configured = server_main._configured
    monkeypatch.setattr(
        server_main, "_configured", lambda args: order.append("files") or real_configured(args)
    )
    import etiketto.render as render_module

    monkeypatch.setattr(render_module, "warm_up", lambda: order.append("pango"))
    real_init = server_http.Server.__init__
    monkeypatch.setattr(
        server_http.Server,
        "__init__",
        lambda self, *a, **k: order.append("server") or real_init(self, *a, **k),
    )
    monkeypatch.setattr(
        server_http.Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    labels = tmp_path / "served"
    labels.mkdir()
    assert run("serve", "--port", "0", "--labels-dir", labels) == cli.EXIT_OK
    assert order == ["sighup", "files", "pango", "server"]


def test_mcp_warms_up_pango_too(monkeypatch, tmp_path, server_env):
    order = []
    import etiketto.render as render_module
    from etiketto.server import mcp as mcp_module

    monkeypatch.setattr(render_module, "warm_up", lambda: order.append("pango"))
    monkeypatch.setattr(mcp_module, "run_stdio", lambda service: order.append("stdio") or 0)
    labels = tmp_path / "served"
    labels.mkdir()
    assert run("mcp", "--labels-dir", labels) == 0
    assert order == ["pango", "stdio"]


# --- 9: printer workers ------------------------------------------------------------------------


def test_idle_workers_make_room_for_a_new_printer(monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_QUEUES", 2)
    queue = jobs_module.JobQueue(lambda job, error: None)
    for key in ("a", "b"):
        job = queue.submit(jobs_module.PrintJob("p", key, "", 1, lambda job: None, key=key))
        assert job.done.wait(5)
    time.sleep(0.05)
    third = queue.submit(jobs_module.PrintJob("p", "c", "", 1, lambda job: None, key="c"))
    assert third.done.wait(5)
    assert ("p", "a") not in queue._queues  # the one idle longest went
    queue.close(timeout=5)


# --- 10, 20, 24: clients and proxies ------------------------------------------------------


def test_a_proxy_without_forwarded_for_is_not_held_to_the_client_limit(make_server, monkeypatch):
    monkeypatch.setattr(server_http, "MAX_PER_CLIENT", 0)  # every named client refused
    server, base = make_server(trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),))
    assert call(base, "GET", "/api/v1/labels")[0] == 200  # the proxy's own request
    status, _r, _b = call(
        base, "GET", "/api/v1/labels", headers={"X-Forwarded-For": "198.51.100.1"}
    )
    assert status == 503


def test_connections_are_counted_by_ipv6_network(make_server, monkeypatch):
    monkeypatch.setattr(server_http, "MAX_PER_CLIENT", 1)
    server, _base = make_server()
    first, second = socket.socket(), socket.socket()
    try:
        assert server._admit(first, "2001:db8::1")
        assert not server._admit(second, "2001:db8::2")  # the same /64
        assert server._admit(second, "2001:db8:0:1::2")  # another network
        server._leave(second, "2001:db8:0:1::2")
    finally:
        server._leave(first, "2001:db8::1")
        first.close()
        second.close()


def test_mapped_hops_are_plain_ipv4():
    assert server_http._hop("::ffff:192.0.2.1") == "192.0.2.1"
    assert server_http._hop("[::ffff:192.0.2.1]:443") == "192.0.2.1"


# --- 11, 16: --allow-printer -------------------------------------------------------------------


def test_addresses_in_the_resolvers_order(monkeypatch):
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 0)),
    ]
    monkeypatch.setattr(service_module.socket, "getaddrinfo", lambda *a, **k: infos)
    assert _resolved("p.lan") == ["192.0.2.9", "192.0.2.1"]


@pytest.mark.parametrize(
    ("pattern", "address"),
    [
        ("192.168.1.0/24", True),
        ("192.168.1.*", True),
        ("192.168.1.5", True),
        ("2001:db8::/32", True),
        ("*.*", False),
        ("123", False),
        ("*.5", False),
        ("*.local", False),
        ("1.2.3", False),
    ],
)
def test_only_real_address_syntax_is_an_address_pattern(pattern, address):
    assert _ip_pattern(pattern) is address
