"""DYMO LabelWriter label printers (300, 400 and 450 series, 4XL), mostly
on USB (see docs/dymo.md).

Each row with ink is SYN and its raw bytes; blank rows are skipped with
ESC f. The sequence follows LPrint by Michael R Sweet (Apache-2.0), written
anew. The printers answer no status Etiketto asks for. The LabelWriter 550
and 5XL only take DYMO's labels with a chip and are not supported (LPrint
leaves them out too).
"""

from __future__ import annotations

from PIL import Image

from etiketto.backends.base import (
    Capabilities,
    LabelSize,
    LabelTooWide,
    Level,
    PrintMode,
    PrintOptions,
)
from etiketto.backends.raw import Bitmap, RawBackend, bitmap, blank_runs, chunks, usb_model

ESC, SYN = b"\x1b", b"\x16"
DPI = 300.0  # across the head and along the feed
HEAD_DOTS = 672  # LabelWriter 4xx: 56.9 mm
WIDE_HEAD_DOTS = 1248  # LabelWriter 4XL
CUT_OFF_MM = 1.0  # a label this much wider than the head is cut, not refused
MAX_SKIP = 255  # dot lines per ESC f
DENSITY = b"cdeg"  # ESC c (light) to ESC g (dark); ESC e is the printer's normal
# common DYMO labels: width across the head × length along the feed
LABEL_SIZES = (
    LabelSize(28, 89),  # 99010, address
    LabelSize(36, 89),  # 99012, large address
    LabelSize(25, 54),  # 11352, return address
    LabelSize(19, 51),  # 11355, multi-purpose
    LabelSize(57, 32),  # 11354, multi-purpose
    LabelSize(54, 101),  # 99014, shipping
    LabelSize(104, 159),  # 1744907, shipping (4XL)
)


def head_dots(host: str) -> int:
    """The print head of the model in a usb:// address; 4xx if unknown."""
    return WIDE_HEAD_DOTS if "4XL" in usb_model(host).upper() else HEAD_DOTS


def rows(image: Bitmap) -> bytes:
    """SYN and the row for each row with ink; blank rows before one are
    skipped with ESC f 1 n, blank rows at the end are not sent at all
    (the form feed passes them)."""
    out = []
    for first, count, empty in blank_runs(image.rows, image.blank):
        if empty:
            if first + count < image.height:
                out += [ESC + b"f\x01" + bytes([n]) for n in chunks(count, MAX_SKIP)]
            continue
        out += [SYN + row for row in image.rows[first : first + count]]
    return b"".join(out)


def page(image: Bitmap, darkness: int | None = None, roll: int = 1) -> bytes:
    """One label: margins and length, bytes per row, roll, density, rows,
    form feed (ESC E)."""
    out = [
        ESC + b"Q\x00\x00",  # as LPrint sends it
        ESC + b"B\x00",  # no dot tab: the label starts at the first dot
        ESC + b"L" + image.height.to_bytes(2, "big"),
        ESC + b"D" + bytes([image.bpr]),
        ESC + b"q" + bytes([roll]),
    ]
    if darkness is not None:  # LPrint's steps: 0-33 %, 34-66 %, 67-99 %, 100 %
        out.append(ESC + bytes([DENSITY[min(3, 3 * darkness // 100)]]))
    return b"".join(out) + rows(image) + ESC + b"E"


def build_job(image: Bitmap, copies: int = 1, darkness: int | None = None) -> bytes:
    """200 ESC end any command a previous job left unfinished, ESC @ resets;
    then one page per copy."""
    return ESC * 200 + ESC + b"@" + page(image, darkness) * max(1, copies)


class Dymo(RawBackend):
    id = "dymo"
    name = "DYMO LabelWriter"
    makers = ("DYMO",)
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI,
        modes=(PrintMode("standard", 300),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=WIDE_HEAD_DOTS / DPI * 25.4,
        data_formats=("dymo",),
        settings=(Level("darkness", 0, 100, 10),),
    )

    def speaks(self, maker: str, model: str, commands: tuple[str, ...]) -> bool:
        """LabelWriters, but not the 550 and 5XL (labels with a chip only)."""
        name = model.upper()
        return (
            maker.casefold().startswith("dymo")
            and "LABELWRITER" in name
            and not any(locked in name for locked in ("550", "5XL"))
        )

    def check(self, image: Image.Image, dpi: float, host: str) -> None:
        """No wider than this model's head. A label sold as 57 mm is a dot
        wider than the 56.9 mm head: up to 1 mm more is cut off, not refused."""
        head_mm = head_dots(host) / dpi * 25.4
        width_mm = image.width / dpi * 25.4
        if width_mm > head_mm + CUT_OFF_MM:
            raise LabelTooWide(
                "%.1f mm is wider than the %.1f mm head" % (width_mm, head_mm),
                max_mm=round(head_mm, 1),
            )

    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        width = min(image.width, head_dots(host))
        return build_job(
            bitmap(image, black_is_one=True, width=width),
            options.copies,
            darkness=self.level(options, "darkness"),
        )
