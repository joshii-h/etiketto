"""Sending a print job over TCP without losing its end.

Two traps of plain sockets, both seen with slow raw printers:

- socket.sendall() with a timeout applies it to the whole call: a large job
  on a printer that takes data at printing pace fails after the timeout
  although data keeps flowing. send_all() restarts the timeout on every
  bit of progress and only caps the total (timeout + size / min_rate).
- close() with bytes received but not read (a late status answer) makes
  the kernel reset the connection, which drops what is still on its way to
  the printer. finish() half-closes the connection, reads and drops what
  comes until the printer closes its side or everything sent was taken.

Usable by every backend that prints over a socket.
"""

from __future__ import annotations

import fcntl
import socket
import struct
import termios
import time
from dataclasses import dataclass

from etiketto.backends.base import PrinterStalled

CHUNK = 65536
FINISH_QUIET = 1.0  # all data taken and nothing heard for this long: done
# all data taken, but the printer keeps talking and never closes: done
# after FINISH_QUIET + this
FINISH_GRACE = 3.0
POLL = 0.2  # seconds per read while finishing

_clock = time.monotonic  # replaced in tests
_sleep = time.sleep


def unsent(sock: socket.socket) -> int:
    """Bytes in the send queue the printer has not acknowledged yet (Linux:
    SIOCOUTQ); 0 where the system cannot tell."""
    try:
        buf = fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, struct.pack("i", 0))
    except (OSError, ValueError):
        return 0
    return struct.unpack("i", buf)[0]


def _send_some(sock: socket.socket, data: memoryview, total: int) -> int:
    """One send: the bytes taken, 0 after a timeout. A reset or broken pipe
    means the printer was there and stopped: PrinterStalled."""
    try:
        return sock.send(data[:CHUNK])
    except TimeoutError:
        return 0
    except ConnectionError as e:
        raise PrinterStalled(
            "connection lost after %d of %d bytes: %s" % (total - len(data), total, e)
        ) from e


def send_all(sock: socket.socket, data: bytes, timeout: float, min_rate: float) -> None:
    """All of data. PrinterStalled once the printer took nothing for
    `timeout` seconds, or when the whole job took longer than
    timeout + len(data) / min_rate (a printer that only trickles)."""
    start = _clock()
    end = start + timeout + len(data) / min_rate
    idle = start + timeout
    view = memoryview(data)
    try:
        while view:
            remaining = min(idle, end) - _clock()
            if remaining <= 0:
                raise PrinterStalled(
                    "printer took %d of %d bytes" % (len(data) - len(view), len(data))
                )
            sock.settimeout(remaining)
            sent = _send_some(sock, view, len(data))
            if sent:
                idle = _clock() + timeout
            view = view[sent:]
    finally:
        sock.settimeout(timeout)


def _listen(sock: socket.socket) -> bool | None:
    """One read while finishing: True if the printer said something, False
    if not, None once it closed its side. A reset is an error: the printer
    dropped what it had not taken yet."""
    try:
        return bool(sock.recv(4096)) or None
    except TimeoutError:
        return False
    except OSError as e:
        raise PrinterStalled("connection lost before the printer took the job: %s" % e) from e


@dataclass
class _Finishing:
    """How far finish() got: when the printer last took data or spoke."""

    timeout: float
    left: int
    progress: float
    quiet: float
    closed: bool = False  # the printer closed its side
    taken: float | None = None  # since when everything was taken

    def heard(self, said: bool | None, now: float) -> None:
        """What _listen() found out."""
        if said:
            self.quiet = now
        elif said is None:
            if self.closed:
                _sleep(POLL)  # recv returns at once now
            self.closed = True

    def done(self, outstanding: int, now: float) -> bool:
        """True once the printer has everything and closed, stayed quiet or
        talked on for the grace period."""
        if outstanding:
            self._taking(outstanding, now)
            return False
        self.taken = now if self.taken is None else self.taken
        return (
            self.closed
            or now - self.quiet >= FINISH_QUIET
            or now - self.taken >= FINISH_QUIET + FINISH_GRACE
        )

    def _taking(self, outstanding: int, now: float) -> None:
        """PrinterStalled if the printer took nothing for the timeout."""
        self.taken = None
        if outstanding < self.left:
            self.left, self.progress = outstanding, now
        elif now - self.progress >= self.timeout:
            raise PrinterStalled("printer stopped taking data, %d bytes not taken" % outstanding)


def finish(sock: socket.socket, timeout: float) -> None:
    """Half-close after a job and wait until the printer has it all: every
    byte acknowledged, and it closed its side, stayed quiet for
    FINISH_QUIET, or kept talking for FINISH_QUIET + FINISH_GRACE. What it
    says meanwhile is read and dropped, so that closing does not reset the
    connection. PrinterStalled if it takes nothing more for `timeout`
    seconds or resets the connection before it has everything (a printer
    that closes after part of the job also leaves bytes unacknowledged)."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError as e:
        raise PrinterStalled("connection lost before the printer took the job: %s" % e) from e
    sock.settimeout(POLL)
    now = _clock()
    state = _Finishing(timeout, unsent(sock), now, now)
    while True:
        state.heard(_listen(sock), _clock())
        if state.done(unsent(sock), _clock()):
            return
