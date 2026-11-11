"""USB printers through the kernel's printer class driver (usblp).

A printer on USB shows up as /dev/usb/lpN; its IEEE 1284 device ID (maker,
model, serial number) is in sysfs. Backends talk to it like to a network
socket: the same printer language, through a file instead of TCP.

Addresses follow CUPS: usb://maker/model?serial=..., so that the printer is
found again when it gets another lpN after being plugged in again.
"""

from __future__ import annotations

import errno
import os
import select
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from etiketto.backends.base import PrinterNotFound, PrinterTimeout, UsbPermission

SYSFS = Path("/sys/class/usbmisc")
DEV = Path("/dev/usb")
SCHEME = "usb://"


@dataclass(frozen=True)
class UsbPrinter:
    device: Path  # /dev/usb/lp0
    manufacturer: str
    model: str
    serial: str = ""

    @property
    def address(self) -> str:
        """usb://cab/EOS2%2F300?serial=1234, like CUPS."""
        uri = SCHEME + quote(self.manufacturer, safe="") + "/" + quote(self.model, safe="")
        return uri + ("?serial=" + quote(self.serial, safe="") if self.serial else "")


def device_id(text: str) -> dict[str, str]:
    """IEEE 1284 device ID "MFG:cab;MDL:EOS2/300;CMD:..." as a dict with the
    long and short key names merged (MANUFACTURER/MFG, MODEL/MDL, ...)."""
    aliases = {"MFG": "MANUFACTURER", "MDL": "MODEL", "SN": "SERIALNUMBER", "CMD": "COMMAND SET"}
    fields: dict[str, str] = {}
    for part in text.split(";"):
        key, sep, value = part.partition(":")
        if sep and key.strip():
            key = key.strip().upper()
            fields[aliases.get(key, key)] = value.strip()
    return fields


def printers() -> list[UsbPrinter]:
    """The USB printers plugged in now; [] where there is no sysfs."""
    found = []
    for node in sorted(SYSFS.glob("lp*")):
        try:
            fields = device_id((node / "device" / "ieee1284_id").read_text(errors="replace"))
        except OSError:
            continue
        serial = fields.get("SERIALNUMBER") or _usb_serial(node)
        found.append(
            UsbPrinter(
                DEV / node.name,
                fields.get("MANUFACTURER", ""),
                fields.get("MODEL", ""),
                serial,
            )
        )
    return found


def _usb_serial(node: Path) -> str:
    """The USB device's serial number (the interface's parent), if any."""
    try:
        return (node / "device" / ".." / "serial").read_text(errors="replace").strip()
    except OSError:
        return ""


def is_usb(host: str) -> bool:
    return host.startswith(SCHEME)


def find(address: str) -> UsbPrinter:
    """The printer plugged in now that matches a usb:// address."""
    parts = urlsplit(address)
    maker, model = unquote(parts.netloc), unquote(parts.path.lstrip("/"))
    serial = parse_qs(parts.query).get("serial", [""])[0]
    for printer in printers():
        same = printer.manufacturer == maker and printer.model == model
        if same and (not serial or printer.serial == serial):
            return printer
    raise PrinterNotFound("no USB printer %s" % address)


class Connection:
    """A USB printer's device file, used like a socket (sendall, recv)."""

    def __init__(self, device: Path, timeout: float):
        self.timeout = timeout
        try:
            self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY)
        except PermissionError as e:
            raise UsbPermission("%s: %s" % (device, e)) from e
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
                raise PrinterNotFound("%s: %s" % (device, e)) from e
            raise

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            self._wait(writing=True)
            written = os.write(self.fd, view)
            view = view[written:]

    def recv(self, size: int) -> bytes:
        """Up to size bytes; b"" never happens, a silent printer times out."""
        self._wait(writing=False)
        return os.read(self.fd, size)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def drain(self, quiet: float = 0.2) -> None:
        """Read and drop what the printer still had to say (an answer
        nobody read): on USB nothing closes, so it would come first."""
        timeout, self.timeout = self.timeout, quiet
        try:
            while True:
                self.recv(4096)
        except PrinterTimeout:
            pass
        finally:
            self.timeout = timeout

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _wait(self, writing: bool) -> None:
        fds = [self.fd]
        readable, writable, _ = select.select(
            [] if writing else fds, fds if writing else [], [], self.timeout
        )
        if not (writable if writing else readable):
            raise PrinterTimeout("USB printer did not answer within %g s" % self.timeout)


def connect(address: str, timeout: float) -> Connection:
    return Connection(find(address).device, timeout)
