"""The print server's REST API: an in-process server on 127.0.0.1 and a free
port, a fake printer; the real printers cannot be reached (server_env)."""

from __future__ import annotations

import http.client
import io
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PIL import Image

from etiketto import cli
from etiketto.backends.base import PaperJam, PrinterInfo
from etiketto.model import CodeElement, Label, TextElement
from etiketto.server import jobs as jobs_module
from etiketto.server import main as server_main
from etiketto.server.http import _host_name, _matches, binds_loopback, is_loopback
from etiketto.server.openapi import spec
from etiketto.ui import i18n

TOKEN = "s3cret-token-for-tests"


def call(base, method, path, body=None, headers=None, token=None, raw=None):
    """(status, headers, JSON or bytes) of one request."""
    url = urlsplit(base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=20)
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    kind = response.getheader("Content-Type", "")
    parsed = json.loads(payload) if kind.startswith("application/json") else payload
    return response.status, response, parsed


def label_file(labels: Path, name: str, *elements, **fields) -> Path:
    fields.setdefault("printer", "fake-mono")
    fields.setdefault("tape_mm", 12)
    path = labels / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Label(elements=list(elements), **fields).save(path)
    return path


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base, service=server.service)


# --- health, OpenAPI, routes --------------------------------------------------------


def test_health_and_unknown_paths(api):
    status, response, body = call(api.base, "GET", "/api/v1/health")
    assert status == 200 and body["status"] == "ok"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Server") == "Etiketto"
    assert call(api.base, "GET", "/api/v1/nothing")[0] == 404
    assert call(api.base, "GET", "/elsewhere")[0] == 404
    status, response, body = call(api.base, "POST", "/api/v1/health", body={})
    assert status == 405 and body["error"]["code"] == "method_not_allowed"
    assert call(api.base, "PUT", "/api/v1/labels")[0] == 405
    assert call(api.base, "PATCH", "/api/v1/labels")[0] == 405
    assert call(api.base, "OPTIONS", "/api/v1/print")[0] == 405


def test_openapi_describes_every_route(api):
    status, _response, document = call(api.base, "GET", "/api/v1/openapi.json")
    assert status == 200
    assert document["openapi"].startswith("3.1")
    assert document["info"]["version"]
    assert "/mcp" in document["info"]["description"]
    from etiketto.server.http import ROUTES

    for verb, pattern in ROUTES:
        parts = ["{name}" if p == "*" else ("{id}" if p == "?" else p) for p in pattern]
        path = "/" + "/".join(parts)
        assert verb.lower() in document["paths"][path], path
    # every reference points somewhere
    text = json.dumps(document)
    for chunk in text.split('"$ref": "#/components/')[1:]:
        kind, name = chunk.split('"')[0].split("/")
        assert name in document["components"][kind]
    assert "/mcp" not in spec(mcp=False)["info"]["description"]


def test_route_patterns():
    assert _matches(("labels", "*"), ["labels", "a", "b.etiketto"])
    assert not _matches(("labels", "*"), ["labels"])
    assert _matches(("jobs", "?"), ["jobs", "x"])
    assert not _matches(("jobs", "?"), ["jobs", ""])
    assert not _matches(("jobs", "?"), ["jobs", "x", "y"])


# --- security -----------------------------------------------------------------------


def test_token_is_required_except_for_health(make_server):
    _server, base = make_server(token=TOKEN)
    assert call(base, "GET", "/api/v1/health")[0] == 200
    status, response, body = call(base, "GET", "/api/v1/labels")
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert response.getheader("WWW-Authenticate") == "Bearer"
    assert call(base, "GET", "/api/v1/labels", token="wrong")[0] == 401
    assert call(base, "GET", "/api/v1/labels", headers={"Authorization": TOKEN})[0] == 401
    assert call(base, "GET", "/api/v1/labels", token=TOKEN)[0] == 200
    assert call(base, "POST", "/mcp", body={}, headers={"Accept": "application/json"})[0] == 401


def test_wrong_tokens_block_the_address(make_server):
    _server, base = make_server(token=TOKEN)
    for _n in range(10):
        assert call(base, "GET", "/api/v1/labels", token="nope")[0] == 401
    status, _response, body = call(base, "GET", "/api/v1/labels", token=TOKEN)
    assert status == 429 and body["error"]["code"] == "too_many_requests"
    assert call(base, "GET", "/api/v1/health")[0] == 200


def test_rate_limit_forgets_after_the_window(monkeypatch):
    from etiketto.server import http as server_http

    limit = server_http.RateLimit(limit=2, window=60)
    clock = [100.0]
    monkeypatch.setattr(server_http.time, "monotonic", lambda: clock[0])
    assert not limit.blocked("a")
    limit.failed("a")
    limit.failed("a")
    assert limit.blocked("a")
    clock[0] += 61
    assert not limit.blocked("a")
    for n in range(10_002):
        limit.failed(str(n))
    assert len(limit._failures) < 10_000


def test_dns_rebinding_and_origins(make_server):
    _server, base = make_server(origins=("https://app.example",))
    port = urlsplit(base).port
    for host in ("evil.example", "evil.example:%d" % port, "192.0.2.1"):
        status, _r, body = call(base, "GET", "/api/v1/labels", headers={"Host": host})
        assert status == 421 and body["error"]["code"] == "wrong_host"
    for host in ("localhost:%d" % port, "127.0.0.1", "[::1]:%d" % port):
        assert call(base, "GET", "/api/v1/labels", headers={"Host": host})[0] == 200
    status, _r, body = call(
        base, "GET", "/api/v1/labels", headers={"Origin": "https://evil.example"}
    )
    assert status == 403 and body["error"]["code"] == "origin_not_allowed"
    assert call(base, "GET", "/api/v1/labels", headers={"Origin": "null"})[0] == 403
    own = "http://127.0.0.1:%d" % port
    assert call(base, "GET", "/api/v1/labels", headers={"Origin": own})[0] == 200
    assert call(base, "GET", "/api/v1/labels", headers={"Origin": "https://app.example"})[0] == 200


def test_post_needs_json_and_a_length(api):
    status, _r, body = call(api.base, "POST", "/api/v1/preview", raw=b"{}", headers={})
    assert status == 415 and body["error"]["code"] == "not_json"
    status, _r, body = call(
        api.base, "POST", "/api/v1/preview", raw=b"{", headers={"Content-Type": "application/json"}
    )
    assert status == 400 and body["error"]["code"] == "not_json"
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body=[1])
    assert status == 400 and body["error"]["code"] == "bad_request"
    status, _r, body = call(
        api.base,
        "POST",
        "/api/v1/preview",
        raw=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "x"},
    )
    assert status == 411


def test_chunked_bodies_are_refused(api):
    url = urlsplit(api.base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    connection.putrequest("POST", "/api/v1/preview")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    connection.send(b"2\r\n{}\r\n0\r\n\r\n")
    response = connection.getresponse()
    assert response.status == 411
    connection.close()


def test_body_size_limit(make_server):
    _server, base = make_server(max_body=100)
    status, _r, body = call(base, "POST", "/api/v1/preview", body={"label": "x" * 200})
    assert status == 413 and body["error"]["code"] == "too_large"


def test_connection_limit(api, monkeypatch):
    server = api.server
    for _n in range(32):
        assert server._slots.acquire(blocking=False)
    try:
        with pytest.raises((ConnectionError, http.client.HTTPException, OSError)):
            call(api.base, "GET", "/api/v1/health")
    finally:
        for _n in range(32):
            server._slots.release()
    assert call(api.base, "GET", "/api/v1/health")[0] == 200


def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("[::1]")
    assert is_loopback("localhost") and is_loopback("127.5.5.5")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.0.2.1")
    assert not is_loopback("example.org")
    assert binds_loopback("localhost") and binds_loopback("127.0.0.1")
    assert not binds_loopback("192.0.2.1")
    assert not binds_loopback("no-such-host.invalid")
    assert _host_name("[::1]:8790") == "::1"
    assert _host_name("localhost:8790") == "localhost"
    assert _host_name("[::1") == "[::1"
    assert _host_name("::1") == "::1"


def test_internal_errors_are_answered(api, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(api.service, "list_labels", broken)
    status, _r, body = call(api.base, "GET", "/api/v1/labels")
    assert status == 500 and body["error"]["code"] == "internal"


# --- paths ------------------------------------------------------------------------------


def test_labels_are_listed_from_the_folder_only(api, server_env, tmp_path):
    labels = server_env.labels
    label_file(labels, "shelf/jar.etiketto")
    label_file(labels, ".hidden/secret.etiketto")
    label_file(labels, ".dot.etiketto")
    (labels / "notes.txt").write_text("x")
    outside = label_file(tmp_path, "outside.etiketto")
    (labels / "link.etiketto").symlink_to(outside)
    (labels / "inner.etiketto").symlink_to(labels / "box.etiketto")
    status, _r, body = call(api.base, "GET", "/api/v1/labels")
    assert status == 200
    names = [entry["name"] for entry in body["labels"]]
    assert names == ["box.etiketto", "inner.etiketto", "shelf/jar.etiketto"]
    assert body["labels"][0]["bytes"] > 0 and body["labels"][0]["modified"]


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "..%2Foutside",
        "%2Fetc%2Fpasswd",
        "shelf/../../outside",
        ".hidden/secret",
        "a%00b",
        "a%5Cb",
    ],
)
def test_label_names_cannot_leave_the_folder(api, server_env, tmp_path, name):
    label_file(tmp_path, "outside.etiketto")
    label_file(server_env.labels, ".hidden/secret.etiketto")
    status, _r, body = call(api.base, "GET", "/api/v1/labels/" + name)
    assert status in (400, 404), body
    assert body["error"]["code"] in ("invalid_field", "label_not_found")


def test_symlinks_out_of_the_folder_are_not_followed(api, server_env, tmp_path):
    outside = label_file(tmp_path, "outside.etiketto", TextElement(text="secret"))
    (server_env.labels / "link.etiketto").symlink_to(outside)
    (server_env.labels / "dir").symlink_to(tmp_path)
    for name in ("link", "dir/outside.etiketto"):
        status, _r, body = call(api.base, "GET", "/api/v1/labels/" + name)
        assert status == 404 and body["error"]["code"] == "label_not_found"
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label": "link"})
    assert status == 404


def test_folders_and_other_files_are_no_labels(api, server_env):
    (server_env.labels / "folder.etiketto").mkdir()
    status, _r, _body = call(api.base, "GET", "/api/v1/labels/folder")
    assert status == 404
    (server_env.labels / "junk.etiketto").write_text("no json")
    status, _r, body = call(api.base, "GET", "/api/v1/labels/junk")
    assert status == 422 and body["error"]["code"] == "label_not_label"
    (server_env.labels / "locked.etiketto").write_text("{}")
    os.chmod(server_env.labels / "locked.etiketto", 0)
    try:
        if not os.access(server_env.labels / "locked.etiketto", os.R_OK):
            status, _r, body = call(api.base, "GET", "/api/v1/labels/locked")
            assert status == 422 and body["error"]["code"] == "file_problem"
    finally:
        os.chmod(server_env.labels / "locked.etiketto", 0o600)


def test_describe_label(api, server_env):
    data = server_env.labels / "items.csv"
    data.write_text("Name,Code\nTea,4006381333931\n")
    label_file(
        server_env.labels,
        "items.etiketto",
        TextElement(text="{Name} {=lot} {=date}", name="title"),
        CodeElement(data="{Code}", symbology="ean13", name="code"),
        data_file=str(data),
        counter_next=5,
    )
    status, _r, body = call(api.base, "GET", "/api/v1/labels/items.etiketto")
    assert status == 200
    assert body["name"] == "items.etiketto"
    assert body["printer"]["id"] == "fake-mono"
    assert body["elements"][0] == {
        "index": 1,
        "kind": "text",
        "name": "title",
        "text": "{Name} {=lot} {=date}",
    }
    assert body["elements"][1]["symbology"] == "ean13"
    assert body["variables"] == ["lot"] and body["builtin_variables"] == ["date"]
    assert body["columns"] == ["Name", "Code"]
    assert body["data_file"] == "items.csv" and not body["data_file_unusable"]
    assert body["counter"] == {"used": False, "next": 5, "step": 1}
    assert set(body["features"]) == {"serial", "variables"}


def test_describe_label_with_a_data_file_elsewhere(api, server_env, tmp_path, png_b64):
    from etiketto.model import ImageElement

    data = tmp_path / "rows.csv"
    data.write_text("Name\nTea\n")
    label_file(
        server_env.labels,
        "far.etiketto",
        ImageElement(data=png_b64(), name="logo.png"),
        data_file=str(data),
    )
    status, _r, body = call(api.base, "GET", "/api/v1/labels/far")
    assert body["data_file"] is None and body["data_file_unusable"]
    assert body["elements"] == [{"index": 1, "kind": "image", "file": "logo.png"}]
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label": "far"})
    assert status == 422 and body["error"]["code"] == "data_outside"


# --- printers -------------------------------------------------------------------------


def test_printers(api, server_env):
    status, _r, body = call(api.base, "GET", "/api/v1/printers")
    assert status == 200
    fake = next(p for p in body["printers"] if p["id"] == "fake-mono")
    assert fake["configured_host"] == "mono.local"
    assert fake["found"] == [{"name": "Fake Mono", "host": "mono.local", "address": "192.0.2.200"}]
    assert fake["kind"] == "tape" and fake["tape_widths_mm"] == [12, 24]
    ids = [p["id"] for p in body["printers"]]
    assert {"vc500w", "cab", "system", "fake-mono"} <= set(ids)
    status, _r, body = call(api.base, "GET", "/api/v1/printers?discover=false")
    assert all(p["found"] == [] for p in body["printers"])


def test_discovery_is_remembered_and_survives_broken_plugins(api, server_env, monkeypatch):
    calls = []

    def discover(timeout=3.0):
        calls.append(1)
        raise RuntimeError("broken plugin")

    monkeypatch.setattr(server_env.printer, "discover", discover)
    for _n in range(2):
        status, _r, body = call(api.base, "GET", "/api/v1/printers")
        assert status == 200
    assert len(calls) == 1
    assert next(p for p in body["printers"] if p["id"] == "fake-mono")["found"] == []


def test_printer_status(api, server_env):
    server_env.printer.info = PrinterInfo(
        "Fake", 12, 900, 1000, "IDLE", error="cover_open", firmware="1.2"
    )
    status, _r, body = call(api.base, "GET", "/api/v1/printers/fake-mono/status")
    assert status == 200
    assert body["host"] == "mono.local" and body["state"] == "IDLE"
    assert body["error"]["code"] == "cover_open" and "cover" in body["error"]["message"]
    assert body["media"]["remaining_mm"] == 900 and body["dpi"] == 203
    assert body["modes"] == ["standard"] and body["firmware"] == "1.2"
    status, _r, body = call(api.base, "GET", "/api/v1/printers/fake-mono/status?host=other.local")
    assert server_env.printer.queries[-1] == "other.local"


def test_printer_status_problems(api, server_env, make_server):
    status, _r, body = call(api.base, "GET", "/api/v1/printers/nope/status")
    assert status == 404 and body["error"]["code"] == "printer_not_found"
    server_env.printer.error = PaperJam("jam")
    status, _r, body = call(api.base, "GET", "/api/v1/printers/fake-mono/status")
    assert status == 502 and body["error"]["code"] == "printer_jam"
    server_env.printer.error = RuntimeError("odd")
    status, _r, body = call(api.base, "GET", "/api/v1/printers/fake-mono/status")
    assert status == 502 and body["error"]["code"] == "printer_generic"
    status, _r, body = call(api.base, "GET", "/api/v1/printers/fake-mono/status?host=a%20b")
    assert status == 400 and body["error"]["code"] == "invalid_field"
    _server, bare = make_server(settings={})
    status, _r, body = call(bare, "GET", "/api/v1/printers/fake-mono/status")
    assert status == 400 and body["error"]["code"] == "no_host"


def test_allowed_printer_addresses(make_server, server_env):
    _server, base = make_server(allowed=("*.local",))
    assert call(base, "GET", "/api/v1/printers/fake-mono/status")[0] == 200
    status, _r, body = call(base, "GET", "/api/v1/printers/fake-mono/status?host=10.0.0.1")
    assert status == 403 and body["error"]["code"] == "host_not_allowed"
    status, _r, body = call(
        base, "POST", "/api/v1/print", body={"label": "box", "host": "10.0.0.1"}
    )
    assert status == 403
    assert server_env.printer.jobs == []


# --- preview ------------------------------------------------------------------------------


def test_preview_png_and_json(api, server_env):
    status, response, png = call(api.base, "POST", "/api/v1/preview", body={"label": "box"})
    assert status == 200 and response.getheader("Content-Type") == "image/png"
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.height > 0
    assert response.getheader("X-Etiketto-Warnings") is None
    status, _r, body = call(
        api.base, "POST", "/api/v1/preview", body={"label": "box", "format": "json"}
    )
    assert body["width"] == image.width and body["warnings"] == []
    assert Image.open(io.BytesIO(__import__("base64").b64decode(body["png"]))).size == image.size
    status, _r, png = call(
        api.base, "POST", "/api/v1/preview", body={"label": "box", "view": "printer"}
    )
    assert status == 200 and Image.open(io.BytesIO(png)).format == "PNG"


def test_preview_values_and_warnings(api, server_env, monkeypatch):
    recorded = []
    real = server_env.printer.preview

    def preview(image, mode=""):
        recorded.append(image)
        return real(image, mode)

    monkeypatch.setattr(server_env.printer, "preview", preview)
    label_file(
        server_env.labels,
        "vars.etiketto",
        TextElement(text="{=lot} {=nope}", name="title"),
        CodeElement(data="12", symbology="ean13", name="code"),
    )
    status, response, _png = call(
        api.base, "POST", "/api/v1/preview", body={"label": "vars", "set": {"lot": 7}}
    )
    assert status == 200
    codes = response.getheader("X-Etiketto-Warnings").split(",")
    assert "unknown_variable" in codes and "ean13_digits" in codes
    status, _r, body = call(
        api.base,
        "POST",
        "/api/v1/preview",
        body={"label": "vars", "set": {"nothing": "x"}},
    )
    assert status == 400 and "nothing" in body["error"]["message"]


def test_preview_with_inline_label_content(api, server_env, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("Name\nhidden\n")
    content = Label(
        printer="fake-mono",
        tape_mm=12,
        elements=[TextElement(text="{Name}", name="t")],
        data_file=str(secret),
    ).to_dict()
    content["data_file"] = str(secret)
    status, _r, body = call(
        api.base,
        "POST",
        "/api/v1/preview",
        body={"label_content": content, "format": "json"},
    )
    # the data file a label sent along names is never read
    assert status == 200 and body["warnings"] == []
    status, _r, body = call(
        api.base,
        "POST",
        "/api/v1/preview",
        body={"label_content": content, "data_rows": [{"Other": "x"}], "format": "json"},
    )
    assert [w["code"] for w in body["warnings"]] == ["unknown_column"]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ({"printer": "no-such-printer"}, "label_newer"),
        ({"version": 99}, "label_newer"),
        ({"printer": "fake-mono", "tape_mm": 13}, "label_damaged"),
        ({"printer": "fake-mono", "tape_mm": 12, "elements": [{"kind": "text", "size": 1}]}, None),
        ({"printer": "fake-mono", "tape_mm": 12, "align": []}, "label_damaged"),
    ],
)
def test_inline_labels_are_checked_by_the_model(api, content, code):
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body={"label_content": content})
    if code is None:
        assert status == 200
    else:
        assert status == 422 and body["error"]["code"] == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "no_label"),
        ({"label": "box", "label_content": {}}, "no_label"),
        ({"label_content": []}, "invalid_field"),
        ({"label": 5}, "invalid_field"),
        ({"label": "box", "copeis": 5}, "unknown_field"),
        ({"label": "box", "row": 0}, "invalid_field"),
        ({"label": "box", "row": True}, "invalid_field"),
        ({"label": "box", "format": "gif"}, "invalid_field"),
        ({"label": "box", "set": []}, "invalid_field"),
        ({"label": "box", "set": {"title": True}}, "invalid_field"),
        ({"label": "box", "set": {" ": "x"}}, "invalid_field"),
        ({"label": "box", "set": {"title": {"a": 1}}}, "invalid_field"),
        ({"label": "box", "counter_start": -1}, "invalid_field"),
        ({"label": "box", "data_rows": []}, "invalid_field"),
        ({"label": "box", "data_rows": [1]}, "invalid_field"),
        ({"label": "box", "data_rows": [{"": "x"}]}, "invalid_field"),
        ({"label": "box", "data_rows": [{"a": [1]}]}, "invalid_field"),
        ({"label": "box", "data_rows": [{"a": "x"}], "data_file": "a.csv"}, "data_conflict"),
        ({"label": "box", "data_file": "a.exe"}, "invalid_field"),
        ({"label": "box", "data_file": "missing.csv"}, "data_not_found"),
        ({"label": "box", "sheet": "One"}, "no_data"),
        ({"label": "box", "printer": "nope"}, "printer_not_found"),
        ({"label": "missing"}, "label_not_found"),
    ],
)
def test_preview_request_errors(api, body, code):
    status, _r, answer = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status in (400, 404), answer
    assert answer["error"]["code"] == code


def test_preview_of_a_data_row(api, server_env):
    (server_env.labels / "rows.csv").write_text("Name\nTea\nCoffee\n")
    label_file(server_env.labels, "named.etiketto", TextElement(text="{Name}", name="t"))
    body = {"label": "named", "data_file": "rows.csv", "row": 2, "format": "json"}
    status, _r, answer = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status == 200 and answer["warnings"] == []
    status, _r, answer = call(api.base, "POST", "/api/v1/preview", body=dict(body, row=3))
    assert status == 400 and answer["error"]["code"] == "invalid_field"
    (server_env.labels / "bad.csv").write_bytes(b"\x00\x01PK")
    status, _r, answer = call(
        api.base, "POST", "/api/v1/preview", body=dict(body, data_file="bad.csv", row=1)
    )
    assert status == 422 and answer["error"]["code"] == "data_problem"


def test_preview_printer_view_failure(api, server_env, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot draw")

    monkeypatch.setattr(server_env.printer, "prepare", broken)
    status, _r, body = call(
        api.base, "POST", "/api/v1/preview", body={"label": "box", "view": "printer"}
    )
    assert status == 422 and body["error"]["code"] == "prepare_failed"


def test_inline_rows_limits(api, monkeypatch):
    from etiketto.merge import limits

    monkeypatch.setattr(limits, "MAX_ROWS", 2)
    monkeypatch.setattr(limits, "MAX_COLUMNS", 2)
    monkeypatch.setattr(limits, "MAX_CELLS", 3)
    base = {"label": "box"}
    rows = [{"a": "1"}, {"a": "2"}, {"a": "3"}]
    status, _r, body = call(api.base, "POST", "/api/v1/preview", body=dict(base, data_rows=rows))
    assert status == 413 and body["error"]["code"] == "too_large"
    rows = [{"a": 1, "b": 2, "c": 3}]
    assert call(api.base, "POST", "/api/v1/preview", body=dict(base, data_rows=rows))[0] == 413
    rows = [{"a": 1, "b": None}, {"a": 2.5}]
    assert call(api.base, "POST", "/api/v1/preview", body=dict(base, data_rows=rows))[0] == 413


# --- printing ------------------------------------------------------------------------------


def test_print_and_follow_the_job(api, server_env):
    status, response, job = call(
        api.base, "POST", "/api/v1/print", body={"label": "box", "copies": 2, "wait": 10}
    )
    assert status == 200, job
    assert job["state"] == "done" and job["printed"] == 2 and job["total"] == 2
    assert response.getheader("Location") == "/api/v1/jobs/" + job["id"]
    assert job["host"] == "mono.local" and job["label"] == "box.etiketto"
    assert [entry[1] for entry in server_env.printer.jobs] == ["mono.local", "mono.local"]
    # each copy has its own number, the next is saved
    assert Label.saved_counter(server_env.labels / "box.etiketto") == 3
    status, _r, again = call(api.base, "GET", "/api/v1/jobs/" + job["id"])
    assert status == 200 and again["state"] == "done" and again["finished"]
    status, _r, body = call(api.base, "DELETE", "/api/v1/jobs/" + job["id"])
    assert status == 409 and body["error"]["code"] == "job_finished"
    status, _r, body = call(api.base, "GET", "/api/v1/jobs/unknown")
    assert status == 404 and body["error"]["code"] == "job_not_found"


def test_print_without_waiting(api, server_env):
    status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
    assert status in (200, 202) and job["state"] in ("queued", "printing", "done")
    wait_for(lambda: call(api.base, "GET", "/api/v1/jobs/" + job["id"])[2]["state"] == "done")


def test_the_counter_goes_on_from_job_to_job(api, server_env):
    for _n in range(3):
        call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert len(server_env.printer.jobs) == 3
    assert Label.saved_counter(server_env.labels / "box.etiketto") == 4


def test_print_options(api, server_env):
    body = {"label": "box", "counter_start": 50, "no_save_counter": True, "wait": 10}
    status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "done"
    assert Label.saved_counter(server_env.labels / "box.etiketto") == 1
    body = {"label": "box", "counter_start": 50, "host": "elsewhere.local", "wait": 10}
    status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["host"] == "elsewhere.local"
    assert Label.saved_counter(server_env.labels / "box.etiketto") == 51


def test_print_inline_label_does_not_save(api, server_env):
    content = Label(printer="fake-mono", tape_mm=12, elements=[TextElement(text="{=counter}")])
    body = {"label_content": content.to_dict(), "wait": 10}
    status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "done" and job["label"] is None


def test_print_refuses_what_would_print_wrongly(api, server_env):
    label_file(server_env.labels, "empty.etiketto")
    label_file(server_env.labels, "unknown.etiketto", TextElement(text="{=nope}"))
    for name in ("empty", "unknown"):
        status, _r, body = call(api.base, "POST", "/api/v1/print", body={"label": name})
        assert status == 422 and body["error"]["code"] == "label_unprintable"
    assert server_env.printer.jobs == []


def test_print_keeps_warnings_that_do_not_refuse(api, server_env):
    label_file(
        server_env.labels, "check.etiketto", CodeElement(data="4006381333932", symbology="ean13")
    )
    status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "check", "wait": 10})
    assert job["state"] == "done"
    assert [w["code"] for w in job["warnings"]] == ["ean13_check"]


def test_print_needs_a_host(make_server, server_env):
    _server, base = make_server(settings={})
    server_env.printer.found = []
    status, _r, body = call(base, "POST", "/api/v1/print", body={"label": "box"})
    assert status == 400 and body["error"]["code"] == "no_host"
    server_env.printer.found = server_env.printer.found + [
        SimpleNamespace(name="One", host="one.local", address="192.0.2.1")
    ]
    _server, base = make_server(settings={})
    status, _r, job = call(base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["host"] == "one.local"


def test_print_refuses_a_counter_it_cannot_save(api, server_env):
    path = server_env.labels / "box.etiketto"
    os.chmod(path, 0o444)
    try:
        if os.access(path, os.W_OK):
            pytest.skip("running as root")
        status, _r, body = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
        assert status == 409 and body["error"]["code"] == "counter_not_saved"
        status, _r, job = call(
            api.base,
            "POST",
            "/api/v1/print",
            body={"label": "box", "no_save_counter": True, "wait": 10},
        )
        assert job["state"] == "done"
    finally:
        os.chmod(path, 0o644)


def test_printer_errors_fail_the_job_and_are_translated(api, server_env, monkeypatch):
    server_env.printer.error = PaperJam("jam")
    status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["state"] == "failed" and job["error"]["code"] == "printer_jam"
    english = job["error"]["message"]
    seen = []

    class Catalog(i18n.gettext.NullTranslations):
        def gettext(self, message):
            seen.append(message)
            return "DE:" + message

    monkeypatch.setattr(i18n, "translation_for", lambda languages: Catalog())
    _status, _r, german = call(
        api.base, "GET", "/api/v1/jobs/" + job["id"], headers={"Accept-Language": "de-CH"}
    )
    assert german["error"]["message"].startswith("DE:") and german["error"]["message"] != english
    server_env.printer.error = RuntimeError("odd")
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["error"]["code"] == "printer_generic"


def test_a_failing_print_still_saves_the_counter(api, server_env, monkeypatch):
    label_file(server_env.labels, "count.etiketto", TextElement(text="{=counter}"))
    sent = []

    def print_label(image, host, options, progress=None):
        if sent:
            raise PaperJam("jam")
        sent.append(image)

    monkeypatch.setattr(server_env.printer, "print_label", print_label)
    body = {"label": "count", "copies": 3, "wait": 10}
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "failed" and job["printed"] == 1
    assert Label.saved_counter(server_env.labels / "count.etiketto") == 2


def test_prepare_failure_fails_the_job(api, server_env, monkeypatch):
    def prepare(label, options, safe_mm=1.5):
        raise RuntimeError("cannot draw")  # the check renders without prepare()

    monkeypatch.setattr(server_env.printer, "prepare", prepare)
    label_file(server_env.labels, "n.etiketto", TextElement(text="{=counter}"))
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "n", "wait": 10})
    assert job["state"] == "failed" and job["error"]["code"] == "prepare_failed"


def test_counter_save_failure_after_printing(api, server_env, monkeypatch):
    def refuse(path, counter):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(Label, "store_counter", staticmethod(refuse))
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["state"] == "failed" and job["error"]["code"] == "counter_not_saved"
    assert job["printed"] == 1 and "box.etiketto" in job["error"]["message"]

    def broken(path, counter):
        raise ValueError("not a label any more")

    monkeypatch.setattr(Label, "store_counter", staticmethod(broken))
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["error"]["code"] == "counter_not_saved"


def test_a_locked_label_fails_the_job(api, server_env, monkeypatch):
    from etiketto.model import LockTimeout
    from etiketto.server import service as service_module

    def locked(path, waiting=None, timeout=60.0):
        raise LockTimeout(11, "locked")

    monkeypatch.setattr(service_module, "locked", locked)
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "box", "wait": 10})
    assert job["state"] == "failed" and job["error"]["code"] == "label_locked"


# --- serial labels ------------------------------------------------------------------


def test_print_data_rows_inline(api, server_env):
    label_file(
        server_env.labels,
        "named.etiketto",
        TextElement(text="{Name} {=counter}", name="t"),
    )
    rows = [{"Name": "Tea"}, {"Name": "Coffee"}, {"Name": "Cocoa"}]
    body = {"label": "named", "data_rows": rows, "rows": "2-", "copies": 2, "wait": 10}
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "done" and job["total"] == 4 and job["printed"] == 4
    assert job["rows_printed"] == 2
    assert Label.saved_counter(server_env.labels / "named.etiketto") == 5


def test_print_the_labels_own_data_file(api, server_env):
    data = server_env.labels / "items.csv"
    data.write_text("Name\nTea\nCoffee\n")
    label_file(
        server_env.labels, "items.etiketto", TextElement(text="{Name}"), data_file=str(data)
    )
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body={"label": "items", "wait": 10})
    assert job["state"] == "done" and job["total"] == 2
    status, _r, body = call(
        api.base, "POST", "/api/v1/print", body={"label": "items", "rows": "5-"}
    )
    assert status == 400 and body["error"]["code"] == "no_rows"
    status, _r, body = call(
        api.base, "POST", "/api/v1/print", body={"label": "items", "rows": "x"}
    )
    assert status == 400 and body["error"]["code"] == "invalid_field"
    status, _r, body = call(api.base, "POST", "/api/v1/print", body={"label": "items", "rows": 3})
    assert status == 400
    status, _r, body = call(
        api.base, "POST", "/api/v1/print", body={"label": "items", "sheet": "Other"}
    )
    assert status == 422 and body["error"]["code"] == "data_problem"


def test_serial_rows_that_cannot_print_are_refused(api, server_env):
    label_file(server_env.labels, "ean.etiketto", CodeElement(data="{Code}", symbology="ean13"))
    body = {"label": "ean", "data_rows": [{"Code": "4006381333931"}, {"Code": "abc"}]}
    status, _r, answer = call(api.base, "POST", "/api/v1/print", body=body)
    assert status == 422 and answer["error"]["code"] == "label_unprintable"
    assert "2" in answer["error"]["message"]


def test_serial_printer_failure(api, server_env, monkeypatch):
    label_file(server_env.labels, "named.etiketto", TextElement(text="{Name}"))
    sent = []

    def print_label(image, host, options, progress=None):
        if len(sent) == 1:
            raise PaperJam("jam")
        sent.append(image)

    monkeypatch.setattr(server_env.printer, "print_label", print_label)
    body = {"label": "named", "data_rows": [{"Name": "a"}, {"Name": "b"}], "wait": 10}
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["state"] == "failed" and job["error"]["code"] == "printer_jam"
    assert "row 2" in job["error"]["message"] and job["rows_printed"] == 1

    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot draw")

    monkeypatch.setattr(server_env.printer, "prepare", broken)
    _status, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert job["error"]["code"] == "prepare_failed"


# --- the queue ------------------------------------------------------------------------


def test_one_job_per_printer_and_cancel_while_waiting(api, server_env):
    printer = server_env.printer
    printer.gate = threading.Event()
    _s, _r, first = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
    assert printer.started.wait(10)
    _s, _r, second = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
    assert call(api.base, "GET", "/api/v1/jobs/" + first["id"])[2]["state"] == "printing"
    assert call(api.base, "GET", "/api/v1/jobs/" + second["id"])[2]["state"] == "queued"
    status, _r, canceled = call(api.base, "DELETE", "/api/v1/jobs/" + second["id"])
    assert status == 202 and canceled["state"] == "canceled"
    printer.gate.set()
    wait_for(lambda: call(api.base, "GET", "/api/v1/jobs/" + first["id"])[2]["state"] == "done")
    assert len(printer.jobs) == 1


def test_cancel_between_copies(api, server_env, monkeypatch):
    printer = server_env.printer
    printer.gate = threading.Event()
    body = {"label": "box", "copies": 5}
    _s, _r, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert printer.started.wait(10)
    status, _r, stopping = call(api.base, "DELETE", "/api/v1/jobs/" + job["id"])
    assert status == 202 and stopping["cancel_requested"]
    printer.gate.set()
    wait_for(lambda: call(api.base, "GET", "/api/v1/jobs/" + job["id"])[2]["state"] == "canceled")
    final = call(api.base, "GET", "/api/v1/jobs/" + job["id"])[2]
    assert final["printed"] == 1 and final["total"] == 5
    assert Label.saved_counter(server_env.labels / "box.etiketto") == 2


def test_cancel_between_rows(api, server_env):
    printer = server_env.printer
    printer.gate = threading.Event()
    label_file(server_env.labels, "named.etiketto", TextElement(text="{Name}"))
    rows = [{"Name": "a"}, {"Name": "b"}, {"Name": "c"}]
    _s, _r, job = call(
        api.base, "POST", "/api/v1/print", body={"label": "named", "data_rows": rows}
    )
    assert printer.started.wait(10)
    call(api.base, "DELETE", "/api/v1/jobs/" + job["id"])
    printer.gate.set()
    wait_for(lambda: call(api.base, "GET", "/api/v1/jobs/" + job["id"])[2]["state"] == "canceled")
    assert len(printer.jobs) == 1


def test_queue_limit(api, server_env, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_PENDING", 1)
    printer = server_env.printer
    printer.gate = threading.Event()
    call(api.base, "POST", "/api/v1/print", body={"label": "box", "no_save_counter": True})
    status, _r, body = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
    assert status == 429 and body["error"]["code"] == "queue_full"
    printer.gate.set()


def test_finished_jobs_are_forgotten(monkeypatch):
    queue = jobs_module.JobQueue(lambda job, error: None)
    monkeypatch.setattr(jobs_module, "MAX_KEPT", 1)
    made = []
    for _n in range(3):
        job = jobs_module.PrintJob("p", "h", "", 1, lambda job: None)
        queue.submit(job)
        assert job.done.wait(5)
        made.append(job)
    queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: None))
    assert queue.get(made[0].id) is None and queue.get(made[1].id) is None
    queue.close(timeout=5)
    with pytest.raises(jobs_module.QueueFull):
        queue.submit(jobs_module.PrintJob("p", "h", "", 1, lambda job: None))


def test_closing_cancels_waiting_jobs():
    gate = threading.Event()
    started = threading.Event()

    def slow(job):
        started.set()
        gate.wait(5)
        job.printed = 1

    queue = jobs_module.JobQueue(lambda job, error: None)
    first = queue.submit(jobs_module.PrintJob("p", "h", "", 1, slow))
    assert started.wait(5)
    second = queue.submit(jobs_module.PrintJob("p", "h", "", 1, slow))
    closer = threading.Thread(target=queue.close, kwargs={"timeout": 5})
    closer.start()
    wait_for(lambda: second.state == "canceled")
    gate.set()
    closer.join(5)
    assert first.state == "done" and second.state == "canceled"


def test_jobs_of_different_printers_run_side_by_side(api, server_env):
    printer = server_env.printer
    printer.gate = threading.Event()
    _s, _r, first = call(api.base, "POST", "/api/v1/print", body={"label": "box"})
    assert printer.started.wait(10)
    body = {"label": "box", "host": "second.local", "no_save_counter": True}
    _s, _r, second = call(api.base, "POST", "/api/v1/print", body=body)
    wait_for(
        lambda: call(api.base, "GET", "/api/v1/jobs/" + second["id"])[2]["state"] == "printing"
    )
    printer.gate.set()
    for job in (first, second):
        wait_for(
            lambda job=job: (
                call(api.base, "GET", "/api/v1/jobs/" + job["id"])[2]["state"] == "done"
            )
        )


# --- languages ----------------------------------------------------------------------


def test_accepted_languages():
    assert i18n.accepted_languages("de-CH,de;q=0.9,en;q=0.8") == ["de_CH", "de"]
    assert i18n.accepted_languages("fr;q=0.5, pt-BR") == ["pt_BR", "fr"]
    assert i18n.accepted_languages("en, de") == []
    assert i18n.accepted_languages("*, de;q=0, x;q=abc, zh-Hant-TW") == ["zh_TW"]
    assert i18n.accepted_languages("") == []


def test_errors_follow_accept_language(api, monkeypatch, tmp_path):
    import gettext

    po = tmp_path / "de.po"
    po.write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgid "Not found"\nmsgstr "Nicht gefunden"\n'
    )
    mo = tmp_path / "locale" / "de" / "LC_MESSAGES" / "etiketto.mo"
    mo.parent.mkdir(parents=True)
    import subprocess  # nosec B404 - msgfmt from gettext

    subprocess.run(["msgfmt", "-o", str(mo), str(po)], check=True)  # nosec B603 B607
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path / "locale")
    i18n.translation_for.cache_clear()
    try:
        _s, _r, body = call(api.base, "GET", "/nothing", headers={"Accept-Language": "de-CH"})
        assert body["error"]["message"] == "Nicht gefunden"
        _s, _r, body = call(api.base, "GET", "/nothing", headers={"Accept-Language": "en,de"})
        assert body["error"]["message"] == "Not found"
        assert isinstance(i18n.translation_for(("de",)), gettext.GNUTranslations)
    finally:
        i18n.translation_for.cache_clear()


# --- the command ------------------------------------------------------------------------


def test_token_from_file_and_environment(tmp_path, caplog):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o644)
    assert server_main.read_token(path, {}) == TOKEN
    assert "can be read by others" in caplog.text
    assert server_main.read_token(None, {"ETIKETTO_TOKEN": TOKEN}) == TOKEN
    assert server_main.read_token(None, {}) is None
    with pytest.raises(cli.CliError) as short:
        server_main.read_token(None, {"ETIKETTO_TOKEN": "short"})
    assert short.value.code == cli.EXIT_USAGE
    with pytest.raises(cli.CliError) as missing:
        server_main.read_token(tmp_path / "none", {})
    assert missing.value.code == cli.EXIT_FILE


def test_serve_refuses_other_addresses_without_a_token(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("ETIKETTO_TOKEN", raising=False)
    code = cli.main(["serve", "--host", "192.0.2.1", "--labels-dir", str(tmp_path)])
    assert code == cli.EXIT_USAGE
    assert "access token" in capsys.readouterr().err


def test_serve_reports_a_missing_folder_and_a_busy_port(monkeypatch, capsys, tmp_path, server_env):
    code = cli.main(["serve", "--labels-dir", str(tmp_path / "none")])
    assert code == cli.EXIT_FILE
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code = cli.main(["serve", "--port", str(port), "--labels-dir", str(tmp_path)])
    assert code == cli.EXIT_USAGE and "Cannot listen" in capsys.readouterr().err


def test_serve_runs_until_stopped(monkeypatch, tmp_path, server_env):
    started = {}

    def serve_forever(self):
        started["server"] = self
        raise KeyboardInterrupt

    from etiketto.server import http as server_http

    monkeypatch.setattr(server_http.Server, "serve_forever", serve_forever)
    monkeypatch.setattr(server_main.signal, "signal", lambda *args: None)
    monkeypatch.setenv("ETIKETTO_TOKEN", TOKEN)
    code = cli.main(["serve", "--port", "0", "--labels-dir", str(tmp_path), "--mcp"])
    assert code == cli.EXIT_OK
    assert started["server"].config.token == TOKEN and started["server"].mcp is not None


def test_default_labels_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(server_main, "default_labels_dir", lambda: tmp_path / "data" / "labels")
    folder = server_main.labels_dir(None)
    assert folder.is_dir() and folder == tmp_path / "data" / "labels"
    assert server_main.default_labels_dir.__module__  # the real one needs GLib only


def test_real_default_labels_folder():
    path = server_main.default_labels_dir()
    assert path.parts[-2:] == ("etiketto", "labels")
