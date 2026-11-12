"""A small black-and-white printer for tests: no cutter, one mode, no
remaining tape, no colour correction. Shows that the interface follows the
capabilities and that mono output is dithered."""

from __future__ import annotations

from tavletta.backends.base import (
    Backend,
    Capabilities,
    FoundPrinter,
    PrinterInfo,
    PrintMode,
    PrintOptions,
    Progress,
)


class FakeMono(Backend):
    id = "fake-mono"
    name = "Fake Mono 200"
    capabilities = Capabilities(
        tape_widths_mm=(12, 24),
        dpi=203,
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=(),
        reports_remaining=False,
        supports_color_adjust=False,
    )

    def __init__(self):
        self.jobs: list = []
        self.queries: list = []
        self.error: Exception | None = None
        self.info = PrinterInfo(self.name, 12, 1000, 2000, "IDLE")
        self.found = [FoundPrinter("Fake Mono", "mono.local", "192.0.2.200")]

    def discover(self, timeout: float = 3.0) -> list[FoundPrinter]:
        return list(self.found)

    def query(self, host: str) -> PrinterInfo:
        self.queries.append(host)
        if self.error:
            raise self.error
        return self.info

    def print_label(self, image, host, options: PrintOptions, progress=None) -> None:
        if self.error:
            raise self.error
        for n in range(1, options.copies + 1):
            if progress:
                progress(Progress(n, options.copies))
            self.jobs.append((image.copy(), host, options))
