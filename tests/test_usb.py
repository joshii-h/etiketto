"""USB printers: IEEE 1284 IDs from sysfs, usb:// addresses, the device file
used like a socket (a pseudo terminal stands in for /dev/usb/lp0)."""

from __future__ import annotations

import errno
import os
import tty
from pathlib import Path

import pytest

from etiketto.backends import cab, usb
from etiketto.backends.base import FoundPrinter, PrinterNotFound, PrinterTimeout, UsbPermission


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """A fake /sys/class/usbmisc; add(lpN, device id, usb serial)."""
    root = tmp_path / "usbmisc"
    root.mkdir()
    monkeypatch.setattr(usb, "SYSFS", root)
    monkeypatch.setattr(usb, "DEV", Path("/dev/usb"))

    def add(name, ieee1284_id=None, serial=None):
        device = root / name / "device"
        device.mkdir(parents=True)
        if ieee1284_id is not None:
            (device / "ieee1284_id").write_text(ieee1284_id)
        if serial is not None:
            (root / name / "serial").write_text(serial + "\n")  # device/../serial

    return add


def test_device_id_merges_short_and_long_keys():
    fields = usb.device_id("MFG:cab;MDL:EOS2/300;CMD:JSCRIPT,ZPL; SN : A1 ;junk;:x;")
    assert fields == {
        "MANUFACTURER": "cab",
        "MODEL": "EOS2/300",
        "COMMAND SET": "JSCRIPT,ZPL",
        "SERIALNUMBER": "A1",
    }
    assert usb.device_id("MANUFACTURER:Brother;MODEL:VC-500W;") == {
        "MANUFACTURER": "Brother",
        "MODEL": "VC-500W",
    }


def test_printers_from_sysfs(sysfs):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;SN:A1;")
    sysfs("lp1", "MANUFACTURER:Brother;MODEL:VC-500W;", serial="E78123")  # serial of the device
    sysfs("lp2")  # no device id: not a printer we can name
    assert usb.printers() == [
        usb.UsbPrinter(Path("/dev/usb/lp0"), "cab", "EOS2/300", "A1"),
        usb.UsbPrinter(Path("/dev/usb/lp1"), "Brother", "VC-500W", "E78123"),
    ]


def test_no_sysfs_no_printers(monkeypatch):
    monkeypatch.setattr(usb, "SYSFS", Path("/nonexistent/usbmisc"))
    assert usb.printers() == []


def test_address_like_cups_and_found_again(sysfs):
    sysfs("lp3", "MFG:cab;MDL:EOS2/300;SN:A 1/2;")
    printer = usb.printers()[0]
    assert printer.address == "usb://cab/EOS2%2F300?serial=A%201%2F2"
    assert usb.is_usb(printer.address) and not usb.is_usb("printer04.local")
    assert usb.find(printer.address) == printer
    assert usb.find("usb://cab/EOS2%2F300") == printer  # without serial: any of the model
    without = usb.UsbPrinter(Path("/dev/usb/lp0"), "Dymo", "LabelWriter 450")
    assert without.address == "usb://Dymo/LabelWriter%20450"
    with pytest.raises(PrinterNotFound, match="no USB printer"):
        usb.find("usb://cab/EOS2%2F300?serial=other")


# --- the device file ------------------------------------------------------------------


@pytest.fixture
def device():
    """A pseudo terminal: the slave is the 'device', the master the printer."""
    master, slave = os.openpty()
    tty.setraw(slave)  # bytes as they come, like a printer; no echo, no lines
    yield Path(os.ttyname(slave)), master
    os.close(master)
    os.close(slave)


def test_connection_sends_and_receives(device):
    path, printer = device
    connection = usb.Connection(path, timeout=2)
    connection.sendall(b"\x1bs")
    assert os.read(printer, 10) == b"\x1bs"
    os.write(printer, b"Y-000000N")
    assert connection.recv(64) == b"Y-000000N"
    connection.settimeout(0.05)
    with pytest.raises(PrinterTimeout, match="did not answer"):
        connection.recv(1)  # the printer says nothing
    connection.close()
    connection.close()  # twice is fine


def test_connection_errors(monkeypatch, tmp_path):
    def refuse(path, flags):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(usb.os, "open", refuse)
    with pytest.raises(UsbPermission, match="Permission denied"):
        usb.Connection(tmp_path / "lp0", 1)

    def gone(path, flags):
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(usb.os, "open", gone)
    with pytest.raises(PrinterNotFound):
        usb.Connection(tmp_path / "lp0", 1)

    def broken(path, flags):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(usb.os, "open", broken)
    with pytest.raises(OSError, match="Input/output"):
        usb.Connection(tmp_path / "lp0", 1)


def test_connect_finds_the_device(sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:EOS2/300;SN:A1;")
    opened = []
    monkeypatch.setattr(usb, "Connection", lambda device, timeout: opened.append(device) or "c")
    assert usb.connect("usb://cab/EOS2%2F300?serial=A1", 5) == "c"
    assert opened == [Path("/dev/usb/lp0")]


# --- the cab backend over USB ------------------------------------------------------------


def test_cab_uses_usb_for_usb_addresses(monkeypatch):
    monkeypatch.undo()  # the real cab._connect, not the test guard
    calls = []
    monkeypatch.setattr(usb, "connect", lambda host, timeout: calls.append(host) or "usb")
    assert cab._connect("usb://cab/EOS2%2F300", 9100) == "usb"
    assert calls == ["usb://cab/EOS2%2F300"]


def test_cab_finds_its_printers_on_usb(sysfs, monkeypatch):
    sysfs("lp0", "MFG:cab;MDL:SQUIX 4/300;SN:9;")
    sysfs("lp1", "MFG:HP;MDL:LaserJet;")  # not a cab
    monkeypatch.setattr(cab.mdns, "browse", lambda service_type, match, timeout: [])
    assert cab.Cab().discover(timeout=0.01) == [
        FoundPrinter("cab SQUIX 4/300 (USB)", "usb://cab/SQUIX%204%2F300?serial=9", "/dev/usb/lp0")
    ]
