"""The VC-500W backend against FakeVC500W (conftest), on the protocol level:
the XML we send and the bytes of the image are checked exactly."""

import io
import json
import socket
import sys
import types
from datetime import date
from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from conftest import status
from PIL import Image

from etiketto.backends import mdns, vc500w
from etiketto.backends.base import (
    BadCassette,
    CassetteInfo,
    ColorAdjust,
    CoverOpen,
    FoundPrinter,
    LabelTooLong,
    NoCassette,
    PaperJam,
    PortClosed,
    PrinterBusy,
    PrinterError,
    PrinterInfo,
    PrinterNotFound,
    PrinterTimeout,
    PrintMode,
    PrintOptions,
    PrintResult,
    Progress,
    Raster,
)
from etiketto.backends.vc500w import BW, COLOR, VC500W, VIVID
from etiketto.model import ImageElement, Label

REAL_CONNECT = vc500w._connect  # before the autouse fixture replaces it
HEAD = "<print>\n<mode>%s</mode>\n<speed>%d</speed>\n<lpi>%d</lpi>\n"


@pytest.fixture
def backend():
    return VC500W()


@pytest.mark.parametrize(
    "inches, expected",
    [(2.0, 50), (1.0, 25), (0.75, 19), (0.5, 12), (0.35, 9), (0.1, 9), (3.0, 50)],
)
def test_nominal_width(inches, expected):
    assert vc500w.nominal_width(inches * 25.4) == expected


# --- configured_host -------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(vc500w.Path, "home", classmethod(lambda cls: tmp_path))
    config = tmp_path / ".config" / "labelprinter" / "config.json"
    config.parent.mkdir(parents=True)
    return config


def test_configured_host_reads_labelprinter_config(home, backend):
    home.write_text(json.dumps({"host": "10.0.0.7", "port": 9100}))
    assert backend.configured_host() == "10.0.0.7"


@pytest.mark.parametrize(
    "content", [None, "{not json", json.dumps({"host": ""}), json.dumps({"port": 1}), "[1]"]
)
def test_configured_host_missing_broken_or_empty(home, content):
    if content is not None:
        home.write_text(content)
    assert vc500w.configured_host() is None


# --- parsing config.xml and status.xml ----------------------------------------


def test_parse_config(printer):
    config = vc500w.parse_config(printer.device.config_xml())
    assert config == vc500w.DeviceConfig(
        model="Wedge",
        firmware="2026031504",
        crossweb_dpi=320,
        tape_mm=25,
        length_in=197.0,
        printable_in=0.95,
        media_id="ABC123456XYZ",
        cassette_sn="180529-0116G133390-0084",
        modes=(VIVID, COLOR, BW),
    )


@pytest.mark.parametrize(
    "cassette, width, tape",
    [("3/8", "0.375", 9), ("1/2", "", 12), ("3/4", "0.75", 19), ("2", "2.000", 50)],
)
def test_parse_config_cassette_types(cassette, width, tape):
    xml = "<cassette_type>%s</cassette_type><width_inches>%s</width_inches>" % (cassette, width)
    assert vc500w.parse_config(xml).tape_mm == tape


def test_parse_config_unknown_type_uses_width_and_no_cassette():
    xml = "<cassette_type>X</cassette_type><width_inches>0.5</width_inches>"
    assert vc500w.parse_config(xml).tape_mm == 12
    assert not vc500w.parse_config(xml).no_cassette
    for empty in ("<cassette_type></cassette_type>", "<cassette_type/>", "<cassette_type> </"):
        config = vc500w.parse_config(empty + "<width_inches>1</width_inches>")
        assert config.tape_mm is None and config.no_cassette
    unknown = vc500w.parse_config("<cassette_type>X</cassette_type>")
    assert unknown.tape_mm is None and not unknown.no_cassette
    # a firmware without the field: the width tells
    missing = vc500w.parse_config("<width_inches>0.75</width_inches>")
    assert missing.tape_mm == 19 and not missing.no_cassette


def test_parse_config_defaults_for_missing_or_broken_values():
    config = vc500w.parse_config(
        "<crossweb_dpi>abc</crossweb_dpi><media_length_initial>0</media_length_initial>"
    )
    assert config.crossweb_dpi == 320 and config.length_in is None
    assert config.modes == () and config.model == "" and config.printable_in is None


def test_parse_modes_document_order_and_triples():
    xml = (
        "<cutmode>full</cutmode>"  # not a mode
        "<speed>9</speed>"  # before any mode: ignored
        "<mode>BW</mode><speed>2</speed><lpi>400</lpi>"
        "<mode>vivid</mode><lpi>300</lpi>"  # speed missing: the known one
        "<mode>wifi</mode><speed>1</speed><lpi>99</lpi>"  # unknown name
        "<mode>color</mode><speed>x</speed><lpi>264</lpi>"  # broken number
        "<mode>vivid</mode><speed>0</speed><lpi>317</lpi>"  # duplicate
        "<media_mode>color 1 260</media_mode><media_mode>bad</media_mode>"
        "<mode>bw</mode><lpi>0</lpi>"
    )
    assert vc500w.parse_modes(xml) == (
        PrintMode("bw", 400, 2),
        PrintMode("vivid", 300, 0),
        PrintMode("color", 260, 1),
    )


def test_parse_status():
    xml = (
        "<print_state>busy</print_state><print_job_stage>PREHEATING</print_job_stage>"
        "<print_job_error>NONE</print_job_error><remain>12.5</remain>"
    )
    assert vc500w.parse_status(xml) == vc500w.DeviceStatus("BUSY", "PREHEATING", "NONE", 12.5)
    assert vc500w.parse_status("<remain>-1</remain>").remain_in is None
    assert vc500w.parse_status("").state == ""


@pytest.mark.parametrize(
    "serial, made",
    [
        ("180529-0116G133390-0084", date(2018, 5, 29)),
        ("240101", date(2024, 1, 1)),
        ("181329-01", None),  # month 13
        ("180230-01", None),  # 30 February
        ("091231-01", None),  # before the VC-500W existed
        ("991231-01", None),  # in the future
        ("1805291-01", None),  # seven digits: not a date
        ("G133390", None),
        ("", None),
    ],
)
def test_cassette_date(serial, made):
    assert vc500w.cassette_date(serial, today=date(2026, 11, 5)) == made


def test_cassette_info(printer):
    config = vc500w.parse_config(printer.device.config_xml())
    assert vc500w.cassette_info(config) == CassetteInfo(
        lot="ABC123456",
        serial="180529-0116G133390-0084",
        made=date(2018, 5, 29),
        printable_mm=24.1,
    )
    config.tape_mm = None
    assert vc500w.cassette_info(config) is None


@pytest.mark.parametrize(
    "error, cls",
    [
        ("NO MEDIA", NoCassette),
        ("JAM", PaperJam),
        ("EJECT JAM", PaperJam),
        ("DOOR OPEN", CoverOpen),
        ("BAD CASSETTE", BadCassette),
        ("SEQUENCE ERROR", PrinterError),
    ],
)
def test_status_error(error, cls):
    found = vc500w.status_error(vc500w.DeviceStatus("ERROR", "PRINTING", error))
    assert type(found) is cls
    assert error in found.details
    assert vc500w.status_error(vc500w.DeviceStatus("BUSY", "PRINTING", error)) is None


# --- errors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error, cls",
    [
        (
            ValueError("Cannot resolve hostname 'x' to IPv4 address: ...\nPossible"),
            PrinterNotFound,
        ),
        (socket.gaierror(-2, "Name or service not known"), PrinterNotFound),
        (OSError("cannot resolve VC.local (mDNS)"), PrinterNotFound),
        (ValueError("Connection to 'x:9100' was refused.\nPossible solutions"), PortClosed),
        (ConnectionRefusedError(111, "refused"), PortClosed),
        (ValueError("Connection to 'x:9100' timed out after 3 seconds.\n"), PrinterTimeout),
        (TimeoutError("timed out"), PrinterTimeout),
        (ValueError("Cannot connect to printer at 'x:9100': No route\n"), PrinterNotFound),
        (ValueError('The XML status code is not OK: "busy"'), PrinterError),
        (RuntimeError(), PrinterError),
    ],
)
def test_map_error(error, cls):
    mapped = vc500w.map_error(error)
    assert type(mapped) is cls
    assert mapped.details == (str(error) or "RuntimeError")


def test_map_error_keeps_printer_errors():
    error = CoverOpen("x")
    assert vc500w.map_error(error) is error


def test_session_maps_connect_and_request_errors(monkeypatch):
    def unreachable(host, port):
        raise ValueError("Connection to 'p:9100' was refused.")

    monkeypatch.setattr(vc500w, "_connect", unreachable)
    with pytest.raises(PortClosed) as info:
        VC500W().query("p")
    assert isinstance(info.value.__cause__, ValueError)


def test_query_lost_answer_is_a_timeout(printer, backend, monkeypatch):
    from conftest import FakeConnection

    def silence(self, long_timeout=False, buffer_size=4096):
        raise TimeoutError("timed out")

    monkeypatch.setattr(FakeConnection, "get_message", silence)
    with pytest.raises(PrinterTimeout):
        backend.query("p")
    assert printer.device.closed == 1


def test_connect_uses_resolved_host(monkeypatch):
    made = {}

    class Connection:
        def __init__(self, host, port, timeout):
            made["args"] = (host, port, timeout)

    monkeypatch.setitem(
        sys.modules, "labelprinter.connection", types.SimpleNamespace(Connection=Connection)
    )
    monkeypatch.setattr(mdns, "resolve_host", lambda host, family: "192.0.2.1")
    connection = REAL_CONNECT("VC.local", 9100)
    assert made["args"] == ("192.0.2.1", 9100, vc500w.PRINT_TIMEOUT)
    assert isinstance(connection, Connection)


# --- query --------------------------------------------------------------------------


def test_query(printer):
    info = VC500W(port=1234).query("printer.example")
    assert printer.calls == [("printer.example", 1234)]
    assert info == PrinterInfo(
        model="Wedge",
        tape_mm=25,
        remaining_mm=round(150 * 25.4),
        total_mm=round(197 * 25.4),
        state="IDLE",
        error="",  # print_job_error=NONE: no problem
        modes=(VIVID, COLOR, BW),
        firmware="2026031504",
        cassette=CassetteInfo("ABC123456", "180529-0116G133390-0084", date(2018, 5, 29), 24.1),
    )
    assert printer.device.names() == ["config", "read", "status", "read", "close"]


def test_query_reports_a_problem_as_its_code(printer, backend):
    printer.device.statuses = [status(state="ERROR", error="DOOR OPEN")]
    assert backend.query("p").error == "cover_open"


def test_query_without_cassette_or_remaining(printer, backend):
    printer.device.config.update(cassette_type="", model_name="")
    info = backend.query("p")
    assert info.model == "VC-500W" and info.tape_mm is None and info.cassette is None
    assert info.remaining_mm is None and info.total_mm is None
    printer.device.config["cassette_type"] = "2"
    printer.device.statuses = [status(remain=None)]
    info = backend.query("p")
    assert info.tape_mm == 50 and info.remaining_mm is None and info.total_mm is None


def test_query_default_port(printer, backend):
    backend.query("p")
    assert printer.calls == [("p", 9100)]


# --- raster geometry ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tape, mode, dpi, tape_px, width",
    [
        (9, "vivid", 320, 120, 133),  # 0.375″ × 320 + 2 × 0.02″ × 320 = 132.8
        (12, "color", 320, 160, 173),
        (19, "vivid", 320, 240, 253),
        (25, "vivid", 320, 320, 333),
        (25, "bw", 400, 400, 416),
        (50, "vivid", 320, 630, 640),  # 2.0″ × dpi
        (50, "bw", 400, 787, 800),
    ],
)
def test_raster_rawrgb(backend, tape, mode, dpi, tape_px, width):
    raster = backend.raster(tape, PrintOptions(mode=mode, data_format="rawrgb"))
    assert raster == Raster(dpi, tape_px, -(-(width - tape_px) // 2), width)


def test_raster_jpeg_is_the_preview_resolution(backend):
    assert backend.raster(25, PrintOptions(mode="bw", data_format="jpeg")) == Raster(317)


def test_rawrgb_is_the_default(backend):
    assert PrintOptions().data_format == "rawrgb"
    assert backend.capabilities.data_formats[0] == "rawrgb"
    assert backend.raster(25, PrintOptions()) == backend.raster(
        25, PrintOptions(data_format="rawrgb")
    )
    assert vc500w.data_format(PrintOptions(data_format="png")) == "rawrgb"
    assert vc500w.data_format(PrintOptions(data_format="jpeg")) == "jpeg"


def test_preview_bw_is_grey(backend):
    image = Image.new("RGB", (4, 4), (255, 0, 0))
    grey = backend.preview(image, "bw")
    assert grey.mode == "RGB"
    r, g, b = grey.getpixel((0, 0))
    assert r == g == b and r < 128
    assert backend.preview(image, "vivid").getpixel((0, 0)) == (255, 0, 0)
    assert backend.preview(image).getpixel((0, 0)) == (255, 0, 0)


def test_prepare_bw_is_grey(backend, png_b64):
    label = Label(tape_mm=25, elements=[ImageElement(data=png_b64((10, 10), "red"))])
    image = backend.prepare(label, PrintOptions(mode="bw"))
    assert all(r == g == b for _n, (r, g, b) in image.getcolors(maxcolors=1 << 24))


def test_prepare_rawrgb_has_exact_width_and_bleed(backend, png_b64):
    label = Label(
        tape_mm=25,
        background="#00ff00",
        margin_mm=0,
        elements=[ImageElement(data=png_b64((10, 10), "red"))],
    )
    image = backend.prepare(label, PrintOptions(data_format="rawrgb"), safe_mm=0)
    # label space: height across the tape = 333 px, 7 px bleed on top, 6 below
    assert image.height == 333
    assert image.getpixel((0, 0)) == (0, 255, 0)
    assert image.getpixel((0, 332)) == (0, 255, 0)
    assert image.getpixel((5, 7)) == (255, 0, 0)  # content starts where the tape does
    assert image.getpixel((5, 6)) == (0, 255, 0)
    assert image.getpixel((5, 7 + 319)) == (255, 0, 0)
    assert image.getpixel((5, 7 + 320)) == (0, 255, 0)
    assert image.width == 320  # a square image element on 1″ of tape


# --- printing -----------------------------------------------------------------------


def _label_image(width=400, height=312, split=100):
    """Label space: red start (x < split), blue rest."""
    image = Image.new("RGB", (width, height), "blue")
    image.paste((255, 0, 0), (0, 0, split, height))
    return image


def test_print_jpeg_request_and_data(printer, backend):
    options = PrintOptions(mode="color", cut="half", data_format="jpeg")
    assert backend.print_label(_label_image(), "p", options) is None
    data = printer.device.data()
    assert printer.device.prints() == [
        HEAD % ("color", 1, 264)
        + "<width>0</width>\n<height>0</height>\n<dataformat>jpeg</dataformat>\n"
        + "<autofit>1</autofit>\n<datasize>%d</datasize>\n<cutmode>half</cutmode>\n</print>"
        % len(data[0])
    ]
    with Image.open(io.BytesIO(data[0])) as img:
        assert img.format == "JPEG"
        assert img.size == (312, 400)  # printer orientation: width = tape
        assert img.info["dpi"] == (264, 264)
        r, g, b = img.convert("RGB").getpixel((150, 5))  # label start comes first
        assert r > 200 and b < 60


def test_print_colour_adjustment(printer, backend):
    color = ColorAdjust(brightness=1, red=-3, green=2, blue=0)
    backend.print_label(_label_image(), "p", PrintOptions(color=color))
    request = printer.device.prints()[0]
    assert request.endswith(
        "<cutmode>full</cutmode>\n"
        "<brightness>0.17</brightness>\n"
        "<color_balance_red>-0.50</color_balance_red>\n"
        "<color_balance_green>0.33</color_balance_green>\n"
        "<color_balance_blue>0.00</color_balance_blue>\n"
        "</print>"
    )
    assert request.startswith(HEAD % ("vivid", 0, 317))  # empty mode: the first one


def test_print_without_adjustment_sends_no_colour_tags(printer, backend):
    backend.print_label(_label_image(), "p", PrintOptions(color=ColorAdjust()))
    assert "brightness" not in printer.device.prints()[0]
    assert "color_balance" not in printer.device.prints()[0]


@pytest.mark.parametrize("mode, lpi, width", [("vivid", 317, 333), ("bw", 400, 416)])
def test_print_rawrgb_geometry_and_bytes(printer, backend, mode, lpi, width):
    options = PrintOptions(mode=mode, data_format="rawrgb", cut="none")
    dpi = vc500w.raster_dpi(mode)
    image = _label_image(width=2 * dpi, height=width)  # 2″ long
    backend.print_label(image, "p", options)
    height = 2 * lpi
    data = printer.device.data()[0]
    assert len(data) == width * height * 3
    speed = {"vivid": 0, "bw": 2}[mode]
    assert printer.device.prints() == [
        HEAD % (mode, speed, lpi)
        + "<width>%d</width>\n<height>%d</height>\n<dataformat>rawrgb</dataformat>\n"
        % (width, height)
        + "<datasize>%d</datasize>\n<cutmode>none</cutmode>\n</print>" % len(data)
    ]
    # rows run along the tape: the first rows are the red label start
    first, last = data[: width * 3], data[-width * 3 :]
    if mode == "bw":  # grey: the same value in all three channels
        assert first[0] == first[1] == first[2] and first[0] < 128
    else:
        assert first[0] > 250 and first[1] < 5 and first[2] < 5
        assert first[-3] > 250 and first[-1] < 5
        assert last[0] < 5 and last[2] > 250


def test_print_rawrgb_follows_the_printers_lpi(printer, backend):
    printer.device.modes = [("vivid", 0, 300)]
    printer.device.config["crossweb_dpi"] = "300"
    image = _label_image(width=640, height=333)  # rendered at 320 dpi, 2″ long
    backend.print_label(image, "p", PrintOptions(mode="vivid", data_format="rawrgb"))
    request = printer.device.prints()[0]
    assert "<width>312</width>\n<height>600</height>" in request  # 333 × 300 / 320
    assert len(printer.device.data()[0]) == 312 * 600 * 3


def test_print_rawrgb_too_long(printer, backend):
    image = _label_image(width=round(17.1 * 320), height=333)
    with pytest.raises(LabelTooLong) as info:
        backend.print_label(image, "p", PrintOptions(data_format="rawrgb"))
    assert info.value.params == {"max_mm": pytest.approx(431.8)}
    assert printer.device.prints() == [] and printer.calls == []  # checked before connecting
    exact = _label_image(width=17 * 320, height=333)
    backend.print_label(exact, "p", PrintOptions(data_format="rawrgb"))
    assert "<height>%d</height>" % (17 * 317) in printer.device.prints()[0]


def test_print_jpeg_too_long(printer, backend):
    image = _label_image(width=round(17.1 * 317), height=312)
    with pytest.raises(LabelTooLong) as info:
        backend.print_label(image, "p", PrintOptions(data_format="jpeg"))
    assert info.value.params == {"max_mm": pytest.approx(431.8)}
    assert printer.calls == []
    backend.print_label(_label_image(width=17 * 317), "p", PrintOptions(data_format="jpeg"))
    assert len(printer.device.prints()) == 1


def test_print_unknown_format_and_cut_fall_back(printer, backend):
    backend.print_label(
        _label_image(height=333), "p", PrintOptions(cut="sideways", data_format="png")
    )
    request = printer.device.prints()[0]
    assert "<cutmode>full</cutmode>" in request and "<dataformat>rawrgb</dataformat>" in request


def test_print_mode_not_offered_by_the_printer(printer, backend):
    printer.device.modes = []  # old firmware: nothing reported, documented modes
    result = backend.print_label(_label_image(), "p", PrintOptions(mode="bw"))
    assert printer.device.prints()[0].startswith(HEAD % ("vivid", 0, 317))
    assert result == PrintResult(mode="vivid")  # the interface tells the user


def test_print_default_mode_is_no_fallback(printer, backend):
    assert backend.print_label(_label_image(), "p", PrintOptions()) is None
    assert backend.print_label(_label_image(), "p", PrintOptions(mode="color")) is None


def test_print_copies_progress_and_stages(printer, backend):
    printer.device.after_print = [
        status("BUSY", "PREHEATING"),
        status("BUSY", "PREHEATING"),
        status("BUSY", "PRINTING"),
        status("BUSY", "READY_FOR_CUT"),
    ]
    events = []
    backend.print_label(_label_image(), "p", PrintOptions(copies=2), events.append)
    assert len(printer.device.prints()) == 2
    assert events == [
        Progress(1, 2),
        Progress(1, 2, "PREHEATING"),
        Progress(1, 2, "PRINTING"),
        Progress(1, 2, "READY_FOR_CUT"),
        Progress(2, 2),
        Progress(2, 2, "PREHEATING"),
        Progress(2, 2, "PRINTING"),
        Progress(2, 2, "READY_FOR_CUT"),
    ]
    # polled every 0.5 s; READY_FOR_CUT ends the copy at once (printed, cut by hand)
    assert printer.sleeps == [0.5] * (2 * 3)
    assert printer.device.closed == 1


def _status_script(printer, script):
    """Statuses in order, one per status request; the last one repeats.
    Prints are recorded in the device log, so the order can be checked."""
    printer.device.statuses = list(script)
    printer.device.after_print = []  # the script keeps running across the print

    from conftest import FakeConnection

    original = FakeConnection.send_file

    def send_file(self, handle):
        statuses = self.device.statuses
        original(self, handle)
        self.device.statuses = statuses

    return send_file


def test_print_waits_until_the_previous_copy_is_really_done(printer, backend, monkeypatch):
    """The race of 0.x: right after sending, the printer still says IDLE /
    SUCCESS of the last job. The second copy must wait for BUSY and IDLE."""
    from conftest import FakeConnection

    send_file = _status_script(
        printer,
        [
            status(),  # ready for copy 1
            status(),  # still the old state after sending
            status(),
            status("BUSY", "PREPARING PRINT"),
            status("BUSY", "PRINTING"),
            status("IDLE", "COOLDOWN"),  # idle, but not ready yet
            status(),  # done: ready for copy 2
            status(),  # ready check before copy 2
            status("BUSY", "PRINTING"),
            status(),
        ],
    )
    monkeypatch.setattr(FakeConnection, "send_file", send_file)
    events = []
    backend.print_label(_label_image(), "p", PrintOptions(copies=2), events.append)
    names = [n for n in printer.device.names() if n in ("status", "print")]
    assert names == ["status", "print"] + ["status"] * 7 + ["print"] + ["status"] * 2
    assert Progress(1, 2, "COOLDOWN") in events and Progress(1, 2, "SUCCESS") not in events


def test_print_waits_for_a_ready_stage_before_sending(printer, backend):
    printer.device.statuses = [status("IDLE", "COOLDOWN"), status("IDLE", "READY FOR PRINT")]
    backend.print_label(_label_image(), "p", PrintOptions())
    names = [n for n in printer.device.names() if n in ("status", "print")]
    assert names[:3] == ["status", "status", "print"]


def test_print_counts_a_job_without_busy_as_done_after_the_grace(printer, backend):
    # after_print empty: the printer never shows BUSY (job between two polls)
    start = printer.now[0]
    backend.print_label(_label_image(), "p", PrintOptions(copies=2))
    assert len(printer.device.prints()) == 2
    waited = printer.now[0] - start
    assert waited == pytest.approx(2 * vc500w.START_GRACE)


def test_print_retries_a_busy_answer(printer, backend):
    printer.device.print_codes = [1, 1, 1]
    backend.print_label(_label_image(), "p", PrintOptions())
    assert len(printer.device.prints()) == 4  # three refused, the fourth accepted
    assert len(printer.device.data()) == 1
    assert printer.sleeps[:3] == [vc500w.POLL_INTERVAL] * 3


def test_waiting_for_the_panel_swipe_has_no_time_limit(printer, backend):
    # half cut: after the first copy the printer waits until someone swipes
    # the panel, here for longer than PRINT_TIMEOUT; that is not a timeout
    waiting = int(vc500w.PRINT_TIMEOUT / vc500w.POLL_INTERVAL) + 20
    printer.device.after_print = [status("BUSY", "PRINTING")] + [
        status("BUSY", "READY_FOR_CUT")
    ] * waiting
    events = []
    backend.print_label(_label_image(), "p", PrintOptions(copies=2, cut="half"), events.append)
    assert len(printer.device.prints()) == 2
    assert Progress(1, 2, "READY_FOR_CUT") in events
    assert sum(printer.sleeps) > vc500w.PRINT_TIMEOUT


def test_print_gives_up_after_the_retries_of_a_busy_answer(printer, backend):
    printer.device.print_code = 1
    printer.device.statuses = [status(), status("BUSY", "PRINTING")]
    with pytest.raises(PrinterBusy) as info:
        backend.print_label(_label_image(), "p", PrintOptions())
    assert len(printer.device.prints()) == 1 + vc500w.SEND_RETRIES
    assert info.value.details == "busy"  # no comment in the answer
    assert printer.device.data() == []


def test_answer_code():
    assert vc500w._answer_code("<status><code>1</code></status>") == 1
    assert vc500w._answer_code("<status><code> 0 </code></status>") == 0
    assert vc500w._answer_code("garbage") == -1


def test_print_waits_for_a_busy_printer_first(printer, backend):
    printer.device.statuses = [status("BUSY", "COOLDOWN"), status()]
    events = []
    backend.print_label(_label_image(), "p", PrintOptions(), events.append)
    assert events[:2] == [Progress(1, 1), Progress(1, 1, "COOLDOWN")]
    names = printer.device.names()
    assert names.index("print") > names.index("status") + 2  # two status requests before


def test_print_aborts_at_once_on_an_error_before_printing(printer, backend):
    printer.device.statuses = [status("ERROR", "IDLE", "DOOR OPEN")]
    with pytest.raises(CoverOpen) as info:
        backend.print_label(_label_image(), "p", PrintOptions(copies=3))
    assert "DOOR OPEN" in info.value.details
    assert printer.device.prints() == [] and printer.sleeps == []
    assert printer.device.closed == 1


def test_print_error_state_without_error_code_aborts(printer, backend):
    printer.device.statuses = [status("ERROR", "PRINTING", "NONE")]
    with pytest.raises(PrinterError) as info:
        backend.print_label(_label_image(), "p", PrintOptions())
    assert type(info.value) is PrinterError and "print_job_error=NONE" in info.value.details
    assert printer.device.prints() == [] and printer.sleeps == []


def test_print_endless_busy_times_out_quickly(printer, backend):
    printer.device.statuses = [status("BUSY", "PRINTING")]
    with pytest.raises(PrinterTimeout):
        backend.print_label(_label_image(), "p", PrintOptions())
    # the fake clock ran PRINT_TIMEOUT, not the real one
    assert len(printer.sleeps) == vc500w.PRINT_TIMEOUT / vc500w.POLL_INTERVAL + 1


def test_print_aborts_on_an_error_while_printing(printer, backend):
    printer.device.after_print = [status("BUSY", "PRINTING"), status("ERROR", "PRINTING", "JAM")]
    with pytest.raises(PaperJam):
        backend.print_label(_label_image(), "p", PrintOptions(copies=3))
    assert len(printer.device.prints()) == 1


def test_print_without_cassette(printer, backend):
    printer.device.config["cassette_type"] = ""
    with pytest.raises(NoCassette):
        backend.print_label(_label_image(), "p", PrintOptions())
    assert printer.device.prints() == []


def test_print_when_the_firmware_does_not_report_the_cassette(printer, backend):
    del printer.device.config["cassette_type"]
    del printer.device.config["width_inches"]
    backend.print_label(_label_image(), "p", PrintOptions())
    assert len(printer.device.prints()) == 1


@pytest.mark.parametrize(
    "after, cls",
    [
        (status("BUSY", "PRINTING"), PrinterBusy),
        (status("ERROR", "IDLE", "NO MEDIA"), NoCassette),
        # IDLE/IDLE without error after a refusal: no cassette (as in Brother's app)
        (status("IDLE", "IDLE"), NoCassette),
        (status("IDLE", "SUCCESS"), PrinterError),
    ],
)
def test_print_rejected_asks_the_status_why(printer, backend, after, cls):
    printer.device.print_code = 1
    printer.device.print_comment = "refused"
    printer.device.statuses = [status(), after]
    with pytest.raises(PrinterError) as info:
        backend.print_label(_label_image(), "p", PrintOptions())
    assert type(info.value) is cls
    assert 'The XML status code is not OK: "refused"' in info.value.details
    assert printer.device.data() == []  # no data after a refused request


def test_print_rejected_with_no_media_comment(printer, backend):
    # seen on the device with the cassette ejected: the refusal says "no media"
    printer.device.print_code = 3
    printer.device.print_comment = "no media"
    printer.device.statuses = [status(), status("IDLE", "SUCCESS")]
    with pytest.raises(NoCassette):
        backend.print_label(_label_image(), "p", PrintOptions())


def test_print_rejected_and_status_unreadable(printer, backend, monkeypatch):
    printer.device.print_code = 3
    calls = []

    def status_xml(original=printer.device.status_xml):
        calls.append(1)
        if len(calls) > 1:
            raise TimeoutError("timed out")
        return original()

    monkeypatch.setattr(printer.device, "status_xml", status_xml)
    with pytest.raises(PrinterError) as info:
        backend.print_label(_label_image(), "p", PrintOptions())
    assert type(info.value) is PrinterError


def test_print_other_answer_errors_are_mapped(printer, backend, monkeypatch):
    def broken(self, handle):
        raise ValueError("Expected a XML status response first: 'garbage'")

    from conftest import FakeConnection

    monkeypatch.setattr(FakeConnection, "send_file", broken)
    with pytest.raises(PrinterError, match="Expected a XML status"):
        backend.print_label(_label_image(), "p", PrintOptions())


def test_print_times_out_without_progress(printer, backend, monkeypatch):
    monkeypatch.setattr(vc500w, "POLL_INTERVAL", 50)
    printer.device.after_print = [status("BUSY", "PRINTING")] * 3 + [status("BUSY", "CLEANING")]
    printer.device.after_print += [status("BUSY", "CLEANING")] * 10
    with pytest.raises(PrinterTimeout, match="CLEANING"):
        backend.print_label(_label_image(), "p", PrintOptions())
    # 1 before printing, then 3 × PRINTING; without the restart of the 180 s
    # by the change to CLEANING it would give up at the second CLEANING
    assert printer.device.names().count("status") == 1 + 3 + 5


def test_discover_uses_mdns(monkeypatch, backend):
    seen = {}

    def browse(service_type, match, timeout):
        seen.update(type=service_type, timeout=timeout)
        names = ["Brother VC-500W (Kitchen)", "brother vc500w", "Office LaserJet", "VC 500W x"]
        return [name for name in names if match(name)]

    monkeypatch.setattr(mdns, "browse", browse)
    assert backend.discover(timeout=2) == [
        "Brother VC-500W (Kitchen)",
        "brother vc500w",
        "VC 500W x",
    ]
    assert seen == {"type": "_ipp._tcp", "timeout": 2}


# --- the contract every backend fulfils ---------------------------------------------


class TestContract(BackendContract):
    backend_id = "vc500w"

    @pytest.fixture
    def harness(self, printer, monkeypatch):
        found = [FoundPrinter("Brother VC-500W", "VC-500W6382.local", "192.0.2.55")]
        monkeypatch.setattr(mdns, "browse", lambda service_type, match, timeout: found)

        def fail():
            printer.device.statuses = [status("ERROR", "IDLE", "JAM")]

        return SimpleNamespace(
            backend=VC500W(), fail=fail, printed=lambda: len(printer.device.prints())
        )
