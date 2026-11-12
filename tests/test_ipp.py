"""IPP encoding and decoding, and the HTTP transport against a local fake."""

from __future__ import annotations

import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from etiketto.backends import ipp, mdns


def response(status: int = 0, groups: list[tuple[int, list[ipp.Attribute]]] = ()) -> bytes:
    """An IPP answer as a printer sends it, built with the request encoder."""
    out = struct.pack(">BBHi", 2, 0, status, 7)
    for tag, attributes in groups:
        out += bytes([tag]) + b"".join(ipp._attribute(a) for a in attributes)
    return out + bytes([ipp.END_OF_ATTRIBUTES])


# --- encoding -----------------------------------------------------------------


def test_request_starts_with_charset_and_language():
    data = ipp.encode(
        ipp.GET_PRINTER_ATTRIBUTES,
        5,
        [ipp.Attribute(ipp.URI, "printer-uri", ["ipp://p/ipp/print"])],
    )
    assert data[:8] == struct.pack(">BBHi", 2, 0, ipp.GET_PRINTER_ATTRIBUTES, 5)
    assert data[8] == ipp.OPERATION_GROUP
    names = [b"attributes-charset", b"attributes-natural-language", b"printer-uri"]
    positions = [data.index(name) for name in names]
    assert positions == sorted(positions) and data.endswith(bytes([ipp.END_OF_ATTRIBUTES]))


def test_values_of_every_kind_survive_a_round_trip():
    media_col = {
        "media-size": ipp.Attribute(
            ipp.BEGIN_COLLECTION,
            "media-size",
            [
                {
                    "x-dimension": ipp.Attribute(ipp.INTEGER, "x-dimension", [5000]),
                    "y-dimension": ipp.Attribute(ipp.INTEGER, "y-dimension", [2500]),
                }
            ],
        )
    }
    attributes = [
        ipp.Attribute(ipp.INTEGER, "copies", [3]),
        ipp.Attribute(ipp.ENUM, "printer-state", [5]),
        ipp.Attribute(ipp.BOOLEAN, "color-supported", [True]),
        ipp.Attribute(ipp.KEYWORD, "printer-state-reasons", ["media-empty-error", "door-open"]),
        ipp.Attribute(ipp.TEXT, "printer-info", ["Büro, 2. Stock"]),
        ipp.Attribute(ipp.RESOLUTION, "printer-resolution-default", [(600, 600, 3)]),
        ipp.Attribute(ipp.RANGE_OF_INTEGER, "copies-supported", [(1, 999)]),
        ipp.Attribute(ipp.BEGIN_COLLECTION, "media-col-ready", [media_col]),
        ipp.Attribute(ipp.OCTET_STRING, "printer-uuid-raw", [b"\x00\xff"]),
    ]
    decoded = ipp.decode(response(groups=[(ipp.PRINTER_GROUP, attributes)]))
    printer = decoded.group(ipp.PRINTER_GROUP)
    assert decoded.ok and decoded.request_id == 7
    assert printer["copies"] == [3] and printer["printer-state"] == [5]
    assert printer["color-supported"] == [True]
    assert printer["printer-state-reasons"] == ["media-empty-error", "door-open"]
    assert printer["printer-info"] == ["Büro, 2. Stock"]
    assert printer["printer-resolution-default"] == [(600, 600, 3)]
    assert printer["copies-supported"] == [(1, 999)]
    size = printer["media-col-ready"][0]["media-size"][0]
    assert size == {"x-dimension": [5000], "y-dimension": [2500]}
    assert printer["printer-uuid-raw"] == [b"\x00\xff"]


def test_groups_keep_their_order_and_out_of_band_values_are_none():
    data = response(
        0x0400,
        [
            (ipp.OPERATION_GROUP, [ipp.Attribute(ipp.TEXT, "status-message", ["not found"])]),
            (ipp.UNSUPPORTED_GROUP, [ipp.Attribute(0x10, "print-scaling", [b""])]),
        ],
    )
    decoded = ipp.decode(data)
    assert not decoded.ok and decoded.status == 0x0400
    assert decoded.group(ipp.OPERATION_GROUP)["status-message"] == ["not found"]
    assert decoded.group(ipp.UNSUPPORTED_GROUP) == {"print-scaling": [None]}
    assert decoded.group(ipp.JOB_GROUP) == {} and len(decoded.all(ipp.UNSUPPORTED_GROUP)) == 1


# --- answers that are not what they should be -------------------------------------


@pytest.mark.parametrize(
    "data, problem",
    [
        (b"", "ends early"),
        (b"HTTP/1.1 200 OK\r\n", "not an IPP answer"),
        (struct.pack(">BBHi", 2, 0, 0, 1) + b"\x41\x00\x04name", "outside a group"),
        (struct.pack(">BBHi", 2, 0, 0, 1) + b"\x04\x41\x00\x00\x00\x01x", "without an attribute"),
        (struct.pack(">BBHi", 2, 0, 0, 1) + b"\x04\x41\x00\x04na", "ends early"),
        (struct.pack(">BBHi", 2, 0, 0, 1) + b"\x04", "ends early"),  # no end tag
    ],
)
def test_malformed_answers_raise_ipp_error(data, problem):
    with pytest.raises(ipp.IppError, match=problem):
        ipp.decode(data)


def test_collection_value_without_member_name():
    data = struct.pack(">BBHi", 2, 0, 0, 1) + b"\x04"
    data += ipp._field(ipp.BEGIN_COLLECTION, b"media-col", b"")
    data += ipp._field(ipp.INTEGER, b"", struct.pack(">i", 1))
    with pytest.raises(ipp.IppError, match="member name"):
        ipp.decode(data)


def test_collections_nested_too_deep():
    nested: dict = {}
    for _ in range(ipp.MAX_DEPTH + 1):
        nested = {"m": ipp.Attribute(ipp.BEGIN_COLLECTION, "m", [nested])}
    data = response(
        groups=[(ipp.PRINTER_GROUP, [ipp.Attribute(ipp.BEGIN_COLLECTION, "x", [nested])])]
    )
    with pytest.raises(ipp.IppError, match="too deep"):
        ipp.decode(data)


# --- printer addresses ---------------------------------------------------------------


@pytest.mark.parametrize(
    "uri, parts",
    [
        ("ipp://localhost/printers/HP", ("ipp", "localhost", 631, "/printers/HP")),
        ("ipps://HP38CA.local:443/ipp/print", ("ipps", "hp38ca.local", 443, "/ipp/print")),
        ("ipp://[::1]:8631", ("ipp", "::1", 8631, "/")),
    ],
)
def test_http_uri(uri, parts):
    assert ipp.http_uri(uri) == parts


@pytest.mark.parametrize("uri", ["http://p/ipp", "ipp:///ipp/print", "printer.local"])
def test_http_uri_refuses_other_addresses(uri):
    with pytest.raises(ipp.IppError, match="not a printer address"):
        ipp.http_uri(uri)


# --- transport --------------------------------------------------------------------------


class FakePrinter:
    """A tiny HTTP server that answers every POST with a fixed body."""

    def __init__(self, body: bytes = b"", status: int = 200):
        fake = self
        self.body, self.status, self.requests = body, status, []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                fake.requests.append((self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/ipp")
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_printer(monkeypatch):
    monkeypatch.setattr(mdns, "resolve_host", lambda host, family=0: "127.0.0.1")
    printers = []

    def make(body=b"", status=200):
        printers.append(FakePrinter(body, status))
        return printers[-1]

    yield make
    for printer in printers:
        printer.close()


@pytest.mark.real_ipp
def test_send_posts_ipp_and_decodes_the_answer(fake_printer):
    printer = fake_printer(
        response(groups=[(ipp.PRINTER_GROUP, [ipp.Attribute(ipp.ENUM, "printer-state", [3])])])
    )
    uri = "ipp://Büro-Drucker.local:%d/ipp/print queue" % printer.port
    answer = ipp.send(uri, b"request", timeout=5)
    path, headers, body = printer.requests[0]
    assert answer.group(ipp.PRINTER_GROUP) == {"printer-state": [3]}
    assert body == b"request" and headers["Content-Type"] == "application/ipp"
    assert path == "/ipp/print%20queue"  # quoted, not sent raw
    # the name the printer is known by, not the address it was reached at
    assert headers["Host"] == "xn--bro-drucker-thb.local:%d" % printer.port


@pytest.mark.real_ipp
def test_send_refuses_http_errors_and_huge_answers(fake_printer, monkeypatch):
    printer = fake_printer(b"Forbidden", status=403)
    with pytest.raises(ipp.IppError, match="HTTP 403"):
        ipp.send("ipp://p:%d/" % printer.port, b"", timeout=5)
    monkeypatch.setattr(ipp, "MAX_RESPONSE", 10)
    big = fake_printer(
        response(groups=[(ipp.PRINTER_GROUP, [ipp.Attribute(ipp.TEXT, "x", ["y" * 50])])])
    )
    with pytest.raises(ipp.IppError, match="larger than 10 bytes"):
        ipp.send("ipp://p:%d/" % big.port, b"", timeout=5)


def test_host_header():
    assert ipp._header_host("::1") == "[::1]"
    assert ipp._header_host("printer.local") == "printer.local"
    with pytest.raises(ipp.IppError, match="not a host name"):
        ipp._header_host("a..b")


@pytest.mark.real_ipp
def test_ipps_trusts_the_printers_own_certificate(monkeypatch):
    # printers come with self-signed certificates, as CUPS knows
    made = {}

    class Connection:
        def __init__(self, host, port, timeout, context):
            made.update(host=host, port=port, context=context)

        def __getattr__(self, name):
            raise OSError("stop here")

        def close(self):
            made["closed"] = True

    monkeypatch.setattr(mdns, "resolve_host", lambda host, family=0: "192.0.2.4")
    monkeypatch.setattr(ipp.http.client, "HTTPSConnection", Connection)
    with pytest.raises(OSError, match="stop here"):
        ipp.send("ipps://hp.local/ipp/print", b"", timeout=5)
    context = made["context"]
    assert made["host"] == "192.0.2.4" and made["port"] == 631 and made["closed"]
    assert not context.check_hostname and context.verify_mode == ipp.ssl.CERT_NONE


def test_values_of_unknown_types_stay_bytes():
    data = struct.pack(">BBHi", 2, 0, 0, 1) + b"\x04"
    data += ipp._field(0x60, b"vendor-thing", b"\x01\x02") + bytes([ipp.END_OF_ATTRIBUTES])
    assert ipp.decode(data).group(ipp.PRINTER_GROUP) == {"vendor-thing": [b"\x01\x02"]}


def test_job_attributes_and_document_follow_the_operation_attributes():
    data = ipp.encode(
        ipp.PRINT_JOB,
        2,
        [ipp.Attribute(ipp.URI, "printer-uri", ["ipp://p/"])],
        [ipp.Attribute(ipp.INTEGER, "copies", [2])],
        data=b"%PDF-1.4",
    )
    assert data.endswith(bytes([ipp.END_OF_ATTRIBUTES]) + b"%PDF-1.4")
    assert data.index(bytes([ipp.JOB_GROUP]) + bytes([ipp.INTEGER])) > data.index(b"printer-uri")
