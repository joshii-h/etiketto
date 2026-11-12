"""User-visible texts: option lists and the messages made from codes of lower layers."""

from __future__ import annotations

import errno
from datetime import date
from pathlib import Path

from PIL import Image

from etiketto.backends.base import PrinterError, PrinterInfo, PrintOptions, PrintResult
from etiketto.merge import MergeError
from etiketto.model import ImageElement, LabelError, PageElement, SymbolElement, TextElement
from etiketto.render import RenderWarning
from etiketto.shipping.detect import Detection
from etiketto.shipping.source import SourceError
from etiketto.shipping.targets import Target
from etiketto.ui.i18n import _, isolate, month_name
from etiketto.units import Unit

# cassettes older than this get a hint: old ZINK paper prints paler
OLD_CASSETTE_DAYS = 2 * 365

CUT_MODES = [
    ("full", _("Full cut")),
    ("half", _("Half cut")),
    ("end", _("Cut after the last label")),
    ("none", _("No cut")),
]
# Capabilities.extras shown as switches: (choice key, title, subtitle)
EXTRA_SWITCHES = [
    (
        "chain",
        _("Chain printing"),
        _("Saves tape: the last label stays in the printer until the next print"),
    ),
    ("high_res", _("High resolution"), _("Twice as fine along the tape, prints more slowly")),
]
# CassetteInfo.media: the kind of tape
MEDIA_NAMES = {
    "laminated": _("Laminated tape"),
    "non_laminated": _("Non-laminated tape"),
    "fabric": _("Fabric tape"),
    "fle": _("FLe tape"),
    "flexible": _("Flexible ID tape"),
    "satin": _("Satin tape"),
    "heat_shrink_2": _("Heat-shrink tube 2:1"),
    "heat_shrink_3": _("Heat-shrink tube 3:1"),
}
# print modes by protocol name; unknown modes are shown by their name
MODE_NAMES = {
    "vivid": _("Vivid"),
    "color": _("Normal"),
    "bw": _("Black and white (sharper)"),
    "black": _("Black"),
    "red": _("Red and black"),
}
QUALITY_NAMES = {"standard": _("Standard"), "high": _("High resolution (600 dpi)")}
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
# a backend's own printer settings (Capabilities.settings): titles by key
SETTING_TITLES = {
    "dpi": _("Resolution"),
    "media": _("Label sensor"),
    "finish": _("After printing"),
    "darkness": _("Darkness"),
    "density": _("Print density"),
    "speed": _("Speed"),
    "upside_down": _("Print upside down"),
}
# their values by protocol name; resolutions are shown as "203 dpi"
CHOICE_NAMES = {
    "auto": _("As reported by the printer"),
    "printer": _("As set on the printer"),
    "gap": _("Gap between labels"),
    "mark": _("Black mark"),
    "continuous": _("Continuous media"),
    "transparent": _("Transparent labels"),
    "tear": _("Tear off"),
    "peel": _("Peel off"),
    "cut": _("Cut off"),
}
# errors that mean the printer cannot be reached at all; others need attention
CONNECTION_ERRORS = ("not_found", "usb_not_found", "timeout", "port_closed")
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
# symbols, turned clockwise
ROTATIONS = [(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")]
FITS = [("fit", _("Whole area, centred")), ("fill", _("Fill the label, edges cut off"))]
# label sizes known by name; the others are shown by their size
TARGET_NAMES = {"4x6": "4 × 6″", "a6": "A6", "a4": "A4"}


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


def setting_title(key: str) -> str:
    return SETTING_TITLES.get(key, key)


def choice_name(value: str) -> str:
    if value in CHOICE_NAMES:
        return CHOICE_NAMES[value]
    return "%s dpi" % value if value.isdigit() else value


def level_name(key: str, value: int | None) -> str:
    """A step of a Level setting; None keeps the printer's own setting."""
    if value is None:
        return CHOICE_NAMES["printer"]
    if key == "darkness":
        return _("%d %%") % value
    if key == "speed":
        # translators: print speed in inches per second, as label printers state it
        return _("%d in/s") % value
    return str(value)


def element_title(element) -> str:
    if isinstance(element, TextElement):
        first = (element.text.splitlines() or [""])[0]
        return first[:40] or _("Text")
    if isinstance(element, ImageElement):
        return element.name or _("Image")
    if isinstance(element, SymbolElement):  # icon names are English, as in the icon set
        return isolate(element.name) if element.name else _("Symbol")
    if isinstance(element, PageElement):
        return element.name or _("Shipping label")
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
    if isinstance(element, SymbolElement):
        return "starred-symbolic"
    if isinstance(element, PageElement):
        return "x-office-document-symbolic"
    return "view-grid-symbolic"


def target_name(target: Target, unit: Unit) -> str:
    """ "4 × 6″ (101.6 × 152.4 mm)", "100 × 150 mm", "Endless, 62 mm wide"."""
    if target.height_mm is None:
        return _("Endless, %(width)s wide") % {"width": unit.tape_name(target.width_mm)}
    size = unit.size(target.width_mm, target.height_mm)
    name = TARGET_NAMES.get(target.key)
    return "%s (%s)" % (name, size) if name else size


def detection_hint(detection: Detection | None) -> str:
    """What the label detection found, and what to check."""
    if detection is None or (detection.how in ("frame", "block") and detection.certain):
        return _(
            "Check that all of the label is inside the selection. Drag to move it or its "
            "edges, or draw a new one."
        )
    if detection.how in ("frame", "block"):
        return _("Several areas could be the label. Check the selection or try the next one.")
    if detection.how == "content":
        return _("Everything printed on the page is selected. Drag to choose the label.")
    return _("Nothing is printed on this page.")


def source_error_text(error: SourceError, name: str) -> str:
    if error.code == "no_pdf":
        return _("PDF files need Poppler (poppler-glib), which is not installed")
    return _("Cannot open %s") % name


# --- messages ---------------------------------------------------------------
# Lower layers report codes; the texts are made (and translated) here.


def backend_name(backend) -> str:
    """The name of a printer model in the list; brand names stay as they are."""
    names = {
        "system": _("System printer (CUPS, IPP)"),
        "escpos": _("Receipt printer (ESC/POS)"),
        # translators: %s is a printer model, e.g. "Brother QL"
        "brother_ql": _("%s, continuous rolls") % backend.name,
        # translators: %s is a printer model, e.g. "Brother QL"
        "brother_ql_diecut": _("%s, die-cut labels") % backend.name,
    }
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
        "upce_form": _(
            "Element %(n)d: no UPC-A number shortens to these UPC-E digits – check them or "
            "use UPC-A"
        ),
        "code39_full_ascii": _(
            "Element %(n)d: scanners set to Full ASCII read a $, /, + or %% before a capital "
            "letter as another character"
        ),
        "code_too_long": _("Element %(n)d: the content is too long for this type of code"),
        "code_invalid": _("Element %(n)d: the content cannot be encoded in this type of code"),
        "code_unavailable": _(
            "Element %(n)d: this type of code needs the Zint library (zint-bindings), which is "
            "not installed"
        ),
        "image_unreadable": _("Element %(n)d: the image cannot be read"),
        "symbol_unknown": _("Element %(n)d: this symbol is not in the symbol library"),
        "page_unreadable": _("Element %(n)d: the document cannot be read"),
        "pdf_missing": _(
            "Element %(n)d: PDF files need Poppler (poppler-glib), which is not installed"
        ),
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
        "unknown_variable": _("Element %(n)d: %(name)s is not a known variable"),
        "date_out_of_range": _(
            "Element %(n)d: the date of %(name)s is before the year 1 or after 9999"
        ),
        "unclosed_variable": _(
            "Element %(n)d: the variable %(name)s… is not closed – end it with }"
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
        "usb_not_found": _(
            "The printer cannot be found on USB. Check the cable and that the printer is "
            "switched on and awake, then search again."
        ),
        "timeout": _("The printer does not answer. Check that it is switched on and awake."),
        "stalled": _(
            "The printer stopped taking data. Check whether it shows a problem, such as no "
            "labels or an open cover, then try again."
        ),
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
        "resolution": _("The printer did not report its resolution in time. Choose it under “%s”.")
        % SETTING_TITLES["dpi"],
        "wrong_media": _(
            "The loaded roll does not match the label. Load a matching roll, or choose "
            "the label size or printer model of the roll that is loaded."
        ),
        "battery": _("The printer's battery is low – charge it or connect the power adapter."),
        "overheat": _("The print head is too hot. Wait until it has cooled down, then try again."),
        "unsupported_model": _(
            "Etiketto cannot print on this printer model: it does not take the raster "
            "images Etiketto sends."
        ),
        "unknown_model": _(
            "Etiketto does not know this printer model and cannot print on it yet."
        ),
        "editor_lite": _(
            "The printer is in Editor Lite mode and shows up as a USB drive. Switch "
            "Editor Lite off on the printer (its lamp goes out), then search again."
        ),
        "usb_permission": _(
            "Etiketto is not allowed to use the printer's USB port. Add your user to the "
            "group “lp” (sudo usermod -aG lp $USER), then log out and in again."
        ),
        "page_too_large": _(
            "The page is too large to make at the resolution the printer takes. Choose a "
            "smaller paper size, or print on a printer that takes PDF."
        ),
        "code_too_large": _(
            "A code does not fit on the label at this printer's resolution and would be cut "
            "off. Make the label larger or the code's content shorter."
        ),
        "serial_permission": _(
            "Etiketto is not allowed to use the printer's serial port. Add your user to the "
            "group “dialout” (sudo usermod -aG dialout $USER; on Arch Linux the group is "
            "“uucp”), then log out and in again."
        ),
        "generic": _(
            "The printer reported a problem. Switch it off and on again, then try once more."
        ),
    }
    text = texts.get(code, texts["generic"])
    if code in ("too_long", "too_wide"):
        text %= {"max": unit.format(max_mm)}
    return text


def printed_text(options: PrintOptions | None, result: PrintResult | None) -> str:
    """What became of a print job that did not fail."""
    return printed_remark(options, result) or _("Printed")


def printed_remark(options: PrintOptions | None, result: PrintResult | None) -> str | None:
    """What to tell about a print job that did not fail; None: just printed."""
    if result and result.queued:  # other jobs first: not printed yet, nothing wrong
        return _("Sent to the printer – it prints when its turn comes")
    if result and result.unconfirmed:  # it may have printed: no "Printed", no error
        return _("Sent to the printer – it did not confirm printing")
    if not (options and result and result.mode and result.mode != options.mode):
        return None
    if result.reason == "roll":
        return _("Printed with “%(used)s”, as the loaded roll needs it") % {
            "used": MODE_NAMES.get(result.mode, result.mode)
        }
    return _(
        "Colour mode “%(wanted)s” is not available on this printer, printed with “%(used)s”"
    ) % {
        "wanted": MODE_NAMES.get(options.mode, options.mode),
        "used": MODE_NAMES.get(result.mode, result.mode),
    }


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


def data_error_text(error: Exception, name: str) -> str:
    """Why a data file for serial labels cannot be used: a MergeError code
    or an OSError while reading it."""
    if isinstance(error, FileNotFoundError):
        return _("The data file %s is not there any more. Choose it again or remove it.") % name
    if not isinstance(error, MergeError):
        return _("Cannot open %s") % name
    if error.code == "no_sheet":  # the detail is the sheet's name
        return _("%(file)s has no sheet named %(sheet)s") % {"file": name, "sheet": str(error)}
    texts = {
        "not_table": _("%s is not a table. Use a CSV, ODS or XLSX file."),
        "too_large": _(
            "%s is too large for serial labels. Split it into smaller files or remove "
            "rows and columns that are not needed."
        ),
        "empty": _("%s contains no data."),
    }
    return texts.get(error.code, _("The file is damaged: %s")) % name


def series_stop_text(
    row: int, copies_printed: int = 0, position: tuple[int, int] | None = None
) -> str:
    """Where a series stopped and how to go on; with the copies of that row
    the printer had already printed, so none is printed twice by mistake;
    on label sheets where that row's label goes (position, sheet)."""
    text = _("Printing stopped at row %(row)d. To continue, print again from row %(row)d.") % {
        "row": row
    }
    if copies_printed:
        text += " " + _("Copies of row %(row)d already printed: %(copies)d.") % {
            "row": row,
            "copies": copies_printed,
        }
    if position:
        text += " " + _("Row %(row)d goes at position %(position)d of sheet %(sheet)d.") % {
            "row": row,
            "position": position[0],
            "sheet": position[1],
        }
    return text


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
