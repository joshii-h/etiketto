"""Encoding codes into their modules, and what each symbology looks like.

The encoders give modules only: rows of bars and spaces (or squares), never
a picture. render.py draws them, each module a whole number of printer
dots. QR codes come from segno, Code 128 and EAN-13 from python-barcode, as
in the first versions, so labels made with them print the same modules
(Zint chooses other QR masks and Code 128 code sets; its EAN-13 is the same,
see tests). Every other symbology comes from Zint (libzint, through
zint-bindings): its raw module matrix, not its bitmap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from etiketto.model import CodeElement


class CodeError(ValueError):
    """The content cannot be encoded in the chosen symbology; key names the
    problem (a RenderWarning key), params go into its text."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(frozen=True)
class Spec:
    """How a symbology is drawn.

    quiet: blank modules left and right (2D codes: on every side), as the
    standards ask: ISO/IEC 15417 (Code 128), 16388 (Code 39), AIM USS Code 93,
    15420 and the GS1 General Specifications (EAN/UPC, ITF-14, GS1-128),
    16022 (Data Matrix), 15438 (PDF417), 24778 (Aztec, none), 18004 (QR).
    layout: "bars" (a linear code, its text centred below), "ean13", "ean8",
    "upca", "upce" (the digits as the GS1 specification places them, guard
    bars longer), "itf14" (bearer bars around bars and quiet zones, the
    digits below), "matrix" (a square 2D code), "stacked" (PDF417: rows of
    row_modules modules high).
    """

    quiet: tuple[int, int]
    layout: str
    row_modules: int = 1


SPECS = {
    "qr": Spec((4, 4), "matrix"),
    "code128": Spec((10, 10), "bars"),
    "ean13": Spec((11, 7), "ean13"),
    "code39": Spec((10, 10), "bars"),
    "code93": Spec((10, 10), "bars"),
    "ean8": Spec((7, 7), "ean8"),
    "upca": Spec((9, 9), "upca"),
    "upce": Spec((9, 7), "upce"),
    "itf14": Spec((10, 10), "itf14"),
    "gs1_128": Spec((10, 10), "bars"),
    "datamatrix": Spec((1, 1), "matrix"),
    "gs1_datamatrix": Spec((1, 1), "matrix"),
    # ISO/IEC 15438: rows at least 3 modules high
    "pdf417": Spec((2, 2), "stacked", row_modules=3),
    "aztec": Spec((0, 0), "matrix"),
}
ITF14_BEARER = 5  # modules: GS1 asks for 4.8 mm at the nominal 1.016 mm module
PDF417_ASPECT = 2.0  # PDF417 columns chosen so the code is about twice as wide as high


@dataclass(frozen=True)
class Symbol:
    """The modules of an encoded code: one string of "1" (dark) and "0" per
    row, all of the same length; a linear code has one row. text: the
    human-readable line ("" for none)."""

    rows: tuple[str, ...]
    text: str = ""

    @property
    def width(self) -> int:
        return len(self.rows[0])


# --- checks -----------------------------------------------------------------

PRINTABLE_ASCII = frozenset(map(chr, range(32, 127)))
CODE39_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-. $/+%")
# what a digits-only code needs: symbology -> (length without the check
# digit, warning key)
DIGITS = {
    "ean13": (12, "ean13_digits"),
    "ean8": (7, "ean8_digits"),
    "upca": (11, "upca_digits"),
    "upce": (7, "upce_digits"),
    "itf14": (13, "itf14_digits"),
}
CHARSETS = {
    "code128": (PRINTABLE_ASCII, "code128_charset"),
    "code93": (PRINTABLE_ASCII, "code93_charset"),
    "code39": (CODE39_CHARS, "code39_charset"),
    "gs1_128": (PRINTABLE_ASCII, "gs1_charset"),
    "gs1_datamatrix": (PRINTABLE_ASCII, "gs1_charset"),
}


def gs1_check_digit(digits: str) -> int:
    """The GS1 mod-10 check digit of digits (EAN, UPC-A, ITF-14, GTIN)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10


def check(element: CodeElement) -> None:
    """Raise CodeError for content that cannot be encoded as it is: the
    libraries would drop or change characters, or correct a check digit."""
    data, symbology = element.data, element.symbology
    if not data:
        raise CodeError("code_empty")
    try:
        data.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: no text a code can carry
        raise CodeError("code_invalid") from None
    if symbology in DIGITS:
        _check_digits(data, symbology)
    elif symbology in CHARSETS:
        chars, key = CHARSETS[symbology]
        if not chars.issuperset(data):
            raise CodeError(key)


# Code 39 Full ASCII reads these pairs as one other character ("+A" is "a"):
# a scanner set to it reads such content differently (warning, not error)
CODE39_FULL_ASCII = re.compile(r"[$/+%][A-Z]")


def notes(element: CodeElement) -> tuple[str, ...]:
    """Content that encodes but may be read differently (warning keys)."""
    if element.symbology == "code39" and CODE39_FULL_ASCII.search(element.data):
        return ("code39_full_ascii",)
    return ()


def _check_digits(data: str, symbology: str) -> None:
    length, key = DIGITS[symbology]
    if not data.isascii() or not data.isdigit() or len(data) not in (length, length + 1):
        raise CodeError(key)
    if symbology == "upce" and data[0] not in "01":
        raise CodeError(key)
    if symbology == "upce" or len(data) == length:
        return  # UPC-E: Zint checks it, against the UPC-A it stands for
    if int(data[-1]) != gs1_check_digit(data[:-1]):
        raise CodeError("ean13_check" if symbology == "ean13" else "code_check")


# --- encoding ---------------------------------------------------------------


def encode(element: CodeElement) -> Symbol:
    """The modules of the code; CodeError if its content cannot be encoded."""
    check(element)
    return _encode(element.symbology, element.data)


@lru_cache(maxsize=64)
def _encode(symbology: str, data: str) -> Symbol:
    if symbology == "qr":
        return _segno(data)
    if symbology in ("code128", "ean13"):
        return _python_barcode(symbology, data)
    return _zint(symbology, data)


def _segno(data: str) -> Symbol:
    """Never a Micro QR code (most phones cannot read those). Text beyond
    ASCII as UTF-8 with ECI 26: without an ECI scanners guess at the bytes
    and read Latin-1 "37°" as katakana. ASCII stays as in the first
    versions, module for module."""
    import segno

    ascii_only = data.isascii()
    try:
        matrix = segno.make(
            data,
            error="m",
            micro=False,
            encoding=None if ascii_only else "utf-8",
            eci=not ascii_only,
        ).matrix
    except segno.DataOverflowError:
        raise CodeError("code_too_long") from None
    return Symbol(tuple("".join("1" if v else "0" for v in row) for row in matrix))


def _python_barcode(symbology: str, data: str) -> Symbol:
    import barcode

    code = barcode.get_barcode_class(symbology)(data)
    return Symbol(("".join(code.build()),), code.get_fullcode())


# Zint's error numbers (libzint 2.15, backend/gs1.c) for GS1 data
GS1_SYNTAX = {250, 252, 253, 254, 255, 256, 257, 258}
GS1_ERRORS = {259: "gs1_length", 260: "gs1_ai"}
UPCE_FORM = {271, 272, 273}  # digits no UPC-A number is zero-suppressed to
ECI_SYMBOLOGIES = ("datamatrix", "pdf417", "aztec")
ZINT_ERROR = re.compile(r"Error (\d+): (.*)")
ZINT_AI = re.compile(r"AI \((\d+)\)")
# rows of encoded_data: bit-packed, this many bytes each (ZINT_COLS_MAX / 8)
ZINT_ROW_BYTES = 144


def _zint(symbology: str, data: str) -> Symbol:
    try:
        import zint
    except ImportError:
        raise CodeError("code_unavailable") from None
    if symbology == "pdf417":
        return _pdf417(zint, data)
    symbol = _zint_symbol(zint, symbology, data)
    return _zint_modules(symbol, symbology, data)


def _zint_symbol(zint, symbology: str, data: str, **options):
    """A Zint symbol of data; strict: a warning (e.g. a wrong GS1 check
    digit, which Zint would otherwise only print) fails too."""
    names = {
        "code39": "CODE39",
        "code93": "CODE93",
        "ean8": "EANX",
        "upca": "UPCA",
        "upce": "UPCE_CHK" if len(data) == 8 else "UPCE",
        "itf14": "ITF14",
        "gs1_128": "GS1_128",
        "datamatrix": "DATAMATRIX",
        "gs1_datamatrix": "DATAMATRIX",
        "pdf417": "PDF417",
        "aztec": "AZTEC",
    }
    symbol = zint.Symbol()
    symbol.symbology = getattr(zint.Symbology, names[symbology])
    symbol.warn_level = zint.WarningLevel.FAIL_ALL
    if symbology.startswith("gs1_"):
        # AIs in brackets: "(01)09501101530003(17)260101"; in square
        # brackets "[01]…[21]A(B)", values may contain round ones
        symbol.input_mode = zint.InputMode.GS1
        if not data.startswith("["):
            symbol.input_mode |= zint.InputMode.GS1PARENS
    else:
        symbol.input_mode = zint.InputMode.UNICODE
        if not data.isascii() and symbology in ECI_SYMBOLOGIES:
            # UTF-8, and say so (ECI 26): scanners guess at bytes without an
            # ECI, and guess Latin-1 wrongly ("37°" as katakana)
            symbol.eci = 26
    if symbology in ("datamatrix", "gs1_datamatrix"):
        symbol.option_3 = zint.DataMatrixOptions.SQUARE
    for name, value in options.items():
        setattr(symbol, name, value)
    if symbology in ("ean8", "upca", "itf14") and len(data) == DIGITS[symbology][0] + 1:
        data = data[:-1]  # the check digit is checked above; Zint adds it again
    try:
        symbol.encode(data)
    except RuntimeError as e:
        raise _zint_error(str(e), symbology) from e
    return symbol


def _zint_error(message: str, symbology: str) -> CodeError:
    """Zint's (English) error as a warning key."""
    match = ZINT_ERROR.match(message)
    number, text = (int(match.group(1)), match.group(2)) if match else (0, message)
    ai = ZINT_AI.search(text)
    if symbology.startswith("gs1_"):
        if number in GS1_SYNTAX:
            return CodeError("gs1_syntax")
        if ai and number in GS1_ERRORS:
            return CodeError(GS1_ERRORS[number], ai=ai.group(1))
        if ai and number == 261:
            key = "gs1_check" if "checksum" in text else "gs1_value"
            return CodeError(key, ai=ai.group(1))
    if number in UPCE_FORM:
        return CodeError("upce_form")
    if "check digit" in text:
        return CodeError("code_check")
    if "too long" in text:
        return CodeError("code_too_long")
    return CodeError("code_invalid")


def _zint_modules(symbol, symbology: str, data: str) -> Symbol:
    raw = symbol.encoded_data.tobytes()
    rows = tuple(
        "".join(
            "1" if raw[row * ZINT_ROW_BYTES + (col >> 3)] >> (col & 7) & 1 else "0"
            for col in range(symbol.width)
        )
        for row in range(symbol.rows)
    )
    return Symbol(rows, _human_readable(symbol, symbology, data))


def _human_readable(symbol, symbology: str, data: str) -> str:
    """The text below a linear code: GS1-128 with its AIs in brackets, the
    EAN/UPC digits with their check digit, Code 39 without its start and
    stop characters; 2D codes have none."""
    if SPECS[symbology].layout in ("matrix", "stacked"):
        return ""
    if symbology == "code39":
        return data
    return symbol.text


def _pdf417(zint, data: str) -> Symbol:
    """PDF417 with as many columns as make it about PDF417_ASPECT times as
    wide as high (quiet zone included): Zint's own choice is much wider than
    high, too long for a label filling the tape's height."""
    best: tuple[float, Symbol] | None = None
    error = CodeError("code_too_long")
    for columns in range(1, 31):
        try:
            symbol = _zint_symbol(zint, "pdf417", data, option_2=columns)
        except CodeError as e:  # too few columns for this much data (more rows than 90)
            error = e
            continue
        quiet = 2 * SPECS["pdf417"].quiet[0]
        aspect = (symbol.width + quiet) / (symbol.rows * SPECS["pdf417"].row_modules + quiet)
        score = abs(aspect - PDF417_ASPECT)
        if best is None or score < best[0]:
            best = (score, _zint_modules(symbol, "pdf417", data))
    if best is None:
        raise error
    return best[1]
