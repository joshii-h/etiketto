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
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from tavletta.backends.base import (
    PrinterBusy,
    PrinterError,
    PrinterStalled,
    PrinterTimeout,
    UsbNotFound,
    UsbPermission,
)

SYSFS = Path("/sys/class/usbmisc")
USB_DEVICES = Path("/sys/bus/usb/devices")  # every USB device, also those that are no printer
DEV = Path("/dev/usb")
SCHEME = "usb://"

SLICE = 1.0  # waits are cut into pieces this long, to notice an unplugged printer
EMPTY_WAIT = 0.05  # after an empty read (a zero-length packet) before reading again
BUSY_WAIT = 5.0  # how long a status query waits for a printer another session uses
# how long a print waits for it: longer than the longest status query takes
# (a cab: draining 2 s plus three answers of 10 s; a VC-500W: draining 2 s
# plus two answers of 5 s that may run on for 10 s each), so that a print
# behind a hung query does not fail as busy
PRINT_WAIT = 45.0
# bytes per second a printer takes at least, on average: a print job may
# take timeout + its size / MIN_RATE to send (USB full speed carries about
# 1 MB/s; a VC-500W job is up to 10 MB)
MIN_RATE = 100_000
DRAIN_QUIET = 0.2  # stale bytes have ended when nothing comes for this long
DRAIN_LIMIT = 2.0  # a printer that keeps talking does not hold up the session longer
CLOSE_WAIT = 5.0  # at most, for the last write to reach the printer before closing
GONE = (errno.ENOENT, errno.ENODEV, errno.ENXIO)


@dataclass(frozen=True)
class UsbPrinter:
    device: Path  # /dev/usb/lp0
    manufacturer: str
    model: str
    serial: str = ""
    command_set: str = ""  # the printer languages, e.g. "ZPL" or "EPL,ZPL"

    @property
    def commands(self) -> tuple[str, ...]:
        """The command set as a list in capitals: ("EPL", "ZPL")."""
        return tuple(c.strip().upper() for c in self.command_set.split(",") if c.strip())

    @property
    def address(self) -> str:
        """usb://cab/EOS2%2F300?serial=1234, like CUPS."""
        uri = SCHEME + quote(self.manufacturer, safe="") + "/" + quote(self.model, safe="")
        return uri + ("?serial=" + quote(self.serial, safe="") if self.serial else "")


def device_id(text: str) -> dict[str, str]:
    """IEEE 1284 device ID "MFG:cab;MDL:EOS2/300;CMD:..." as a dict with the
    long and short key names merged (MANUFACTURER/MFG, MODEL/MDL, ...)."""
    aliases = {
        "MFG": "MANUFACTURER",
        "MDL": "MODEL",
        "SN": "SERIALNUMBER",
        "SERN": "SERIALNUMBER",
        "CMD": "COMMAND SET",
    }
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
                fields.get("COMMAND SET", ""),
            )
        )
    return found


def _usb_serial(node: Path) -> str:
    """The USB device's serial number (the interface's parent), if any."""
    try:
        return (node / "device" / ".." / "serial").read_text(errors="replace").strip()
    except OSError:
        return ""


def product_id(printer: UsbPrinter, vendor: int) -> int | None:
    """The USB product ID of a printer from that vendor (idVendor, idProduct
    of the USB device in sysfs); None if unknown or another vendor's."""
    node = SYSFS / printer.device.name / "device" / ".."
    ids = _ids(node)
    return ids[1] if ids and ids[0] == vendor else None


def _ids(node: Path) -> tuple[int, int] | None:
    try:
        vendor = int((node / "idVendor").read_text().strip(), 16)
        product = int((node / "idProduct").read_text().strip(), 16)
    except (OSError, ValueError):
        return None
    return vendor, product


def other_devices(vendor: int) -> list[int]:
    """Product IDs of that vendor's USB devices that are no printer for
    usblp (no /dev/usb/lpN), e.g. a printer that shows up as a drive."""
    found = []
    for node in sorted(USB_DEVICES.glob("*")):
        ids = _ids(node)
        if ids and ids[0] == vendor and not any(node.glob("*:*/usbmisc/lp*")):
            found.append(ids[1])
    return found


def is_usb(host: str) -> bool:
    return host.startswith(SCHEME)


def find(address: str) -> UsbPrinter:
    """The printer plugged in now that matches a usb:// address; maker and
    model in any case, as printers do not agree on it."""
    try:
        parts = urlsplit(address)
        serial = parse_qs(parts.query).get("serial", [""])[0]
    except ValueError as e:  # e.g. "usb://[x": not an address at all
        raise UsbNotFound("%s: %s" % (address, e)) from e
    maker, model = unquote(parts.netloc).casefold(), unquote(parts.path.lstrip("/")).casefold()
    for printer in printers():
        same = printer.manufacturer.casefold() == maker and printer.model.casefold() == model
        if same and (not serial or printer.serial == serial):
            return printer
    raise UsbNotFound("no USB printer %s" % address)


# usblp lets one open file use a printer at a time (EBUSY for the next); in
# Tavletta the status query and printing run in threads of their own, so they
# take turns here instead of failing
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _device_lock(device: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(device), threading.Lock())


def _device_error(device: Path, error: OSError) -> PrinterError:
    """An OS error of the device file: unplugged (UsbNotFound), or a failing
    transfer (EIO; it also comes with unplugging) as a generic problem."""
    text = "%s: %s" % (device, error)
    if error.errno in GONE or not device.exists():
        return UsbNotFound(text)
    return PrinterError(text)


def _open(device: Path, deadline: float) -> int:
    """The device file, non-blocking: every wait goes through select() with
    a deadline. Retries while another program has it open (EBUSY)."""
    while True:
        try:
            return os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as e:
            raise UsbPermission("%s: %s" % (device, e)) from e
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise _device_error(device, e) from e
            if time.monotonic() >= deadline:
                raise PrinterBusy("%s: %s" % (device, e)) from e
        time.sleep(0.1)


class Connection:
    """A USB printer's device file, used like a socket (sendall, recv).
    `wait`: how long to wait while another session or program uses it."""

    def __init__(self, device: Path, timeout: float, wait: float | None = None):
        self.device = device
        self.timeout = timeout
        # bytes per second a job may average at least (see sendall)
        self.min_rate: float = MIN_RATE
        self.fd = -1
        wait = BUSY_WAIT if wait is None else wait
        # one wait for both: another session here (the lock), then another
        # program (EBUSY)
        deadline = time.monotonic() + wait
        self.lock = _device_lock(device)
        if not self.lock.acquire(timeout=wait):
            raise PrinterBusy("%s is in use by another session" % device)
        try:
            self.fd = _open(device, deadline)
        except BaseException:
            self.lock.release()
            raise

    def sendall(self, data: bytes) -> None:
        """All of data, like socket.sendall. The printer may stop taking
        data (paper out, buffer full): PrinterStalled once it took nothing
        for the timeout. Every write that gets bytes through starts the
        timeout again, so a large job on a slow port goes through; one that
        trickles still ends after timeout + len(data) / min_rate."""
        start = time.monotonic()
        end = start + self.timeout + len(data) / self.min_rate
        idle = start + self.timeout
        view = memoryview(data)
        while view:
            self._wait(writing=True, deadline=min(idle, end), slow=end < idle)
            with self._errors():
                try:
                    written = self._write(view)
                except BlockingIOError:  # writable after all only for select()
                    continue
            if written:
                idle = time.monotonic() + self.timeout
            view = view[written:]

    def recv(self, size: int) -> bytes:
        """Up to size bytes; never b"": a silent printer times out. usblp
        returns b"" for a zero-length packet, which means nothing yet."""
        deadline = time.monotonic() + self.timeout
        while True:
            self._wait(writing=False, deadline=deadline)
            with self._errors():
                try:
                    data = self._read(size)
                except BlockingIOError:
                    data = b""
            if data:
                return data
            time.sleep(min(EMPTY_WAIT, max(0.0, deadline - time.monotonic())))

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def drain(self, quiet: float = DRAIN_QUIET, limit: float = DRAIN_LIMIT) -> None:
        """Read and drop what the printer still had to say (an answer
        nobody read): on USB nothing closes, so it would come first. Stops
        after `limit` seconds even if the printer keeps talking."""
        timeout, self.timeout = self.timeout, quiet
        end = time.monotonic() + limit
        try:
            while time.monotonic() < end:
                self.recv(4096)
        except PrinterTimeout:
            pass
        finally:
            self.timeout = timeout

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            # closing cancels a write still on its way to the printer (usblp
            # kills its URB): wait until the device takes data again
            select.select([], [self.fd], [], min(self.timeout, CLOSE_WAIT))
        except (OSError, ValueError):
            pass
        finally:
            os.close(self.fd)
            self.fd = -1
            self.lock.release()

    def _read(self, size: int) -> bytes:
        return os.read(self.fd, size)

    def _write(self, data: memoryview) -> int:
        return os.write(self.fd, data)

    @contextmanager
    def _errors(self) -> Iterator[None]:
        """OS errors of the device as PrinterErrors (see _device_error)."""
        try:
            yield
        except OSError as e:
            raise _device_error(self.device, e) from e

    def _wait(self, writing: bool, deadline: float, slow: bool = False) -> None:
        """Until the device can be read or written; at the deadline
        PrinterTimeout (nothing to read) or PrinterStalled (the printer
        takes no data, or with `slow` took it too slowly), UsbNotFound as
        soon as the device file is gone."""
        fds = [self.fd]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if writing and slow:
                    raise PrinterStalled("USB printer took data too slowly")
                if writing:
                    raise PrinterStalled("USB printer stopped taking data")
                raise PrinterTimeout("USB printer did not answer within %g s" % self.timeout)
            ready = select.select(
                [] if writing else fds, fds if writing else [], [], min(remaining, SLICE)
            )
            if ready[1 if writing else 0]:
                return
            if not self.device.exists():
                raise UsbNotFound("%s is gone" % self.device)


def connect(address: str, timeout: float, wait: float | None = None) -> Connection:
    """The printer's device file, with what it still had to say dropped;
    `wait` as for Connection (BUSY_WAIT, PRINT_WAIT for a print)."""
    connection = Connection(find(address).device, timeout, wait)
    try:
        connection.drain()
    except BaseException:
        connection.close()
        raise
    return connection
