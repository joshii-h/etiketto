"""Zebra EPL2 label printers (LP/TLP 2824 and 2844, GK420 in EPL mode and
compatibles) on TCP port 9100 or USB (see docs/epl.md).

Each row with ink is one GW command with its raw bytes, 0 = black; blank
rows are not sent. The commands follow LPrint by Michael R Sweet
(Apache-2.0), written anew. EPL2 has no status Tavletta asks for: the
printer is reachable or not.
"""

from __future__ import annotations

from PIL import Image

from tavletta.backends.base import Capabilities, Choice, Level, PrintMode, PrintOptions, Toggle
from tavletta.backends.raw import DPI, Bitmap, RawBackend, bitmap, percent_to
from tavletta.backends.zpl import LABEL_SIZES, MAX_WIDTH_MM


def media_command(media: str, height: int, gap: int) -> str | None:
    """Q: label length and the gap (or black mark, B) in dots; 0 for
    continuous media. None keeps the printer's setup, as LPrint does."""
    if media not in ("gap", "mark", "continuous"):
        return None
    if media == "continuous":
        return "Q%d,0" % height
    if media == "mark":
        return "Q%d,B%d" % (height, gap)
    return "Q%d,%d" % (height, gap)


def rows(image: Bitmap) -> bytes:
    """GW x,y,bytes per row,1 and the raw row for each row with ink.

    LPrint skips rows of 0x00, which in EPL2 are black ones, and sends the
    white ones; blank here are rows of 0xFF (white)."""
    out = []
    for y, row in enumerate(image.rows):
        if row != image.blank:
            out.append(b"GW0,%d,%d,1\n" % (y, image.bpr) + row + b"\n")
    return b"".join(out)


def build_job(
    image: Bitmap,
    copies: int = 1,
    media: str = "printer",
    gap: int = 24,
    darkness: int | None = None,
) -> bytes:
    """Clear the buffer (N), label width and length, the rows, print
    `copies` labels (P). Darkness in per cent, D0 to D15, only when set."""
    head = ["", "N"]  # the job begins with "\nN\n", which marks it as EPL2
    if darkness is not None:
        head.append("D%d" % percent_to(darkness, 15))
    head.append("q%d" % (image.bpr * 8))
    sensor = media_command(media, image.height, gap)
    if sensor:
        head.append(sensor)
    start = "".join(line + "\n" for line in head).encode()
    return start + rows(image) + b"P%d\n" % max(1, copies)


class Epl(RawBackend):
    id = "epl"
    name = "Zebra EPL2"
    command_sets = ("EPL", "EPL2")
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI["203"],
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=MAX_WIDTH_MM,
        data_formats=("epl2",),
        settings=(
            Choice("dpi", ("203", "300")),
            Choice("media", ("printer", "gap", "mark", "continuous")),
            Level("darkness", 0, 100, 10),
            Toggle("upside_down"),
        ),
    )

    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        return build_job(
            bitmap(image, black_is_one=False),
            options.copies,
            media=self.choice(options, "media"),
            gap=round(options.gap_mm * dpi / 25.4),
            darkness=self.level(options, "darkness"),
        )
