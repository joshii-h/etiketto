"""The system printer backend (IPP: CUPS queues and network printers),
against a fake that answers IPP requests by operation."""

from __future__ import annotations

import io
import ssl
import struct
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from PIL import Image

from etiketto.backends import get_backend, ipp, mdns, pwg, system
from etiketto.backends.base import (
    CoverOpen,
    FoundPrinter,
    MediaJam,
    NoFormat,
    OutOfPaper,
    PrinterError,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintOptions,
    QueuePaused,
    SuppliesEmpty,
    UnsupportedSize,
)
from etiketto.model import Label, TextElement

A4 = {"x-dimension": [21000], "y-dimension": [29700]}
CUSTOM = {"x-dimension": [(7620, 21590)], "y-dimension": [(12700, 35560)]}  # like the HP


class FakeIpp:
    """Answers what system.py asks; records operations and job attributes."""

    def __init__(self):
        self.state = system.IDLE
        self.reasons = ["none"]
        self.formats = ["application/pdf", "image/pwg-raster"]
        self.pwg_types = ["srgb_8", "sgray_8"]
        self.pwg_resolutions = [(300, 300, 3), (600, 600, 3)]
        self.sizes = [A4, CUSTOM]
        self.scaling = ["auto", "fit", "none"]
        self.job_states = [5, 9]  # processing, then completed
        self.job_errors: list[int] = []  # IPP status of the next job queries
        self.status = 0  # IPP status of every answer
        self.queues = [
            ("HP_Office", "HP LaserJet (Office)", "ipp://localhost:631/printers/HP_Office")
        ]
        self.requests: list[tuple[int, str, dict, dict]] = []
        self.printed = 0
        self.documents: list[tuple[str, bytes]] = []

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
        sizes = [_size(s) for s in self.sizes]
        attrs = [
            ipp.Attribute(ipp.ENUM, "printer-state", [self.state]),
            ipp.Attribute(ipp.KEYWORD, "printer-state-reasons", self.reasons),
            ipp.Attribute(ipp.TEXT, "printer-make-and-model", ["HP LaserJet Pro 4002"]),
            ipp.Attribute(ipp.TEXT, "printer-firmware-string-version", ["6.21"]),
            ipp.Attribute(ipp.MIME_TYPE, "document-format-supported", self.formats),
            ipp.Attribute(ipp.KEYWORD, "print-scaling-supported", self.scaling),
            ipp.Attribute(ipp.KEYWORD, "pwg-raster-document-type-supported", self.pwg_types),
            ipp.Attribute(
                ipp.RESOLUTION, "pwg-raster-document-resolution-supported", self.pwg_resolutions
            ),
        ]
        if sizes:
            attrs.append(ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size-supported", sizes))
        return _answer(0, [(ipp.PRINTER_GROUP, attrs)])

    def _op_0002(self, body, ops, job):  # Print-Job
        self.printed += job["copies"][0]
        mime = ops["document-format"][0]
        start = {"application/pdf": b"%PDF", "image/pwg-raster": pwg.SYNC}.get(mime, b"\xff\xd8")
        self.documents.append((mime, body[body.find(start) :]))
        return _answer(0, [(ipp.JOB_GROUP, [ipp.Attribute(ipp.INTEGER, "job-id", [12])])])

    def _op_0009(self, _body, ops, _job):  # Get-Job-Attributes
        assert ops["job-id"] == [12]
        if self.job_errors:
            status = self.job_errors.pop(0)
            return _answer(status, [(ipp.OPERATION_GROUP, [])])
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
                    ipp.Attribute(ipp.URI, "printer-uri-supported", [uri]),
                ],
            )
            for name, info, uri in self.queues
        ]
        return _answer(0, groups)

    def operations(self) -> list[int]:
        return [op for op, *_rest in self.requests]

    def job(self) -> dict:
        return next(r[3] for r in self.requests if r[0] == ipp.PRINT_JOB)


def _size(size: dict) -> dict:
    return {
        name: ipp.Attribute(
            ipp.RANGE_OF_INTEGER if isinstance(values[0], tuple) else ipp.INTEGER, name, values
        )
        for name, values in size.items()
    }


def _answer(status, groups):
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


def _image(width=1240, height=1748, size_mm=None):
    image = Image.new("RGB", (width, height), "white")
    if size_mm:
        image.info["size_mm"] = size_mm
    return image


def _print(image=None, host="HP_Office", copies=1, events=None):
    get_backend("system").print_label(
        image or _image(),
        host,
        PrintOptions(copies=copies),
        events.append if events is not None else None,
    )


# --- addresses ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, uri",
    [
        ("ipps://hp.local/ipp/print", "ipps://hp.local/ipp/print"),
        ("IPP://hp.local/ipp/print", "ipp://hp.local/ipp/print"),
        ("http://localhost:631/printers/HP", "ipp://localhost:631/printers/HP"),
        ("https://hp.local/ipp/print?waitjob=false", "ipps://hp.local/ipp/print?waitjob=false"),
        ("HP_Office", "ipp://localhost/printers/HP_Office"),
        ("Büro Drucker", "ipp://localhost/printers/B%C3%BCro%20Drucker"),
        ("hp.local", "ipp://hp.local/ipp/print"),
        ("hp.local:631", "ipp://hp.local:631/ipp/print"),
        (" 192.0.2.20:8631 ", "ipp://192.0.2.20:8631/ipp/print"),
        ("2001:db8::20", "ipp://[2001:db8::20]/ipp/print"),
        ("fe80::1%eth0", "ipp://[fe80::1%eth0]/ipp/print"),
        ("hp.local/ipp/print2", "ipp://hp.local/ipp/print2"),
    ],
)
def test_printer_uri(host, uri):
    assert system.printer_uri(host) == uri


# --- problems the printer reports -------------------------------------------------------


@pytest.mark.parametrize(
    "reasons, cls",
    [
        (["none"], None),
        (["toner-low-warning", "media-low-report"], None),  # no reason to stop
        # CUPS reports these, without a suffix, while all is well
        (["connecting-to-device", "cups-waiting-for-job-completed", "sending-to-device"], None),
        (["media-empty-error"], OutOfPaper),
        (["media-needed"], OutOfPaper),  # without a suffix: an error
        (["media-jam-error"], MediaJam),
        (["door-open-error"], CoverOpen),
        (["marker-supply-empty-error"], SuppliesEmpty),
        (["paused"], PrinterPaused),
        (["offline-report", "offline-error"], PrinterTimeout),
        (["spool-area-full-error"], PrinterError),  # unknown: generic
        (["vendor-x-error", "media-empty-error"], OutOfPaper),  # the known one wins
        ([3, b"media-empty", None], None),  # not text: a broken answer, ignored
    ],
)
def test_reasons(reasons, cls):
    error = system.reasons_error(reasons)
    assert (type(error) if error else None) is cls
    if error:
        assert error.details.startswith("printer-state-reasons: ")


def test_a_paused_queue_on_this_computer_is_resumed_in_the_settings():
    assert type(system.reasons_error(["paused"], local_queue=True)) is QueuePaused


def test_map_error():
    assert type(system.map_error(ConnectionRefusedError("x"))) is PrinterNotFound  # no CUPS
    assert type(system.map_error(ssl.SSLError("bad record"))) is PrinterError
    assert type(system.map_error(OSError("cannot resolve x"))) is PrinterNotFound
    assert type(system.map_error(ipp.IppError("HTTP 404"))) is PrinterError
    assert type(system.map_error(TimeoutError())) is PrinterTimeout
    error = CoverOpen("x")
    assert system.map_error(error) is error


def test_ipp_status_errors(fake):
    fake.status = 0x0406
    with pytest.raises(PrinterNotFound, match="0x0406 refused"):
        get_backend("system").query("Gone")
    fake.status = 0x0500
    with pytest.raises(PrinterError, match="0x0500"):
        get_backend("system").query("HP_Office")


def test_request_encoding_problems_are_printer_errors(fake):
    with pytest.raises(PrinterError):  # a job id that is not a number
        system._request(
            "ipp://p/", ipp.GET_JOB_ATTRIBUTES, [ipp.Attribute(ipp.INTEGER, "job-id", ["x"])]
        )


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
    assert backend.query("HP_Office").error == "queue_paused"  # a stopped queue here
    assert backend.query("hp.local").error == "paused"  # a stopped printer
    assert fake.requests[0][1] == "ipp://localhost/printers/HP_Office"


def test_query_survives_values_of_the_wrong_type(fake, monkeypatch):
    def broken(self, _body, _ops, _job):
        attrs = [
            ipp.Attribute(ipp.TEXT, "printer-state", ["idle"]),
            ipp.Attribute(ipp.INTEGER, "printer-state-reasons", [7]),
            ipp.Attribute(ipp.INTEGER, "printer-make-and-model", [42]),
        ]
        return _answer(0, [(ipp.PRINTER_GROUP, attrs)])

    monkeypatch.setattr(FakeIpp, "_op_000b", broken)
    info = get_backend("system").query("HP_Office")
    assert (info.model, info.state, info.error) == ("", "IDLE", "")


# --- page size and documents ----------------------------------------------------------------


def test_media_size_comes_from_the_label_in_millimetres(fake):
    _print(_image(2480, 3508, size_mm=(210, 297)))  # A4 renders to 2480 px: 209.97 mm
    assert fake.job()["media-col"][0]["media-size"][0] == {
        "x-dimension": [21000],
        "y-dimension": [29700],
    }


def test_media_size_without_size_info_comes_from_the_pixels(fake):
    _print(_image())  # 1240 × 1748 px at 300 dpi
    size = fake.job()["media-col"][0]["media-size"][0]
    assert size == {"x-dimension": [10499], "y-dimension": [14800]}


def test_a_landscape_page_is_turned_if_the_printer_only_takes_portrait(fake):
    fake.sizes, fake.formats = [A4], ["image/pwg-raster"]  # no custom sizes
    _print(_image(3508, 2480, size_mm=(297, 210)))
    assert fake.job()["media-col"][0]["media-size"][0]["x-dimension"] == [21000]
    page = pwg.decode(fake.documents[0][1])
    assert page.size[0] < page.size[1]  # portrait


def test_a_size_the_printer_does_not_take_is_refused_before_sending(fake):
    fake.sizes = [A4]
    with pytest.raises(UnsupportedSize, match="50 × 30 mm"):
        _print(_image(591, 354, size_mm=(50, 30)))
    assert ipp.PRINT_JOB not in fake.operations()


def test_unknown_or_odd_size_lists_let_the_printer_decide():
    assert system.fits([], 5000, 3000)
    assert system.fits(["not a size"], 5000, 3000)
    assert not system.fits([{"x-dimension": ["x"], "y-dimension": [1]}], 5000, 3000)


@pytest.mark.parametrize(
    "formats, mime",
    [
        (["application/pdf", "image/pwg-raster"], "application/pdf"),
        ([], "application/pdf"),  # not said: CUPS takes PDF
        (["image/urf", "image/pwg-raster", "image/jpeg"], "image/pwg-raster"),
        (["image/jpeg"], "image/jpeg"),
    ],
)
def test_document_format(formats, mime):
    attrs = {"document-format-supported": formats}
    _data, chosen = system.document(_image(), attrs)
    assert chosen == mime


def test_no_format_the_printer_takes(fake):
    fake.formats = ["image/urf", "application/vnd.hp-PCL"]
    with pytest.raises(NoFormat, match="image/urf"):
        _print()


def test_pdf_keeps_small_text_sharp():
    image = Image.new("RGB", (400, 100), "white")
    for x in range(0, 400, 2):  # one-pixel stripes: the worst case for JPEG
        image.paste((0, 0, 0), (x, 0, x + 1, 100))
    data, _mime = system.document(image, {"document-format-supported": ["application/pdf"]})
    start = data.index(b"\xff\xd8")
    embedded = Image.open(io.BytesIO(data[start : data.index(b"\xff\xd9", start) + 2]))
    worst = max(
        abs(a - b)
        for a, b in zip(embedded.convert("L").tobytes(), image.convert("L").tobytes(), strict=True)
    )
    assert worst < 60  # quality 75 with subsampling smears it by far more


def test_pwg_raster_in_the_printers_colour_space_and_resolution(fake):
    fake.formats, fake.sizes = ["image/pwg-raster"], []  # sizes: not said
    fake.pwg_types, fake.pwg_resolutions = ["sgray_8"], [(600, 600, 3)]
    _print(_image(118, 59, size_mm=(10, 5)))
    mime, data = fake.documents[0]
    page = pwg.decode(data)
    assert mime == "image/pwg-raster" and page.mode == "L" and page.size == (236, 118)
    assert struct.unpack_from(">II", data, 4 + 276) == (600, 600)  # HWResolution


@pytest.mark.parametrize(
    "resolutions, dpi",
    [
        ([], 300),
        ([(300, 300, 3), (600, 600, 3)], 300),
        ([(600, 600, 3), (1200, 1200, 3)], 600),  # never less sharp than the render
        ([(150, 150, 3)], 150),
        ([(300, 600, 3), "x", (300, 300, 4)], 300),  # odd entries: ignored
    ],
)
def test_pwg_resolution(resolutions, dpi):
    assert system._pwg_resolution(resolutions) == dpi


def test_pwg_raster_without_a_usable_colour_space(fake):
    fake.formats, fake.pwg_types = ["image/pwg-raster"], ["black_1", "cmyk_8"]
    with pytest.raises(NoFormat, match="black_1"):
        _print()


# --- printing ------------------------------------------------------------------------------


def test_print_sends_one_unscaled_page(fake):
    events = []
    _print(copies=3, events=events)
    _op, _uri, ops, job = next(r for r in fake.requests if r[0] == ipp.PRINT_JOB)
    assert ops["document-format"] == ["application/pdf"] and ops["job-name"] == ["Etiketto"]
    assert ops["requesting-user-name"][0]  # the login name, so the queue shows it as one's own
    assert job["copies"] == [3] and job["print-scaling"] == ["none"]
    assert fake.documents[0][1].startswith(b"%PDF") and fake.printed == 3
    assert events[0].copy == 1 and events[-1].copy == 3


def test_user_name_without_a_login(monkeypatch):
    def nobody():
        raise OSError("no login")

    monkeypatch.setattr(system.getpass, "getuser", nobody)
    assert system._user().values == ["etiketto"]


def test_print_without_print_scaling_or_job_id(fake, monkeypatch):
    fake.scaling = ["auto", "fit"]
    no_job_id = FakeIpp._op_0002

    def print_job(self, body, ops, job):
        no_job_id(self, body, ops, job)
        return _answer(0, [])

    monkeypatch.setattr(FakeIpp, "_op_0002", print_job)
    _print()
    assert "print-scaling" not in fake.job()
    assert ipp.GET_JOB_ATTRIBUTES not in fake.operations()  # nothing to wait for


def test_a_printer_with_a_problem_gets_no_job(fake):
    fake.reasons = ["door-open-error"]
    with pytest.raises(CoverOpen):
        _print()
    assert ipp.PRINT_JOB not in fake.operations()


def test_routine_cups_states_while_printing_do_not_stop_the_job(fake):
    # what CUPS's ipp backend reports during every job
    fake.reasons = ["none"]
    fake.job_states = [5, 5, 5, 9]
    original = FakeIpp._op_000b
    calls = []

    def attributes(self, body, ops, job):
        calls.append(1)
        if len(calls) > 1:
            self.reasons = ["connecting-to-device", "cups-waiting-for-job-completed"]
        return original(self, body, ops, job)

    FakeIpp._op_000b, saved = attributes, original
    try:
        _print()
    finally:
        FakeIpp._op_000b = saved
    assert ipp.CANCEL_JOB not in fake.operations()


def test_aborted_and_canceled_jobs(fake):
    fake.job_states = [8]
    with pytest.raises(PrinterError, match="aborted"):
        _print()
    fake.job_states = [7]
    with pytest.raises(PrinterError, match="canceled"):
        _print()


def test_a_stopped_job_is_canceled_with_the_printers_reason(fake):
    fake.job_states = [5, 6]  # processing, then processing-stopped
    fake.reasons = ["none"]
    original = FakeIpp._op_000b
    calls = []

    def attributes(self, body, ops, job):
        calls.append(1)
        self.reasons = ["none"] if len(calls) == 1 else ["media-jam-error"]
        return original(self, body, ops, job)

    FakeIpp._op_000b, saved = attributes, original
    try:
        with pytest.raises(MediaJam):
            _print()
    finally:
        FakeIpp._op_000b = saved
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_stopped_job_without_a_reason(fake):
    fake.job_states = [6]
    with pytest.raises(PrinterError, match="stopped"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_job_that_prints_for_ever_times_out_and_is_canceled(fake, monkeypatch):
    fake.job_states = [5]
    now = iter([0, 0, 1000])  # asked, started, then much later
    monkeypatch.setattr(system, "_clock", lambda: next(now))
    with pytest.raises(PrinterTimeout, match="still printing"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_job_waiting_behind_others_is_left_to_print(fake, monkeypatch):
    fake.job_states = [3]  # pending, for longer than Etiketto waits
    now = iter([0, 1000])
    monkeypatch.setattr(system, "_clock", lambda: next(now))
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


def test_a_missed_answer_while_waiting_is_tolerated(fake):
    fake.job_errors, fake.job_states = [0x0500, 0x0500], [9]  # a busy printer
    _print()
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == 3


def test_a_printer_that_never_answers_about_the_job(fake):
    fake.job_errors = [0x0500] * (system.POLL_FAILURES + 1)
    with pytest.raises(PrinterError, match="0x0500"):
        _print()


def test_a_job_the_printer_forgot_is_done(fake):
    fake.job_errors = [0x0406]  # finished and dropped from its list already
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


def test_cancel_ignores_a_printer_that_refuses(fake):
    fake.status = 0x0400
    system._cancel("ipp://localhost/printers/HP_Office", 12)  # no exception
    assert fake.operations() == [ipp.CANCEL_JOB]


def test_prepare_gives_a_portrait_page_with_its_size():
    backend = get_backend("system")
    label = Label(printer="system", elements=[TextElement(text="A")])
    label.use_default_size(backend.capabilities)
    across = backend.prepare(label, PrintOptions())
    label.set_orientation("along")
    along = backend.prepare(label, PrintOptions())
    assert across.size == along.size == (2480, 3508)  # A4 at 300 dpi, portrait
    assert across.info["size_mm"] == along.info["size_mm"] == (210, 297)


# --- discovery -----------------------------------------------------------------------------


def test_discover_lists_cups_queues_then_network_printers(fake, monkeypatch):
    fake.queues.append(("Labels", "", "ipp://localhost:631/classes/Labels"))
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
        FoundPrinter("Labels", "ipp://localhost/classes/Labels", "CUPS"),  # a class
        FoundPrinter("Canon Lobby", "ipp://canon.local:631/ipp/print", "192.0.2.30"),
        FoundPrinter("Epson Hall", "ipps://epson.local:443/ipp/print2", "192.0.2.40"),
    ]


def test_discover_without_cups(fake):
    fake.status = 0x0500
    assert get_backend("system").discover(timeout=0.01) == []
    fake.status, fake.queues = 0, [("", "nameless", ""), ("Plain", "", "")]
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
