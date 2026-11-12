"""The Model Context Protocol: the server's abilities as tools for AI clients.

JSON-RPC 2.0 over two transports: stdio (`etiketto mcp`, which Claude
Desktop and Claude Code start themselves) and Streamable HTTP at /mcp of
`etiketto serve --mcp`, behind the same token as the REST API.

Both eras of the protocol are served ("dual-era" in the specification):

- 2026-07-28 ("modern"): stateless, every request names its protocol
  version and the client's capabilities in params._meta; server/discover
  tells the versions; on HTTP the headers MCP-Protocol-Version, Mcp-Method
  and Mcp-Name must match the body.
- 2025-11-25 and 2025-06-18 ("legacy"): an initialize request negotiates
  the version, on HTTP it opens a session (Mcp-Session-Id). 2025-03-26 is
  not offered: it needs JSON-RPC batches.

A small implementation of the standard library instead of the official
`mcp` package: it would bring pydantic, starlette, uvicorn, anyio and more
into a program that needs eight tools and two transports.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import IO, Any

from etiketto import __version__
from etiketto.server import requests
from etiketto.server.config import LOCAL, Caller
from etiketto.server.errors import ApiError
from etiketto.server.service import Service

log = logging.getLogger(__name__)

MODERN = ("2026-07-28",)
# 2025-03-26 is left out: it needs JSON-RPC batches, which later versions dropped
LEGACY = ("2025-11-25", "2025-06-18")
SUPPORTED = MODERN + LEGACY
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022

SERVER_INFO = {"name": "etiketto", "title": "Etiketto", "version": __version__}
INSTRUCTIONS = (
    "Etiketto prints labels on label printers. list_labels and describe_label show the "
    "label files and what can be filled in (element names and variables for 'set', "
    "columns for data rows). preview_label shows a label as it will print. print_label "
    "queues a print job and returns its id; job_status follows it, cancel_job stops it "
    "between two labels. Printing uses real labels: preview first and print only what "
    "the user asked for."
)
MAX_LINE = 64 * 1024 * 1024  # one message on stdio
MAX_SESSIONS = 100
SESSION_SECONDS = 3600.0  # an HTTP session unused this long ends
DEFAULT_WAIT = 10.0  # seconds print_label waits for its job, unless told


# set while a tools/call runs: notifications/cancelled for it sets the event,
# and print_label and job_status stop waiting (the job itself goes on)
_CANCEL: ContextVar[threading.Event | None] = ContextVar("mcp_cancel", default=None)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


# --- tools ---------------------------------------------------------------------

_TEXTS = {"type": "object", "additionalProperties": {"type": ["string", "number"]}}
_LABEL_PROPERTIES = {
    "label": {
        "type": "string",
        "description": "Name of a label file in the labels folder, as list_labels shows it.",
    },
    "label_content": {
        "type": "object",
        "description": "Or the label itself: the JSON content of an .etiketto file.",
    },
    "printer": {
        "type": "string",
        "description": "The printer's name from list_printers; without a printer list on "
        "the server, a printer model id, if not the label's own.",
    },
    "set": dict(
        _TEXTS,
        description="Element name or variable name -> value. Variables like {=date} in "
        "a value are filled in; 'date' sets the date and time to print.",
    ),
    "set_literal": dict(_TEXTS, description="Like set, but braces in a value stay as they are."),
    "counter_start": {
        "type": "integer",
        "minimum": 0,
        "description": "Counter of the first label instead of the saved one.",
    },
    "data_rows": {
        "type": "array",
        "items": {"type": "object"},
        "description": "Serial labels: one label per row, column name -> value; fills "
        "{Column} placeholders.",
    },
    "data_file": {
        "type": "string",
        "description": "Or a CSV, ODS or XLSX file in the labels folder.",
    },
    "sheet": {"type": "string", "description": "Sheet of the data file; the first by default."},
}
_JOB_ID = {"job_id": {"type": "string", "description": "The id print_label returned."}}
_WAIT = {
    "wait": {
        "type": "number",
        "minimum": 0,
        "maximum": requests.MAX_WAIT,
        "description": "Seconds to wait for the job to finish before answering.",
    }
}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _tool(name: str, title: str, description: str, schema: dict, read_only: bool) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "openWorldHint": not read_only or name == "printer_status",
        },
    }


TOOLS = [
    _tool(
        "list_printers",
        "List printers",
        "The printers this server prints on, with their state; without a printer list, "
        "the printer models with the address set for each.",
        _schema(
            {
                "status": {"type": "boolean", "description": "Ask each printer for its state."},
                "discover": {
                    "type": "boolean",
                    "description": "Search the network, if the server allows it.",
                },
            }
        ),
        True,
    ),
    _tool(
        "printer_status",
        "Printer status",
        "Ask a printer for its state: loaded media, problems, resolution, print modes.",
        _schema(
            {
                "printer": {"type": "string", "description": "Printer name or model id."},
                "host": {
                    "type": "string",
                    "description": "Address, only without a printer list; the one set by default.",
                },
            },
            ("printer",),
        ),
        True,
    ),
    _tool(
        "list_labels",
        "List labels",
        "The label files in the labels folder.",
        _schema({}),
        True,
    ),
    _tool(
        "describe_label",
        "Describe a label",
        "Elements (with the names 'set' fills in), variables, data columns, size and "
        "printer of a label file.",
        _schema({"label": _LABEL_PROPERTIES["label"]}, ("label",)),
        True,
    ),
    _tool(
        "preview_label",
        "Preview a label",
        "A PNG image of the label filled in as it will print, and what would print wrongly.",
        _schema(
            dict(
                _LABEL_PROPERTIES,
                row={"type": "integer", "minimum": 1, "description": "Data row to show."},
                view={
                    "type": "string",
                    "enum": ["preview", "printer"],
                    "description": "'printer': the image as the printer gets it.",
                },
            )
        ),
        True,
    ),
    _tool(
        "print_label",
        "Print a label",
        "Print a label on a printer: queued, one job per printer at a time. Returns the job "
        "with its id and state.",
        _schema(
            dict(
                _LABEL_PROPERTIES,
                host={
                    "type": "string",
                    "description": "Printer address, only without a printer list on the server.",
                },
                copies={"type": "integer", "minimum": 1, "maximum": requests.MAX_COPIES},
                rows={"type": "string", "description": "Data rows to print, e.g. 3-10, 5 or 7-."},
                no_save_counter={
                    "type": "boolean",
                    "description": "Do not save the next counter value in the label file.",
                },
                **_WAIT,
            )
        ),
        False,
    ),
    _tool(
        "job_status",
        "Print job status",
        "State of a print job: queued, printing, done, failed or canceled; labels printed.",
        _schema(dict(_JOB_ID, **_WAIT), ("job_id",)),
        True,
    ),
    _tool(
        "cancel_job",
        "Cancel a print job",
        "Cancel a waiting job, or stop a printing one after the label being printed.",
        _schema(_JOB_ID, ("job_id",)),
        False,
    ),
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
PRINTING = frozenset({"print_label", "job_status", "cancel_job"})  # need the role print


def tools_for(caller: Caller) -> list[dict]:
    """The tools a token may use: a preview token sees no printing tools."""
    return [t for t in TOOLS if caller.may("print") or t["name"] not in PRINTING]


def _json_text(data: object) -> dict:
    return {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}


def _structured(data: dict) -> dict:
    return {"content": [_json_text(data)], "structuredContent": data, "isError": False}


@dataclass
class Session:
    """What an initialize request agreed on (legacy protocol versions)."""

    version: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    owner: str = ""  # the token that opened it; no other may use it
    used: float = field(default_factory=time.monotonic)


class Mcp:
    """The protocol, without a transport: handle() takes one message and
    returns the answer (None for a notification)."""

    def __init__(self, service: Service):
        self.service = service
        self._calls: dict[tuple[str, object], threading.Event] = {}
        self._calls_lock = threading.Lock()
        self._tools: dict[str, Callable[[dict, Caller], dict]] = {
            "list_printers": self._list_printers,
            "printer_status": self._printer_status,
            "list_labels": lambda _args, _caller: _structured(
                {"labels": self.service.list_labels()}
            ),
            "describe_label": lambda args, _caller: _structured(
                self.service.describe(args.get("label"))
            ),
            "preview_label": self._preview,
            "print_label": self._print,
            "job_status": self._job_status,
            "cancel_job": lambda args, caller: _structured(
                self.service.job_dict(self.service.cancel(args.get("job_id"), caller))
            ),
        }

    def handle(
        self, message: object, session: Session | None = None, caller: Caller = LOCAL
    ) -> dict | None:
        """The answer to one JSON-RPC message; None for a notification or
        a response (the server sends no requests, so they are ignored)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, RpcError(INVALID_REQUEST, "Invalid Request"))
        if "method" not in message:
            return None  # a response
        ident = message.get("id")
        scope = session.id if session else caller.name
        if "id" not in message:
            self._notified(message, scope)  # notifications/initialized, .../cancelled
            return None
        if isinstance(ident, bool) or not isinstance(ident, (str, int)):
            return _error(None, RpcError(INVALID_REQUEST, "Invalid Request"))
        cancel = threading.Event()
        with self._calls_lock:
            self._calls[(scope, ident)] = cancel
        token = _CANCEL.set(cancel)
        try:
            result = self.result(message, session, caller)
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except RpcError as e:
            return _error(ident, e)
        finally:
            _CANCEL.reset(token)
            with self._calls_lock:
                self._calls.pop((scope, ident), None)

    def _notified(self, message: dict, scope: str) -> None:
        params = message.get("params")
        if message.get("method") != "notifications/cancelled" or not isinstance(params, dict):
            return
        with self._calls_lock:
            cancel = self._calls.get((scope, params.get("requestId")))
        if cancel is not None:
            cancel.set()

    def result(self, message: dict, session: Session | None, caller: Caller = LOCAL) -> dict:
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise RpcError(INVALID_REQUEST, "Invalid Request")
        meta = params.get("_meta")
        if isinstance(meta, dict) and VERSION_KEY in meta:
            result = self._modern(method, params, meta, caller)
            return dict(result, resultType="complete", _meta={SERVER_INFO_KEY: SERVER_INFO})
        return self._legacy(method, params, session, caller)

    def _modern(self, method: str, params: dict, meta: dict, caller: Caller) -> dict:
        requested = meta[VERSION_KEY]
        if requested not in MODERN:
            data = {"supported": list(SUPPORTED), "requested": requested}
            raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version", data)
        if not isinstance(meta.get(CAPABILITIES_KEY), dict):
            raise RpcError(INVALID_PARAMS, "Missing %s in _meta" % CAPABILITIES_KEY)
        if method == "server/discover":
            return {
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": INSTRUCTIONS,
            }
        return self._common(method, params, caller)

    def _legacy(self, method: str, params: dict, session: Session | None, caller: Caller) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in LEGACY else LEGACY[0]
            if session is not None:
                session.version = version
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }
        return self._common(method, params, caller)

    def _common(self, method: str, params: dict, caller: Caller) -> dict:
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools_for(caller)}
        if method == "tools/call":
            return self.call(params, caller)
        raise RpcError(METHOD_NOT_FOUND, "Method not found: %s" % method)

    def call(self, params: Mapping, caller: Caller = LOCAL) -> dict:
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOL_NAMES:
            raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % name)
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        try:
            return self._tools[name](arguments, caller)
        except ApiError as e:
            return {
                "content": [{"type": "text", "text": "%s (%s)" % (e.message, e.code)}],
                "structuredContent": {"error": e.to_dict()},
                "isError": True,
            }

    # --- the tools ------------------------------------------------------------

    def _list_printers(self, args: dict, caller: Caller) -> dict:
        discover, status = (args.get(key, True) is not False for key in ("discover", "status"))
        return _structured({"printers": self.service.printers(discover, status, caller)})

    def _printer_status(self, args: dict, caller: Caller) -> dict:
        host = args.get("host")
        return _structured(self.service.status(args.get("printer"), host, caller))

    def _preview(self, args: dict, caller: Caller) -> dict:
        request = requests.preview_request(args)
        preview = self.service.preview(request, caller)
        summary = {"width": preview.width, "height": preview.height, "warnings": preview.warnings}
        image = {
            "type": "image",
            "data": base64.b64encode(preview.png).decode(),
            "mimeType": "image/png",
        }
        return {"content": [image, _json_text(summary)], "structuredContent": summary}

    def _print(self, args: dict, caller: Caller) -> dict:
        args = dict(args)
        args.setdefault("wait", DEFAULT_WAIT)
        request = requests.print_request(args)
        job = self.service.submit(request, caller)
        _wait(job, request.wait)
        return _structured(self.service.job_dict(job))

    def _job_status(self, args: dict, caller: Caller) -> dict:
        job = self.service.job(args.get("job_id"), caller)
        _wait(job, requests._number(args, "wait", 0.0, requests.MAX_WAIT))
        return _structured(self.service.job_dict(job))


def _wait(job, seconds: float) -> None:
    """Wait for a job to finish, at most `seconds`, or until the client
    cancels the request."""
    cancel = _CANCEL.get()
    deadline = time.monotonic() + seconds
    while not job.done.is_set() and not (cancel and cancel.is_set()):
        left = deadline - time.monotonic()
        if left <= 0:
            return
        job.done.wait(min(left, 0.1))


def _error(ident: object, error: RpcError) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": error.to_dict()}


# --- stdio -----------------------------------------------------------------------


def serve_stdio(mcp: Mcp, stdin: IO[bytes], stdout: IO[bytes], workers: int = 4) -> None:
    """Messages one per line on stdin, answers one per line on stdout,
    until stdin ends. Tool calls run in threads, so a long print does not
    hold up a ping; everything else is answered in order."""
    session = Session(LEGACY[0], id="stdio")
    lock = threading.Lock()

    def send(answer: dict | None) -> None:
        if answer is None:
            return
        line = json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        with lock:
            stdout.write(line)
            stdout.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for message in _messages(stdin, send):
            if isinstance(message, dict) and message.get("method") == "tools/call":
                pool.submit(_answer, mcp, message, session, send)
            else:
                send(_guarded(mcp, message, session))


def _answer(mcp: Mcp, message: object, session: Session, send) -> None:
    send(_guarded(mcp, message, session))


def _guarded(mcp: Mcp, message: object, session: Session) -> dict | None:
    try:
        return mcp.handle(message, session)
    except Exception:  # noqa: BLE001 - one broken request must not end the server
        log.exception("MCP request failed")
        ident = message.get("id") if isinstance(message, dict) else None
        return _error(ident, RpcError(INTERNAL_ERROR, "Internal error"))


def _messages(stdin: IO[bytes], send: Callable[[dict | None], None]):
    while line := stdin.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE and not line.endswith(b"\n"):
            while (rest := stdin.readline(MAX_LINE)) and not rest.endswith(b"\n"):
                pass
            send(_error(None, RpcError(INVALID_REQUEST, "Message too large")))
            continue
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            send(_error(None, RpcError(PARSE_ERROR, "Parse error")))
            continue
        yield message


def run_stdio(service: Service) -> int:
    """`etiketto mcp`: stdout carries only protocol messages, so anything
    else printed goes to stderr."""
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        serve_stdio(Mcp(service), sys.stdin.buffer, stdout)
    finally:
        service.close()
    return 0


# --- Streamable HTTP ---------------------------------------------------------------


@dataclass
class Reply:
    status: int
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _decoded(value: str) -> str | None:
    """A header value, Base64 sentinel ("=?base64?...?=") decoded."""
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _accepts_json(accept: str | None) -> bool:
    if not accept:
        return True
    kinds = {part.split(";")[0].strip().lower() for part in accept.split(",")}
    return bool(kinds & {"application/json", "application/*", "*/*"})


class HttpTransport:
    """POST, DELETE (ends a legacy session) and GET (405) at /mcp; the
    HTTP server has checked the token and the Origin before."""

    def __init__(self, mcp: Mcp):
        self.mcp = mcp
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def post(self, headers: Mapping[str, str], body: bytes, caller: Caller = LOCAL) -> Reply:
        if not _accepts_json(headers.get("accept")):
            return Reply(406, _error(None, RpcError(INVALID_REQUEST, "Accept application/json")))
        try:
            message = json.loads(body)
        except (ValueError, RecursionError):
            return Reply(400, _error(None, RpcError(PARSE_ERROR, "Parse error")))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return Reply(400, _error(None, RpcError(INVALID_REQUEST, "Invalid Request")))
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict) and VERSION_KEY in meta:
            return self._modern(headers, message, meta, caller)
        return self._legacy(headers, message, caller)

    def _modern(
        self, headers: Mapping[str, str], message: dict, meta: dict, caller: Caller
    ) -> Reply:
        if "id" not in message:
            return Reply(202)
        mismatch = _header_mismatch(headers, message, meta)
        if mismatch:
            error = RpcError(HEADER_MISMATCH, "Header mismatch: " + mismatch)
            return Reply(400, _error(message.get("id"), error))
        answer = self.mcp.handle(message, caller=caller)
        return Reply(_status(answer), answer)

    def _legacy(self, headers: Mapping[str, str], message: dict, caller: Caller) -> Reply:
        if message.get("method") == "initialize" and "id" in message:
            return self._initialize(message, caller)
        session_id = headers.get("mcp-session-id")
        if not session_id:
            return Reply(400, _error(message.get("id"), RpcError(INVALID_REQUEST, "No session")))
        session = self._session(session_id, caller)
        if session is None:
            return Reply(
                404, _error(message.get("id"), RpcError(INVALID_REQUEST, "No such session"))
            )
        version = headers.get("mcp-protocol-version")
        if version is not None and version not in LEGACY:
            error = RpcError(INVALID_REQUEST, "Unsupported MCP-Protocol-Version: %s" % version)
            return Reply(400, _error(message.get("id"), error))
        answer = self.mcp.handle(message, session, caller)
        return Reply(202) if answer is None else Reply(200, answer)

    def _initialize(self, message: dict, caller: Caller) -> Reply:
        session = Session(LEGACY[0], owner=caller.name)
        answer = self.mcp.handle(message, session, caller)
        if answer is None or "error" in answer:
            return Reply(200, answer)
        self._open(session)
        return Reply(200, answer, {"Mcp-Session-Id": session.id})

    def delete(self, headers: Mapping[str, str], caller: Caller = LOCAL) -> Reply:
        session_id = headers.get("mcp-session-id")
        if not session_id:
            return Reply(400, _error(None, RpcError(INVALID_REQUEST, "No session")))
        if self._session(session_id, caller) is None:
            return Reply(404)
        with self._lock:
            self._sessions.pop(session_id, None)
        return Reply(204)

    def _open(self, session: Session) -> None:
        with self._lock:
            self._expire()
            if len(self._sessions) >= MAX_SESSIONS:  # the one unused longest makes room
                oldest = min(self._sessions.values(), key=lambda s: s.used)
                del self._sessions[oldest.id]
            self._sessions[session.id] = session

    def _session(self, session_id: str, caller: Caller = LOCAL) -> Session | None:
        """The session, if the same token opened it."""
        with self._lock:
            self._expire()
            session = self._sessions.get(session_id)
            if session and session.owner != caller.name:
                return None
            if session:
                session.used = time.monotonic()
            return session

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [k for k, s in self._sessions.items() if now - s.used > SESSION_SECONDS]:
            del self._sessions[key]


def _header_mismatch(headers: Mapping[str, str], message: dict, meta: dict) -> str | None:
    """Modern requests: the headers that mirror the body must match it."""
    if headers.get("mcp-protocol-version") != meta.get(VERSION_KEY):
        return "MCP-Protocol-Version"
    if headers.get("mcp-method") != message.get("method"):
        return "Mcp-Method"
    params = message.get("params") or {}
    if message.get("method") == "tools/call":
        name = headers.get("mcp-name")
        if name is None or _decoded(name) != params.get("name"):
            return "Mcp-Name"
    return None


def _status(answer: dict | None) -> int:
    """The HTTP status of a modern answer: errors of the protocol are 400,
    an unknown method 404."""
    if answer is None:
        return 202
    code = answer.get("error", {}).get("code")
    if code == METHOD_NOT_FOUND:
        return 404
    if code in (UNSUPPORTED_VERSION, INVALID_PARAMS, INVALID_REQUEST, HEADER_MISMATCH):
        return 400
    return 200
