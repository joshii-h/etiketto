"""MCP: the protocol (both eras), stdio as a subprocess and in-process, and
Streamable HTTP at /mcp of the in-process server with the fake printer."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess  # nosec B404 - runs this checkout's `tavletta mcp`
import sys
import threading
from pathlib import Path

import pytest
from test_server import TOKEN, call

from tavletta.backends.base import PaperJam
from tavletta.model import Label, TextElement
from tavletta.server import mcp as mcp_module
from tavletta.server.mcp import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY,
    METHOD_NOT_FOUND,
    MODERN,
    PARSE_ERROR,
    TOOLS,
    UNSUPPORTED_VERSION,
    Mcp,
    Session,
    serve_stdio,
)
from tavletta.server.service import Service
from tavletta.ui.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
ACCEPT = {"Accept": "application/json, text/event-stream"}


def request(method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return message


def modern(method, params=None, ident=1, version=MODERN[0]):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    return request(method, params, ident)


@pytest.fixture
def mcp(server_env):
    settings = {"printers": {"fake-mono": {"host": "mono.local"}}}
    service = Service(server_env.labels, settings=lambda: Settings(dict(settings)))
    yield Mcp(service)
    service.close(timeout=5)


# --- the protocol -------------------------------------------------------------------


def test_tool_definitions_are_valid():
    names = [tool["name"] for tool in TOOLS]
    assert names == [
        "list_printers",
        "printer_status",
        "list_labels",
        "describe_label",
        "preview_label",
        "print_label",
        "job_status",
        "cancel_job",
    ]
    for tool in TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert tool["description"] and tool["title"]
        json.dumps(tool)


def test_legacy_initialize_negotiates_the_version(mcp):
    session = Session(LEGACY[0])
    answer = mcp.handle(request("initialize", {"protocolVersion": "2025-06-18"}), session)
    result = answer["result"]
    assert result["protocolVersion"] == "2025-06-18" == session.version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "tavletta" and result["instructions"]
    answer = mcp.handle(request("initialize", {"protocolVersion": "1999-01-01"}))
    assert answer["result"]["protocolVersion"] == LEGACY[0]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.handle(request("ping"))["result"] == {}
    assert "resultType" not in mcp.handle(request("ping"))["result"]


def test_modern_requests(mcp):
    answer = mcp.handle(modern("server/discover"))["result"]
    assert answer["resultType"] == "complete"
    assert answer["supportedVersions"] == list(MODERN + LEGACY)
    assert answer["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "tavletta"
    tools = mcp.handle(modern("tools/list"))["result"]
    assert tools["resultType"] == "complete" and len(tools["tools"]) == len(TOOLS)
    error = mcp.handle(modern("tools/list", version="1900-01-01"))["error"]
    assert error["code"] == UNSUPPORTED_VERSION
    assert error["data"] == {"supported": list(MODERN + LEGACY), "requested": "1900-01-01"}
    message = modern("tools/list")
    del message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    assert mcp.handle(message)["error"]["code"] == INVALID_PARAMS
    assert mcp.handle(request("server/discover"))["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ([], INVALID_REQUEST),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, METHOD_NOT_FOUND),
        (request("tools/call", {"name": "nope"}), INVALID_PARAMS),
        (request("tools/call", {"name": "list_labels", "arguments": []}), INVALID_PARAMS),
    ],
)
def test_invalid_messages(mcp, message, code):
    assert mcp.handle(message)["error"]["code"] == code


def test_responses_from_the_client_are_ignored(mcp):
    assert mcp.handle({"jsonrpc": "2.0", "id": 5, "result": {}}) is None


def tool(mcp, name, **arguments):
    answer = mcp.handle(request("tools/call", {"name": name, "arguments": arguments}))
    return answer["result"]


def test_tools_list_and_describe(mcp, server_env):
    result = tool(mcp, "list_labels")
    assert result["structuredContent"]["labels"][0]["name"] == "box.tavletta"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    result = tool(mcp, "describe_label", label="box")
    assert result["structuredContent"]["elements"][0]["name"] == "title"
    result = tool(mcp, "list_printers", discover=False)
    assert any(p["id"] == "fake-mono" for p in result["structuredContent"]["printers"])
    result = tool(mcp, "printer_status", printer="fake-mono")
    assert result["structuredContent"]["host"] == "mono.local"


def test_tool_errors_are_results(mcp):
    result = tool(mcp, "describe_label", label="../x")
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_field"
    result = tool(mcp, "describe_label")
    assert result["isError"] and result["structuredContent"]["error"]["code"] == "invalid_field"
    result = tool(mcp, "job_status", job_id="nope")
    assert result["structuredContent"]["error"]["code"] == "job_not_found"
    result = tool(mcp, "print_label", label="box", copies=0)
    assert result["isError"]


def test_preview_tool_returns_an_image(mcp):
    result = tool(mcp, "preview_label", label="box", set={"title": "Hello"})
    image, text = result["content"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]).startswith(b"\x89PNG")
    assert json.loads(text["text"]) == result["structuredContent"]
    assert result["structuredContent"]["warnings"] == []


def test_print_tool_waits_and_cancel(mcp, server_env):
    result = tool(mcp, "print_label", label="box", copies=2)
    job = result["structuredContent"]
    assert job["state"] == "done" and job["printed"] == 2
    status = tool(mcp, "job_status", job_id=job["id"], wait=0)["structuredContent"]
    assert status["id"] == job["id"]
    result = tool(mcp, "cancel_job", job_id=job["id"])
    assert result["isError"] and "job_finished" in result["content"][0]["text"]
    printer = server_env.printer
    printer.gate = threading.Event()
    job = tool(mcp, "print_label", label="box", wait=0)["structuredContent"]
    assert printer.started.wait(10)
    queued = tool(mcp, "print_label", label="box", wait=0)["structuredContent"]
    canceled = tool(mcp, "cancel_job", job_id=queued["id"])["structuredContent"]
    assert canceled["state"] == "canceled"
    printer.gate.set()
    assert (
        tool(mcp, "job_status", job_id=job["id"], wait=10)["structuredContent"]["state"] == "done"
    )


# --- stdio ------------------------------------------------------------------------


def run_stdio(mcp, *messages, raw=b""):
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages) + raw)
    stdout = io.BytesIO()
    serve_stdio(mcp, stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_stdio_in_process_prints_on_the_fake_printer(mcp, server_env):
    answers = run_stdio(
        mcp,
        request("initialize", {"protocolVersion": "2025-11-25"}, 1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("tools/call", {"name": "print_label", "arguments": {"label": "box"}}, 2),
        raw=b"\n{broken\n",
    )
    by_id = {a.get("id"): a for a in answers}
    assert by_id[1]["result"]["protocolVersion"] == "2025-11-25"
    assert by_id[2]["result"]["structuredContent"]["state"] == "done"
    assert by_id[None]["error"]["code"] == PARSE_ERROR
    assert len(server_env.printer.jobs) == 1


def test_stdio_limits_and_internal_errors(mcp, monkeypatch):
    monkeypatch.setattr(mcp_module, "MAX_LINE", 50)
    answers = run_stdio(mcp, raw=b'{"x":"' + b"a" * 200 + b'"}\n' + b"\n")
    assert answers == [
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": INVALID_REQUEST, "message": "Message too large"},
        }
    ]

    def broken(message, session=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp, "handle", broken)
    answers = run_stdio(mcp, request("ping", ident=7))
    assert answers[0]["id"] == 7 and answers[0]["error"]["code"] == -32603


def test_stdio_subprocess(tmp_path):
    labels = tmp_path / "labels"
    labels.mkdir()
    Label(elements=[TextElement(text="Hi", name="t")]).save(labels / "hello.tavletta")
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "config"), PYTHONPATH=str(ROOT))
    messages = [
        request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, 1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("tools/list", ident=2),
        request("tools/call", {"name": "list_labels", "arguments": {}}, 3),
        request("tools/call", {"name": "preview_label", "arguments": {"label": "hello"}}, 4),
        modern("server/discover", ident=5),
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    done = subprocess.run(  # nosec B603 - this checkout, fixed arguments
        [sys.executable, "-m", "tavletta.cli", "mcp", "--labels-dir", str(labels)],
        input=stdin.encode(),
        capture_output=True,
        env=env,
        cwd=ROOT,
        timeout=60,
        check=True,
    )
    answers = {a["id"]: a for a in map(json.loads, done.stdout.splitlines())}
    assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in answers[2]["result"]["tools"]] == [t["name"] for t in TOOLS]
    assert answers[3]["result"]["structuredContent"]["labels"][0]["name"] == "hello.tavletta"
    assert answers[4]["result"]["content"][0]["type"] == "image"
    assert answers[5]["result"]["resultType"] == "complete"


# --- Streamable HTTP ---------------------------------------------------------------


def post(base, message, headers=None, token=None):
    return call(
        base, "POST", "/mcp", body=message, headers=dict(ACCEPT, **(headers or {})), token=token
    )


def test_http_legacy_session(make_server, server_env):
    _server, base = make_server(token=TOKEN)
    status, response, answer = post(
        base, request("initialize", {"protocolVersion": "2025-06-18"}), token=TOKEN
    )
    assert status == 200 and answer["result"]["protocolVersion"] == "2025-06-18"
    session = response.getheader("Mcp-Session-Id")
    assert session and all(33 <= ord(c) < 127 for c in session)
    headers = {"Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-06-18"}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, _r, body = post(base, note, headers, TOKEN)
    assert status == 202
    status, _r, answer = post(base, request("tools/list", ident=2), headers, TOKEN)
    assert status == 200 and len(answer["result"]["tools"]) == len(TOOLS)
    call_print = request(
        "tools/call", {"name": "print_label", "arguments": {"label": "box", "copies": 3}}, 3
    )
    status, _r, answer = post(base, call_print, headers, TOKEN)
    job = answer["result"]["structuredContent"]
    assert job["state"] == "done" and job["printed"] == 3
    assert len(server_env.printer.jobs) == 3
    # no session, an unknown one, a wrong version
    assert post(base, request("tools/list"), token=TOKEN)[0] == 400
    assert post(base, request("tools/list"), {"Mcp-Session-Id": "nope"}, TOKEN)[0] == 404
    wrong = dict(headers, **{"MCP-Protocol-Version": "1999-01-01"})
    assert post(base, request("tools/list"), wrong, TOKEN)[0] == 400
    # ending the session
    status, _r, _b = call(base, "DELETE", "/mcp", headers={"Mcp-Session-Id": session}, token=TOKEN)
    assert status == 204
    assert post(base, request("tools/list"), headers, TOKEN)[0] == 404
    assert call(base, "DELETE", "/mcp", headers={"Mcp-Session-Id": session}, token=TOKEN)[0] == 404
    assert call(base, "DELETE", "/mcp", token=TOKEN)[0] == 400
    status, response, _b = call(base, "GET", "/mcp", headers=ACCEPT, token=TOKEN)
    assert status == 405 and "POST" in response.getheader("Allow")


def test_http_modern_requests(make_server):
    _server, base = make_server()
    message = modern("tools/call", {"name": "describe_label", "arguments": {"label": "box"}})
    good = {
        "MCP-Protocol-Version": MODERN[0],
        "Mcp-Method": "tools/call",
        "Mcp-Name": "describe_label",
    }
    status, response, answer = post(base, message, good)
    assert status == 200 and answer["result"]["resultType"] == "complete"
    assert answer["result"]["structuredContent"]["name"] == "box.tavletta"
    assert response.getheader("Mcp-Session-Id") is None
    encoded = "=?base64?" + base64.b64encode(b"describe_label").decode() + "?="
    assert post(base, message, dict(good, **{"Mcp-Name": encoded}))[0] == 200
    for wrong in (
        {"Mcp-Name": "list_labels"},
        {"Mcp-Name": "=?base64?***?="},
        {"Mcp-Method": "tools/list"},
        {"MCP-Protocol-Version": "2025-11-25"},
    ):
        status, _r, answer = post(base, message, dict(good, **wrong))
        assert status == 400 and answer["error"]["code"] == HEADER_MISMATCH, wrong
    old = modern("tools/list", version="1900-01-01")
    status, _r, answer = post(
        base, old, {"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"}
    )
    assert status == 400 and answer["error"]["code"] == UNSUPPORTED_VERSION
    unknown = modern("resources/list")
    status, _r, answer = post(
        base, unknown, {"MCP-Protocol-Version": MODERN[0], "Mcp-Method": "resources/list"}
    )
    assert status == 404 and answer["error"]["code"] == METHOD_NOT_FOUND
    note = modern("notifications/whatever")
    del note["id"]
    assert post(base, note)[0] == 202


def test_http_print_with_the_fake_printer_modern(make_server, server_env):
    _server, base = make_server()
    arguments = {"label": "box", "data_rows": [{"x": "1"}], "wait": 10}
    message = modern("tools/call", {"name": "print_label", "arguments": arguments})
    headers = {
        "MCP-Protocol-Version": MODERN[0],
        "Mcp-Method": "tools/call",
        "Mcp-Name": "print_label",
    }
    status, _r, answer = post(base, message, headers)
    assert status == 200 and answer["result"]["structuredContent"]["state"] == "done"
    assert len(server_env.printer.jobs) == 1
    server_env.printer.error = PaperJam("jam")
    status, _r, answer = post(base, message, headers)
    job = answer["result"]["structuredContent"]
    assert job["state"] == "failed" and job["error"]["code"] == "printer_jam"


def test_http_transport_checks(make_server):
    _server, base = make_server(token=TOKEN)
    init = request("initialize", {"protocolVersion": LEGACY[0]})
    status, _r, body = post(base, init, {"Origin": "https://evil.example"}, TOKEN)
    assert status == 403 and body["error"]["code"] == "origin_not_allowed"
    assert post(base, init)[0] == 401
    status, _r, answer = call(
        base, "POST", "/mcp", body=init, headers={"Accept": "text/html"}, token=TOKEN
    )
    assert status == 406
    status, _r, answer = call(
        base,
        "POST",
        "/mcp",
        raw=b"{",
        headers=dict(ACCEPT, **{"Content-Type": "application/json"}),
        token=TOKEN,
    )
    assert status == 400 and answer["error"]["code"] == PARSE_ERROR
    assert post(base, [init], token=TOKEN)[0] == 400
    bad_init = request("initialize", {"protocolVersion": LEGACY[0]}, ident=True)
    status, response, answer = post(base, bad_init, token=TOKEN)
    assert (
        answer["error"]["code"] == INVALID_REQUEST and response.getheader("Mcp-Session-Id") is None
    )


def test_http_mcp_off(make_server):
    _server, base = make_server(mcp=False)
    assert post(base, request("ping"))[0] == 404


def test_sessions_expire_and_make_room(monkeypatch, mcp):
    transport = mcp_module.HttpTransport(mcp)
    clock = [1000.0]
    monkeypatch.setattr(mcp_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mcp_module, "MAX_SESSIONS", 2)
    ids = []
    for _n in range(3):
        reply = transport.post({}, json.dumps(request("initialize", {})).encode())
        ids.append(reply.headers["Mcp-Session-Id"])
        clock[0] += 1
    assert transport._session(ids[0]) is None and transport._session(ids[2]) is not None
    clock[0] += mcp_module.SESSION_SECONDS + 1
    assert transport._session(ids[2]) is None
