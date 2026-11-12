"""The parts the raw label printer languages share: 1-bit rows, short
questions that may stay unanswered, the connection (a TCP printer on
localhost, a pseudo terminal standing in for /dev/usb/lp0), finding the
printers and their settings. Nothing here reaches a real printer or the
network: TCP goes to a server of this test on 127.0.0.1, mDNS is faked."""

from __future__ import annotations

import os
import socket
import threading
import tty
from pathlib import Path

import pytest
from fake_raw import ZPL_HI, ZPL_HQES, ZPL_HS
from PIL import Image

from etiketto.backends import mdns, raw, tspl, usb, zpl
from etiketto.backends.base import (
    Choice,
    FoundPrinter,
    Level,
    PortClosed,
    PrinterError,
    PrinterNotFound,
    PrinterTimeout,
    PrintOptions,
)
from etiketto.backends.dymo import Dymo
from etiketto.backends.epl import Epl
from etiketto.backends.escpos import EscPos
from etiketto.ui.settings import PrinterSettings, Settings

REAL_CONNECT = raw._connect  # before conftest's guard replaces it


# --- 1-bit rows -----------------------------------------------------------------------


def _image(width, height, black=()):
    image = Image.new("RGB", (width, height), "white")
    for xy in black:
        image.putpixel(xy, (0, 0, 0))
    return image


def test_bitmap_polarity_and_bit_order():
    image = _image(8, 2, black=[(0, 0), (7, 1)])
    ink = raw.bitmap(image, black_is_one=True)
    assert ink.rows == (b"\x80", b"\x01") and ink.bpr == 1 and ink.height == 2
    white = raw.bitmap(image, black_is_one=False)
    assert white.rows == (b"\x7f", b"\xfe")
    assert ink.blank == b"\x00" and white.blank == b"\xff"
    assert ink.data() == b"\x80\x01"


@pytest.mark.parametrize("black_is_one, pad", [(True, 0x00), (False, 0x01)])
def test_bitmap_pads_with_white(black_is_one, pad):
    # 9 dots: two bytes per row, the 7 extra bits are white
    row = raw.bitmap(_image(9, 1, black=[(8, 0)]), black_is_one).rows[0]
    if black_is_one:
        assert row == b"\x00\x80"
    else:
        assert row == b"\xff\x7f"
    assert row[1] & 0x7F == (0x7F if pad else 0x00)


def test_bitmap_crops_and_widens():
    image = _image(16, 1, black=[(0, 0), (15, 0)])
    assert raw.bitmap(image, True, width=8).rows == (b"\x80",)
    wide = raw.bitmap(image, True, width=24)
    assert wide.rows == (b"\x80\x01\x00",) and wide.width == 24


def test_bitmap_is_a_threshold_not_dithered():
    grey = Image.new("L", (16, 4), 100)  # dark grey: all black, no pattern
    assert set(raw.bitmap(grey, True).rows) == {b"\xff\xff"}
    light = Image.new("L", (16, 4), 200)
    assert set(raw.bitmap(light, True).rows) == {b"\x00\x00"}


def test_blank_runs_and_chunks():
    rows = (b"\x00", b"\x00", b"\x01", b"\x00", b"\x02", b"\x03")
    assert raw.blank_runs(rows, b"\x00") == [
        (0, 2, True),
        (2, 1, False),
        (3, 1, True),
        (4, 2, False),
    ]
    assert raw.blank_runs((), b"\x00") == []
    assert raw.chunks(600, 255) == [255, 255, 90]
    assert raw.chunks(255, 255) == [255]
    assert raw.chunks(0, 255) == []


def test_numbers_in_commands():
    assert [raw.percent_to(p, 15) for p in (-5, 0, 50, 100, 120)] == [0, 0, 7, 15, 15]
    assert [raw.mm_text(v) for v in (50.0, 12.5, 0.375, 101.6)] == ["50", "12.5", "0.38", "101.6"]


# --- questions ------------------------------------------------------------------------


class Talker:
    """A connection that answers in pieces, then stays silent or closes."""

    def __init__(self, pieces, end="timeout"):
        self.pieces = list(pieces)
        self.end = end
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        if self.pieces:
            return self.pieces.pop(0)
        if self.end == "closed":
            return b""
        if self.end == "usb":
            raise PrinterTimeout("USB printer did not answer")
        raise TimeoutError("timed out")


def test_ask_reads_until_the_answer_is_complete():
    talker = Talker([b"\x02ZD", b"621,V1,8\x03\r\n", b"next"])
    answer = raw.Session(talker).ask(b"~HI\n", raw.ends_with(b"\x03"))
    assert answer == b"\x02ZD621,V1,8\x03\r\n"
    assert talker.sent == [b"~HI\n"] and talker.pieces == [b"next"]
    assert talker.timeouts[-1] == raw.SOCKET_TIMEOUT  # back to the normal timeout
    assert all(t <= raw.ANSWER_TIMEOUT for t in talker.timeouts[:-1])


@pytest.mark.parametrize("end", ["timeout", "closed", "usb"])
def test_silence_is_no_error(end):
    assert raw.Session(Talker([], end)).ask(b"\x1b!?", raw.has_bytes(1)) == b""
    # a part of an answer is what there is
    assert raw.Session(Talker([b"\x02half"], end)).ask(b"~HS\n", raw.ends_with(b"\x03")) == (
        b"\x02half"
    )


def test_a_printer_that_keeps_talking_stops_at_the_timeout(monkeypatch):
    now = [0.0]

    class Chatty(Talker):
        def recv(self, size):
            now[0] += 0.4
            return b"x"

    monkeypatch.setattr(raw, "_clock", lambda: now[0])
    answer = raw.Session(Chatty([])).ask(b"~HS\n", raw.ends_with(b"\x03"), timeout=1.0)
    assert answer == b"xxx"


def test_an_unplugged_printer_is_an_error():
    class Gone(Talker):
        def recv(self, size):
            raise usb.UsbNotFound("gone")

    with pytest.raises(usb.UsbNotFound):
        raw.Session(Gone([])).ask(b"~HS\n", raw.ends_with(b"\x03"))


# --- connections ----------------------------------------------------------------------


def test_connect_uses_usb_for_usb_addresses(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usb, "connect", lambda host, timeout, wait=None: calls.append((host, timeout, wait)) or 1
    )
    assert REAL_CONNECT("usb://Zebra/ZD621", 9100) == 1
    assert REAL_CONNECT("usb://Zebra/ZD621", 9100, usb.PRINT_WAIT) == 1
    assert calls == [
        ("usb://Zebra/ZD621", raw.SOCKET_TIMEOUT, usb.BUSY_WAIT),
        ("usb://Zebra/ZD621", raw.SOCKET_TIMEOUT, usb.PRINT_WAIT),
    ]


@pytest.mark.parametrize(
    "error, cls",
    [
        (ConnectionRefusedError("refused"), PortClosed),
        (socket.gaierror("no such host"), PrinterNotFound),
        (TimeoutError("timed out"), PrinterTimeout),
        (OSError("cannot resolve x (mDNS)"), PrinterNotFound),
        (OSError("network unreachable"), PrinterNotFound),
        (ValueError("cannot resolve"), PrinterNotFound),
        (ValueError("odd"), PrinterError),
    ],
)
def test_map_error(error, cls):
    assert type(raw.map_error(error)) is cls


class Server(threading.Thread):
    """A raw printer on 127.0.0.1: answers the questions it knows, keeps
    everything it receives."""

    def __init__(self, answers):
        super().__init__(daemon=True)
        self.answers = answers
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = b""

    def run(self):
        connection, _address = self.listener.accept()
        with connection:
            connection.settimeout(5)
            pending = b""
            while chunk := connection.recv(65536):
                self.received += chunk
                pending += chunk
                for question, answer in self.answers.items():
                    if pending.endswith(question):
                        connection.sendall(answer)
                        pending = b""
        self.listener.close()


def test_a_zpl_printer_on_tcp(monkeypatch):
    monkeypatch.setattr(raw, "_connect", REAL_CONNECT)
    answers = {b"~HI\n": ZPL_HI, b"~HS\n": ZPL_HS, b"~HQES\n": ZPL_HQES}
    server = Server(answers)
    server.start()
    info = zpl.Zpl(port=server.port).query("127.0.0.1")
    server.join(5)
    assert (info.model, info.state) == ("ZD621-203dpi", "IDLE")
    assert server.received == b"~HI\n~HS\n~HQES\n"

    server = Server(answers)
    server.start()
    backend = zpl.Zpl(port=server.port)
    image = _image(16, 8, black=[(0, 0)])
    backend.print_label(image, "127.0.0.1", PrintOptions(copies=2))
    server.join(5)
    job = server.received.split(b"~HQES\n", 1)[1]
    assert job.startswith(b"~DGR:ETIKETTO.GRF,16,2,80,,::::::\n^XA\n")
    assert b"^PQ2,0,1,N\n" in job and job.endswith(b"^XZ\n")


def test_a_closed_port(monkeypatch):
    monkeypatch.setattr(raw, "_connect", REAL_CONNECT)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nobody listens there now
    with pytest.raises(PortClosed):
        Epl(port=port).query("127.0.0.1")


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A fake /sys/class/usbmisc; add(lpN, device id)."""
    root = tmp_path / "usbmisc"
    root.mkdir()
    monkeypatch.setattr(usb, "SYSFS", root)
    monkeypatch.setattr(usb, "DEV", Path("/dev/usb"))

    def add(name, ieee1284_id):
        device = root / name / "device"
        device.mkdir(parents=True)
        (device / "ieee1284_id").write_text(ieee1284_id)

    return add


@pytest.fixture
def usb_printer(sysfs, tmp_path, monkeypatch):
    """/dev/usb/lp0 is a pseudo terminal; the master end is the printer."""
    master, slave = os.openpty()
    tty.setraw(slave)
    (tmp_path / "lp0").symlink_to(os.ttyname(slave))
    monkeypatch.setattr(usb, "DEV", tmp_path)
    monkeypatch.setattr(raw, "_connect", REAL_CONNECT)
    monkeypatch.setattr(usb, "DRAIN_QUIET", 0.01)
    yield master
    os.close(master)
    os.close(slave)


def _answer_on(master, answers, received, last):
    """The printer side of the pseudo terminal, until it received `last`."""

    def run():
        pending = b""
        while True:
            chunk = os.read(master, 65536)
            received.append(chunk)
            pending += chunk
            if pending.endswith(last):
                return
            for question, answer in answers.items():
                if pending.endswith(question):
                    os.write(master, answer)
                    pending = b""

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_tspl_printer_on_usb(sysfs, usb_printer):
    sysfs("lp0", "MFG:Printer;MDL:Thermal Printer;CMD:XPP,XL;")
    backend = tspl.Tspl()
    found = backend.discover(timeout=0.01)
    assert found == [
        FoundPrinter(
            "Printer Thermal Printer (USB)",
            "usb://Printer/Thermal%20Printer",
            str(usb.DEV / "lp0"),
        )
    ]
    received: list[bytes] = []
    answers = {b"\x1b!?": b"\x00", b"~!T\r\n": b"X1038\r"}
    thread = _answer_on(usb_printer, answers, received, b"PRINT 1,1\n")
    info = backend.query(found[0].host)
    assert (info.model, info.state) == ("X1038", "IDLE")
    backend.print_label(_image(16, 8), found[0].host, PrintOptions())
    thread.join(5)
    assert not thread.is_alive()
    data = b"".join(received)
    assert data.startswith(b"\x1b!?~!T\r\n\x1b!?SIZE 2 mm,1 mm\n")
    assert data.endswith(b"PRINT 1,1\n")


def test_a_silent_printer_on_usb(sysfs, usb_printer, monkeypatch):
    sysfs("lp0", "MFG:Printer;MDL:Thermal Printer;CMD:XPP,XL;")
    monkeypatch.setattr(raw, "ANSWER_TIMEOUT", 0.05)
    info = tspl.Tspl().query("usb://Printer/Thermal%20Printer")
    assert (info.state, info.error, info.model) == ("UNKNOWN", "", "Thermal Printer")
    assert os.read(usb_printer, 100) == b"\x1b!?"  # asked once, the model not at all


# --- finding printers -----------------------------------------------------------------


def test_usb_printers_by_language(sysfs):
    sysfs("lp0", "MFG:Zebra Technologies;CMD:ZPL;MDL:ZTC ZD621-203dpi ZPL;")
    sysfs("lp1", "MFG:Zebra;MDL:LP 2844;CMD:EPL;")
    sysfs("lp2", "MFG:EPSON;MDL:TM-T88V;CMD:ESC/POS;")
    sysfs("lp3", "MFG:DYMO;MDL:LabelWriter 450;")
    sysfs("lp4", "MFG:DYMO;MDL:LabelWriter 550;")  # chip labels only
    sysfs("lp5", "MFG:Zebra;MDL:GK420d;")  # no command set: Zebra speaks ZPL
    sysfs("lp6", "MFG:cab;MDL:EOS2/300;CMD:JSCRIPT,ZPL;")  # cab printers speak ZPL too
    sysfs("lp7", "MFG:EPSON;MDL:ET-2850;CMD:ESCPL2,BDC;")  # an inkjet

    def names(backend):
        return [p.name for p in backend.discover(timeout=0.01)]

    assert names(zpl.Zpl()) == [
        "Zebra Technologies ZTC ZD621-203dpi ZPL (USB)",
        "Zebra GK420d (USB)",
        "cab EOS2/300 (USB)",
    ]
    assert names(Epl()) == ["Zebra LP 2844 (USB)"]
    assert names(EscPos()) == ["EPSON TM-T88V (USB)"]
    assert names(Dymo()) == ["DYMO LabelWriter 450 (USB)"]
    assert names(tspl.Tspl()) == []


def test_usb_command_sets(sysfs):
    sysfs("lp0", "MFG:Zebra;MDL:ZD;COMMAND SET: epl , zpl ;")
    printer = usb.printers()[0]
    assert printer.command_set == "epl , zpl" and printer.commands == ("EPL", "ZPL")


def test_network_printers_by_announcement(monkeypatch):
    services = [
        mdns.Service("Zebra ZD421 (ab:cd)", "zd421.local", "192.0.2.10", 9100, {}),
        mdns.Service("cab EOS2/300", "eos2.local", "192.0.2.11", 9100, {"usb_cmd": "JSCRIPT"}),
        mdns.Service(
            "Label printer",
            "tsc.local",
            "192.0.2.12",
            9100,
            {"usb_mfg": "TSC", "usb_mdl": "TE200", "usb_cmd": "TSPL2"},
        ),
        mdns.Service("TM-m30", "tm.local", "192.0.2.13", 9100, {"usb_cmd": "ESC/POS"}),
    ]
    searches = []

    def browse(service_type, match, timeout):
        searches.append((service_type, timeout))
        return [s for s in services if match(s.name)]

    monkeypatch.setattr(raw, "_browse", browse)
    assert zpl.Zpl().discover(timeout=0.5) == [
        FoundPrinter("Zebra ZD421 (ab:cd)", "zd421.local", "192.0.2.10")
    ]
    assert [p.host for p in tspl.Tspl().discover()] == ["tsc.local"]
    assert [p.host for p in EscPos().discover()] == ["tm.local"]
    assert Epl().discover() == []
    assert searches[0] == ("_pdl-datastream._tcp", 0.5)


# --- settings -------------------------------------------------------------------------


def test_choice_values():
    choice = Choice("media", ("gap", "mark", "continuous"))
    assert [choice.value(v) for v in ("mark", "junk", None, 3)] == ["mark", "gap", "gap", "gap"]


def test_level_values():
    level = Level("darkness", 0, 100, 10)
    assert list(level.steps()) == list(range(0, 101, 10))
    values = [None, True, "50", float("nan"), 55, 55.6, -3, 1000]
    assert [level.value(v) for v in values] == [None, None, None, None, 55, 56, 0, 100]


def test_backend_settings_from_options():
    backend = zpl.Zpl()
    options = PrintOptions(settings={"media": "mark", "darkness": 40, "dpi": "1200"})
    assert backend.setting(options, "media") == "mark"
    assert backend.setting(options, "darkness") == 40
    assert backend.setting(options, "speed") is None  # the printer's own
    assert backend.setting(options, "dpi") == "auto"  # unknown: the default
    with pytest.raises(KeyError):
        backend.setting(options, "colour")
    assert backend.level(options, "darkness") == 40 and backend.choice(options, "media") == "mark"
    assert Dymo().dpi(options) == 300.0  # a fixed resolution, no setting


def test_unknown_resolution_falls_back(monkeypatch):
    backend = Epl()
    monkeypatch.setattr(backend, "setting", lambda options, key: "999")
    assert backend.dpi(PrintOptions()) == raw.DPI["203"]


def test_printer_settings_hand_the_backend_settings_on(tmp_path, monkeypatch):
    from etiketto.ui import settings as config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    settings = PrinterSettings(Settings(), zpl.Zpl())
    assert settings.backend_settings() == {}
    settings.update(media="mark", darkness=None, speed=4, host="zebra.local")
    assert settings.backend_settings() == {"media": "mark", "speed": 4}
