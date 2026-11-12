"""Printers on a USB serial port (CDC ACM): /dev/ttyACM0 and the like.

Some label printers do not use the printer class (usblp, /dev/usb/lpN) but
show up as a modem-like serial port. The kernel's cdc_acm driver makes it
/dev/ttyACMn; the USB device's IDs and strings are in sysfs next to it, and
udev adds a stable name under /dev/serial/by-id/. Only the standard library
is used: termios for the line settings, ioctl for the modem lines.

The device node usually belongs to the group "dialout" (Fedora, Debian,
Ubuntu) or "uucp" (Arch), not "lp" as printers on usblp do.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import stat
import struct
import termios
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from tavletta.backends.base import (
    PrinterBusy,
    PrinterError,
    PrinterStalled,
    SerialPermission,
    UsbNotFound,
)

SYSFS = Path("/sys/class/tty")
DEV = Path("/dev")
BY_ID = Path("/dev/serial/by-id")
PATTERN = "ttyACM*"

SLICE = 1.0  # waits are cut into pieces this long, to notice an unplugged printer
BUSY_WAIT = 5.0  # how long a status query waits for a port another session uses
PRINT_WAIT = 30.0  # how long a print waits for it (longer than a status query takes)
MIN_RATE = 10_000  # bytes per second a printer takes at least while a job is sent
GONE = (errno.ENOENT, errno.ENODEV, errno.ENXIO)


@dataclass(frozen=True)
class SerialDevice:
    """A CDC ACM port and the USB device behind it."""

    device: Path  # /dev/ttyACM0
    vendor_id: int
    product_id: int
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    by_id: Path | None = None  # /dev/serial/by-id/usb-..., stable across replugging

    @property
    def address(self) -> str:
        """The stable name if udev made one, else the device file."""
        return str(self.by_id or self.device)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _hex(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        return 0


def _stable_names() -> dict[str, Path]:
    """ttyACM0 -> /dev/serial/by-id/usb-...: the links udev made."""
    names: dict[str, Path] = {}
    try:
        links = sorted(BY_ID.iterdir())
    except OSError:
        return names
    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        names.setdefault(Path(target).name, link)
    return names


def devices() -> list[SerialDevice]:
    """The CDC ACM ports plugged in now; [] where there is no sysfs.

    /sys/class/tty/ttyACM0/device is the USB interface; its parent, the
    USB device, has the IDs and strings."""
    found = []
    stable = _stable_names()
    for node in sorted(SYSFS.glob(PATTERN)):
        usb_device = node / "device" / ".."
        vendor = _read(usb_device / "idVendor")
        if not vendor:
            continue  # not on USB, or sysfs went away meanwhile
        found.append(
            SerialDevice(
                DEV / node.name,
                _hex(vendor),
                _hex(_read(usb_device / "idProduct")),
                _read(usb_device / "manufacturer"),
                _read(usb_device / "product"),
                _read(usb_device / "serial"),
                stable.get(node.name),
            )
        )
    return found


USB_TTY = re.compile(r"tty(ACM|USB)\d+")


def device_path(host: str) -> Path:
    """The device file of an address: a path, or a bare name ("ttyACM0").
    Only USB serial ports (ttyACM, ttyUSB, or a /dev/serial link to one)
    are taken; UsbNotFound for anything else, e.g. a host name typed by
    mistake, a pseudo terminal or a built-in serial port."""
    host = host.strip()
    path = DEV / host if host.startswith("tty") and "/" not in host else Path(host)
    if not path.is_absolute():
        raise UsbNotFound("%r is not a serial port (e.g. /dev/ttyACM0)" % host)
    real = Path(os.path.realpath(path))
    linked = path.is_relative_to(DEV / "serial")  # a stable name, maybe unplugged
    if not USB_TTY.fullmatch(real.name) and not (linked and real == path):
        raise UsbNotFound("%s is not a USB serial port (e.g. /dev/ttyACM0)" % host)
    with suppress(OSError):  # missing: the open says so
        if not stat.S_ISCHR(real.stat().st_mode):
            raise UsbNotFound("%s is not a device" % host)
    return path


# a status query and a print run in threads of their own: they take turns
# here instead of fighting over the port
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _port_lock(device: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(device), threading.Lock())


def _device_error(device: Path, error: OSError) -> PrinterError:
    text = "%s: %s" % (device, error)
    if error.errno in GONE or not device.exists():
        return UsbNotFound(text)
    return PrinterError(text)


def _open(device: Path, deadline: float) -> int:
    """Non-blocking: every wait goes through select() with a deadline.
    Retries while another program holds the port exclusively (EBUSY)."""
    while True:
        try:
            return os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as e:
            raise SerialPermission("%s: %s" % (device, e)) from e
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise _device_error(device, e) from e
            if time.monotonic() >= deadline:
                raise PrinterBusy("%s: %s" % (device, e)) from e
        time.sleep(0.1)


def configure(fd: int, baud: int = termios.B115200) -> None:
    """Raw 8N1 at `baud`, no flow control, reads that never block in the
    driver; then assert DTR and RTS (some printers only talk with both
    set) and keep other programs (ModemManager) off the port."""
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag: no input processing, no XON/XOFF
    attrs[1] = 0  # oflag: no output processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8N1, no RTS/CTS
    attrs[3] = 0  # lflag: no echo, no line editing, no signals
    attrs[4] = attrs[5] = baud
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)  # stale answers would come first
    lines = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
    _ioctl(fd, termios.TIOCMBIS, lines)
    _ioctl(fd, termios.TIOCEXCL)


def _ioctl(fd: int, request: int, arg: bytes | int = 0) -> None:
    """Modem lines and exclusive mode are a bonus: a pseudo terminal (the
    tests) or an unusual driver may not have them."""
    with suppress(OSError):
        fcntl.ioctl(fd, request, arg)


class Port:
    """An open serial port: write() and read() with deadlines.
    `wait`: how long to wait while another session or program uses it."""

    def __init__(self, device: Path, wait: float = BUSY_WAIT):
        self.device = device
        self.fd = -1
        self.lock = _port_lock(device)
        if not self.lock.acquire(timeout=wait):
            raise PrinterBusy("%s is in use by another session" % device)
        self.locked = True
        try:
            self.fd = _open(device, time.monotonic() + wait)
            try:
                configure(self.fd)
            except termios.error as e:  # not a terminal at all, e.g. a disk
                raise PrinterError("%s: %s" % (device, e)) from e
        except BaseException:
            self.close()
            raise

    def write(self, data: bytes, timeout: float) -> None:
        """All of data. PrinterStalled once the port took nothing for
        `timeout`; at most timeout + len(data) / MIN_RATE in all."""
        start = time.monotonic()
        end = start + timeout + len(data) / MIN_RATE
        idle = start + timeout
        view = memoryview(data)
        while view:
            self._wait(writing=True, deadline=min(idle, end))
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                continue
            except OSError as e:
                raise _device_error(self.device, e) from e
            if written:
                idle = time.monotonic() + timeout
            view = view[written:]

    def read(self, deadline: float) -> bytes:
        """What the printer sent, at least one byte; b"" at the deadline."""
        while True:
            if not self._wait(writing=False, deadline=deadline):
                return b""
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                data = b""
            except OSError as e:
                raise _device_error(self.device, e) from e
            if data:
                return data
            # readable without data: the line hung up, the printer is gone
            if not self.device.exists():
                raise UsbNotFound("%s is gone" % self.device)
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.01)

    def close(self) -> None:
        if self.fd >= 0:
            try:
                _ioctl(self.fd, termios.TIOCNXCL)
            finally:
                os.close(self.fd)
                self.fd = -1
        if self.locked:  # once: the lock may belong to another session by now
            self.locked = False
            self.lock.release()

    def _wait(self, writing: bool, deadline: float) -> bool:
        """Until the port can be written (else PrinterStalled at the
        deadline) or read (else False); UsbNotFound once the device is gone."""
        fds = [self.fd]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if writing:
                    raise PrinterStalled("%s stopped taking data" % self.device)
                return False
            ready = select.select(
                [] if writing else fds, fds if writing else [], [], min(remaining, SLICE)
            )
            if ready[1 if writing else 0]:
                return True
            if not self.device.exists():
                raise UsbNotFound("%s is gone" % self.device)
