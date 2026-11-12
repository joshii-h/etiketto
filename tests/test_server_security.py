"""The fixes of the security review: render limits, image bombs, the rate
limit, printer addresses, slow clients, memory of finished jobs, CORS,
requests that contact printers, and the smaller points."""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time

import pytest
from PIL import Image
from test_server import ASK, TOKEN, call, label_file, wait_for
from test_server_mcp import ACCEPT, request

from tavletta import render as render_module
from tavletta.model import ImageElement, Label, TextElement
from tavletta.server import http as server_http
from tavletta.server import jobs as jobs_module
from tavletta.server import main as server_main
from tavletta.server import service as service_module
from tavletta.server.mcp import LEGACY, Mcp
from tavletta.server.openapi import spec
from tavletta.server.service import Service, host_name, queue_key, redacted
from tavletta.ui.settings import Settings


def content(*elements, **fields):
    fields.setdefault("printer", "fake-mono")
    fields.setdefault("tape_mm", 12)
    return Label(elements=list(elements), **fields).to_dict()


@pytest.fixture
def api(make_server):
    _server, base = make_server()
    return base


# --- 1: labels that would render too large ------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"label_content": content(*[TextElement(text="x") for _n in range(201)])},
        {"label_content": content(TextElement(text="W\n" * 101))},
        {"label_content": content(TextElement(text="W" * 5001))},
        {"label": "box", "set": {"title": "W" * 5001}},
        {
            "label_content": content(TextElement(text="{Name}")),
            "data_rows": [{"Name": "x\n" * 200}],
        },
    ],
)
def test_labels_too_large_are_refused_before_rendering(api, body):
    started = time.monotonic()
    for path in ("/api/v1/preview", "/api/v1/print"):
        status, _r, answer = call(api, "POST", path, body=body)
        assert status == 413 and answer["error"]["code"] == "label_too_large", answer
    assert time.monotonic() - started < 5


def test_long_cells_of_unused_columns_are_fine(api):
    body = {"label": "box", "data_rows": [{"Other": "x" * 6000}], "format": "json"}
    assert call(api, "POST", "/api/v1/preview", body=body)[0] == 200


def test_render_pixel_limit_over_http(api, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PIXELS", 1000)
    status, _r, answer = call(api, "POST", "/api/v1/preview", body={"label": "box"})
    assert status == 413 and answer["error"]["code"] == "label_too_large"
    body = {"label": "box", "view": "printer"}
    assert call(api, "POST", "/api/v1/preview", body=body)[0] == 413
    assert call(api, "POST", "/api/v1/print", body={"label": "box"})[0] == 413


def test_render_limits(make_label):
    label = make_label(elements=[TextElement(text="Hello")])
    assert render_module.render(label, 100).image.width > 0  # no limits by default
    with (
        render_module.limited(10**9, max_length_mm=5),
        pytest.raises(render_module.RenderTooLarge),
    ):
        render_module.render(label, 100)
    # the text block fails (a placeholder instead), the label itself too
    with render_module.limited(100), pytest.raises(render_module.RenderTooLarge):
        render_module.render(label, 100)
    with render_module.limited(10**9):
        assert render_module.render(label, 100).warnings == []


# --- 2: image bombs ------------------------------------------------------------------------


def png(size) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_images_are_checked_by_their_size_before_unpacking(make_label):
    big = png((300, 200))
    with render_module.limited(50_000), pytest.raises(render_module.RenderTooLarge):
        render_module._decoded(big)
    label = make_label(elements=[ImageElement(data=big)])
    with render_module.limited(50_000), pytest.raises(render_module.RenderTooLarge):
        render_module.render(label, 100)  # refused, not drawn as a placeholder


def test_limited_renders_skip_the_caches():
    data = png((20, 10))
    render_module._decoded_cached.cache_clear()
    render_module._scaled_cached.cache_clear()
    with render_module.limited(10**6):
        render_module._decoded(data)
        render_module._scaled(data, 5)
    assert render_module._decoded_cached.cache_info().currsize == 0
    assert render_module._scaled_cached.cache_info().currsize == 0
    render_module._scaled(data, 5)
    assert render_module._scaled_cached.cache_info().currsize == 1


def test_image_bomb_over_http(api, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PIXELS", 50_000)
    image = {"label_content": content(ImageElement(data=png((300, 200))))}
    for path in ("/api/v1/preview", "/api/v1/print"):
        status, _r, answer = call(api, "POST", path, body=image)
        assert status == 413 and answer["error"]["code"] == "label_too_large"


# --- 4 and 11: printer addresses ------------------------------------------------------


def test_host_names():
    assert host_name("ipp://Cab.Local:631/ipp/print") == "cab.local"
    assert host_name("192.0.2.5") == "192.0.2.5"
    assert host_name("2001:db8::5") is None or host_name("[2001:db8::5]") == "2001:db8::5"
    assert host_name("HP_LaserJet") == "hp_laserjet"
    assert host_name("evil/x.printers.lan") == "evil"
    for bad in ("192.168.1.9@localhost:18631/x", "a b", "", "x\\y", "h:port", "ü.local"):
        assert host_name(bad) is None, bad
    assert queue_key("Printer.LOCAL") == queue_key("printer.local")
    assert (
        redacted("ipps://user:secret@cups.example/printers/a") == "ipps://cups.example/printers/a"
    )
    assert redacted("user@host") == "host" and redacted(None) is None


def test_allow_printer_matches_the_host_only(make_server, server_env):
    _server, base = make_server(allowed=("*.printers.lan", "localhost"))
    for host in ("evil/x.printers.lan", "192.168.1.9@localhost:18631/ipp/print"):
        status, _r, body = call(base, "POST", "/api/v1/print", body={"label": "box", "host": host})
        assert status in (400, 403), (host, body)
    body = {"label": "box", "host": "ipp://Cab.Printers.LAN/ipp/print", "wait": 10}
    status, _r, job = call(base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "done"
    assert server_env.printer.jobs[-1][1] == "ipp://Cab.Printers.LAN/ipp/print"


def test_credentials_are_never_shown(make_server, server_env):
    settings = {"printers": {"fake-mono": {"host": "ipp://user:secret@mono.local/ipp"}}}
    _server, base = make_server(settings=settings)
    _s, _r, body = call(base, "GET", "/api/v1/printers?discover=false", headers=ASK)
    fake = next(p for p in body["printers"] if p["id"] == "fake-mono")
    assert fake["configured_host"] == "ipp://mono.local/ipp"
    assert "secret" not in json.dumps(body)
    status, _r, _b = call(base, "POST", "/api/v1/print", body={"label": "box"})
    assert status == 400  # such an address is not printed on


def test_one_queue_per_printer_however_written(api, server_env):
    printer = server_env.printer
    printer.gate = threading.Event()
    body = {"label": "box", "host": "Mono.Local", "no_save_counter": True}
    _s, _r, first = call(api, "POST", "/api/v1/print", body=body)
    assert printer.started.wait(10)
    _s, _r, second = call(api, "POST", "/api/v1/print", body=dict(body, host="mono.local"))
    assert call(api, "GET", "/api/v1/jobs/" + second["id"])[2]["state"] == "queued"
    printer.gate.set()
    wait_for(lambda: call(api, "GET", "/api/v1/jobs/" + second["id"])[2]["state"] == "done")


def test_printer_requests_need_the_header_without_a_token(make_server):
    _server, base = make_server()
    for path in ("/api/v1/printers", "/api/v1/printers/fake-mono/status"):
        status, _r, body = call(base, "GET", path)
        assert status == 403 and body["error"]["code"] == "header_required"
        assert call(base, "GET", path, headers=ASK)[0] == 200
    _server, secured = make_server(token=TOKEN)
    assert call(secured, "GET", "/api/v1/printers", token=TOKEN)[0] == 200


# --- 5: slow clients -------------------------------------------------------------------------


def test_the_whole_request_has_a_deadline(api, monkeypatch):
    monkeypatch.setattr(server_http, "REQUEST_SECONDS", 0.5)
    port = int(api.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /api/v1/health HTTP/1.1\r\n")
        for _n in range(4):  # a header line now and then, never the end
            time.sleep(0.25)
            try:
                sock.sendall(b"X-Slow: 1\r\n")
            except OSError:
                break
        sock.settimeout(5)
        answer = b""
        try:
            while chunk := sock.recv(1024):
                answer += chunk
        except OSError:
            pass
    assert b"200" not in answer.split(b"\r\n", 1)[0]


def test_slow_body_times_out(api, monkeypatch):
    monkeypatch.setattr(server_http, "REQUEST_SECONDS", 0.5)
    port = int(api.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(
            b"POST /api/v1/preview HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
        )
        answer = sock.recv(1024)
    assert answer.startswith(b"HTTP/1.0 408")


def test_connections_per_address(api, monkeypatch):
    monkeypatch.setattr(server_http, "MAX_PER_CLIENT", 1)
    port = int(api.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        time.sleep(0.1)  # the first one holds the only slot of this address
        with pytest.raises((ConnectionError, OSError, Exception)):
            call(api, "GET", "/api/v1/health")

    def answers() -> bool:
        try:
            return call(api, "GET", "/api/v1/health")[0] == 200
        except OSError:  # the slot is given back a moment after the close
            return False

    wait_for(answers)


# --- 6: finished jobs ---------------------------------------------------------------------------


def test_finished_jobs_keep_no_request(make_server, server_env):
    server, base = make_server()
    from tavletta.backends.base import PaperJam

    _s, _r, done = call(base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    server_env.printer.error = PaperJam("jam")
    _s, _r, failed = call(base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    for job_id in (done["id"], failed["id"]):
        job = server.service.jobs.get(job_id)
        assert job.run is None
    job = server.service.jobs.get(failed["id"])
    assert job.failure.code == "printer_jam"
    cells = [c.cell_contents for c in job.failure.message.__closure__ or ()]
    errors = [c for c in cells if isinstance(c, Exception)]
    assert errors and all(e.__traceback__ is None for e in errors)
    assert (
        "jam" in call(base, "GET", "/api/v1/jobs/" + failed["id"])[2]["error"]["message"].lower()
    )


def test_waiting_for_the_label_file(api, server_env, monkeypatch):
    from tavletta.server import service as svc

    held = threading.Lock()
    held.acquire()
    real = svc.Service._exclusive

    def exclusive(self, path):
        if path is None:
            return real(self, path)
        import contextlib

        @contextlib.contextmanager
        def waiting():
            with held:
                yield

        return waiting()

    monkeypatch.setattr(svc.Service, "_exclusive", exclusive)
    _s, _r, job = call(api, "POST", "/api/v1/print", body={"label": "box"})
    path = "/api/v1/jobs/" + job["id"]
    wait_for(lambda: call(api, "GET", path)[2]["state"] == "waiting")
    status, _r, _b = call(api, "DELETE", path)
    assert status == 202
    held.release()
    wait_for(lambda: call(api, "GET", path)[2]["state"] == "canceled")
    assert call(api, "GET", path)[2]["printed"] == 0 and server_env.printer.jobs == []


# --- 8, 10: MCP content type, CORS ----------------------------------------------------------


def test_mcp_post_needs_json(make_server):
    _server, base = make_server()
    status, _r, body = call(
        base, "POST", "/mcp", raw=json.dumps(request("ping")).encode(), headers=dict(ACCEPT)
    )
    assert status == 415 and body["error"]["code"] == "not_json"


def test_cors_for_allowed_origins(make_server):
    _server, base = make_server(origins=("https://app.example",))
    allowed = {"Origin": "https://app.example", "Access-Control-Request-Method": "POST"}
    status, response, _b = call(base, "OPTIONS", "/api/v1/print", headers=allowed)
    assert status == 204
    assert response.getheader("Access-Control-Allow-Origin") == "https://app.example"
    assert "Authorization" in response.getheader("Access-Control-Allow-Headers")
    assert response.getheader("Vary") == "Origin"
    status, response, _b = call(
        base, "GET", "/api/v1/labels", headers={"Origin": "https://app.example"}
    )
    assert (
        status == 200
        and response.getheader("Access-Control-Allow-Origin") == "https://app.example"
    )
    assert "Location" in response.getheader("Access-Control-Expose-Headers")
    status, response, _b = call(
        base, "OPTIONS", "/api/v1/print", headers={"Origin": "https://evil.example"}
    )
    assert status == 403 and response.getheader("Access-Control-Allow-Origin") is None
    assert call(base, "OPTIONS", "/api/v1/print")[0] == 405
    status, response, _b = call(base, "GET", "/api/v1/labels")
    assert response.getheader("Access-Control-Allow-Origin") is None


# --- 12: the smaller points ------------------------------------------------------------------


def test_the_server_binds_the_resolved_address(server_env):
    from tavletta.server.http import Config, Server

    service = Service(server_env.labels)
    server = Server(Config("localhost", 0), service)
    try:
        assert server.server_address[0] in ("127.0.0.1", "::1") and server.loopback
    finally:
        server.server_close()
        service.close(timeout=1)
    assert server_http.resolve("no-such-host.invalid") == "no-such-host.invalid"


def test_mcp_no_longer_offers_2025_03_26(server_env):
    mcp = Mcp(Service(server_env.labels))
    answer = mcp.handle(request("initialize", {"protocolVersion": "2025-03-26"}))
    assert answer["result"]["protocolVersion"] == LEGACY[0] == "2025-11-25"
    assert "2025-03-26" not in LEGACY


def test_cancelled_notification_ends_the_wait(server_env):
    settings = {"printers": {"fake-mono": {"host": "mono.local"}}}
    service = Service(server_env.labels, settings=lambda: Settings(dict(settings)))
    mcp = Mcp(service)
    printer = server_env.printer
    printer.gate = threading.Event()
    answers = []
    message = request(
        "tools/call", {"name": "print_label", "arguments": {"label": "box", "wait": 30}}, 7
    )
    worker = threading.Thread(target=lambda: answers.append(mcp.handle(message)))
    started = time.monotonic()
    worker.start()
    assert printer.started.wait(10)
    mcp.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    mcp.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": "junk"})
    worker.join(10)
    assert time.monotonic() - started < 10
    assert answers[0]["result"]["structuredContent"]["state"] == "printing"
    printer.gate.set()
    service.close(timeout=5)


def test_discovery_runs_once_for_callers_at_the_same_time(server_env, monkeypatch):
    calls = []

    def discover(timeout=3.0):
        calls.append(1)
        time.sleep(0.3)
        return []

    monkeypatch.setattr(server_env.printer, "discover", discover)
    service = Service(server_env.labels, discover=True)
    threads = [threading.Thread(target=service.printers) for _n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(calls) == 1
    service.close(timeout=1)


def test_openapi_documents_the_errors():
    paths = spec()["paths"]
    assert "401" not in paths["/health"]["get"]["responses"]
    print_answers = paths["/print"]["post"]["responses"]
    for code in ("401", "403", "408", "411", "413", "415", "421", "429"):
        assert code in print_answers, code
    assert "411" not in paths["/labels"]["get"]["responses"]


def test_tokens_must_be_printable_ascii():
    with pytest.raises(server_main.CliError):
        server_main.read_token(None, {"TAVLETTA_TOKEN": "geheim-token-mit-ümlaut"})
    with pytest.raises(server_main.CliError):
        server_main.read_token(None, {"TAVLETTA_TOKEN": "a token with spaces in it"})


def test_jobs_queue_clears_run_when_canceled_while_queued():
    queue = jobs_module.JobQueue(lambda job, error: None)
    gate = threading.Event()
    first = queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: gate.wait(5) and None))
    second = queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: None))
    queue.cancel(second)
    assert second.run is None and second.state == "canceled"
    gate.set()
    assert first.done.wait(5) and first.run is None
    queue.close(timeout=5)


def test_label_file_kept_unchanged_by_refusals(api, server_env):
    label_file(server_env.labels, "big.tavletta", *[TextElement(text="x") for _n in range(201)])
    status, _r, _b = call(api, "GET", "/api/v1/labels/big")
    assert status == 200  # describing is fine, rendering is refused
    assert call(api, "POST", "/api/v1/preview", body={"label": "big"})[0] == 413
