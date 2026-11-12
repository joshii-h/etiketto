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

from etiketto.backends.base import PrinterStalled

CHUNK = 65536
FINISH_QUIET = 1.0  # all data taken and nothing heard for this long: done
POLL = 0.2  # seconds per read while finishing

_clock = time.monotonic  # replaced in tests


def unsent(sock: socket.socket) -> int:
    """Bytes in the send queue the printer has not acknowledged yet (Linux:
    SIOCOUTQ); 0 where the system cannot tell."""
    try:
        buf = fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, struct.pack("i", 0))
    except (OSError, ValueError):
        return 0
    return struct.unpack("i", buf)[0]


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
            try:
                sent = sock.send(view[:CHUNK])
            except TimeoutError:
                continue
            if sent:
                idle = _clock() + timeout
            view = view[sent:]
    finally:
        sock.settimeout(timeout)


def _listen(sock: socket.socket) -> bool | None:
    """One read while finishing: True if the printer said something, False
    if not, None once it closed its side (or the connection is gone)."""
    try:
        return bool(sock.recv(4096)) or None
    except TimeoutError:
        return False
    except OSError:
        return None


def finish(sock: socket.socket, timeout: float) -> None:
    """Half-close after a job and wait until the printer has it: it closes
    its side, or it acknowledged everything and stayed quiet for
    FINISH_QUIET. What it says meanwhile is read and dropped, so that
    closing does not reset the connection. PrinterStalled if the printer
    takes nothing more for `timeout` seconds."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return  # already gone: nothing left to protect
    sock.settimeout(POLL)
    left = unsent(sock)
    progress = quiet = _clock()
    while (heard := _listen(sock)) is not None:
        now = _clock()
        if heard:
            quiet = now
        outstanding = unsent(sock)
        if outstanding < left:
            left, progress = outstanding, now
        if not outstanding and now - quiet >= FINISH_QUIET:
            return
        if outstanding and now - progress >= timeout:
            raise PrinterStalled("printer stopped taking data, %d bytes not taken" % outstanding)
