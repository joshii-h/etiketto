"""Receipt and label printers with ESC/POS on 58 or 80 mm paper rolls, on
TCP port 9100 or USB (see docs/escpos.md).

The label is printed as raster images (GS v 0) on endless paper; blank rows
are fed (ESC J) instead of sent. The sequence follows LPrint by Michael R
Sweet (Apache-2.0), written anew; status (DLE EOT) and cut (GS V) follow
Epson's ESC/POS reference.
"""

from __future__ import annotations

from PIL import Image

from etiketto.backends.base import (
    Backend,
    Capabilities,
    Choice,
    CoverOpen,
    OutOfPaper,
    PrinterError,
    PrinterInfo,
    PrintMode,
    PrintOptions,
    Raster,
    Toggle,
    check_length,
)
from etiketto.backends.raw import (
    DPI,
    DPI_INFO,
    Bitmap,
    RawBackend,
    Session,
    bitmap,
    blank_runs,
    chunks,
    has_bytes,
    state,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM

ESC, GS, DLE = b"\x1b", b"\x1d", b"\x10"
# the printable width of a paper roll: the print head is narrower than the
# paper (384 dots at 203 dpi on 58 mm paper, 576 on 80 mm)
PRINTABLE_MM = {58: 48.0, 80: 72.0}
MAX_FEED = 255  # dots per ESC J
BLOCK_BYTES = 32768  # at most this much image data in one GS v 0 (LPrint)
CUT = {"full": GS + b"V\x00", "half": GS + b"V\x01", "none": b""}

# DLE EOT n answers one byte with bits 1 and 4 set, 0 and 7 clear
FIXED_MASK, FIXED_BITS = 0x93, 0x12
COVER_OPEN, PAPER_STOP, ERROR = 0x04, 0x20, 0x40  # n = 2: why it is offline
PAPER_END = 0x60  # n = 4: the roll paper sensor


def head_dots(tape_mm: int, dpi: float) -> int:
    """Dots across the printable width, in whole bytes."""
    printable = PRINTABLE_MM.get(tape_mm, tape_mm - 10.0)
    return max(8, round(printable * dpi / 25.4 / 8) * 8)


def raster_block(image: Bitmap, first: int, count: int) -> bytes:
    """GS v 0: width in bytes and height in dots (little endian), the rows."""
    data = b"".join(image.rows[first : first + count])
    return GS + b"v0\x00" + image.bpr.to_bytes(2, "little") + count.to_bytes(2, "little") + data


def page(image: Bitmap, dpi: float, cut: str = "none") -> bytes:
    """One label: images for the rows with ink, feeds for the blank ones
    (also at the start: the spacing belongs to the label, where LPrint
    drops it), an inch of paper to bring it past the cutter, the cut."""
    out = []
    per_block = max(1, BLOCK_BYTES // image.bpr)
    for first, count, empty in blank_runs(image.rows, image.blank):
        if empty:
            out += [ESC + b"J" + bytes([n]) for n in chunks(count, MAX_FEED)]
            continue
        for offset in range(0, count, per_block):
            out.append(raster_block(image, first + offset, min(per_block, count - offset)))
    out.append(ESC + b"J" + bytes([min(MAX_FEED, round(dpi))]))
    out.append(CUT.get(cut, b""))
    return b"".join(out)


def build_job(image: Bitmap, dpi: float, copies: int = 1, cut: str = "none") -> bytes:
    """Reset (ESC @), motion units of one dot (GS P: else ESC J counts in
    1/180 in on some printers, 1/203 in on others), no left margin
    (GS L 0), each copy, reset again."""
    units = bytes([min(255, round(dpi))]) * 2
    body = page(image, dpi, cut) * max(1, copies)
    return ESC + b"@" + GS + b"P" + units + GS + b"L\x00\x00" + body + ESC + b"@"


def parse_status(raw: bytes) -> int | None:
    """One DLE EOT answer; None if nothing or not a status byte came."""
    if len(raw) != 1 or raw[0] & FIXED_MASK != FIXED_BITS:
        return None
    return raw[0]


def status_error(offline: int | None, paper: int | None) -> PrinterError | None:
    """DLE EOT 2 (offline cause) and 4 (paper sensor); None = not known."""
    if offline is not None and offline & COVER_OPEN:
        return CoverOpen("DLE EOT 2 %02X: cover open" % offline)
    if (paper is not None and paper & PAPER_END) or (offline is not None and offline & PAPER_STOP):
        return OutOfPaper("DLE EOT %s: paper end" % _raw(offline, paper))
    if offline is not None and offline & ERROR:
        return PrinterError("DLE EOT 2 %02X: error" % offline)
    return None


def _raw(offline: int | None, paper: int | None) -> str:
    return "2 %s, 4 %s" % tuple("--" if b is None else "%02X" % b for b in (offline, paper))


class EscPos(RawBackend):
    id = "escpos"
    name = "ESC/POS"  # the interface shows a translated name
    command_sets = ("ESC/POS", "ESCPOS")
    capabilities = Capabilities(
        tape_widths_mm=(58, 80),
        dpi=DPI["203"],
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=("full", "half", "none"),
        continuous=True,
        cassettes=False,
        data_formats=("escpos",),
        settings=(Choice("dpi", ("203", "180")), Toggle("upside_down")),
    )

    def raster(self, tape_mm: float, options: PrintOptions) -> Raster:
        dpi = self.dpi(options)
        return Raster(dpi, tape_px=head_dots(round(tape_mm), dpi))

    def prepare(
        self, label: Label, options: PrintOptions, safe_mm: float = DEFAULT_SAFE_MM
    ) -> Image.Image:
        """Label space, like any endless tape (x along the paper): the
        plain Backend.prepare(), not RawBackend's die-cut one."""
        image = Backend.prepare(self, label, options, safe_mm)
        image.info[DPI_INFO] = self.dpi(options)
        return image

    def check(self, image: Image.Image, dpi: float, host: str) -> None:
        check_length(image.width / dpi * 25.4, self.capabilities)

    def _status(self, s: Session) -> tuple[int | None, int | None]:
        offline = parse_status(s.ask(DLE + b"\x04\x02", has_bytes(1)))
        paper = parse_status(s.ask(DLE + b"\x04\x04", has_bytes(1)))
        return offline, paper

    def ask_status(self, s: Session) -> PrinterInfo:
        offline, paper = self._status(s)
        error = status_error(offline, paper)
        answered = offline is not None or paper is not None
        return PrinterInfo(
            "", None, None, None, state(answered, error), error=error.code if error else ""
        )

    def problem(self, s: Session) -> PrinterError | None:
        return status_error(*self._status(s))

    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        # label space to printer rows: the start of the label leaves first
        rows = image.rotate(-90, expand=True)
        cut = options.cut if options.cut in self.capabilities.cut_modes else "none"
        return build_job(bitmap(rows, black_is_one=True), dpi, options.copies, cut)
