"""IPP over HTTP (RFC 8010 encoding, RFC 8011 operations): just enough to ask
a printer or a CUPS queue for its state and media, and to send a job.

Answers come from the network, so decoding is defensive: a size limit, bounds
checks on every length, a depth limit for collections; anything malformed
raises IppError instead of an exception from deep inside struct.
"""

from __future__ import annotations

import http.client
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from etiketto.backends import mdns

# operations
PRINT_JOB = 0x0002
VALIDATE_JOB = 0x0004
CANCEL_JOB = 0x0008
GET_JOB_ATTRIBUTES = 0x0009
GET_PRINTER_ATTRIBUTES = 0x000B
CUPS_GET_PRINTERS = 0x4002

# delimiter tags (attribute groups)
OPERATION_GROUP = 0x01
JOB_GROUP = 0x02
END_OF_ATTRIBUTES = 0x03
PRINTER_GROUP = 0x04
UNSUPPORTED_GROUP = 0x05

# value tags
INTEGER = 0x21
BOOLEAN = 0x22
ENUM = 0x23
OCTET_STRING = 0x30
DATE_TIME = 0x31
RESOLUTION = 0x32
RANGE_OF_INTEGER = 0x33
BEGIN_COLLECTION = 0x34
END_COLLECTION = 0x37
TEXT = 0x41
NAME = 0x42
KEYWORD = 0x44
URI = 0x45
CHARSET = 0x47
LANGUAGE = 0x48
MIME_TYPE = 0x49
MEMBER_NAME = 0x4A
STRING_TAGS = frozenset({0x35, 0x36, TEXT, NAME, KEYWORD, URI, 0x46, CHARSET, LANGUAGE, MIME_TYPE})

DEFAULT_PORT = 631
MAX_RESPONSE = 8 * 1024 * 1024  # a printer's attributes are some kB; CUPS lists can be more
MAX_DEPTH = 8  # nested collections (media-col holds media-size: depth 2)


class IppError(ValueError):
    """A malformed answer, or one that is not IPP at all."""


@dataclass
class Attribute:
    """One attribute to send: value tag, name, one or more values. A
    collection is a dict of member name to Attribute."""

    tag: int
    name: str
    values: list[Any] = field(default_factory=list)


@dataclass
class Response:
    status: int
    request_id: int
    groups: list[tuple[int, dict[str, list[Any]]]]

    @property
    def ok(self) -> bool:
        return self.status < 0x0100  # successful-ok and its variants

    def group(self, tag: int) -> dict[str, list[Any]]:
        """The first group with this tag (job or printer attributes)."""
        return next((attrs for group_tag, attrs in self.groups if group_tag == tag), {})

    def all(self, tag: int) -> list[dict[str, list[Any]]]:
        return [attrs for group_tag, attrs in self.groups if group_tag == tag]


# --- encoding ------------------------------------------------------------------


def _value(tag: int, value: Any) -> bytes:
    if tag in (INTEGER, ENUM):
        return struct.pack(">i", value)
    if tag == BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == RANGE_OF_INTEGER:
        return struct.pack(">ii", *value)
    if tag == RESOLUTION:
        return struct.pack(">iib", *value)
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _attribute(attribute: Attribute) -> bytes:
    out = b""
    for i, value in enumerate(attribute.values):
        name = attribute.name.encode("utf-8") if i == 0 else b""
        if attribute.tag == BEGIN_COLLECTION:
            out += _field(BEGIN_COLLECTION, name, b"") + _members(value)
            out += _field(END_COLLECTION, b"", b"")
        else:
            out += _field(attribute.tag, name, _value(attribute.tag, value))
    return out


def _members(collection: dict[str, Attribute]) -> bytes:
    out = b""
    for member in collection.values():
        out += _field(MEMBER_NAME, b"", member.name.encode("utf-8"))
        out += _attribute(Attribute(member.tag, "", member.values))
    return out


def _field(tag: int, name: bytes, value: bytes) -> bytes:
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def encode(
    operation: int,
    request_id: int,
    operation_attributes: list[Attribute],
    job_attributes: list[Attribute] | None = None,
    data: bytes = b"",
) -> bytes:
    """A request: IPP 2.0, the operation attributes (charset and natural
    language first, as RFC 8011 wants), optional job template attributes,
    then the document."""
    head = [
        Attribute(CHARSET, "attributes-charset", ["utf-8"]),
        Attribute(LANGUAGE, "attributes-natural-language", ["en"]),
    ]
    out = struct.pack(">BBHi", 2, 0, operation, request_id)
    out += bytes([OPERATION_GROUP]) + b"".join(_attribute(a) for a in head + operation_attributes)
    if job_attributes:
        out += bytes([JOB_GROUP]) + b"".join(_attribute(a) for a in job_attributes)
    return out + bytes([END_OF_ATTRIBUTES]) + data


# --- decoding ------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise IppError("answer ends early at byte %d" % self.pos)
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def short(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def field(self, tag: int) -> tuple[str, bytes]:
        name = self.take(self.short())
        return name.decode("utf-8", "replace"), self.take(self.short())


# value tags of a fixed size: struct format; one number, or a tuple
FIXED = {INTEGER: ">i", ENUM: ">i", RANGE_OF_INTEGER: ">ii", RESOLUTION: ">iib"}


def _decode_value(tag: int, raw: bytes) -> Any:
    fmt = FIXED.get(tag)
    if fmt and len(raw) == struct.calcsize(fmt):
        values = struct.unpack(fmt, raw)
        return values[0] if len(values) == 1 else values
    if tag == BOOLEAN and len(raw) == 1:
        return raw != b"\x00"
    if tag in STRING_TAGS:
        return raw.decode("utf-8", "replace")
    if 0x10 <= tag <= 0x1F:  # out of band: unsupported, unknown, no-value
        return None
    return raw  # octetString, dateTime and anything this decoder does not know


def _collection(reader: _Reader, depth: int) -> dict[str, list[Any]]:
    if depth > MAX_DEPTH:
        raise IppError("collections nested too deep")
    members: dict[str, list[Any]] = {}
    current = None
    while True:
        tag = reader.byte()
        _name, raw = reader.field(tag)
        if tag == END_COLLECTION:
            return members
        if tag == MEMBER_NAME:
            current = members.setdefault(raw.decode("utf-8", "replace"), [])
        elif current is None:
            raise IppError("collection value without a member name")
        elif tag == BEGIN_COLLECTION:
            current.append(_collection(reader, depth + 1))
        else:
            current.append(_decode_value(tag, raw))


def decode(data: bytes) -> Response:
    """A response: status, request id and the attribute groups in order."""
    reader = _Reader(data)
    major = reader.byte()
    reader.byte()  # minor version
    if major not in (1, 2):
        raise IppError("not an IPP answer (version %d)" % major)
    status = reader.short()
    request_id = struct.unpack(">i", reader.take(4))[0]
    groups: list[tuple[int, dict[str, list[Any]]]] = []
    attrs: dict[str, list[Any]] | None = None
    last: list[Any] | None = None
    while True:
        tag = reader.byte()
        if tag == END_OF_ATTRIBUTES:
            return Response(status, request_id, groups)
        if tag < 0x10:  # a new group
            attrs = {}
            groups.append((tag, attrs))
            continue
        if attrs is None:
            raise IppError("attribute outside a group")
        name, raw = reader.field(tag)
        if name:
            last = attrs.setdefault(name, [])
        elif last is None:
            raise IppError("additional value without an attribute")
        last.append(_collection(reader, 1) if tag == BEGIN_COLLECTION else _decode_value(tag, raw))


# --- transport -------------------------------------------------------------------


def http_uri(uri: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path) for an ipp:// or ipps:// printer URI."""
    parts = urlsplit(uri)
    if parts.scheme not in ("ipp", "ipps") or not parts.hostname:
        raise IppError("not a printer address: %r" % uri)
    return parts.scheme, parts.hostname, parts.port or DEFAULT_PORT, parts.path or "/"


def send(uri: str, body: bytes, timeout: float) -> Response:
    """POST one request to the printer and decode the answer. OSError for
    network problems, IppError for answers that are not IPP."""
    scheme, host, port, path = http_uri(uri)
    address = mdns.resolve_host(host)
    if scheme == "ipps":
        # printers bring self-signed certificates; like CUPS, trust them
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            address, port, timeout=timeout, context=context
        )
    else:
        connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        connection.putrequest("POST", quote(path, safe="/%"), skip_host=True)
        # CUPS checks the Host header against its own names
        connection.putheader("Host", "%s:%d" % (_header_host(host), port))
        connection.putheader("Content-Type", "application/ipp")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        answer = connection.getresponse()
        data = answer.read(MAX_RESPONSE + 1)
    finally:
        connection.close()
    if answer.status != 200:
        raise IppError("HTTP %d %s" % (answer.status, answer.reason))
    if len(data) > MAX_RESPONSE:
        raise IppError("answer larger than %d bytes" % MAX_RESPONSE)
    return decode(data)


def _header_host(host: str) -> str:
    """The host as an HTTP header wants it: IPv6 in brackets, other names
    IDNA-encoded ("xn--..." for umlauts)."""
    if ":" in host:
        return "[%s]" % host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise IppError("not a host name: %r" % host) from e
