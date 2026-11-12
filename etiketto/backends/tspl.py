"""TSPL and TSPL2 label printers: TSC and the many 4 × 6 in shipping label
printers that speak it (Munbyn, iDPRT, Xprinter, Rollo, ...), on TCP port
9100 or USB (see docs/tspl.md).

The label is one BITMAP command with raw rows, 0 = black, as LPrint by
Michael R Sweet (Apache-2.0) sends it; written anew. The status question
(ESC !?) and the model name (~!T) come from TSC's manual; many compatible
printers answer neither, which leaves the state unknown.
"""

from __future__ import annotations

from PIL import Image

from etiketto.backends.base import (
    Capabilities,
    Choice,
    HeadOpen,
    LabelSize,
    Level,
    OutOfLabels,
    PaperJam,
    PrinterError,
    PrinterInfo,
    PrinterPaused,
    PrintMode,
    PrintOptions,
    RibbonError,
)
from etiketto.backends.raw import (
    DPI,
    Bitmap,
    RawBackend,
    Session,
    bitmap,
    ends_with,
    has_bytes,
    mm_text,
    state,
)

# 4 inch heads; TSC gives 108 mm as the widest print width at 203 dpi
MAX_WIDTH_MM = 108.0
LABEL_SIZES = (
    LabelSize(101.6, 152.4),  # 4 × 6 in, shipping labels
    LabelSize(100, 150),
    LabelSize(101.6, 101.6),
    LabelSize(76.2, 50.8),
    LabelSize(57.15, 31.75),
    LabelSize(50.8, 25.4),
    LabelSize(50, 30),
    LabelSize(40, 30),
)
# what to do after a label; "printer" sends nothing
FINISH = {"tear": "SET TEAR ON", "peel": "SET PEEL ON", "cut": "SET CUTTER 1"}

# ESC !? answers one byte; the bits combine (TSC's manual)
STATUS_BITS: tuple[tuple[int, type[PrinterError], str], ...] = (
    (0x04, OutOfLabels, "out of paper"),
    (0x01, HeadOpen, "head open"),
    (0x02, PaperJam, "paper jam"),
    (0x08, RibbonError, "out of ribbon"),
    (0x10, PrinterPaused, "paused"),
    (0x80, PrinterError, "other error"),
)
PRINTING = 0x20
ESC = b"\x1b"


def media_command(media: str, gap_mm: float) -> str:
    """GAP for labels with gaps, BLINE for black marks, GAP 0 for
    continuous media. LPrint always sends 3 mm; this is the label's gap."""
    if media == "continuous":
        return "GAP 0 mm,0 mm"
    keyword = "BLINE" if media == "mark" else "GAP"
    return "%s %s mm,0 mm" % (keyword, mm_text(gap_mm))


def build_job(
    image: Bitmap,
    dots_per_mm: float,
    copies: int = 1,
    media: str = "gap",
    gap_mm: float = 3.0,
    finish: str = "printer",
    darkness: int | None = None,
    speed: int | None = None,
) -> bytes:
    """SIZE in mm, the sensor, the label as BITMAP x,y,bytes per row,
    height,mode 1 and its raw bytes, then PRINT copies,1. The bytes follow
    the last comma; their number is known, PRINT follows them directly."""
    lines = [
        "SIZE %s mm,%s mm"
        % (mm_text(image.width / dots_per_mm), mm_text(image.height / dots_per_mm)),
        media_command(media, gap_mm),
        "DIRECTION 0,0",
    ]
    if darkness is not None:
        lines.append("DENSITY %d" % ((darkness * 15 + 50) // 100))
    if speed is not None:
        lines.append("SPEED %d" % speed)
    if finish in FINISH:
        lines.append(FINISH[finish])
    lines += ["CLS", "BITMAP 0,0,%d,%d,1," % (image.bpr, image.height)]
    text = "\n".join(lines).encode()
    return text + image.data() + b"PRINT %d,1\n" % max(1, copies)


def parse_status(raw: bytes) -> int | None:
    """ESC !?: one byte; None if nothing came. A longer answer is not one."""
    return raw[0] if len(raw) == 1 else None


def status_error(status: int) -> PrinterError | None:
    for bit, cls, text in STATUS_BITS:
        if status & bit:
            return cls("ESC !? %02X: %s" % (status, text))
    return None


def parse_model(raw: bytes) -> str:
    """~!T: the model name, e.g. "TTP-244 Plus"; printable text only."""
    text = raw.decode("ascii", "replace").split("\r")[0].split("\n")[0]
    text = "".join(c for c in text if c.isprintable() and c != "�").strip()
    return text[:64]


class Tspl(RawBackend):
    id = "tspl"
    name = "TSPL (TSC, Munbyn, iDPRT, Xprinter, Rollo)"
    # "XPP,XL" is what several of the compatible printers name (LPrint's list)
    command_sets = ("TSPL", "TSPL2", "XPP", "XL")
    makers = ("TSC",)
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI["203"],
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=MAX_WIDTH_MM,
        data_formats=("tspl",),
        settings=(
            Choice("dpi", ("203", "300")),
            Choice("media", ("gap", "mark", "continuous")),
            Choice("finish", ("printer", "tear", "peel", "cut")),
            Level("darkness", 0, 100, 10),
            Level("speed", 1, 12),
        ),
    )

    def display_name(self, info: PrinterInfo | None) -> str:
        return info.model if info and info.model else self.name

    def _status(self, s: Session) -> int | None:
        return parse_status(s.ask(ESC + b"!?", has_bytes(1)))

    def ask_status(self, s: Session) -> PrinterInfo:
        status = self._status(s)
        error = status_error(status) if status is not None else None
        # a printer that does not answer the status will not name itself
        model = parse_model(s.ask(b"~!T\r\n", ends_with(b"\r"))) if status is not None else ""
        busy = status is not None and bool(status & PRINTING)
        return PrinterInfo(
            model,
            None,
            None,
            None,
            state(status is not None, error, busy),
            error=error.code if error else "",
        )

    def problem(self, s: Session) -> PrinterError | None:
        status = self._status(s)
        return status_error(status) if status is not None else None

    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        return build_job(
            bitmap(image, black_is_one=False),
            dpi / 25.4,
            options.copies,
            media=self.choice(options, "media"),
            gap_mm=options.gap_mm,
            finish=self.choice(options, "finish"),
            darkness=self.level(options, "darkness"),
            speed=self.level(options, "speed"),
        )
