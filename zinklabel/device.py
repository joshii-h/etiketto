"""Talk to the VC-500W through labelprinter (native protocol, port 9100)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from zinklabel.model import TAPE_WIDTHS_MM

DEFAULT_PORT = 9100
PRINT_TIMEOUT = 180


@dataclass
class PrinterInfo:
    model: str
    tape_mm: int | None  # nominal cassette width, None = no cassette
    remaining_mm: int | None
    total_mm: int | None
    state: str


def nominal_width(width_mm: float) -> int:
    return min(TAPE_WIDTHS_MM, key=lambda w: abs(w - width_mm))


def configured_host() -> str | None:
    """Host from labelprinter's config (~/.config/labelprinter/config.json)."""
    config = Path.home() / ".config" / "labelprinter" / "config.json"
    try:
        return json.loads(config.read_text()).get("host") or None
    except (OSError, ValueError):
        return None


def _connect(host: str, port: int):
    from labelprinter.connection import Connection
    from labelprinter.printer import LabelPrinter

    connection = Connection(host, port, PRINT_TIMEOUT)
    return connection, LabelPrinter(connection)


def query(host: str, port: int = DEFAULT_PORT) -> PrinterInfo:
    connection, printer = _connect(host, port)
    try:
        config = printer.get_configuration()
        status = printer.get_status()
    finally:
        connection.close()
    tape = nominal_width(config.tape_width * 25.4) if config.tape_width else None
    remaining = total = None
    if config.tape_length_initial and status.tape_length_remaining != -1.0:
        total = int(config.tape_length_initial * 2.54)
        remaining = int(status.tape_length_remaining * 2.54)
    return PrinterInfo(config.model or "VC-500W", tape, remaining, total, status.print_state)


def print_label(
    image: Image.Image,
    host: str,
    port: int = DEFAULT_PORT,
    mode: str = "vivid",
    cut: str = "full",
    copies: int = 1,
    progress=None,
) -> None:
    """Print an image in printer orientation (width = tape width)."""
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="zinklabel-")
    os.close(fd)
    try:
        dpi = 317 if mode == "vivid" else 264
        image.convert("RGB").save(path, quality=95, dpi=(dpi, dpi))
        connection, printer = _connect(host, port)
        try:
            for n in range(1, copies + 1):
                if progress:
                    progress(n, copies)
                status = printer.get_status()
                if status.print_state != "IDLE":
                    printer.wait_to_turn_idle(PRINT_TIMEOUT)
                with open(path, "rb") as handle:
                    printer.print_jpeg(handle, mode, cut)
                printer.wait_to_turn_idle(PRINT_TIMEOUT)
        finally:
            connection.close()
    finally:
        os.unlink(path)
