"""The system printer backend (IPP: CUPS queues and network printers),
against a fake that answers IPP requests by operation."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from PIL import Image

from etiketto.backends import get_backend, ipp, mdns, system
from etiketto.backends.base import (
    CoverOpen,
    FoundPrinter,
    MediaJam,
    OutOfPaper,
    PrinterError,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintOptions,
    SuppliesEmpty,
)
from etiketto.model import Label, TextElement


class FakeIpp:
    """Answers what system.py asks; records operations and job attributes."""

    def __init__(self):
        self.state = system.IDLE
        self.reasons = ["none"]
        self.formats = ["application/pdf", "image/pwg-raster"]
        self.scaling = ["auto", "fit", "none"]
        self.job_states = [5, 9]  # processing, then completed
        self.status = 0  # IPP status of every answer
        self.queues = [("HP_Office", "HP LaserJet (Office)")]
        self.requests: list[tuple[int, str, dict, dict]] = []
        self.printed = 0
        self.documents: list[bytes] = []

    def send(self, uri, body, timeout):
        request = ipp.decode(body)  # the status field carries the operation
        operation = request.status
        ops = request.group(ipp.OPERATION_GROUP)
        job = request.group(ipp.JOB_GROUP)
        self.requests.append((operation, uri, ops, job))
        if self.status:
            message = [ipp.Attribute(ipp.TEXT, "status-message", ["refused"])]
            return _answer(self.status, [(ipp.OPERATION_GROUP, message)])
        return getattr(self, "_op_%04x" % operation)(body, ops, job)

    def _op_000b(self, _body, _ops, _job):  # Get-Printer-Attributes
        attrs = [
            ipp.Attribute(ipp.ENUM, "printer-state", [self.state]),
            ipp.Attribute(ipp.KEYWORD, "printer-state-reasons", self.reasons),
            ipp.Attribute(ipp.TEXT, "printer-make-and-model", ["HP LaserJet Pro 4002"]),
            ipp.Attribute(ipp.TEXT, "printer-firmware-string-version", ["6.21"]),
            ipp.Attribute(ipp.MIME_TYPE, "document-format-supported", self.formats),
            ipp.Attribute(ipp.KEYWORD, "print-scaling-supported", self.scaling),
        ]
        return _answer(0, [(ipp.PRINTER_GROUP, attrs)])

    def _op_0002(self, body, _ops, job):  # Print-Job
        self.printed += job["copies"][0]
        self.documents.append(body[body.find(b"%PDF") :])
        return _answer(0, [(ipp.JOB_GROUP, [ipp.Attribute(ipp.INTEGER, "job-id", [12])])])

    def _op_0009(self, _body, ops, _job):  # Get-Job-Attributes
        assert ops["job-id"] == [12]
        state = self.job_states.pop(0) if len(self.job_states) > 1 else self.job_states[0]
        attrs = [
            ipp.Attribute(ipp.ENUM, "job-state", [state]),
            ipp.Attribute(ipp.INTEGER, "job-impressions-completed", [1]),
        ]
        return _answer(0, [(ipp.JOB_GROUP, attrs)])

    def _op_0008(self, _body, _ops, _job):  # Cancel-Job
        return _answer(0, [])

    def _op_4002(self, _body, _ops, _job):  # CUPS-Get-Printers
        groups = [
            (
                ipp.PRINTER_GROUP,
                [
                    ipp.Attribute(ipp.NAME, "printer-name", [name]),
                    ipp.Attribute(ipp.TEXT, "printer-info", [info]),
                ],
            )
            for name, info in self.queues
        ]
        return _answer(0, groups)

    def operations(self) -> list[int]:
        return [op for op, *_rest in self.requests]


def _answer(status, groups):
    import struct

    out = struct.pack(">BBHi", 2, 0, status, 1)
    for tag, attributes in groups:
        out += bytes([tag]) + b"".join(ipp._attribute(a) for a in attributes)
    return ipp.decode(out + bytes([ipp.END_OF_ATTRIBUTES]))


@pytest.fixture
def fake(monkeypatch):
    printer = FakeIpp()
    monkeypatch.setattr(ipp, "send", printer.send)
    monkeypatch.setattr(system, "_sleep", lambda seconds: None)
    monkeypatch.setattr(mdns, "browse_services", lambda service_type, match, timeout: [])
    return printer


def _image(width=1240, height=1748):
    return Image.new("RGB", (width, height), "white")


# --- addresses and errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    "host, uri",
    [
        ("ipps://hp.local/ipp/print", "ipps://hp.local/ipp/print"),
        ("HP_Office", "ipp://localhost/printers/HP_Office"),
        ("Büro Drucker", "ipp://localhost/printers/B%C3%BCro%20Drucker"),
        ("hp.local", "ipp://hp.local/ipp/print"),
        (" 192.0.2.20 ", "ipp://192.0.2.20/ipp/print"),
        ("2001:db8::20", "ipp://[2001:db8::20]/ipp/print"),
    ],
)
def test_printer_uri(host, uri):
    assert system.printer_uri(host) == uri


@pytest.mark.parametrize(
    "reasons, cls",
    [
        (["none"], None),
        (["toner-low-warning", "media-low-report"], None),  # no reason to stop
        (["media-empty-error"], OutOfPaper),
        (["media-needed"], OutOfPaper),  # without a suffix: an error
        (["media-jam-error"], MediaJam),
        (["door-open-error"], CoverOpen),
        (["marker-supply-empty-error"], SuppliesEmpty),
        (["paused"], PrinterPaused),
        (["offline-report", "offline-error"], PrinterTimeout),
        (["spool-area-full-error"], PrinterError),  # unknown: generic
    ],
)
def test_reasons(reasons, cls):
    error = system.reasons_error(reasons)
    assert (type(error) if error else None) is cls
    if error:
        assert error.details.startswith("printer-state-reasons: ")


def test_map_error():
    assert isinstance(system.map_error(ConnectionRefusedError("x")), PrinterError)
    assert type(system.map_error(OSError("cannot resolve x"))) is PrinterNotFound
    assert type(system.map_error(ipp.IppError("HTTP 404"))) is PrinterError
    error = CoverOpen("x")
    assert system.map_error(error) is error


def test_ipp_status_errors(fake):
    fake.status = 0x0406
    with pytest.raises(PrinterNotFound, match="0x0406 refused"):
        get_backend("system").query("Gone")
    fake.status = 0x0500
    with pytest.raises(PrinterError, match="0x0500"):
        get_backend("system").query("HP_Office")


def test_network_errors_become_printer_errors(monkeypatch):
    def unreachable(uri, body, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ipp, "send", unreachable)
    with pytest.raises(PrinterTimeout):
        get_backend("system").query("hp.local")


# --- query --------------------------------------------------------------------------------


def test_query_reports_model_state_and_problems(fake):
    backend = get_backend("system")
    info = backend.query("HP_Office")
    assert (info.model, info.state, info.error, info.firmware) == (
        "HP LaserJet Pro 4002",
        "IDLE",
        "",
        "6.21",
    )
    assert backend.display_name(info) == "HP LaserJet Pro 4002"
    assert backend.display_name(None) == backend.name
    fake.state = system.PROCESSING
    assert backend.query("HP_Office").state == "BUSY"
    fake.reasons = ["media-empty-error"]
    info = backend.query("HP_Office")
    assert (info.state, info.error) == ("ERROR", "out_of_paper")
    fake.state, fake.reasons = system.STOPPED, ["none"]
    assert backend.query("HP_Office").error == "paused"  # stopped without a reason
    assert fake.requests[0][1] == "ipp://localhost/printers/HP_Office"


# --- documents ----------------------------------------------------------------------------


def test_document_is_pdf_or_jpeg():
    data, mime = system.document(_image(), ["application/pdf"])
    assert mime == "application/pdf" and data.startswith(b"%PDF")
    data, mime = system.document(_image(), ["image/pwg-raster", "image/jpeg"])
    assert mime == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (1240, 1748)


def test_media_is_the_label_size_in_hundredths_of_millimetres():
    media = system._media(_image())  # 1240 × 1748 px at 300 dpi = 105 × 148 mm
    size = media.values[0]["media-size"].values[0]
    assert (size["x-dimension"].values, size["y-dimension"].values) == ([10499], [14800])


# --- printing ------------------------------------------------------------------------------


def test_print_sends_one_unscaled_pdf_page(fake):
    events = []
    get_backend("system").print_label(_image(), "HP_Office", PrintOptions(copies=3), events.append)
    _op, _uri, ops, job = next(r for r in fake.requests if r[0] == ipp.PRINT_JOB)
    assert ops["document-format"] == ["application/pdf"] and ops["job-name"] == ["Etiketto"]
    assert job["copies"] == [3] and job["print-scaling"] == ["none"]
    assert job["media-col"][0]["media-size"][0] == {"x-dimension": [10499], "y-dimension": [14800]}
    assert fake.documents[0].startswith(b"%PDF") and fake.printed == 3
    assert events[0].copy == 1 and events[-1].copy == 3


def test_print_without_print_scaling_or_job_id(fake, monkeypatch):
    fake.scaling = ["auto", "fit"]
    no_job_id = FakeIpp._op_0002

    def print_job(self, body, ops, job):
        no_job_id(self, body, ops, job)
        return _answer(0, [])

    monkeypatch.setattr(FakeIpp, "_op_0002", print_job)
    get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    job = next(r[3] for r in fake.requests if r[0] == ipp.PRINT_JOB)
    assert "print-scaling" not in job
    assert ipp.GET_JOB_ATTRIBUTES not in fake.operations()  # nothing to wait for


def test_a_printer_with_a_problem_gets_no_job(fake):
    fake.reasons = ["door-open-error"]
    with pytest.raises(CoverOpen):
        get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    assert ipp.PRINT_JOB not in fake.operations()


def test_aborted_job(fake):
    fake.job_states = [8]
    with pytest.raises(PrinterError, match="aborted"):
        get_backend("system").print_label(_image(), "HP_Office", PrintOptions())


def test_a_problem_while_printing_cancels_the_job(fake, monkeypatch):
    fake.job_states = [5]  # processing for ever
    reasons = iter([["none"], ["media-jam-error"]])
    original = FakeIpp._op_000b

    def attributes(self, body, ops, job):
        self.reasons = next(reasons)
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", attributes)
    with pytest.raises(MediaJam):
        get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_job_that_never_ends_times_out_and_is_canceled(fake, monkeypatch):
    fake.job_states = [5]
    now = iter([0, 0, 1000])
    monkeypatch.setattr(system, "_clock", lambda: next(now))
    with pytest.raises(PrinterTimeout, match="still 5"):
        get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_cancel_ignores_a_printer_that_refuses(fake):
    fake.status = 0x0400
    system._cancel("ipp://localhost/printers/HP_Office", 12)  # no exception
    assert fake.operations() == [ipp.CANCEL_JOB]


def test_prepare_turns_content_along_the_paper():
    backend = get_backend("system")
    label = Label(printer="system", elements=[TextElement(text="A")])
    label.use_default_size(backend.capabilities)
    across = backend.prepare(label, PrintOptions())
    label.set_orientation("along")
    along = backend.prepare(label, PrintOptions())
    assert across.size == along.size == (2480, 3508)  # A4 at 300 dpi, portrait


# --- discovery -----------------------------------------------------------------------------


def test_discover_lists_cups_queues_then_network_printers(fake, monkeypatch):
    services = {
        "_ipp._tcp": [
            mdns.Service("HP LaserJet (Office)", "hp.local", "192.0.2.20", 631, {}),  # a queue
            mdns.Service("Canon Lobby", "canon.local", "192.0.2.30", 631, {"rp": "ipp/print"}),
        ],
        "_ipps._tcp": [
            mdns.Service("Canon Lobby", "canon.local", "192.0.2.30", 443, {}),  # seen already
            mdns.Service("Epson Hall", "epson.local", "192.0.2.40", 443, {"rp": "ipp/print2"}),
        ],
    }
    monkeypatch.setattr(
        mdns, "browse_services", lambda service_type, match, timeout: services[service_type]
    )
    assert get_backend("system").discover(timeout=0.01) == [
        FoundPrinter("HP LaserJet (Office)", "ipp://localhost/printers/HP_Office", "CUPS"),
        FoundPrinter("Canon Lobby", "ipp://canon.local:631/ipp/print", "192.0.2.30"),
        FoundPrinter("Epson Hall", "ipps://epson.local:443/ipp/print2", "192.0.2.40"),
    ]


def test_discover_without_cups(fake):
    fake.status = 0x0500
    assert get_backend("system").discover(timeout=0.01) == []
    fake.status, fake.queues = 0, [("", "nameless"), ("Plain", "")]
    assert get_backend("system").discover(timeout=0.01) == [
        FoundPrinter("Plain", "ipp://localhost/printers/Plain", "CUPS")
    ]


# --- the contract every backend passes ------------------------------------------------------


class TestContract(BackendContract):
    backend_id = "system"

    @pytest.fixture
    def harness(self, fake):
        def fail():
            fake.reasons = ["media-empty-error"]

        return SimpleNamespace(
            backend=get_backend("system"), fail=fail, printed=lambda: fake.printed
        )
