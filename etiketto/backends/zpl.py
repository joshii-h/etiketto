"""Zebra label printers and compatibles: ZPL on TCP port 9100 or USB (see
docs/zpl.md).

The label goes to the printer's RAM as a graphic (~DG, hex rows with
Zebra's run-length compression) and is printed from there (^XG), as LPrint
by Michael R Sweet (Apache-2.0) does; written anew. ~HI names the model and
resolution, ~HS and ~HQES report problems.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from etiketto.backends.base import (
    Capabilities,
    Choice,
    HeadOpen,
    LabelNotFound,
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
    Toggle,
)
from etiketto.backends.raw import (
    DPI,
    Bitmap,
    RawBackend,
    Session,
    bitmap,
    percent_to,
    state,
)

GRAPHIC = "R:ETIKETTO.GRF"  # RAM drive; each job replaces and then deletes it
STX, ETX = b"\x02", b"\x03"
# dots per mm in the ~HI answer -> the dpi setting
DOTS_PER_MM = {8: "203", 12: "300", 24: "600"}
# ^MM: what happens after a label; the second value is "prepeel" (LPrint)
FINISH = {"tear": "T,Y", "peel": "P,N", "cut": "C,Y"}
# ^MN: gap labels are "web" (Y), black marks M, continuous N. LPrint sends M
# for gap labels too, which the manual calls black mark sensing.
MEDIA = {"gap": "Y", "mark": "M", "continuous": "N"}

# 4 inch heads: 832 dots at 203 dpi (manual), the usual widest label
MAX_WIDTH_MM = 104.0
LABEL_SIZES = (
    LabelSize(101.6, 152.4),  # 4 × 6 in, shipping labels
    LabelSize(101.6, 101.6),
    LabelSize(101.6, 50.8),
    LabelSize(76.2, 50.8),
    LabelSize(57.15, 31.75),  # 2.25 × 1.25 in
    LabelSize(50.8, 25.4),
    LabelSize(100, 150),
    LabelSize(50, 25),
)

# ~HQES: bits of the second error word (LPrint's list of Zebra's bits)
ERROR_BITS: tuple[tuple[int, type[PrinterError], str], ...] = (
    (0x00000001, OutOfLabels, "media out"),
    (0x00000002, RibbonError, "ribbon out"),
    (0x00000004, HeadOpen, "head open"),
    (0x00001000, PaperJam, "paper jam"),
    (0x00010000, PrinterPaused, "paused"),
    (0x00040000, LabelNotFound, "mark calibrate error"),
    (0x00080000, LabelNotFound, "mark not found"),
    (0x00000008, PrinterError, "cutter fault"),
    (0xFFFFFFFF, PrinterError, "error"),  # any other bit
)


# --- the graphic ----------------------------------------------------------------


def rle_run(char: str, count: int) -> str:
    """A run of one hex character in Zebra's alphabet: z = 400, g..y =
    20..380, G..Y = 1..19, then the character; a single one stays as it is."""
    out = ""
    if count > 1:
        out += "z" * (count // 400)
        count %= 400
        if count >= 20:
            out += chr(ord("f") + count // 20)
            count %= 20
        if count:
            out += chr(ord("F") + count)
    return out + char


def _runs(text: str) -> list[tuple[str, int]]:
    runs: list[tuple[str, int]] = []
    for char in text:
        if runs and runs[-1][0] == char:
            runs[-1] = (char, runs[-1][1] + 1)
        else:
            runs.append((char, 1))
    return runs


def encode_row(row: bytes, previous: bytes | None) -> str:
    """One row: ":" if it repeats the previous one, else compressed hex;
    trailing zeros become ",". An odd run of zeros keeps one "0", so the
    comma starts on a whole byte."""
    if row == previous:
        return ":"
    runs = _runs(row.hex().upper())
    char, count = runs[-1]
    if char != "0":
        return "".join(rle_run(c, n) for c, n in runs)
    body = "".join(rle_run(c, n) for c, n in runs[:-1])
    return body + "0" * (count & 1) + ("," if count > 1 else "")


def encode_graphic(image: Bitmap) -> str:
    previous = None
    parts = []
    for row in image.rows:
        parts.append(encode_row(row, previous))
        previous = row
    return "".join(parts)


def download(image: Bitmap, name: str = GRAPHIC) -> bytes:
    """~DG: the graphic into the printer's memory, total bytes and bytes
    per row first. The data follows the last comma without a separator."""
    return ("~DG%s,%d,%d," % (name, image.bpr * image.height, image.bpr)).encode() + (
        encode_graphic(image).encode()
    )


# --- the job --------------------------------------------------------------------


def build_job(
    image: Bitmap,
    copies: int = 1,
    media: str = "printer",
    finish: str = "printer",
    darkness: int | None = None,
    speed: int | None = None,
) -> bytes:
    """Download the label, print it `copies` times, delete it again.

    ^PON: the first row leaves the printer first, like in the other
    languages (LPrint sends ^POI, turned by 180°; "Print upside down" turns
    the image instead). Sensor, finish, darkness (per cent) and speed
    (inches per second) are only sent when set; otherwise the printer's
    setup applies."""
    head = []
    if darkness is not None:
        head.append("~SD%02d" % percent_to(darkness, 30))
    fmt = ["^XA"]
    if finish in FINISH:
        fmt.append("^MM" + FINISH[finish])
    if speed is not None:
        fmt.append("^PR%d,%d,%d" % (speed, speed, speed))
    fmt += [
        "^PON",
        "^PW%d" % image.width,
        "^LH0,0",
        "^LL%d" % image.height,  # LPrint only for continuous: harmless, right before calibrating
    ]
    if media in MEDIA:
        fmt.append("^MN" + MEDIA[media])
    fmt += [
        "^PQ%d,0,1,N" % max(1, copies),
        "^FO0,0^XG%s,1,1^FS" % GRAPHIC,
        "^XZ",
        "^XA^ID%s^FS^XZ" % GRAPHIC,
    ]
    start = "".join(line + "\n" for line in head).encode()
    return start + download(image) + b"\n" + "".join(line + "\n" for line in fmt).encode()


# --- answers ----------------------------------------------------------------------


def _frames(raw: bytes) -> list[list[str]]:
    """The STX ... ETX frames of an answer, each split at commas."""
    text = raw.decode("ascii", "replace")
    return [frame.split(",") for frame in re.findall(r"\x02([^\x02\x03]*)\x03", text)]


def _flag(fields: list[str], index: int) -> bool:
    return index < len(fields) and fields[index].strip() == "1"


def _number(fields: list[str], index: int) -> int:
    value = fields[index].strip() if index < len(fields) else ""
    return int(value) if value.isdigit() else 0


def frames_done(count: int):
    return lambda answer: answer.count(ETX) >= count


@dataclass(frozen=True)
class Identity:
    """~HI: "<STX>ZD621-203dpi,V93.21.18,8,8192KB<ETX>"."""

    model: str = ""
    firmware: str = ""
    dpi: str = ""  # the dpi setting it matches, "" = unknown


def parse_identity(raw: bytes) -> Identity:
    """The ~HI frame of an answer: 3 to 6 fields, the third a number. It
    may come late, behind the next question: frames of ~HS (12, 11 and 2
    fields) are not it."""
    for frame in _frames(raw):
        if 3 <= len(frame) <= 6 and frame[2].strip().isdigit():
            break
    else:
        return Identity()
    model, firmware, dpmm = (field.strip() for field in frame[:3])
    dpi = DOTS_PER_MM.get(int(dpmm), "") if dpmm.isdigit() else ""
    return Identity(model, firmware, dpi)


@dataclass(frozen=True)
class HostStatus:
    """~HS, three frames; the flags Etiketto uses. Line 1
    "aaa,b,c,dddd,eee,...": b paper out, c paused, eee formats waiting.
    Line 2 "mmm,n,o,p,...,uuuuuuuu,...": o head up, p ribbon out,
    uuuuuuuu labels still to print."""

    paper_out: bool = False
    paused: bool = False
    head_up: bool = False
    ribbon_out: bool = False
    busy: bool = False
    raw: str = ""


def parse_host_status(raw: bytes) -> HostStatus | None:
    """None for an answer that is not a status (nothing, garbage). The two
    frames are found by their number of fields (12 and 11), not by their
    place: a late answer to an earlier question may come first."""
    frames = _frames(raw)
    pairs = zip(frames, frames[1:], strict=False)
    found = next(((a, b) for a, b in pairs if len(a) >= 12 and len(b) >= 11), None)
    if found is None:
        return None
    one, two = found
    return HostStatus(
        paper_out=_flag(one, 1),
        paused=_flag(one, 2),
        head_up=_flag(two, 2),
        ribbon_out=_flag(two, 3),
        busy=_number(one, 4) > 0 or _number(two, 8) > 0,
        raw=raw.decode("ascii", "replace").strip(),
    )


def host_status_error(status: HostStatus) -> PrinterError | None:
    found: list[tuple[bool, type[PrinterError], str]] = [
        (status.paper_out, OutOfLabels, "paper out"),
        (status.head_up, HeadOpen, "head up"),
        (status.ribbon_out, RibbonError, "ribbon out"),
        (status.paused, PrinterPaused, "paused"),
    ]
    for flag, cls, text in found:
        if flag:
            return cls("~HS: %s (%s)" % (text, status.raw))
    return None


def parse_errors(raw: bytes) -> int | None:
    """~HQES: "ERRORS: 1 00000000 00000005", the second word is the bit
    field (as LPrint reads it); None without such a line."""
    text = raw.decode("ascii", "replace")
    match = re.search(r"ERRORS:\s*(\d+)\s+([0-9A-Fa-f]{1,8})\s+([0-9A-Fa-f]{1,8})", text)
    return int(match.group(3), 16) if match else None


def errors_error(bits: int) -> PrinterError | None:
    for bit, cls, text in ERROR_BITS:
        if bits & bit:
            return cls("~HQES errors %08X: %s" % (bits, text))
    return None


# --- the backend ------------------------------------------------------------------


class Zpl(RawBackend):
    id = "zpl"
    name = "Zebra ZPL"
    command_sets = ("ZPL", "ZPL2", "ZPLII")
    makers = ("Zebra",)
    capabilities = Capabilities(
        tape_widths_mm=(),
        dpi=DPI["203"],
        modes=(PrintMode("standard", 203),),
        color_model="mono",
        cut_modes=(),
        continuous=False,
        label_sizes=LABEL_SIZES,
        max_width_mm=MAX_WIDTH_MM,
        data_formats=("zpl",),
        settings=(
            Choice("dpi", ("auto", "203", "300", "600")),
            Choice("media", ("printer", "gap", "mark", "continuous")),
            Choice("finish", ("printer", "tear", "peel", "cut")),
            Level("darkness", 0, 100, 10),
            Level("speed", 1, 12),
            Toggle("upside_down"),
        ),
    )

    def display_name(self, info: PrinterInfo | None) -> str:
        """The model the printer names in ~HI, e.g. "ZD621-203dpi"."""
        return info.model if info and info.model else self.name

    def _status(self, s: Session) -> tuple[HostStatus | None, PrinterError | None, bytes]:
        """~HS, then ~HQES if ~HS showed no problem; and the ~HS answer."""
        answer = s.ask(b"~HS\n", frames_done(3))
        status = parse_host_status(answer)
        if status is None:  # no status at all: nothing more to ask
            return None, None, answer
        error = host_status_error(status)
        if error is None:
            bits = parse_errors(s.ask(b"~HQES\n", frames_done(1)))
            error = errors_error(bits) if bits else None
        return status, error, answer

    def _identity(self, s: Session, answer: bytes) -> Identity:
        identity = parse_identity(answer)
        if identity.dpi:
            self.report_dpi(s.host, DPI[identity.dpi])
        return identity

    def printer_dpi(self, s: Session) -> float | None:
        identity = self._identity(s, s.ask(b"~HI\n", frames_done(1)))
        return DPI[identity.dpi] if identity.dpi else None

    def ask_status(self, s: Session) -> PrinterInfo:
        answer = s.ask(b"~HI\n", frames_done(1))
        status, error, more = self._status(s)
        identity = self._identity(s, answer + more)  # a late ~HI answer comes with ~HS
        return PrinterInfo(
            identity.model,
            None,
            None,
            None,
            state(status is not None, error, bool(status and status.busy)),
            error=error.code if error else "",
            firmware=identity.firmware,
        )

    def problem(self, s: Session) -> PrinterError | None:
        return self._status(s)[1]

    def build_job(self, image: Image.Image, dpi: float, options: PrintOptions, host: str) -> bytes:
        return build_job(
            bitmap(image, black_is_one=True),
            options.copies,
            media=self.choice(options, "media"),
            finish=self.choice(options, "finish"),
            darkness=self.level(options, "darkness"),
            speed=self.level(options, "speed"),
        )
