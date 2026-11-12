"""User-visible texts: option lists and the messages made from codes of lower layers."""

from __future__ import annotations

import errno
from datetime import date
from pathlib import Path

from PIL import Image

from etiketto.backends.base import PrinterError, PrinterInfo
from etiketto.model import ImageElement, LabelError, TextElement
from etiketto.render import RenderWarning
from etiketto.ui.i18n import _, isolate, month_name
from etiketto.units import Unit

# cassettes older than this get a hint: old ZINK paper prints paler
OLD_CASSETTE_DAYS = 2 * 365

CUT_MODES = [("full", _("Full cut")), ("half", _("Half cut")), ("none", _("No cut"))]
# print modes by protocol name; unknown modes are shown by their name
MODE_NAMES = {
    "vivid": _("Vivid"),
    "color": _("Normal"),
    "bw": _("Black and white (sharper)"),
}
COLOR_ADJUST_ROWS = [
    ("brightness", _("Brightness")),
    ("red", _("Red")),
    ("green", _("Green")),
    ("blue", _("Blue")),
]
ALIGNS = [("start", _("Start")), ("center", _("Centre")), ("end", _("End"))]
ORIENTATIONS = [("across", _("Across the roll")), ("along", _("Along the roll"))]
# page printers: the label as a page of its own, or on a label sheet
PRINT_ON = [("page", _("Single label")), ("sheet", _("Label sheet"))]
# errors that mean the printer cannot be reached at all; others need attention
CONNECTION_ERRORS = ("not_found", "timeout", "port_closed")
TEXT_ALIGNS = [("left", _("Left")), ("center", _("Centre")), ("right", _("Right"))]
# the code types in the order and groups of the choice; names of standards
# are not translated
SYMBOLOGY_GROUPS = [
    (
        _("2D codes"),
        [
            ("qr", _("QR code")),
            ("datamatrix", "Data Matrix"),
            ("pdf417", "PDF417"),
            ("aztec", "Aztec"),
        ],
    ),
    (
        _("Linear barcodes"),
        [
            ("code128", "Code 128"),
            ("code39", "Code 39"),
            ("code93", "Code 93"),
            ("ean13", "EAN-13"),
            ("ean8", "EAN-8"),
            ("upca", "UPC-A"),
            ("upce", "UPC-E"),
            ("itf14", "ITF-14"),
        ],
    ),
    ("GS1", [("gs1_128", "GS1-128"), ("gs1_datamatrix", "GS1 DataMatrix")]),
]
SYMBOLOGIES = [item for _group, items in SYMBOLOGY_GROUPS for item in items]
_GS1_HELP = _("Application identifiers in brackets, e.g. (01)09501101530003(17)261231(10)AB12")
# what each code type is for and what it accepts
SYMBOLOGY_HELP = {
    "qr": _("Links, text, Wi-Fi access; phones read it"),
    "datamatrix": _("Text or numbers in little space, e.g. on small parts"),
    "pdf417": _("Much text, e.g. an address or a document number"),
    "aztec": _("Text or numbers; needs no blank margin, e.g. on tickets"),
    "code128": _("Latin letters, digits and punctuation"),
    "code39": _("Capital letters, digits, spaces and - . $ / + %"),
    "code93": _("Latin letters, digits and punctuation; shorter than Code 39"),
    "ean13": _("12 digits, the check digit is added (or 13 with it)"),
    "ean8": _("7 digits, the check digit is added (or 8 with it)"),
    "upca": _("11 digits, the check digit is added (or 12 with it)"),
    "upce": _("7 digits starting with 0 or 1, the check digit is added (or 8 with it)"),
    "itf14": _("13 digits of a shipping carton's GTIN, the check digit is added (or 14 with it)"),
    "gs1_128": _GS1_HELP,
    "gs1_datamatrix": _GS1_HELP,
}


def image_from_path(path) -> ImageElement:
    """Read an image file into an element; ValueError if it is not an image."""
    if not path:
        raise ValueError(_("Only local files can be used"))
    try:
        with Image.open(path) as img:
            img.verify()
        return ImageElement.from_file(path)
    except Exception as e:  # noqa: BLE001 - PIL raises many kinds
        raise ValueError(_("Not an image: %s") % Path(path).name) from e


def element_title(element) -> str:
    if isinstance(element, TextElement):
        first = (element.text.splitlines() or [""])[0]
        return first[:40] or _("Text")
    if isinstance(element, ImageElement):
        return element.name or _("Image")
    return (
        dict(SYMBOLOGIES).get(element.symbology, element.symbology)
        + ": "
        + isolate(element.data[:30])
    )


def element_icon(element) -> str:
    if isinstance(element, TextElement):
        return "format-text-bold-symbolic"
    if isinstance(element, ImageElement):
        return "image-x-generic-symbolic"
    return "view-grid-symbolic"


# --- messages ---------------------------------------------------------------
# Lower layers report codes; the texts are made (and translated) here.


def backend_name(backend) -> str:
    """The name of a printer model in the list; brand names stay as they are."""
    names = {"system": _("System printer (CUPS, IPP)")}
    return names.get(backend.id, backend.name)


def warning_text(warning: RenderWarning) -> str:
    texts = {
        "code_empty": _("Element %(n)d: the code is empty"),
        "ean13_digits": _("Element %(n)d: EAN-13 needs exactly 12 digits"),
        "ean13_check": _("Element %(n)d: the EAN-13 check digit is wrong"),
        "code128_charset": _(
            "Element %(n)d: Code 128 only accepts Latin letters, digits and punctuation"
        ),
        "ean8_digits": _("Element %(n)d: EAN-8 needs 7 digits, or 8 with the check digit"),
        "upca_digits": _("Element %(n)d: UPC-A needs 11 digits, or 12 with the check digit"),
        "upce_digits": _(
            "Element %(n)d: UPC-E needs 7 digits starting with 0 or 1, or 8 with the check digit"
        ),
        "itf14_digits": _("Element %(n)d: ITF-14 needs 13 digits, or 14 with the check digit"),
        "code_check": _("Element %(n)d: the check digit is wrong"),
        "code39_charset": _(
            "Element %(n)d: Code 39 only accepts capital letters, digits, spaces and - . $ / + %%"
        ),
        "code93_charset": _(
            "Element %(n)d: Code 93 only accepts Latin letters, digits and punctuation"
        ),
        "gs1_charset": _(
            "Element %(n)d: GS1 data only accepts Latin letters, digits and punctuation"
        ),
        "gs1_syntax": _(
            "Element %(n)d: GS1 data must be application identifiers in brackets, each followed "
            "by its value, e.g. (01)09501101530003(10)AB12"
        ),
        "gs1_ai": _("Element %(n)d: (%(ai)s) is not a GS1 application identifier"),
        "gs1_length": _(
            "Element %(n)d: the value of application identifier (%(ai)s) has the wrong length"
        ),
        "gs1_check": _(
            "Element %(n)d: the check digit of application identifier (%(ai)s) is wrong"
        ),
        "gs1_value": _(
            "Element %(n)d: the value of application identifier (%(ai)s) is not valid: a date, "
            "number or character it does not allow"
        ),
        "code_too_long": _("Element %(n)d: the content is too long for this type of code"),
        "code_invalid": _("Element %(n)d: the content cannot be encoded in this type of code"),
        "code_unavailable": _(
            "Element %(n)d: this type of code needs the Zint library (zint-bindings), which is "
            "not installed"
        ),
        "image_unreadable": _("Element %(n)d: the image cannot be read"),
        "element_failed": _("Element %(n)d cannot be drawn"),
        "no_room": _("Safety margin, border and edge spacing leave no room – reduce them"),
        "fixed_too_short": _("The fixed length is shorter than the spacing – increase it"),
        "background_unreadable": _("The background image cannot be read"),
        "code_thin": _(
            "Element %(n)d: the code's modules are only one printer dot wide and may not "
            "scan – make the code larger"
        ),
        "code_quiet": _(
            "Element %(n)d: the blank space the code needs around it does not fit – make "
            "the code larger"
        ),
        "code_too_big": _(
            "Element %(n)d: the code does not fit in its space, even at one printer dot per "
            "module – make the label larger or the content shorter"
        ),
    }
    return texts.get(warning.key, warning.key) % warning.params


def printer_error_text(error: Exception, unit: Unit) -> str:
    """What went wrong and what to do, for any error from a backend."""
    if not isinstance(error, PrinterError):
        return code_text("generic", unit)
    text = code_text(error.code, unit, float(error.params.get("max_mm", 0)))
    printed = error.params.get("printed")
    if printed:  # some of the copies (or sheets) came out before the problem
        if error.params.get("pages"):
            done = _("Sheets already printed: %(n)d of %(total)d.")
        else:
            done = _("Labels already printed: %(n)d of %(total)d.")
        text += " " + done % {"n": printed, "total": error.params.get("copies", printed)}
    if error.params.get("queue_canceled"):  # sent behind other jobs, not printed yet
        if error.params.get("pages"):
            text += " " + _("The sheets still waiting in the queue were canceled.")
        else:
            text += " " + _("The labels still waiting in the queue were canceled.")
    return text


def code_text(code: str, unit: Unit, max_mm: float = 0.0) -> str:
    """The text for a PrinterError code (also PrinterInfo.error); max_mm
    is the limit of "too_long" and "too_wide"."""
    texts = {
        "not_found": _(
            "The printer cannot be found. Check that it is switched on and in the same "
            "network, or enter its IP address."
        ),
        "timeout": _("The printer does not answer. Check that it is switched on and awake."),
        "port_closed": _(
            "The printer refuses the connection. Switch it off and on again. If its web "
            "interface offers a “Port 9100” option, turn it on."
        ),
        "no_cassette": _("No cassette is inserted. Insert one and try again."),
        "cover_open": _("The printer's cover is open. Close it and try again."),
        "jam": _("The label is jammed. Open the cover, remove it and try again."),
        "bad_cassette": _("The cassette is not recognised. Insert it again or try another one."),
        "busy": _("The printer is busy. Wait a moment and try again."),
        "too_long": _("The label is too long for this printer: at most %(max)s."),
        "too_wide": _("The label is too wide for this printer: at most %(max)s."),
        "out_of_labels": _("The printer is out of labels. Load a new roll and try again."),
        "head_open": _("The print head is open. Close it and try again."),
        "label_not_found": _(
            "The printer cannot find the next label. Check that the labels are loaded "
            "correctly and run through the label sensor."
        ),
        "ribbon": _(
            "There is a problem with the ribbon. Check that it is inserted correctly and "
            "matches the print method set on the printer."
        ),
        "paused": _("The printer is paused. Resume it on its display and try again."),
        "out_of_paper": _("The printer is out of paper. Load paper and try again."),
        "media_jam": _(
            "The paper is jammed. Remove it as the printer's display shows and try again."
        ),
        "supplies": _("The printer is out of toner or ink. Replace it and try again."),
        "paper_size": _("The printer cannot print this size. Choose a paper size it offers."),
        "format": _(
            "The printer takes none of the formats Etiketto can send (PDF, PWG Raster, JPEG)."
        ),
        "queue_paused": _(
            "The printer's queue is paused. Resume it in the system's printer settings and "
            "try again."
        ),
        "job_held": _(
            "The print job was held in the queue, for example until it is released or a "
            "password is entered. Check the queue in the system's printer settings and try "
            "again."
        ),
        "job_canceled": _("The print job was canceled at the printer or in the queue."),
        "code_too_large": _(
            "A code does not fit on the label at this printer's resolution and would be cut "
            "off. Make the label larger or the code's content shorter."
        ),
        "generic": _(
            "The printer reported a problem. Switch it off and on again, then try once more."
        ),
    }
    text = texts.get(code, texts["generic"])
    if code in ("too_long", "too_wide"):
        text %= {"max": unit.format(max_mm)}
    return text


def stage_text(stage: str) -> str | None:
    """Printer stages worth telling the user about."""
    return {
        "PREHEATING": _("Warming up…"),
        "COOLDOWN": _("Cooling down…"),
        "CLEANING": _("Cleaning…"),
        "READY_FOR_CUT": _("Swipe the printer's panel to cut the label"),
        # the system printer waits until the user fixes it
        "OUT_OF_PAPER": _("Out of paper – load paper to continue"),
        "COVER_OPEN": _("Cover open – close it to continue"),
        "MEDIA_JAM": _("Paper jammed – remove it to continue"),
    }.get(stage)


def error_details(error: Exception) -> str:
    """The original error for bug reports: "ValueError: bad value"."""
    return "%s: %s" % (type(error).__name__, error)


def label_error_text(error: LabelError, name: str) -> str:
    if error.code == "newer":
        return _("This label needs a newer version of Etiketto")
    if error.code == "not_label":
        return _("%s is not an Etiketto label") % name
    return _("The file is damaged: %s") % name


def save_error_text(error: OSError, name: str) -> str:
    # PermissionError covers EACCES and EPERM; a read-only disk has no class
    if isinstance(error, PermissionError) or error.errno == errno.EROFS:
        return _("Cannot save %s: no permission to write there") % name
    if error.errno in (errno.ENOSPC, errno.EDQUOT):
        return _("Cannot save %s: the disk is full") % name
    return _("Cannot save %s") % name


def cassette_age_hint(info: PrinterInfo | None, today: date | None = None) -> str | None:
    """A hint for cassettes that seem to be more than two years old."""
    made = info.cassette.made if info and info.cassette else None
    if made is None or ((today or date.today()) - made).days <= OLD_CASSETTE_DAYS:
        return None
    return _(
        "This cassette seems to be from %(month)s %(year)d. Old ZINK paper prints paler colours."
    ) % {"month": month_name(made.month), "year": made.year}
