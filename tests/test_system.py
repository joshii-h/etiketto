"""The system printer backend (IPP: CUPS queues and network printers),
against a fake that answers IPP requests by operation."""

from __future__ import annotations

import io
import re
import ssl
import struct
import zlib
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from fake_backends import FakeMono
from PIL import Image, ImageChops

from etiketto.backends import get_backend, ipp, mdns, pdf, pwg, system, urf
from etiketto.backends.base import (
    CodeTooLarge,
    CoverOpen,
    FoundPrinter,
    JobCanceled,
    JobHeld,
    Margins,
    MediaJam,
    NoFormat,
    OutOfPaper,
    PrinterBusy,
    PrinterError,
    PrinterNotFound,
    PrinterPaused,
    PrinterTimeout,
    PrintOptions,
    PrintResult,
    Progress,
    QueuePaused,
    SuppliesEmpty,
    UnsupportedSize,
    margins_for,
)
from etiketto.model import CodeElement, Label, TextElement
from etiketto.sheets import find
from etiketto.sheets.layout import Copies, Pages, SheetJob, px

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
        self.urf: list[str] | None = None  # urf-supported, None: not said
        self.resolution = (600, 600, 3)  # printer-resolution-default
        self.sizes = [A4, CUSTOM]
        self.scaling = ["auto", "fit", "none"]
        # processing, then completed; or (state, pages done); None: no such attribute
        self.job_states: list = [5, 9]
        self.job_reasons = ["none"]
        self.job_errors: list = []  # IPP status (or exception) of the next job queries
        self.copies_supported: tuple[int, int] | None = (1, 999)
        self.ignored: list[str] = []  # Print-Job answers 0x0001 with these unsupported
        self.operations_supported = [0x0002, 0x0004, 0x0008, 0x0009, 0x000B]
        self.validate_status: int | None = None  # Validate-Job: else as Print-Job
        self.print_errors: list[int] = []  # IPP status of the next Print-Jobs (0: fine)
        self.canceled: list[int] = []
        self.later: dict = {}  # printer state and reasons once a job was sent
        self.now = 0.0  # the clock system.py sees; each poll is a second
        self.job_id = 11
        self.status = 0  # IPP status of every answer
        self.queues = [
            ("HP_Office", "HP LaserJet (Office)", "ipp://localhost:631/printers/HP_Office")
        ]
        self.extra: list[ipp.Attribute] = []  # more printer attributes (margins)
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
            ipp.Attribute(ipp.RESOLUTION, "printer-resolution-default", [self.resolution]),
        ]
        if self.copies_supported:
            attrs.append(
                ipp.Attribute(ipp.RANGE_OF_INTEGER, "copies-supported", [self.copies_supported])
            )
        if sizes:
            attrs.append(ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size-supported", sizes))
        attrs.append(ipp.Attribute(ipp.ENUM, "operations-supported", self.operations_supported))
        if self.urf is not None:
            attrs.append(ipp.Attribute(ipp.KEYWORD, "urf-supported", self.urf))
        return _answer(0, [(ipp.PRINTER_GROUP, attrs + self.extra)])

    def _unsupported(self) -> list:
        if not self.ignored:
            return []
        attributes = [ipp.Attribute(0x10, name, [b""]) for name in self.ignored]
        return [(ipp.UNSUPPORTED_GROUP, attributes)]

    def _op_0004(self, _body, _ops, _job):  # Validate-Job
        if self.validate_status is not None:
            return _answer(self.validate_status, [(ipp.OPERATION_GROUP, []), *self._unsupported()])
        return _answer(1 if self.ignored else 0, [(ipp.OPERATION_GROUP, []), *self._unsupported()])

    def _op_0002(self, body, ops, job):  # Print-Job
        status = self.print_errors.pop(0) if self.print_errors else 0
        if status:
            return _answer(status, [(ipp.OPERATION_GROUP, [])])
        mime = ops["document-format"][0]
        starts = {"application/pdf": b"%PDF", "image/pwg-raster": pwg.SYNC, "image/urf": urf.SYNC}
        document = body[body.find(starts.get(mime, b"\xff\xd8")) :]
        self.documents.append((mime, document))
        copies = 1 if "copies" in self.ignored else job.get("copies", [1])[0]
        pages = document.count(b"PwgRaster")
        if mime == "image/urf":
            pages = struct.unpack_from(">I", document, len(urf.SYNC))[0]
        self.printed += copies * max(1, pages)
        self.job_id += 1
        groups = [(ipp.JOB_GROUP, [ipp.Attribute(ipp.INTEGER, "job-id", [self.job_id])])]
        groups += self._unsupported()
        self.state = self.later.get("state", self.state)
        self.reasons = self.later.get("reasons", self.reasons)
        return _answer(1 if self.ignored else 0, groups)

    def _op_0009(self, _body, ops, _job):  # Get-Job-Attributes
        assert ops["job-id"] == [self.job_id]
        if self.job_errors:
            error = self.job_errors.pop(0)
            if isinstance(error, Exception):
                raise error
            return _answer(error, [(ipp.OPERATION_GROUP, [])])
        state = self.job_states.pop(0) if len(self.job_states) > 1 else self.job_states[0]
        state, done = state if isinstance(state, tuple) else (state, 1)
        attrs = [ipp.Attribute(ipp.KEYWORD, "job-state-reasons", self.job_reasons)]
        if state is not None:
            attrs.append(ipp.Attribute(ipp.ENUM, "job-state", [state]))
        if done is not None:
            attrs.append(ipp.Attribute(ipp.INTEGER, "job-impressions-completed", [done]))
        return _answer(0, [(ipp.JOB_GROUP, attrs)])

    def _op_0008(self, _body, ops, _job):  # Cancel-Job
        self.canceled += ops["job-id"]
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
    monkeypatch.setattr(system, "_clock", lambda: printer.now)

    def sleep(seconds):
        printer.now += seconds

    monkeypatch.setattr(system, "_sleep", sleep)
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
        ("2001:db8::5/ipp/print", "ipp://[2001:db8::5]/ipp/print"),
        ("fe80::1%eth0/ipp/print2", "ipp://[fe80::1%eth0]/ipp/print2"),
        ("[2001:db8::5]:631/ipp/print", "ipp://[2001:db8::5]:631/ipp/print"),
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
        (["toner-low", "media-low", "marker-supply-low"], None),  # low, even without suffix
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
    assert system.match_size([], 5000, 3000) == (5000, 3000)
    assert system.match_size(["not a size"], 5000, 3000) == (5000, 3000)
    assert system.match_size([{"x-dimension": ["x"], "y-dimension": [1]}], 5000, 3000) is None
    assert system.match_size([{"x-dimension": 5000, "y-dimension": [3000]}], 5000, 3000) is None


def test_a_size_within_a_millimetre_is_sent_as_the_listed_size(fake):
    letter = {"x-dimension": [21590], "y-dimension": [27940]}
    fake.sizes = [A4, letter]
    _print(_image(size_mm=(216.5, 279)))  # 0.6 and 0.4 mm off US Letter
    assert fake.job()["media-col"][0]["media-size"][0] == {
        "x-dimension": [21590],
        "y-dimension": [27940],
    }


def test_the_nearest_listed_size_wins():
    sizes = [
        {"x-dimension": [10000], "y-dimension": [15000]},
        {"x-dimension": [10080], "y-dimension": [15000]},
        {"x-dimension": [(7620, 9990)], "y-dimension": [(12700, 35560)]},  # a custom range
    ]
    assert system.match_size(sizes, 10060, 15000) == (10080, 15000)
    assert system.match_size(sizes, 9995, 15030) == (9990, 15030)  # the range's edge
    assert system.match_size(sizes, 8000, 20000) == (8000, 20000)  # inside the range


@pytest.mark.parametrize(
    "formats, mime",
    [
        (["application/pdf", "image/pwg-raster"], "application/pdf"),
        ([], "application/pdf"),  # not said: CUPS takes PDF
        (["image/urf", "image/pwg-raster", "image/jpeg"], "image/pwg-raster"),
        (["image/jpeg", "image/urf"], "image/urf"),
        (["image/jpeg"], "image/jpeg"),
    ],
)
def test_document_format(formats, mime):
    attrs = {"document-format-supported": formats}
    _data, chosen = system.document(_image(), attrs)
    assert chosen == mime


def test_a_raster_format_in_colour_spaces_etiketto_cannot_make_is_passed_over():
    attrs = {
        "document-format-supported": ["image/pwg-raster", "image/urf", "image/jpeg"],
        "pwg-raster-document-type-supported": ["black_1"],
        "urf-supported": ["V1.4", "DEVCMYK32"],
    }
    assert system.document_format(attrs) == "image/jpeg"
    attrs["urf-supported"] = ["V1.4", "W8", "RS600"]
    assert system.document_format(attrs) == "image/urf"


def test_no_format_the_printer_takes(fake):
    fake.formats = ["image/urf", "image/pwg-raster", "application/vnd.hp-PCL"]
    fake.urf, fake.pwg_types = ["V1.4", "DEVCMYK32", "RS600"], ["black_1"]  # no W8, SRGB24
    with pytest.raises(NoFormat, match="black_1; urf-supported: V1.4, DEVCMYK32"):
        _print()
    assert ipp.PRINT_JOB not in fake.operations()


HP_URF = (  # an HP LaserJet Pro 4002
    "CP1,MT1-2-8-9-10-11,PQ3-4-5,RS600,SRGB24,OB10,W8,DEVW8,DEVRGB24,"
    "ADOBERGB24,DM3,IS1-1-2,V1.5,FN3"
)


@pytest.mark.parametrize(
    "keywords, mode, dpis, copies",
    [
        ([], "L", [], False),  # not said: W8, which every printer has
        (["V1.4", "RS600", "CP1"], "L", [600], True),  # no colour space: W8 too
        (HP_URF.split(","), "RGB", [600], True),
        ([HP_URF], "RGB", [600], True),  # one comma-joined value
        (["W8", "SRGB24", "RS300-600", "DM1", "IS1-4-5-19", "MT1-2-5"], "RGB", [300, 600], False),
        (["w8", " RS203 ", "RSx", "RS6e2", "RS\u0666\u0660\u0660", "RS-"], "L", [203], False),
        (["W8", "CP0", "CP", "CPx", "CP-1", "CP\u0661"], "L", [], False),  # CP strictly
        (["V1.5", "DEVW8"], None, [], False),
    ],
)
def test_urf_supported(keywords, mode, dpis, copies):
    attrs = {"urf-supported": keywords}
    assert system._urf_mode(attrs) == mode
    assert system._urf_resolutions(attrs) == dpis
    assert system._urf_copies(attrs) is copies


@pytest.mark.parametrize(
    "dpis, dpi",
    [
        ([], 300),
        ([600], 600),
        ([300, 600], 300),
        ([203], 203),
        ([150, 360, 720], 360),
        ([10, 50], 300),  # not believed
        ([600, 2400, 100000], 600),
        ([100, 1200], 1200),
    ],
)
def test_resolution_within_reason(dpis, dpi):
    assert system._resolution(dpis) == dpi


def test_apple_raster_in_the_printers_colour_space_and_resolution(fake):
    fake.formats, fake.sizes = ["image/urf", "image/jpeg"], []
    fake.urf = ["CP1", "IS1-4-5-19", "MT1-2-3-4-5-6", "RS600", "V1.4", "W8"]  # ippeveprinter
    image = _image(118, 59, size_mm=(10, 5))
    image.paste((0, 0, 0), (10, 0, 11, 59))  # a bar one pixel wide
    _print(image)
    mime, data = fake.documents[0]
    (page,) = urf.decode(data)
    assert mime == "image/urf" and page.mode == "L" and page.size == (236, 118)
    assert page.info["dpi"] == (600, 600)
    # each pixel twice, not blurred: the bar is two pixels, black, with hard edges
    assert set(page.tobytes()) == {0, 255}
    assert [page.getpixel((x, 5)) for x in range(19, 24)] == [255, 0, 0, 255, 255]


@pytest.mark.parametrize("dpi", [203, 600])
def test_a_label_is_rendered_at_the_apple_raster_resolution(fake, dpi):
    fake.formats, fake.sizes, fake.urf = ["image/urf"], [], ["W8", "RS%d" % dpi]
    backend = get_backend("system")
    backend.print_label(
        backend.prepare(_shipping_label(), PrintOptions()), "x.local", PrintOptions()
    )
    (page,) = urf.decode(fake.documents[0][1])
    assert page.size == (px(101.6, dpi), px(152.4, dpi))  # rendered again, not resized
    assert _greys(page) == 0


IMAGE_RE = re.compile(
    rb"/Width (\d+) /Height (\d+) /ColorSpace /(\w+) /BitsPerComponent (\d+) "
    rb"/Filter /FlateDecode /Length (\d+) >>\nstream\n"
)


def pdf_images(data: bytes) -> list[tuple[Image.Image, str, int]]:
    """The images in a PDF from pdf.encode(): image, colour space, bits."""
    found = []
    for match in IMAGE_RE.finditer(data):
        width, height, space, bits, length = match.groups()
        raw = zlib.decompress(data[match.end() : match.end() + int(length)])
        mode = {(b"DeviceGray", b"1"): "1", (b"DeviceGray", b"8"): "L"}.get((space, bits), "RGB")
        size = (int(width), int(height))
        found.append((Image.frombytes(mode, size, raw), space.decode(), int(bits)))
    return found


def test_pdf_keeps_small_text_sharp():
    image = Image.new("RGB", (401, 100), "white")
    for x in range(0, 401, 2):  # one-pixel stripes: the worst case for JPEG
        image.paste((0, 0, 0), (x, 0, x + 1, 100))
    data, _mime = system.document(image, {"document-format-supported": ["application/pdf"]})
    [(embedded, space, bits)] = pdf_images(data)
    assert (space, bits) == ("DeviceGray", 1)  # black and white only: 1 bit per pixel
    assert embedded.convert("RGB").tobytes() == image.tobytes()  # every pixel as it was
    assert len(data) < 2000  # and small


@pytest.mark.parametrize(
    "colour, space, bits",
    [((0, 0, 0), "DeviceGray", 1), ((90, 90, 90), "DeviceGray", 8), ((200, 0, 0), "DeviceRGB", 8)],
)
def test_pdf_pages_in_the_smallest_lossless_form(colour, space, bits):
    image = Image.new("RGB", (37, 20), "white")
    image.paste(colour, (3, 4, 30, 15))
    [(embedded, found_space, found_bits)] = pdf_images(pdf.encode([image], 300))
    assert (found_space, found_bits) == (space, bits)
    assert embedded.convert("RGB").tobytes() == image.tobytes()


def test_pdf_of_an_a4_label_is_small():
    page = Image.new("RGB", (2480, 3508), "white")  # A4 at 300 dpi
    page.paste((0, 0, 0), (300, 300, 2000, 900))
    page.info["size_mm"] = (210, 297)
    assert len(pdf.encode([page], 300)) < 20_000  # a JPEG of it is about 300 kB


def test_pwg_raster_in_the_printers_colour_space_and_resolution(fake):
    fake.formats, fake.sizes = ["image/pwg-raster"], []  # sizes: not said
    fake.pwg_types, fake.pwg_resolutions = ["sgray_8"], [(600, 600, 3)]
    _print(_image(118, 59, size_mm=(10, 5)))
    mime, data = fake.documents[0]
    page = pwg.decode(data)
    assert mime == "image/pwg-raster" and page.mode == "L" and page.size == (236, 118)
    assert struct.unpack_from(">II", data, 4 + 276) == (600, 600)  # HWResolution


def _shipping_label() -> Label:
    """A 4 × 6 inch label of pure black and white: a QR code, no text."""
    label = Label(printer="system", margin_mm=3, elements=[CodeElement("1Z999AA10123456784")])
    label.auto_length, label.orientation = False, "along"
    label.set_media_size(101.6, 152.4)
    return label


def _greys(image: Image.Image) -> int:
    """Pixels that are neither black nor white."""
    return sum(image.convert("L").histogram()[1:255])


def test_pwg_raster_at_a_whole_multiple_doubles_pixels(fake):
    fake.formats, fake.sizes, fake.pwg_resolutions = ["image/pwg-raster"], [], [(600, 600, 3)]
    image = _image(119, 59, size_mm=(10.075, 4.995))
    for x in range(0, 119, 2):  # one-pixel stripes
        image.paste((0, 0, 0), (x, 0, x + 1, 59))
    _print(image)
    page = pwg.decode(fake.documents[0][1])
    assert page.size == (238, 118) and _greys(page) == 0  # no grey edges
    assert page.tobytes() == image.resize((238, 118), Image.Resampling.NEAREST).tobytes()


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_a_label_is_rendered_at_the_printers_resolution(fake, dpi):
    fake.formats, fake.sizes, fake.pwg_resolutions = ["image/pwg-raster"], [], [(dpi, dpi, 3)]
    backend = get_backend("system")
    image = backend.prepare(_shipping_label(), PrintOptions())
    assert _greys(image) == 0
    backend.print_label(image, "HP_Office", PrintOptions())
    page = pwg.decode(fake.documents[0][1])
    assert page.size == (px(101.6, dpi), px(152.4, dpi))  # rendered again, not resized
    assert _greys(page) == 0


def test_label_sheet_pages_are_composed_at_the_printers_resolution(fake):
    fake.formats, fake.pwg_resolutions = ["image/pwg-raster"], [(203, 203, 3)]
    backend = get_backend("system")
    sheet = find("Avery L7160")
    label = _shipping_label()
    label.orientation = "across"  # the QR code fits on the label unscaled
    label.set_media_size(sheet.width, sheet.height)
    image = backend.prepare(label, PrintOptions())
    pages = Pages(SheetJob(sheet, 30), Copies(image), backend.capabilities.dpi)
    backend.print_pages(pages, "HP_Office", PrintOptions())
    found = pwg.decode_all(fake.documents[0][1])
    assert [p.size for p in found] == [(px(210, 203), px(297, 203))] * 2
    assert all(_greys(page) == 0 for page in found)
    assert Pages(SheetJob(sheet, 1), lambda n: image, 300).at_dpi(203) is None  # cannot


def _pdf_image(data: bytes) -> tuple[int, int, int]:
    """Width, height and bits per component of the first page image."""
    found = re.search(rb"/Width (\d+) /Height (\d+) .*?/BitsPerComponent (\d+)", data)
    assert found
    return int(found[1]), int(found[2]), int(found[3])


@pytest.mark.parametrize(
    "resolution, dpi",
    [
        (None, 300),
        ((300, 300, 3), 300),
        ((203, 203, 3), 203),  # a thermal printer: its own dots
        ((80, 80, 4), 203),  # 8 dots per mm
        ((150, 150, 3), 150),
        ((400, 400, 3), 400),
        ((600, 600, 3), 300),  # every pixel doubled exactly
        ((1200, 1200, 3), 300),
        ((720, 720, 3), 300),  # above 600: 300 dpi is fine enough
        ((72, 72, 3), 300),  # below MIN_DPI: not believed
        ((203, 406, 3), 300),  # not square
        ("x", 300),
    ],
)
def test_pdf_resolution(resolution, dpi):
    attrs = {} if resolution is None else {"printer-resolution-default": [resolution]}
    assert system.pdf_resolution(attrs) == dpi


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_a_pdf_label_is_rendered_at_the_printers_resolution(fake, dpi):
    fake.formats, fake.sizes, fake.resolution = ["application/pdf"], [], (dpi, dpi, 3)
    backend = get_backend("system")
    backend.print_label(
        backend.prepare(_shipping_label(), PrintOptions()), "HP_Office", PrintOptions()
    )
    data = fake.documents[0][1]
    at = 203 if dpi == 203 else 300
    assert _pdf_image(data) == (px(101.6, at), px(152.4, at), 1)  # 1 bit: no grey
    assert b"/MediaBox [0 0 288.000 432.000]" in data  # 4 x 6 inches, whatever the dpi


def test_pdf_label_sheet_pages_at_the_printers_resolution(fake):
    fake.formats, fake.resolution = ["application/pdf"], (203, 203, 3)
    backend = get_backend("system")
    sheet = find("Avery L7160")
    label = _shipping_label()
    label.orientation = "across"
    label.set_media_size(sheet.width, sheet.height)
    image = backend.prepare(label, PrintOptions())
    pages = Pages(SheetJob(sheet, 30), Copies(image), backend.capabilities.dpi)
    backend.print_pages(pages, "HP_Office", PrintOptions())
    assert _pdf_image(fake.documents[0][1])[:2] == (px(210, 203), px(297, 203))


def test_a_pdf_page_that_cannot_be_rendered_again_stays_at_300_dpi(fake):
    fake.formats, fake.sizes, fake.resolution = ["application/pdf"], [], (203, 203, 3)
    _print(_image(118, 59))  # no size in mm, nothing to render it again with
    data = fake.documents[0][1]
    assert _pdf_image(data)[:2] == (118, 59)
    assert b"/MediaBox [0 0 28.320 14.160]" in data  # its pixels at 300 dpi


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
    attrs = {"pwg-raster-document-resolution-supported": resolutions}
    assert system._resolution(system._pwg_resolutions(attrs)) == dpi


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


@pytest.mark.parametrize("host, cls", [("HP_Office", QueuePaused), ("hp.local", PrinterPaused)])
def test_a_stopped_printer_gets_no_job(fake, host, cls):
    fake.state, fake.reasons = system.STOPPED, ["none"]
    with pytest.raises(cls, match="printer-state: stopped"):
        _print(host=host)
    assert ipp.PRINT_JOB not in fake.operations()


# --- copies ----------------------------------------------------------------------------------


def test_copies_the_printer_cannot_make_are_separate_jobs(fake):
    fake.copies_supported = (1, 1)
    events = []
    _print(copies=3, events=events)
    jobs = [r[3] for r in fake.requests if r[0] == ipp.PRINT_JOB]
    assert len(jobs) == 3 and all("copies" not in job for job in jobs)
    assert fake.printed == 3
    copies = [e.copy for e in events]
    assert copies == sorted(copies) and copies[-1] == 3 and 2 in copies


def test_copies_in_pwg_raster_are_pages(fake):
    fake.copies_supported, fake.formats, fake.sizes = (1, 1), ["image/pwg-raster"], []
    _print(_image(118, 59, size_mm=(10, 5)), copies=3)
    ((mime, data),) = fake.documents
    assert mime == "image/pwg-raster" and "copies" not in fake.job()
    assert data.count(b"PwgRaster") == 3 and fake.printed == 3
    assert struct.unpack_from(">I", data, 4 + 452)[0] == 3  # TotalPageCount


def test_a_printer_that_does_not_say_how_many_copies(fake):
    fake.copies_supported = None
    _print(copies=5)
    assert fake.job()["copies"] == [5] and fake.operations().count(ipp.PRINT_JOB) == 1


@pytest.mark.parametrize(
    "supported, takes", [((1, 99), True), ((1, 4), False), (9, True), (2, False), ("x", True)]
)
def test_takes_copies(supported, takes):
    assert system.takes_copies({"copies-supported": [supported]}, 5) is takes


def test_ignored_copies_are_printed_one_by_one(fake):
    fake.operations_supported = [0x0002, 0x0008, 0x0009, 0x000B]  # no Validate-Job
    fake.ignored = ["copies"]  # accepted, but it prints one
    _print(copies=3)
    assert fake.operations().count(ipp.PRINT_JOB) == 3
    assert fake.job()["copies"] == [3] and fake.printed == 3


def test_ignored_paper_size_is_an_error(fake):
    fake.operations_supported = [0x0002, 0x0008, 0x0009, 0x000B]  # no Validate-Job
    fake.ignored = ["media-col"]  # it would print on its default paper
    with pytest.raises(UnsupportedSize, match="media-col ignored"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB and fake.canceled == [12]


@pytest.mark.parametrize("status", [None, 0x040B])
def test_validate_job_finds_an_ignored_paper_size_before_printing(fake, status):
    fake.ignored, fake.validate_status = ["media-col"], status
    with pytest.raises(UnsupportedSize, match="Validate-Job"):
        _print()
    assert ipp.PRINT_JOB not in fake.operations() and not fake.canceled


def test_validate_job_finds_ignored_copies_before_printing(fake):
    fake.ignored = ["copies"]
    _print(copies=3)
    ops = fake.operations()
    assert ops.index(ipp.VALIDATE_JOB) < ops.index(ipp.PRINT_JOB)
    jobs = [r[3] for r in fake.requests if r[0] == ipp.PRINT_JOB]
    assert len(jobs) == 3 and all("copies" not in job for job in jobs)
    validate = next(r for r in fake.requests if r[0] == ipp.VALIDATE_JOB)
    assert validate[3]["copies"] == [3] and "media-col" in validate[3]
    assert validate[2]["document-format"] == ["application/pdf"]
    assert fake.printed == 3


def test_a_printer_that_cannot_validate_the_job_still_prints(fake):
    fake.validate_status = 0x0501  # server-error-operation-not-supported
    _print()
    assert fake.printed == 1 and ipp.VALIDATE_JOB in fake.operations()


def test_no_validate_job_for_a_printer_without_it(fake):
    fake.operations_supported = [0x0002, 0x0008, 0x0009, 0x000B]
    _print()
    assert ipp.VALIDATE_JOB not in fake.operations() and fake.printed == 1


def test_copies_after_a_queued_one_are_sent_without_waiting(fake):
    fake.copies_supported, fake.job_states = (1, 1), [3]
    result = get_backend("system").print_label(_image(), "HP_Office", PrintOptions(copies=3))
    assert result == PrintResult(queued=True) and fake.printed == 3
    ops = fake.operations()
    last_poll = len(ops) - 1 - ops[::-1].index(ipp.GET_JOB_ATTRIBUTES)
    assert last_poll < ops.index(ipp.PRINT_JOB, ops.index(ipp.PRINT_JOB) + 1)


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


def test_a_stopped_job_is_canceled_with_the_printers_reason(fake):  # one nobody fixes
    fake.job_states = [5, 6]  # processing, then processing-stopped
    fake.reasons = ["none"]
    original = FakeIpp._op_000b
    calls = []

    def attributes(self, body, ops, job):
        calls.append(1)
        self.reasons = ["none"] if len(calls) == 1 else ["marker-supply-empty-error"]
        return original(self, body, ops, job)

    FakeIpp._op_000b, saved = attributes, original
    try:
        with pytest.raises(SuppliesEmpty):
            _print()
    finally:
        FakeIpp._op_000b = saved
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_stopped_job_without_a_reason(fake):
    fake.job_states = [6]
    with pytest.raises(PrinterError, match="stopped"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == system.PROBLEM_POLLS


def test_a_stopped_job_is_canceled_even_if_the_printer_does_not_answer(fake, monkeypatch):
    original = FakeIpp._op_000b

    def gone(self, body, ops, job):
        if ipp.PRINT_JOB in self.operations():
            return _answer(0x0500, [])
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", gone)
    fake.job_states = [6]
    with pytest.raises(PrinterError, match="job 12 stopped"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_job_without_progress_times_out_and_is_canceled(fake, monkeypatch):
    monkeypatch.setattr(system, "PRINT_TIMEOUT", 10)
    fake.job_states = [5]  # processing, the same page for ever
    with pytest.raises(PrinterTimeout, match="no progress for 10 s"):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.now == 11


def test_a_long_job_that_makes_progress_does_not_time_out(fake, monkeypatch):
    monkeypatch.setattr(system, "PRINT_TIMEOUT", 10)
    # a page every 8 s: longer than the timeout in all, never without progress
    fake.job_states = [(5, page) for page in range(4) for _second in range(8)] + [9]
    events = []
    _print(copies=4, events=events)
    assert ipp.CANCEL_JOB not in fake.operations() and fake.now > 30
    assert [e.copy for e in events][-1] == 4


def test_a_job_waiting_behind_others_is_left_to_print(fake):
    fake.job_states = [3]  # pending, for longer than Etiketto waits
    result = get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    assert result == PrintResult(queued=True)
    assert ipp.CANCEL_JOB not in fake.operations()
    assert fake.now == pytest.approx(system.PENDING_LIMIT + 1)


def test_a_job_that_is_done_says_nothing_more(fake):
    assert get_backend("system").print_label(_image(), "HP_Office", PrintOptions()) is None


@pytest.mark.parametrize(
    "reasons", [["job-hold-until-specified"], ["cups-held-for-authentication"]]
)
def test_a_held_job_is_canceled_with_its_reason(fake, reasons):
    fake.job_states, fake.job_reasons = [4], reasons
    with pytest.raises(JobHeld, match=reasons[0]) as info:
        _print()
    assert info.value.code == "job_held"
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == system.PROBLEM_POLLS


def test_a_job_held_for_a_moment_prints(fake):
    fake.job_states = [4, 3, 5, 9]  # held while it arrives, then on its way
    fake.job_reasons = ["job-incoming"]
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


@pytest.mark.parametrize("host, cls", [("HP_Office", QueuePaused), ("hp.local", PrinterPaused)])
def test_a_job_pending_in_a_stopped_queue_is_canceled(fake, host, cls):
    fake.job_states = [3]
    fake.later = {"state": system.STOPPED, "reasons": ["none"]}  # stopped after sending
    with pytest.raises(cls):
        _print(host=host)
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.now < system.PENDING_LIMIT


def test_a_job_that_says_the_printer_stopped(fake):
    fake.job_states, fake.job_reasons = [3], ["printer-stopped"]
    fake.later = {"reasons": ["paused"]}
    with pytest.raises(QueuePaused):
        _print()


def test_a_known_problem_while_printing_cancels_the_job(fake):
    fake.job_states = [5]
    fake.later = {"reasons": ["toner-empty-error", "media-low"]}
    with pytest.raises(SuppliesEmpty):
        _print()
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == system.PROBLEM_POLLS


RECOVERABLE = [
    (["media-empty-error"], OutOfPaper, "OUT_OF_PAPER"),
    (["media-needed"], OutOfPaper, "OUT_OF_PAPER"),
    (["media-jam"], MediaJam, "MEDIA_JAM"),
    (["cover-open-error", "toner-low"], CoverOpen, "COVER_OPEN"),
    (["door-open"], CoverOpen, "COVER_OPEN"),
]


@pytest.mark.parametrize("reasons, cls, stage", RECOVERABLE)
def test_a_problem_the_user_can_fix_is_shown_and_waited_for(fake, reasons, cls, stage):
    fake.job_states = [5] * 60 + [(5, 2), 9]  # a minute without paper, then it goes on
    fake.later = {"reasons": reasons}
    original = FakeIpp._op_0009

    def fixed(self, body, ops, job):
        if self.now >= 60:
            self.reasons = ["none"]
        return original(self, body, ops, job)

    FakeIpp._op_0009, saved = fixed, original
    events = []
    try:
        _print(copies=2, events=events)
    finally:
        FakeIpp._op_0009 = saved
    assert ipp.CANCEL_JOB not in fake.operations()
    stages = [e.stage for e in events]
    assert stage in stages and stages[-1] == ""
    assert fake.now > system.PRINT_TIMEOUT / 5


@pytest.mark.parametrize("reasons, cls, stage", RECOVERABLE)
def test_a_problem_not_fixed_in_time_cancels_the_job(fake, reasons, cls, stage):
    fake.job_states = [5]
    fake.later = {"reasons": reasons}
    events = []
    with pytest.raises(cls, match="not fixed within"):
        _print(events=events)
    assert fake.operations()[-1] == ipp.CANCEL_JOB
    assert fake.now == pytest.approx(system.RECOVERY_LIMIT + 1)
    assert events[-1].stage == stage


def test_a_new_problem_gets_its_own_time(fake, monkeypatch):
    monkeypatch.setattr(system, "RECOVERY_LIMIT", 10)
    fake.job_states = [5] * 30 + [9]
    original = FakeIpp._op_000b
    calls = []

    def changing(self, body, ops, job):  # 8 s out of paper, 8 s cover open, 8 s paper again
        calls.append(1)
        if len(calls) > 1:
            self.reasons = [["media-empty"], ["cover-open"]][(len(calls) - 2) // 8 % 2]
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", changing)
    _print()
    assert ipp.CANCEL_JOB not in fake.operations() and fake.now > 20


@pytest.mark.parametrize("reasons, cls, stage", RECOVERABLE)
def test_a_job_the_printer_stopped_for_a_fixable_problem_waits(fake, reasons, cls, stage):
    fake.job_states = [5] + [6] * 20 + [5, 9]  # stopped for 20 s, then on again
    fake.later = {"reasons": reasons}
    events = []
    _print(events=events)
    assert ipp.CANCEL_JOB not in fake.operations()
    assert stage in [e.stage for e in events]


def test_a_short_or_unknown_problem_while_printing_is_not_one(fake):
    fake.job_states = [5, 5, 5, 5, 9]
    fake.later = {"reasons": ["vendor-thing-error", "toner-low", "media-empty-warning"]}
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


def test_a_problem_must_last(fake, monkeypatch):
    original = FakeIpp._op_000b
    calls = []

    def flicker(self, body, ops, job):  # every other answer: out of paper
        calls.append(1)
        self.reasons = ["none"] if len(calls) % 2 else ["media-empty-error"]
        if len(calls) == 1:
            self.reasons = ["none"]  # the check before sending
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", flicker)
    fake.job_states = [5] * 8 + [9]
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


def test_the_printer_not_answering_while_the_job_prints(fake, monkeypatch):
    original = FakeIpp._op_000b

    def gone(self, body, ops, job):
        if ipp.PRINT_JOB in self.operations():
            raise ConnectionResetError("reset")
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", gone)
    _print()  # the job's own state decides
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
    events = []
    _print(events=events)
    assert ipp.CANCEL_JOB not in fake.operations()
    assert events[-1].copy == 1


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        OSError("cannot resolve hp.local"),
        ConnectionResetError("reset"),
    ],
)
def test_a_lost_connection_is_not_a_finished_job(fake, error):
    # all of these map to PrinterNotFound, but the printer did not say so
    assert type(system.map_error(error)) is PrinterNotFound
    fake.job_errors = [error] * (system.POLL_FAILURES + 1)
    with pytest.raises(PrinterNotFound):
        _print()
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == system.POLL_FAILURES + 1


def test_a_lost_connection_for_a_moment_is_tolerated(fake):
    fake.job_errors = [ConnectionRefusedError("refused")]
    fake.job_states = [5, 9]
    events = []
    _print(events=events)
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == 3


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


# --- several jobs, partly printed ---------------------------------------------------------


def test_a_failed_copy_cancels_the_queued_jobs_sent_before(fake):
    fake.copies_supported, fake.job_states = (1, 1), [(3, 0)]  # one job per copy, queued
    fake.print_errors = [0, 0, 0x0500]  # the third Print-Job is refused
    with pytest.raises(PrinterError, match="0x0500") as info:
        _print(copies=3)
    assert fake.canceled == [12, 13]  # nothing of them printed yet: they never will
    assert "printed" not in info.value.params


def test_a_failed_copy_says_how_many_were_printed(fake):
    fake.copies_supported = (1, 1)
    fake.job_states = [5, 9, 9, 8]  # two copies printed, the third aborted
    with pytest.raises(PrinterError, match="aborted") as info:
        _print(copies=3)
    assert info.value.params == {"printed": 2, "copies": 3}
    assert 12 not in fake.canceled and 13 not in fake.canceled


def test_pages_finished_of_a_failed_job_count_as_printed(fake):
    fake.copies_supported = (1, 1)
    fake.job_states = [(5, 0), (5, 1), (5, 2), 6]  # stopped on page 3 of 5
    fake.formats, fake.sizes = ["image/pwg-raster"], []
    fake.later = {"reasons": ["vendor-thing-error"]}  # only a stopped job shows it
    with pytest.raises(PrinterError, match="vendor-thing") as info:
        _print(_image(118, 59, size_mm=(10, 5)), copies=5)
    assert info.value.params == {"printed": 2, "copies": 5}
    assert fake.canceled == [12]  # by the watch, not once more


def test_a_job_that_fails_before_a_page_is_done_says_nothing_about_copies(fake):
    fake.job_states = [8]
    with pytest.raises(PrinterError) as info:
        _print()
    assert info.value.params == {}


def test_canceled_jobs(fake):
    fake.job_states = [7]
    with pytest.raises(JobCanceled, match="canceled") as info:
        _print()
    assert info.value.code == "job_canceled"
    fake.job_states, fake.job_reasons = [8], ["job-canceled-at-device"]
    with pytest.raises(JobCanceled, match="aborted"):
        _print()
    fake.job_states, fake.job_reasons = [8], ["aborted-by-system"]
    with pytest.raises(PrinterError, match="aborted-by-system") as aborted:
        _print()
    assert type(aborted.value) is PrinterError


# --- page counters and time limits ----------------------------------------------------------


@pytest.mark.parametrize("done", [None, 0])  # no counter, or one that stays 0 (ippeveprinter)
def test_a_printer_that_counts_no_pages_gets_time_per_label(fake, monkeypatch, done):
    monkeypatch.setattr(system, "PRINT_TIMEOUT", 10)
    monkeypatch.setattr(system, "TIME_PER_LABEL", 2)
    fake.job_states = [(5, done)]
    with pytest.raises(PrinterTimeout, match="no progress for 16 s"):
        _print(copies=3)
    assert fake.now == 17 and fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_long_job_without_page_counter_is_not_cut_short(fake, monkeypatch):
    monkeypatch.setattr(system, "PRINT_TIMEOUT", 10)
    monkeypatch.setattr(system, "TIME_PER_LABEL", 2)
    fake.job_states = [(5, 0)] * 15 + [9]  # 15 s for 3 labels: below 10 + 3 × 2
    _print(copies=3)
    assert ipp.CANCEL_JOB not in fake.operations()


@pytest.mark.parametrize("state", [None, 0])
def test_an_answer_without_a_job_state_is_a_missed_one(fake, state):
    fake.job_states = [(state, 1), (state, 1), 5, 9]
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()
    assert fake.operations().count(ipp.GET_JOB_ATTRIBUTES) == 4
    fake.job_states = [(state, 1)]
    with pytest.raises(PrinterError, match="no job-state"):
        _print()


def test_pending_time_counts_from_when_the_job_became_pending(fake):
    # pending 20 s, printing a moment, then pending again (a printer that
    # took another job in between): 30 s from then on
    fake.job_states = [3] * 20 + [(5, 0)] + [3]
    result = get_backend("system").print_label(_image(), "HP_Office", PrintOptions())
    assert result == PrintResult(queued=True)
    assert fake.now == pytest.approx(21 + system.PENDING_LIMIT + 1)


# --- documents ----------------------------------------------------------------------------


def test_many_copies_as_pages_go_in_several_jobs(fake, monkeypatch):
    fake.copies_supported, fake.formats, fake.sizes = (1, 1), ["image/pwg-raster"], []
    made = []
    original = system.document_of

    def document_of(images, count, attrs, repeat=1, **kwargs):
        made.append(repeat)
        return original(images, count, attrs, repeat, **kwargs)

    monkeypatch.setattr(system, "document_of", document_of)
    events = []
    _print(_image(118, 59, size_mm=(10, 5)), copies=23, events=events)
    pages = [data.count(b"PwgRaster") for _mime, data in fake.documents]
    assert pages == [10, 10, 3] and fake.printed == 23
    assert made == [1]  # the page compressed once, for all jobs
    assert events[-1].copy == 23


def test_the_document_is_made_once_for_all_copies(fake, monkeypatch):
    fake.copies_supported = (1, 1)
    made = []
    original = system.document_of

    def document_of(images, count, attrs, repeat=1, **kwargs):
        made.append(repeat)
        return original(images, count, attrs, repeat, **kwargs)

    monkeypatch.setattr(system, "document_of", document_of)
    _print(copies=4)
    assert made == [1] and len(fake.documents) == 4
    assert len({data for _mime, data in fake.documents}) == 1


# --- waiting for fixes: the time limits hold ------------------------------------------------


def _printer_answers(monkeypatch, answer):
    """Get-Printer-Attributes after the first (the check before sending):
    answer(self, n) sets the printer's reasons, or returns an IPP status
    to fail with; n counts from 1."""
    original = FakeIpp._op_000b
    calls = []

    def attributes(self, body, ops, job):
        calls.append(1)
        if len(calls) > 1:
            status = answer(self, len(calls) - 1)
            if status:
                return _answer(status, [(ipp.OPERATION_GROUP, [])])
        return original(self, body, ops, job)

    monkeypatch.setattr(FakeIpp, "_op_000b", attributes)


def test_a_stopped_job_whose_reason_clears_a_moment_early_prints(fake, monkeypatch):
    # out of paper 20 s; paper loaded: the printer's reason goes one answer
    # before the job leaves processing-stopped
    fake.job_states = [5] + [6] * 21 + [5, 9]
    fake.later = {"reasons": ["media-empty-error"]}

    def answer(self, n):
        self.reasons = ["none"] if n >= 22 else self.reasons

    _printer_answers(monkeypatch, answer)
    _print()
    assert ipp.CANCEL_JOB not in fake.operations()


def test_a_missed_printer_answer_changes_nothing(fake, monkeypatch):
    fake.job_states = [5] + [6] * 20 + [5, 9]  # stopped for paper
    fake.later = {"reasons": ["media-empty-error"]}
    _printer_answers(monkeypatch, lambda self, n: 0x0507 if n == 7 else 0)
    events = []
    _print(events=events)
    assert ipp.CANCEL_JOB not in fake.operations()
    assert events[7].stage == "OUT_OF_PAPER"  # also while the printer did not answer


def test_a_printer_that_misses_answers_cannot_put_off_the_limit(fake, monkeypatch):
    fake.job_states = [5]
    fake.later = {"reasons": ["media-empty-error"]}
    _printer_answers(monkeypatch, lambda self, n: 0x0507 if n % 50 == 0 else 0)
    with pytest.raises(OutOfPaper, match="not fixed within 300 s"):
        _print()
    assert fake.now == pytest.approx(system.RECOVERY_LIMIT + 1)


def test_other_words_for_the_same_problem_keep_its_time(fake, monkeypatch):
    fake.job_states = [5]

    def answer(self, n):
        self.reasons = [["media-empty-error"], ["media-needed"]][n % 2]

    _printer_answers(monkeypatch, answer)
    with pytest.raises(OutOfPaper, match="not fixed within"):
        _print()
    assert fake.now == pytest.approx(system.RECOVERY_LIMIT + 1)


def test_changing_problems_have_a_limit_in_all(fake, monkeypatch):
    monkeypatch.setattr(system, "RECOVERY_LIMIT", 10)
    monkeypatch.setattr(system, "RECOVERY_TOTAL", 30)
    fake.job_states = [5]

    def answer(self, n):  # 8 s out of paper, 8 s cover open, and so on
        self.reasons = [["media-empty"], ["cover-open"]][(n - 1) // 8 % 2]

    _printer_answers(monkeypatch, answer)
    with pytest.raises(CoverOpen, match="not fixed within 30 s"):
        _print()
    assert fake.now == pytest.approx(31)
    assert fake.operations()[-1] == ipp.CANCEL_JOB


def test_a_problem_that_comes_and_goes_has_a_limit_in_all(fake, monkeypatch):
    monkeypatch.setattr(system, "RECOVERY_TOTAL", 30)
    fake.job_states = [5]

    def answer(self, n):  # every other answer: out of paper
        self.reasons = ["media-empty"] if n % 2 else ["none"]

    _printer_answers(monkeypatch, answer)
    with pytest.raises(OutOfPaper, match="not fixed within 30 s"):
        _print()
    assert fake.now < 2 * 30 + 3


def test_pages_coming_out_while_the_printer_reports_a_problem(fake, monkeypatch):
    # out of paper in another tray: the job still prints, a page every 40 s
    monkeypatch.setattr(system, "RECOVERY_LIMIT", 60)
    fake.job_states = [(5, second // 40) for second in range(400)] + [9]
    fake.later = {"reasons": ["media-empty-error"]}
    _print(copies=10)
    assert ipp.CANCEL_JOB not in fake.operations() and fake.now >= 399


def test_no_page_while_the_problem_lasts_cancels_the_job(fake, monkeypatch):
    monkeypatch.setattr(system, "RECOVERY_LIMIT", 60)
    fake.job_states = [(5, 0)] * 30 + [(5, 1)]  # a page after 30 s, then none
    fake.later = {"reasons": ["media-empty-error"]}
    with pytest.raises(OutOfPaper):
        _print(copies=2)
    assert fake.now == pytest.approx(30 + 60 + 1)


# --- canceling the rest -------------------------------------------------------------------


def test_canceling_stops_once_the_printer_cannot_be_reached(fake, monkeypatch):
    fake.copies_supported, fake.formats, fake.job_states = (1, 1), ["application/pdf"], [3]
    send = fake.send
    tries = []

    def gone(uri, body, timeout):  # the network goes after the fifth job
        operation = ipp.decode(body).status
        if operation == ipp.CANCEL_JOB:
            tries.append(1)
        if fake.operations().count(ipp.PRINT_JOB) >= 5 and operation != ipp.GET_JOB_ATTRIBUTES:
            raise TimeoutError("timed out")
        return send(uri, body, timeout)

    monkeypatch.setattr(ipp, "send", gone)
    with pytest.raises(PrinterTimeout) as info:
        _print(copies=20)
    assert len(tries) == 1
    assert "queue_canceled" not in info.value.params  # they could not be


@pytest.mark.parametrize("status", [0x0406, 0x0400])
def test_canceling_goes_on_after_a_printer_refuses_one(fake, status):
    fake.copies_supported, fake.formats, fake.job_states = (1, 1), ["application/pdf"], [(3, 0)]
    fake.print_errors = [0, 0, 0, 0x0500]
    original = FakeIpp._op_0008

    def cancel(self, body, ops, job):
        if ops["job-id"] == [12]:
            return _answer(status, [(ipp.OPERATION_GROUP, [])])
        return original(self, body, ops, job)

    FakeIpp._op_0008, saved = cancel, original
    try:
        with pytest.raises(PrinterError, match="0x0500"):
            _print(copies=4)
    finally:
        FakeIpp._op_0008 = saved
    assert fake.canceled == [13, 14]


@pytest.mark.parametrize("state", [7, 8])
def test_a_job_that_ended_is_not_canceled(fake, state):
    fake.copies_supported, fake.formats = (1, 1), ["application/pdf"]
    fake.job_states = [5, 9, 5, state]
    with pytest.raises(PrinterError):
        _print(copies=3)
    assert ipp.CANCEL_JOB not in fake.operations()


def test_a_failure_after_queued_jobs_says_they_were_canceled(fake):
    fake.copies_supported, fake.formats, fake.job_states = (1, 1), ["application/pdf"], [(3, 0)]
    fake.print_errors = [0, 0, 0x0500]
    with pytest.raises(PrinterError) as info:
        _print(copies=3)
    assert fake.canceled == [12, 13]
    assert info.value.params == {"queue_canceled": True}


# --- a busy printer -------------------------------------------------------------------------


def test_a_busy_printer_is_asked_again(fake):
    fake.copies_supported, fake.formats = (1, 1), ["application/pdf"]
    fake.job_states = [5, 9]
    fake.print_errors = [0, 0x0507, 0x0507, 0]  # busy twice before the second copy
    _print(copies=2)
    assert fake.printed == 2 and fake.operations().count(ipp.PRINT_JOB) == 4
    assert ipp.CANCEL_JOB not in fake.operations()


def test_a_printer_busy_for_too_long(fake):
    fake.copies_supported, fake.formats = (1, 1), ["application/pdf"]
    fake.job_states = [5, 9]
    fake.print_errors = [0, 0, *[0x0507] * 100]
    start = []
    original = FakeIpp._op_0002

    def print_job(self, body, ops, job):
        if len(fake.print_errors) == 100:
            start.append(self.now)
        return original(self, body, ops, job)

    FakeIpp._op_0002, saved = print_job, original
    try:
        with pytest.raises(PrinterBusy) as info:
            _print(copies=3)
    finally:
        FakeIpp._op_0002 = saved
    assert info.value.code == "busy"
    assert info.value.params == {"printed": 2, "copies": 3}
    assert fake.now - start[0] == pytest.approx(system.BUSY_LIMIT)
    assert fake.printed == 2 and ipp.CANCEL_JOB not in fake.operations()


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


# --- unprintable margins -----------------------------------------------------------------


def _media_col(width, height, top, right, bottom, left, key="iso_a4_210x297mm"):
    size = {
        "x-dimension": ipp.Attribute(ipp.INTEGER, "x-dimension", [width]),
        "y-dimension": ipp.Attribute(ipp.INTEGER, "y-dimension", [height]),
    }
    members = {"media-key": ipp.Attribute(ipp.KEYWORD, "media-key", [key])}
    members["media-size"] = ipp.Attribute(ipp.BEGIN_COLLECTION, "media-size", [size])
    for side, value in zip(system.MARGIN_SIDES, (top, right, bottom, left), strict=True):
        name = "media-%s-margin" % side
        members[name] = ipp.Attribute(ipp.INTEGER, name, [value])
    return members


def _margins_supported(values):
    return [
        ipp.Attribute(ipp.INTEGER, "media-%s-margin-supported" % side, values)
        for side in system.MARGIN_SIDES
    ]


def test_query_reports_the_unprintable_margins_per_paper(fake):
    """As ippeveprinter reports them: per paper in media-col-ready (loaded)
    and media-col-database, borderless entries, and for any paper."""
    fake.extra = [
        ipp.Attribute(
            ipp.BEGIN_COLLECTION,
            "media-col-ready",
            [_media_col(21590, 27940, 635, 635, 635, 635, "na_letter")],
        ),
        ipp.Attribute(
            ipp.BEGIN_COLLECTION,
            "media-col-database",
            [
                _media_col(21000, 29700, 0, 0, 0, 0, "borderless"),  # only when asked for
                _media_col(21000, 29700, 635, 340, 635, 340),
            ],
        ),
        *_margins_supported([0, 340, 635]),
    ]
    info = get_backend("system").query("HP_Office")
    ops = fake.requests[0][2]
    assert "media-col-database" in ops["requested-attributes"]
    assert info.margins == (
        Margins(6.35, 6.35, 6.35, 6.35, (215.9, 279.4)),
        Margins(6.35, 3.4, 6.35, 3.4, (210, 297)),
        Margins(3.4, 3.4, 3.4, 3.4),  # the smallest but borderless
    )
    assert margins_for(info.margins, (210, 297)) == info.margins[1]
    assert margins_for(info.margins, (215.9, 279.4)).top == 6.35
    assert margins_for(info.margins, (100, 150)) == info.margins[2]


def test_margins_the_printer_does_not_say_are_unknown(fake):
    assert get_backend("system").query("HP_Office").margins == ()
    assert margins_for((), (210, 297)) is None
    junk = {
        "media-col-ready": ["text", {"media-size": [{"x-dimension": [(1, 2)]}]}],
        "media-top-margin-supported": [True, -5, "x"],
    }
    assert system.margins(junk) == ()
    only_borderless = {"media-%s-margin-supported" % side: [0] for side in system.MARGIN_SIDES}
    assert system.margins(only_borderless) == (Margins(0, 0, 0, 0),)


# --- several pages: label sheets ------------------------------------------------------


def _pages(count, size_mm=(210, 297)):
    pages = []
    for n in range(count):
        size = (248, 351) if size_mm[0] < size_mm[1] else (351, 248)
        page = Image.new("RGB", size, (n * 40, 0, 0))  # 30 dpi: small and quick
        page.info["size_mm"] = size_mm
        pages.append(page)
    return pages


def test_pages_go_in_one_pdf_job(fake):
    events = []
    result = get_backend("system").print_pages(
        _pages(3), "HP_Office", PrintOptions(copies=40), events.append
    )
    assert result is None
    assert fake.operations().count(ipp.PRINT_JOB) == 1
    assert "copies" not in fake.job()  # every page once: the labels are on them
    mime, data = fake.documents[0]
    assert mime == "application/pdf" and data.count(b"/Type /Page ") == 3
    assert data.count(b"/MediaBox [0 0 595.276 841.890]") == 3  # exactly A4
    assert events[0] == Progress(1, 3) and events[-1] == Progress(3, 3)


def _noisy_pages(count, size=(248, 351)):
    """Colour noise: pages that compress badly, every one different."""
    import random

    rng = random.Random(7)
    pages = []
    for _n in range(count):
        page = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
        page.info["size_mm"] = (210, 297)
        pages.append(page)
    return pages


def test_large_pdf_jobs_are_split_by_bytes(fake, monkeypatch):
    monkeypatch.setattr(system, "MAX_JOB_BYTES", 1_000_000)  # a noisy page is about 261 kB
    events = []
    pages = _noisy_pages(9)
    get_backend("system").print_pages(pages, "HP_Office", PrintOptions(), events.append)
    counts = [data.count(b"/Type /Page ") for _mime, data in fake.documents]
    assert counts == [3, 3, 3]  # a fourth page would take a job over 1 MB
    assert all(len(data) < 1_000_000 for _mime, data in fake.documents)
    assert fake.operations().count(ipp.PRINT_JOB) == 3
    assert events[-1] == Progress(9, 9)


def test_pages_in_pwg_raster(fake):
    fake.formats = ["image/pwg-raster"]
    fake.pwg_resolutions = [(600, 600, 3)]
    get_backend("system").print_pages(_pages(2), "HP_Office", PrintOptions())
    pages = pwg.decode_all(fake.documents[0][1])
    assert [p.size for p in pages] == [(496, 702)] * 2  # at the printer's 600 dpi
    assert pages[1].getpixel((10, 10)) == (40, 0, 0)
    assert struct.unpack_from(">I", fake.documents[0][1], 4 + 452)[0] == 2  # TotalPageCount


def test_pages_as_jpeg_are_one_job_each(fake):
    fake.formats = ["image/jpeg"]
    events = []
    get_backend("system").print_pages(_pages(2), "HP_Office", PrintOptions(), events.append)
    assert [mime for mime, _data in fake.documents] == ["image/jpeg"] * 2
    assert events[-1] == Progress(2, 2)


def test_landscape_pages_are_turned_each(fake):
    fake.sizes, fake.formats = [A4], ["image/pwg-raster"]
    get_backend("system").print_pages(_pages(2, (297, 210)), "HP_Office", PrintOptions())
    assert all(p.width < p.height for p in pwg.decode_all(fake.documents[0][1]))
    assert fake.job()["media-col"][0]["media-size"][0]["x-dimension"] == [21000]


def test_pages_waiting_in_the_queue(fake):
    fake.job_states = [3]  # pending behind other jobs
    result = get_backend("system").print_pages(_pages(2), "HP_Office", PrintOptions())
    assert result == PrintResult(queued=True)


def test_a_sheet_from_the_composer_prints_page_by_page(fake):
    sheet = find("Avery L7160")
    label = Image.new("RGB", (px(sheet.width, 30), px(sheet.height, 30)), "black")
    pages = Pages(SheetJob(sheet, 30, outlines=True), lambda n: label, 30)
    get_backend("system").print_pages(pages, "HP_Office", PrintOptions())
    assert fake.documents[0][1].count(b"/Type /Page ") == 2


def test_many_pages_go_in_jobs_of_pages_per_job(fake):
    events = []
    get_backend("system").print_pages(_pages(23), "HP_Office", PrintOptions(), events.append)
    pages = [data.count(b"/Type /Page ") for _mime, data in fake.documents]
    assert pages == [10, 10, 3]  # a document is made in memory: never all of them at once
    assert fake.operations().count(ipp.VALIDATE_JOB) == 1
    assert "copies" not in fake.job()
    assert events[-1] == Progress(23, 23)


def test_pages_are_validated_before_anything_prints(fake):
    fake.ignored = ["media-col"]
    with pytest.raises(UnsupportedSize, match="Validate-Job"):
        get_backend("system").print_pages(_pages(2), "HP_Office", PrintOptions())
    assert ipp.PRINT_JOB not in fake.operations()


def test_a_failed_sheet_job_cancels_the_rest_and_counts_sheets(fake):
    fake.job_states = [(9, 10), (3, 0)]  # the first job done, the second waits in the queue
    fake.print_errors = [0, 0, 0x0500]  # the third is refused
    with pytest.raises(PrinterError, match="0x0500") as info:
        get_backend("system").print_pages(_pages(23), "HP_Office", PrintOptions())
    assert info.value.params == {
        "printed": 10,
        "copies": 23,
        "pages": True,
        "queue_canceled": True,
    }
    assert fake.canceled == [13]  # the one waiting in the queue: it must not print later


def test_queued_sheets_canceled_before_any_printed_are_sheets(fake):
    fake.job_states = [(3, 0)]  # the first job waits in the queue
    fake.print_errors = [0, 0x0500]  # the second is refused
    with pytest.raises(PrinterError, match="0x0500") as info:
        get_backend("system").print_pages(_pages(12), "HP_Office", PrintOptions())
    assert info.value.params == {"pages": True, "queue_canceled": True}
    assert fake.canceled == [12]


def test_a_busy_printer_gets_the_same_pages_again(fake):
    fake.print_errors = [system.BUSY, 0, 0]
    get_backend("system").print_pages(_pages(12), "HP_Office", PrintOptions())
    pages = [data.count(b"/Type /Page ") for _mime, data in fake.documents]
    assert pages == [10, 2]  # the first ten once, after the busy answer


def test_other_page_printers_print_page_by_page():
    class Pages1(FakeMono):
        pass

    backend = Pages1()
    events = []
    pages = _pages(2)
    backend.print_pages(pages, "mono.local", PrintOptions(copies=7), events.append)
    assert [job[2].copies for job in backend.jobs] == [1, 1]
    assert [job[0].getpixel((0, 0)) for job in backend.jobs] == [(0, 0, 0), (40, 0, 0)]
    assert events == [Progress(1, 2), Progress(2, 2)]


def test_pdf_is_well_formed():
    data = pdf.encode(_pages(2, (100, 150)), 30)
    assert data.startswith(b"%PDF-1.4") and data.endswith(b"%%EOF\n")
    xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    lines = data[xref:].split(b"\n")
    assert lines[0] == b"xref" and lines[1] == b"0 9"  # catalog, page tree, 3 per page
    for number, line in enumerate(lines[3:11], 1):  # every object where xref says
        offset = int(line[:10])
        assert data[offset:].startswith(b"%d 0 obj" % number)
    assert b"/MediaBox [0 0 283.465 425.197]" in data
    plain = Image.new("RGB", (300, 150), "white")  # without a size: its pixels
    assert b"/MediaBox [0 0 72.000 36.000]" in pdf.encode([plain], 300)


def _hairlines(dpi: float, size_mm: tuple[float, float]) -> Image.Image:
    """One-pixel lines, across and down, one every millimetre or so."""
    from PIL import ImageDraw

    width, height = px(size_mm[0], dpi), px(size_mm[1], dpi)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for i in range(40):
        x, y = px(5 + i, dpi), px(5 + i * 0.6, dpi)
        draw.line((x, 10, x, height - 10), fill=0)
        draw.line((10, y, width - 10, y), fill=0)
    image.info["size_mm"] = size_mm
    return image


def test_a_large_colour_page_goes_as_jpeg(monkeypatch):
    monkeypatch.setattr(pdf, "DCT_ABOVE", 100_000)
    photo, plain = _noisy_pages(2)
    plain.paste((200, 30, 30), (0, 0, *plain.size))
    data = pdf.encode([photo, plain], 30)
    assert data.count(b"/Filter /DCTDecode") == 1  # only the large page
    assert data.count(b"/Filter /FlateDecode") == 1
    start = data.index(b"\xff\xd8")  # the JPEG in the PDF
    with Image.open(io.BytesIO(data[start:])) as jpeg:
        assert jpeg.size == photo.size and jpeg.mode == "RGB"
    bw = Image.new("1", (2000, 2000), 1)  # never a black-and-white page
    assert b"/DCTDecode" not in pdf.encode([bw], 300)


def test_pdf_stops_taking_pages_short_of_the_limit():
    pages = _noisy_pages(5)
    taken = []

    def given():
        for page in pages:
            taken.append(page)
            yield page

    data = pdf.encode(given(), 30, limit=700_000)  # 261 kB a page
    assert len(taken) == 2 and data.count(b"/Type /Page ") == 2
    assert pdf.encode(iter(pages[:1]), 30, limit=1).count(b"/Type /Page ") == 1  # always one


def test_pdf_image_one_pixel_per_dot_slightly_inset():
    a4 = Image.new("1", (2480, 3508), 1)  # 2480.3 x 3507.9 pixels at 300 dpi
    a4.info["size_mm"] = (210, 297)
    data = pdf.encode([a4], 300)
    # its own size (2480 pixels: 595.2 pt) from the top left, 0.01 pt smaller
    assert b"q 595.190 0 0 841.910 0.005 -0.025 cm /Im0 Do Q" in data
    odd = Image.new("1", (100, 100), 1)
    odd.info["size_mm"] = (20, 20)  # not 300 dpi: it fills the page
    assert b"q 56.683 0 0 56.683 0.005 0.005 cm" in pdf.encode([odd], 300)


@pytest.mark.parametrize("tool", ["pdftoppm", "gs"])
@pytest.mark.parametrize(
    "dpi, size_mm",
    [(203, (101.6, 152.4)), (300, (101.6, 152.4)), (300, (210, 297)), (203, (50, 30))],
)
def test_pdf_renders_pixel_for_pixel(tmp_path, tool, dpi, size_mm):
    """Poppler (cups-filters, pdftoppm) and Ghostscript, the two renderers
    CUPS uses, give back every pixel: no grey, no hairline lost."""
    import shutil
    import subprocess

    if not shutil.which(tool):
        pytest.skip(tool + " not installed")
    image = _hairlines(dpi, size_mm)
    (tmp_path / "page.pdf").write_bytes(pdf.encode([image], dpi))
    if tool == "gs":
        command = ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pgmraw"]
        command += ["-r%d" % dpi, "-sOutputFile=%s" % (tmp_path / "out.pgm")]
    else:
        command = ["pdftoppm", "-r", str(dpi), "-gray", "-singlefile"]
    command.append(str(tmp_path / "page.pdf"))
    if tool == "pdftoppm":
        command.append(str(tmp_path / "out"))
    subprocess.run(command, check=True, timeout=60)  # noqa: S603 - fixed command
    with Image.open(tmp_path / "out.pgm") as out:
        found = out.convert("L").crop((0, 0, image.width, image.height))
    assert _greys(found) == 0
    assert ImageChops.difference(found, image).getbbox() is None


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


def test_a_printer_that_says_busy_but_made_the_job_gets_it_once(fake):
    # not as the standard says (busy makes no job), but then: never twice
    fake.job_states = [5, 9]
    original = FakeIpp._op_0002

    def busy_yet_made(self, body, ops, job):
        original(self, body, ops, job)
        groups = [(ipp.JOB_GROUP, [ipp.Attribute(ipp.INTEGER, "job-id", [self.job_id])])]
        return _answer(0x0507, groups)

    FakeIpp._op_0002, saved = busy_yet_made, original
    try:
        _print()
    finally:
        FakeIpp._op_0002 = saved
    assert fake.printed == 1 and fake.operations().count(ipp.PRINT_JOB) == 1


def test_a_cancel_that_did_not_get_through_is_tried_again(fake):
    fake.job_states = [6]
    original = FakeIpp._op_0008
    tries = []

    def cancel(self, body, ops, job):
        tries.append(1)
        if len(tries) == 1:
            raise TimeoutError("timed out")  # the network, for a moment
        return original(self, body, ops, job)

    FakeIpp._op_0008, saved = cancel, original
    try:
        with pytest.raises(PrinterError, match="stopped"):
            _print()
    finally:
        FakeIpp._op_0008 = saved
    assert len(tries) == 2 and fake.canceled == [12]


def test_a_problem_that_comes_and_goes_still_ends_after_the_total(fake, monkeypatch):
    # reported only every other answer, never a page: RECOVERY_TOTAL is absolute
    monkeypatch.setattr(system, "RECOVERY_TOTAL", 120)
    fake.job_states = [5]
    original = FakeIpp._op_000b
    calls = []

    def flicker(self, body, ops, job):
        calls.append(1)
        self.reasons = ["none"] if len(calls) % 2 else ["media-empty-error"]
        return original(self, body, ops, job)

    FakeIpp._op_000b, saved = flicker, original
    try:
        with pytest.raises(OutOfPaper, match="within 120"):
            _print()
    finally:
        FakeIpp._op_000b = saved
    assert fake.now < 120 + 10


# --- Apple Raster: copies and pages ----------------------------------------------------------


def test_copies_in_apple_raster_without_cp_are_pages(fake):
    fake.copies_supported, fake.urf = None, ["W8", "RS300", "V1.4"]  # no CP
    fake.formats, fake.sizes = ["image/urf"], []
    _print(_image(118, 59, size_mm=(10, 5)), copies=3)
    ((mime, data),) = fake.documents
    assert mime == "image/urf" and "copies" not in fake.job()
    assert len(urf.decode(data)) == 3 and fake.printed == 3


def test_copies_in_apple_raster_with_cp_are_left_to_the_printer(fake):
    fake.copies_supported, fake.urf = None, ["W8", "RS300", "V1.4", "CP1"]
    fake.formats, fake.sizes = ["image/urf"], []
    _print(_image(118, 59, size_mm=(10, 5)), copies=3)
    ((_mime, data),) = fake.documents
    assert fake.job()["copies"] == [3] and len(urf.decode(data)) == 1


def test_many_copies_in_apple_raster_go_in_several_jobs(fake, monkeypatch):
    fake.copies_supported, fake.urf = (1, 1), ["W8", "CP1"]  # copies-supported decides
    fake.formats, fake.sizes = ["image/urf"], []
    made = []
    original = system.document_of

    def document_of(images, count, attrs, repeat=1, **kwargs):
        made.append((count, repeat))
        return original(images, count, attrs, repeat, **kwargs)

    monkeypatch.setattr(system, "document_of", document_of)
    _print(_image(118, 59, size_mm=(10, 5)), copies=12)
    pages = [len(urf.decode(data)) for _mime, data in fake.documents]
    assert pages == [10, 2] and fake.printed == 12
    assert made == [(1, 1)]  # compressed once
    assert fake.documents[0][1][12:] == fake.documents[1][1][12:] * 5  # the same page


@pytest.mark.parametrize("mime", ["image/urf", "image/pwg-raster"])
def test_raster_copies_are_limited_by_bytes_too(fake, monkeypatch, mime):
    fake.copies_supported, fake.formats, fake.sizes = (1, 1), [mime], []
    image = _image(118, 59, size_mm=(10, 5))
    single = len(system.document(image, {"document-format-supported": [mime]})[0])
    monkeypatch.setattr(system, "MAX_JOB_BYTES", single * 3)
    _print(image, copies=7)
    decode = urf.decode if mime == "image/urf" else pwg.decode_all
    assert [len(decode(data)) for _mime, data in fake.documents] == [3, 3, 1]
    assert fake.printed == 7


def test_a_page_larger_than_the_byte_limit_still_goes(fake, monkeypatch):
    fake.copies_supported, fake.formats, fake.sizes = (1, 1), ["image/urf"], []
    monkeypatch.setattr(system, "MAX_JOB_BYTES", 10)
    _print(_image(118, 59, size_mm=(10, 5)), copies=2)
    assert [len(urf.decode(data)) for _mime, data in fake.documents] == [1, 1]


@pytest.mark.parametrize("mime", ["image/urf", "image/pwg-raster"])
def test_raster_pages_go_in_jobs_limited_by_bytes(fake, monkeypatch, mime):
    fake.formats, fake.urf = [mime], ["SRGB24", "RS300"]
    pages = _pages(23)  # pages of one colour each: the same size compressed
    attrs = {"document-format-supported": [mime], "urf-supported": fake.urf}
    one = len(system.document(pages[0], attrs)[0])
    monkeypatch.setattr(system, "MAX_JOB_BYTES", one * 4 + 1)
    events = []
    get_backend("system").print_pages(pages, "HP_Office", PrintOptions(), events.append)
    decode = urf.decode if mime == "image/urf" else pwg.decode_all
    found = [decode(data) for _mime, data in fake.documents]
    assert [len(job) for job in found] == [4, 4, 4, 4, 4, 3]
    every = [page for job in found for page in job]
    assert [page.getpixel((5, 5)) for page in every] == [page.getpixel((5, 5)) for page in pages]
    assert events[-1] == Progress(23, 23)


def test_raster_pages_without_a_byte_limit_go_ten_a_job(fake):
    fake.formats, fake.urf = ["image/urf"], ["SRGB24", "RS300"]
    get_backend("system").print_pages(_pages(23), "HP_Office", PrintOptions())
    found = [urf.decode(data) for _mime, data in fake.documents]
    assert [len(pages) for pages in found] == [10, 10, 3]
    assert struct.unpack_from(">I", fake.documents[0][1], 8)[0] == 10


# --- codes at the printer's resolution ------------------------------------------------------


def _code_label(length_mm: float, data: str = "ABC-123ABC-123") -> Label:
    label = Label(printer="system", orientation="across", elements=[CodeElement(data, "code128")])
    label.set_media_size(length_mm, 14)
    return label


def test_query_says_the_resolution_the_label_is_printed_at(fake):
    fake.formats, fake.resolution = ["application/pdf"], (203, 203, 3)
    assert get_backend("system").query("HP_Office").dpi == 203
    fake.formats = ["image/pwg-raster"]
    assert get_backend("system").query("HP_Office").dpi == 300
    fake.formats = ["image/jpeg"]
    assert get_backend("system").query("HP_Office").dpi == 300
    fake.formats = ["text/plain"]
    assert get_backend("system").query("HP_Office").dpi is None


def test_a_code_that_does_not_fit_at_the_printers_resolution_is_not_printed(fake):
    fake.formats, fake.sizes, fake.resolution = ["application/pdf"], [], (203, 203, 3)
    backend = get_backend("system")
    image = backend.prepare(_code_label(27), PrintOptions())  # fits at 300 dpi
    with pytest.raises(CodeTooLarge) as info:
        backend.print_label(image, "HP_Office", PrintOptions())
    assert info.value.params == {"n": 1}
    assert ipp.PRINT_JOB not in fake.operations()
    with pytest.raises(CodeTooLarge):  # nor at 300 dpi, where it does not fit either
        backend.prepare(_code_label(20), PrintOptions())


@pytest.mark.parametrize(
    "mode, space, size",
    [
        ("P", "DeviceRGB", 3),
        ("RGBA", "DeviceRGB", 3),
        ("CMYK", "DeviceRGB", 3),
        ("LA", "DeviceGray", 1),
    ],
)
def test_pdf_pages_in_other_modes(mode, space, size):
    page = Image.new("RGB", (8, 4), (200, 30, 30)).convert(mode)
    found, bits, raw = pdf.pixels(page)
    assert (found, bits) == (space, 8) and len(raw) == 8 * 4 * size
