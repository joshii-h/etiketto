"""The HTTP server: the REST API under /api/v1 and MCP at /mcp.

The standard library's ThreadingHTTPServer, one thread per connection
(at most MAX_CONNECTIONS at once). Every request passes these checks, in
this order, before it reaches a route:

1. Host: a server on a loopback address answers only requests for a
   loopback name (localhost, 127.0.0.1, [::1]). A web page whose own name
   was made to point at 127.0.0.1 (DNS rebinding) sends its own name.
2. Origin: a request from a web page (it has an Origin header) is refused
   unless that origin is the server itself or allowed with --allow-origin.
   POST also needs Content-Type application/json, which a page cannot send
   to another site without asking first (CORS, never granted here).
3. Token: with a token every request but /api/v1/health needs
   "Authorization: Bearer <token>", compared in constant time. Ten wrong
   tokens from one address within a minute block it for that minute.
4. Size: a body needs a Content-Length of at most --max-body bytes.

Logs name the client, method, path (without the query) and status, never
a header.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from etiketto.server import requests
from etiketto.server.errors import ApiError, bad_request
from etiketto.server.mcp import HttpTransport, Mcp
from etiketto.server.openapi import spec
from etiketto.server.service import Service
from etiketto.ui.i18n import _, language

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
MAX_BODY = 32 * 1024 * 1024  # a label with large images, or many data rows
MAX_CONNECTIONS = 32
AUTH_FAILURES = 10  # wrong tokens per address and minute before it is blocked
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def is_loopback(host: str) -> bool:
    """Whether an address (or name) only reaches this computer."""
    name = host.strip("[]").lower()
    if name in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%")[0]).is_loopback
    except ValueError:
        return False


def binds_loopback(host: str) -> bool:
    """Whether listening on `host` (a name is resolved) is loopback only."""
    if is_loopback(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(is_loopback(str(info[4][0])) for info in infos)


def _host_name(header: str) -> str:
    """The name of a Host header, without the port: "[::1]:8790" -> "::1"."""
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8790
    token: str | None = None
    mcp: bool = False
    origins: tuple[str, ...] = ()  # web origins allowed besides the server itself
    max_body: int = MAX_BODY


class RateLimit:
    """Wrong tokens per client address within a minute."""

    def __init__(self, limit: int = AUTH_FAILURES, window: float = 60.0):
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, client: str) -> bool:
        with self._lock:
            failures = self._failures.get(client)
            if not failures:
                return False
            while failures and time.monotonic() - failures[0] > self.window:
                failures.popleft()
            if not failures:
                del self._failures[client]
            return len(failures) >= self.limit

    def failed(self, client: str) -> None:
        with self._lock:
            if len(self._failures) > 10_000:  # many addresses: forget, do not grow
                self._failures.clear()
            self._failures[client].append(time.monotonic())


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, service: Service):
        self.config = config
        self.service = service
        self.loopback = binds_loopback(config.host)
        self.mcp = HttpTransport(Mcp(service)) if config.mcp else None
        self.rate_limit = RateLimit()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        family = socket.AF_INET6 if ":" in config.host else socket.AF_INET
        self.address_family = family
        super().__init__((config.host, config.port), Handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):  # too many at once: hang up
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


Route = Callable[["Handler", list[str]], None]


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "Etiketto"
    timeout = 60  # seconds a client may take to send its request

    # --- the checks -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802 - no CORS: preflights are refused
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        with language(self.headers.get("Accept-Language")):
            try:
                self._check()
                self._route(method)
            except ApiError as e:
                self._error(e)
            except Exception:  # noqa: BLE001 - one broken request must not end the server
                log.exception("request failed")
                self._error(ApiError(500, "internal", _("Internal error")))

    def _check(self) -> None:
        host = self.headers.get("Host")  # browsers always send it
        if self.server.loopback and host is not None and not is_loopback(_host_name(host)):
            raise ApiError(421, "wrong_host", _("This server only answers on its local address"))
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_ok(origin):
            raise ApiError(
                403, "origin_not_allowed", _("Requests from this web page are not allowed")
            )
        token = self.server.config.token
        if token is not None and self._path() != PREFIX + "/health":
            self._authorize(token)

    def _authorize(self, token: str) -> None:
        client = self.client_address[0]
        if self.server.rate_limit.blocked(client):
            raise ApiError(429, "too_many_requests", _("Too many requests – try again later"))
        scheme, _sep, value = self.headers.get("Authorization", "").partition(" ")
        right = hmac.compare_digest(value.strip().encode(), token.encode())
        if scheme.lower() != "bearer" or not right:
            self.server.rate_limit.failed(client)
            raise ApiError(401, "unauthorized", _("Access token missing or wrong"))

    def _origin_ok(self, origin: str) -> bool:
        own = {"http://" + self.headers.get("Host", ""), "https://" + self.headers.get("Host", "")}
        return origin in own or origin in self.server.config.origins

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, max_num_fields=20)

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", _("The request needs a Content-Length"))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(
                411, "length_required", _("The request needs a Content-Length")
            ) from None
        if length < 0 or length > self.server.config.max_body:
            raise ApiError(413, "too_large", _("The request is too large"))
        return self.rfile.read(length)

    def _json(self) -> object:
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise ApiError(415, "not_json", _("Send the request as application/json"))
        body = self._body()
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise bad_request(_("The request is not valid JSON"), "not_json") from None

    # --- routes -------------------------------------------------------------------

    def _route(self, method: str) -> None:
        path = self._path()
        if path == "/mcp" and self.server.mcp is not None:
            self._mcp(method)
            return
        if not path.startswith(PREFIX + "/"):
            raise ApiError(404, "not_found", _("Not found"))
        parts = [unquote(p) for p in path[len(PREFIX) + 1 :].split("/")]
        for (verb, pattern), route in ROUTES.items():
            if _matches(pattern, parts):
                if verb != method:
                    continue
                route(self, parts)
                return
        if any(_matches(pattern, parts) for _verb, pattern in ROUTES):
            raise ApiError(405, "method_not_allowed", _("Method not allowed"))
        raise ApiError(404, "not_found", _("Not found"))

    def health(self, _parts: list[str]) -> None:
        self._send(200, self.server.service.health())

    def openapi(self, _parts: list[str]) -> None:
        self._send(200, spec(mcp=self.server.mcp is not None))

    def printers(self, _parts: list[str]) -> None:
        discover = self._query().get("discover", ["true"])[-1].lower() not in ("false", "0", "no")
        self._send(200, {"printers": self.server.service.printers(discover)})

    def status(self, parts: list[str]) -> None:
        host = self._query().get("host", [None])[-1]
        self._send(200, self.server.service.status(parts[1], host))

    def labels(self, _parts: list[str]) -> None:
        self._send(200, {"labels": self.server.service.list_labels()})

    def label(self, parts: list[str]) -> None:
        self._send(200, self.server.service.describe("/".join(parts[1:])))

    def preview(self, _parts: list[str]) -> None:
        request = requests.preview_request(self._json())
        preview = self.server.service.preview(request)
        if request.format == "json":
            self._send(200, preview.to_dict())
            return
        codes = ",".join(w["code"] for w in preview.warnings)
        extra = {"X-Etiketto-Warnings": codes} if codes else {}
        self._send_bytes(200, preview.png, "image/png", extra)

    def print_job(self, _parts: list[str]) -> None:
        request = requests.print_request(self._json())
        service = self.server.service
        job = service.submit(request)
        finished = job.done.wait(request.wait) if request.wait else job.done.is_set()
        location = {"Location": "%s/jobs/%s" % (PREFIX, job.id)}
        self._send(200 if finished else 202, service.job_dict(job), location)

    def job(self, parts: list[str]) -> None:
        service = self.server.service
        self._send(200, service.job_dict(service.job(parts[1])))

    def cancel(self, parts: list[str]) -> None:
        service = self.server.service
        self._send(202, service.job_dict(service.cancel(parts[1])))

    def _mcp(self, method: str) -> None:
        transport = self.server.mcp
        assert transport is not None
        headers = {key.lower(): value for key, value in self.headers.items()}
        if method == "POST":
            reply = transport.post(headers, self._body())
        elif method == "DELETE":
            reply = transport.delete(headers)
        else:
            self._send(405, None, {"Allow": "POST, DELETE"})
            return
        self._send(reply.status, reply.body, reply.headers)

    # --- answers ------------------------------------------------------------------

    def _error(self, error: ApiError) -> None:
        extra = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        if error.status == 405:
            extra["Allow"] = "GET, POST, DELETE"
        self._send(error.status, {"error": error.to_dict()}, extra)

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        if body is None:
            self._send_bytes(status, b"", None, headers)
            return
        data = json.dumps(body, ensure_ascii=False).encode()
        self._send_bytes(status, data, "application/json; charset=utf-8", headers)

    def _send_bytes(
        self, status: int, data: bytes, kind: str | None, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def version_string(self) -> str:
        return self.server_version

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        status = code.value if isinstance(code, HTTPStatus) else code
        log.info("%s %s %s %s", self.client_address[0], self.command, self._path(), status)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - http.server's name
        log.debug("%s " + format, self.client_address[0], *args)


def _matches(pattern: tuple[str, ...], parts: list[str]) -> bool:
    """("labels", "*") matches labels/<anything, also with slashes>,
    ("jobs", "?") one part."""
    if pattern and pattern[-1] == "*":
        return len(parts) >= len(pattern) and _matches(pattern[:-1], parts[: len(pattern) - 1])
    return len(parts) == len(pattern) and all(
        p == "?" and part != "" or p == part for p, part in zip(pattern, parts, strict=True)
    )


ROUTES: dict[tuple[str, tuple[str, ...]], Route] = {
    ("GET", ("health",)): Handler.health,
    ("GET", ("openapi.json",)): Handler.openapi,
    ("GET", ("printers",)): Handler.printers,
    ("GET", ("printers", "?", "status")): Handler.status,
    ("GET", ("labels",)): Handler.labels,
    ("GET", ("labels", "*")): Handler.label,
    ("POST", ("preview",)): Handler.preview,
    ("POST", ("print",)): Handler.print_job,
    ("GET", ("jobs", "?")): Handler.job,
    ("DELETE", ("jobs", "?")): Handler.cancel,
}
