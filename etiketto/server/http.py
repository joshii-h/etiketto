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

import contextlib
import hmac
import ipaddress
import json
import logging
import re
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
from etiketto.server.config import LOCAL, Caller, TokenStore
from etiketto.server.errors import ApiError, bad_request
from etiketto.server.mcp import HttpTransport, Mcp
from etiketto.server.openapi import spec
from etiketto.server.service import Service
from etiketto.ui.i18n import _, language

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
# a label with images, or many data rows; JSON takes up to about 30 times
# its size once parsed, so large bodies are parsed two at a time
MAX_BODY = 4 * 1024 * 1024
LARGE_BODY = 512 * 1024
PARSES = 2
ANSWER_SECONDS = 60.0  # to send the answer, once the request is in
MAX_CONNECTIONS = 32
MAX_PER_CLIENT = 8  # connections from one address at once
REQUEST_SECONDS = 30.0  # to send the whole request: line, headers and body
# without a token a web page could make the server ask printers with an
# <img src=...>: requests that contact printers need this header then,
# which a page cannot send to another site without CORS permission
CLIENT_HEADER = "X-Etiketto-Request"
CORS_HEADERS = (
    "Authorization, Content-Type, Accept, Accept-Language, Mcp-Session-Id, "
    "MCP-Protocol-Version, Mcp-Method, Mcp-Name, " + CLIENT_HEADER
)
CORS_EXPOSED = "Location, X-Etiketto-Warnings, Mcp-Session-Id"
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


def resolve(host: str) -> str:
    """The address to listen on: a name is resolved once, here, and the
    server binds that address (a name looked up again could change)."""
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return host
    return str(infos[0][4][0]) if infos else host


def binds_loopback(host: str) -> bool:
    """Whether listening on `host` (a name is resolved) is loopback only."""
    return is_loopback(resolve(host))


_HOST_HEADER = re.compile(r"(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9.-]+))(?::(\d{1,5}))?\Z")


def _host_name(header: str) -> str:
    """The name of a Host header, without the port: "[::1]:8790" -> "::1";
    "" for anything else ("[::1]evil.com", "user@host", spaces)."""
    match = _HOST_HEADER.match(header)
    if not match:
        return ""
    return match.group(1) or match.group(2)


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8790
    token: str | None = None  # one token for everything (role admin)
    mcp: bool = False
    origins: tuple[str, ...] = ()  # web origins allowed besides the server itself
    max_body: int = MAX_BODY
    # reverse proxies whose X-Forwarded-For names the client (--trusted-proxy)
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    tokens: TokenStore | None = None  # named tokens with roles (tokens.toml)


def rate_key(address: str) -> str:
    """Whom wrong tokens are counted for: the address, an IPv6 address by
    its /64 network (one computer can use all of it)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network("%s/64" % ip, strict=False))
    return str(ip)


class RateLimit:
    """Wrong tokens per client address within a minute."""

    MAX_CLIENTS = 10_000

    def __init__(self, limit: int = AUTH_FAILURES, window: float = 60.0):
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = {}
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
            if client not in self._failures and len(self._failures) >= self.MAX_CLIENTS:
                self._failures.pop(next(iter(self._failures)))  # the oldest makes room
            # never more than the limit kept: enough to know it is reached
            self._failures.setdefault(client, deque(maxlen=self.limit)).append(time.monotonic())


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, service: Service):
        self.config = config
        self.service = service
        address = resolve(config.host)
        self.loopback = is_loopback(address)
        self.mcp = HttpTransport(Mcp(service)) if config.mcp else None
        self.rate_limit = RateLimit()
        self.parses = threading.BoundedSemaphore(PARSES)
        self.reaper = Reaper()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._clients: dict[str, int] = defaultdict(int)
        self._clients_lock = threading.Lock()
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        super().__init__((address, config.port), Handler)

    def trusted(self, address: str) -> bool:
        """Whether the address is a reverse proxy given with --trusted-proxy."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.config.trusted_proxies)

    def _admit(self, client: str) -> bool:
        """A slot for one more connection, at most MAX_PER_CLIENT per address
        (not for a trusted proxy: every client comes from it)."""
        with self._clients_lock:
            if self._clients[client] >= MAX_PER_CLIENT and not self.trusted(client):
                return False
            if not self._slots.acquire(blocking=False):
                return False
            self._clients[client] += 1
            return True

    def _leave(self, client: str) -> None:
        with self._clients_lock:
            self._clients[client] -= 1
            if self._clients[client] <= 0:
                del self._clients[client]
            self._slots.release()

    def process_request(self, request, client_address) -> None:
        client = client_address[0]
        if not self._admit(client):  # too many at once: hang up
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._leave(client)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._leave(client_address[0])


class Reaper:
    """Closes connections whose request is not in by its deadline. A socket
    timeout only bounds one recv: a client sending a byte every few
    seconds would hold a connection for ever (slowloris), inside a line or
    a body too."""

    def __init__(self, interval: float = 0.25):
        self._deadlines: dict[socket.socket, float] = {}
        self._expired: set[socket.socket] = set()  # cut off, not yet finished
        self._lock = threading.Lock()
        self._interval = interval
        self._thread: threading.Thread | None = None

    def watch(self, connection: socket.socket, seconds: float) -> None:
        with self._lock:
            self._deadlines[connection] = time.monotonic() + seconds
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="reaper")
                self._thread.start()

    def done(self, connection: socket.socket) -> None:
        with self._lock:
            self._deadlines.pop(connection, None)

    def finished(self, connection: socket.socket) -> None:
        with self._lock:
            self._deadlines.pop(connection, None)
            self._expired.discard(connection)

    def expired(self, connection: socket.socket) -> bool:
        """Cut off: what was read of the request (headers ending early at
        the cut) must not be taken as a request."""
        with self._lock:
            return connection in self._expired

    def _run(self) -> None:
        while True:
            time.sleep(self._interval)
            now = time.monotonic()
            with self._lock:
                late = [c for c, deadline in self._deadlines.items() if deadline <= now]
                for connection in late:
                    del self._deadlines[connection]
                    self._expired.add(connection)
            for connection in late:
                with contextlib.suppress(OSError):
                    # reading ends; an answer (408) can still be sent
                    connection.shutdown(socket.SHUT_RD)


class _Deadline:
    """The request stream with one deadline for the whole request: a client
    sending a byte now and then (slowloris) is cut off after REQUEST_SECONDS
    however slowly it sends."""

    def __init__(self, stream, connection, seconds: float):
        self._stream, self._connection = stream, connection
        self._deadline = time.monotonic() + seconds

    def _arm(self) -> None:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request deadline")
        self._connection.settimeout(left)

    def readline(self, *args):
        self._arm()
        return self._stream.readline(*args)

    def read(self, *args):
        self._arm()
        return self._stream.read(*args)

    def __getattr__(self, name):
        return getattr(self._stream, name)


Route = Callable[["Handler", list[str]], None]


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "Etiketto"
    timeout = REQUEST_SECONDS  # see _Deadline for the whole request

    def setup(self) -> None:
        super().setup()
        self.rfile = _Deadline(self.rfile, self.connection, REQUEST_SECONDS)  # type: ignore[assignment]
        self.server.reaper.watch(self.connection, REQUEST_SECONDS)
        self.who = "-"

    def finish(self) -> None:
        self.server.reaper.finished(self.connection)
        with contextlib.suppress(OSError):  # the client went away
            super().finish()

    def _request_in(self) -> None:
        """The whole request is read: no deadline for it any more, and time
        to send the answer."""
        self.server.reaper.done(self.connection)
        with contextlib.suppress(OSError):
            self.connection.settimeout(ANSWER_SECONDS)

    def client(self) -> str:
        """The client's address; behind a trusted proxy the last address in
        X-Forwarded-For that is no trusted proxy itself."""
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For") if hasattr(self, "headers") else None
        if not forwarded or not self.server.trusted(peer):
            return peer
        for hop in reversed([part.strip() for part in forwarded.split(",")]):
            if not self.server.trusted(hop):
                return hop
        return peer

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

    caller: Caller = LOCAL

    def _handle(self, method: str) -> None:
        self.caller = LOCAL
        config = self.server.config
        self.who = "-" if config.token or config.tokens else LOCAL.name
        if self.server.reaper.expired(self.connection):
            self.close_connection = True
            self._error(ApiError(408, "timeout", _("The request took too long")))
            return
        if method not in ("POST",) or self._path() == PREFIX + "/health":
            self._request_in()  # no body to wait for
        with language(self.headers.get("Accept-Language")):
            self._answer(method)

    def _answer(self, method: str) -> None:
        """Check and route the request; every problem becomes an answer."""
        try:
            if method == "OPTIONS" and self._preflight():
                return
            self._check()
            self._route(method)
        except ApiError as e:
            self._error(e)
        except TimeoutError:  # the body came too slowly
            self.close_connection = True
            self._error(ApiError(408, "timeout", _("The request took too long")))
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
        config = self.server.config
        if (config.token or config.tokens) and self._path() != PREFIX + "/health":
            self.caller = self._authorize()
            self.who = self.caller.name

    def _authorize(self) -> Caller:
        """Who the token belongs to; 401 for a wrong, expired or revoked one.
        Wrong tokens block the address for a while, valid ones never: a
        blocked address only skips the slow hash of tokens not seen before."""
        client = rate_key(self.client())
        blocked = self.server.rate_limit.blocked(client)
        scheme, _sep, value = self.headers.get("Authorization", "").partition(" ")
        caller = self._caller(value.strip(), blocked) if scheme.lower() == "bearer" else None
        if caller is not None:
            return caller
        if blocked:  # not counted again: the block ends a minute after the last counted one
            raise ApiError(429, "too_many_requests", _("Too many requests – try again later"))
        self.server.rate_limit.failed(client)
        raise ApiError(401, "unauthorized", _("Access token missing or wrong"))

    def _caller(self, given: str, known_only: bool = False) -> Caller | None:
        config = self.server.config
        if config.tokens is not None:
            return config.tokens.find(given, cheap_only=known_only) if given else None
        assert config.token is not None
        return (
            Caller("token") if hmac.compare_digest(given.encode(), config.token.encode()) else None
        )

    def _preflight(self) -> bool:
        """A CORS preflight from an origin allowed with --allow-origin: 204
        with what it may send; False for any other OPTIONS request."""
        origin = self.headers.get("Origin")
        if origin is None or origin not in self.server.config.origins:
            return False
        self._send(
            204,
            None,
            {
                "Access-Control-Allow-Methods": "GET, POST, DELETE",
                "Access-Control-Allow-Headers": CORS_HEADERS,
                "Access-Control-Max-Age": "600",
            },
        )
        return True

    def _origin_ok(self, origin: str) -> bool:
        own = {"http://" + self.headers.get("Host", ""), "https://" + self.headers.get("Host", "")}
        return origin in own or origin in self.server.config.origins

    def _path(self) -> str:
        # also for a request line that could not be read (no path then)
        return urlsplit(getattr(self, "path", "") or "").path

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, max_num_fields=20)

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", _("The request needs a Content-Length"))
        value = self.headers.get("Content-Length", "").strip()
        if not (value.isascii() and value.isdigit()):  # not "+5", " 5", "٥"
            raise ApiError(411, "length_required", _("The request needs a Content-Length"))
        length = int(value)
        if length > self.server.config.max_body:
            raise ApiError(413, "too_large", _("The request is too large"))
        body = self.rfile.read(length)
        if len(body) < length:  # cut off at the deadline, or the client stopped
            raise TimeoutError("request body incomplete")
        self._request_in()
        return body

    def _json_type(self) -> None:
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise ApiError(415, "not_json", _("Send the request as application/json"))

    def _json(self) -> object:
        self._json_type()
        body = self._body()
        large = len(body) > LARGE_BODY
        if large:  # parsed, it takes many times its size: a few at a time
            self.server.parses.acquire()
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise bad_request(_("The request is not valid JSON"), "not_json") from None
        finally:
            if large:
                self.server.parses.release()

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

    def _asks_printers(self) -> None:
        """Without a token, requests that make the server contact printers
        need CLIENT_HEADER: an <img> on a web page cannot send it."""
        config = self.server.config
        if not (config.token or config.tokens) and self.headers.get(CLIENT_HEADER) is None:
            raise ApiError(
                403,
                "header_required",
                _("Send the header %s with this request") % CLIENT_HEADER,
            )

    def printers(self, _parts: list[str]) -> None:
        self._asks_printers()
        query = self._query()
        discover, status = (
            query.get(key, ["true"])[-1].lower() not in ("false", "0", "no")
            for key in ("discover", "status")
        )
        printers = self.server.service.printers(discover, status, self.caller)
        self._send(200, {"printers": printers})

    def status(self, parts: list[str]) -> None:
        self._asks_printers()
        host = self._query().get("host", [None])[-1]
        self._send(200, self.server.service.status(parts[1], host, self.caller))

    def labels(self, _parts: list[str]) -> None:
        self._send(200, {"labels": self.server.service.list_labels()})

    def label(self, parts: list[str]) -> None:
        self._send(200, self.server.service.describe("/".join(parts[1:])))

    def preview(self, _parts: list[str]) -> None:
        request = requests.preview_request(self._json())
        preview = self.server.service.preview(request, self.caller)
        if request.format == "json":
            self._send(200, preview.to_dict())
            return
        codes = ",".join(w["code"] for w in preview.warnings)
        extra = {"X-Etiketto-Warnings": codes} if codes else {}
        self._send_bytes(200, preview.png, "image/png", extra)

    def print_job(self, _parts: list[str]) -> None:
        request = requests.print_request(self._json())
        service = self.server.service
        job = service.submit(request, self.caller)
        finished = job.done.wait(request.wait) if request.wait else job.done.is_set()
        location = {"Location": "%s/jobs/%s" % (PREFIX, job.id)}
        self._send(200 if finished else 202, service.job_dict(job), location)

    def job(self, parts: list[str]) -> None:
        service = self.server.service
        self._send(200, service.job_dict(service.job(parts[1], self.caller)))

    def cancel(self, parts: list[str]) -> None:
        service = self.server.service
        self._send(202, service.job_dict(service.cancel(parts[1], self.caller)))

    def _mcp(self, method: str) -> None:
        transport = self.server.mcp
        assert transport is not None
        headers = {key.lower(): value for key, value in self.headers.items()}
        if method == "POST":
            self._json_type()
            reply = transport.post(headers, self._body(), self.caller)
        elif method == "DELETE":
            reply = transport.delete(headers, self.caller)
        else:
            self._send(405, None, {"Allow": "POST, DELETE"})
            return
        self._send(reply.status, reply.body, reply.headers)

    # --- answers ------------------------------------------------------------------

    def _cors(self) -> dict[str, str]:
        """For a page of an origin allowed with --allow-origin: it may read
        the answer."""
        origin = self.headers.get("Origin") if hasattr(self, "headers") else None
        if origin is None or origin not in self.server.config.origins:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Expose-Headers": CORS_EXPOSED,
            "Vary": "Origin",
        }

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
        for key, value in dict(self._cors(), **(headers or {})).items():
            self.send_header(key, value)
        try:
            self.end_headers()
            if data and self.command != "HEAD":
                self.wfile.write(data)
        except OSError:  # the client went away (or never finished its request)
            self.close_connection = True

    def version_string(self) -> str:
        return self.server_version

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        status = code.value if isinstance(code, HTTPStatus) else code
        log.info(
            "%s %s %s %s %s",
            self.client() if hasattr(self, "headers") else self.client_address[0],
            getattr(self, "who", "-"),
            getattr(self, "command", None) or "-",
            self._path(),
            status,
        )

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
