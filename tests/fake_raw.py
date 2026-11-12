"""A fake raw label printer (ZPL, EPL2, TSPL, ESC/POS, DYMO) on the socket
level: it answers the questions it knows, everything else is a job. Never
a real device and never the network."""

from __future__ import annotations

import re
from types import SimpleNamespace

from tavletta.backends import raw

# realistic answers; framing and fields as the manuals describe them
# ~HI: model, firmware, dots per mm, memory, options (5 fields)
ZPL_HI = b"\x02ZD621-203dpi,V93.21.18Z,8,8192KB,\x03\r\n"
ZPL_HS = (
    b"\x02030,0,0,1245,000,0,0,0,000,0,0,0\x03\r\n"
    b"\x02000,0,0,0,1,2,6,0,00000000,1,000\x03\r\n"
    b"\x021234,0\x03\r\n"
)
ZPL_HS_PAPER_OUT = ZPL_HS.replace(b"030,0,0,1245", b"030,1,0,1245")
ZPL_HQES = (
    b"\x02\r\n PRINTER STATUS                \r\n"
    b"   ERRORS:         0 00000000 00000000\r\n"
    b"   WARNINGS:       0 00000000 00000000\r\n\x03\r\n"
)


# every question the backends ask; anything else sent is (part of) a job
QUESTIONS = {
    b"~HI\n",
    b"~HS\n",
    b"~HQES\n",
    b"\x1b!?",
    b"~!T\r\n",
    b"\x10\x04\x02",
    b"\x10\x04\x04",
    b"\x1bA",
}


class FakePrinter:
    """answers: question -> answer bytes; questions without an answer stay
    silent (recv times out), like a printer that does not know them."""

    def __init__(self, answers: dict[bytes, bytes] | None = None):
        self.answers = dict(answers or {})
        self.requests: list[bytes] = []
        self.jobs: list[bytes] = []
        self.closed = 0
        self.refuse = False  # the connection is refused (switched off)


class FakeSocket:
    def __init__(self, printer: FakePrinter):
        self.printer = printer
        self.out = b""
        self.timeouts: list[float] = []

    def sendall(self, data: bytes) -> None:
        printer = self.printer
        if data in QUESTIONS:
            printer.requests.append(data)
            self.out += printer.answers.get(data, b"")
        else:
            printer.jobs.append(data)

    def recv(self, size: int) -> bytes:
        if not self.out:
            raise TimeoutError("timed out")
        chunk, self.out = self.out[:size], self.out[size:]
        return chunk

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.printer.closed += 1


def install(monkeypatch, printer: FakePrinter) -> SimpleNamespace:
    """The fake behind raw._connect; returns the connection calls."""
    calls: list = []

    def connect(host, port, wait=None):
        calls.append((host, port, wait))
        if printer.refuse:
            raise ConnectionRefusedError("connection refused")
        return FakeSocket(printer)

    monkeypatch.setattr(raw, "_connect", connect)
    return SimpleNamespace(printer=printer, calls=calls)


# how many labels a job asks for, per language
COUNTERS = {
    "zpl": lambda job: sum(int(n) for n in re.findall(rb"\^PQ(\d+),", job)),
    "epl": lambda job: sum(int(n) for n in re.findall(rb"\nP(\d+)\n$", job)),
    "tspl": lambda job: sum(int(n) for n in re.findall(rb"PRINT (\d+),1\n$", job)),
    "escpos": lambda job: job.count(b"\x1dV"),
    "dymo": lambda job: job.count(b"\x1bQ\x00\x00\x1bB\x00"),
}
