"""GTK4 / libadwaita user interface."""

from __future__ import annotations

import copy
import errno
import gettext
import io
import json
import math
import sys
import threading
from datetime import date
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from etiketto import __version__, report  # noqa: E402
from etiketto.backends import DEFAULT_BACKEND, backends, get_backend  # noqa: E402
from etiketto.backends.base import (  # noqa: E402
    Backend,
    ColorAdjust,
    PrinterError,
    PrinterInfo,
    PrintOptions,
    PrintResult,
    Progress,
)
from etiketto.model import (  # noqa: E402
    CodeElement,
    ImageElement,
    Label,
    LabelError,
    TextElement,
)
from etiketto.render import DEFAULT_SAFE_MM, Rendered, RenderWarning, render  # noqa: E402
from etiketto.units import Unit, default_unit, unit_for  # noqa: E402

APP_ID = "xyz.joshuahirsig.Etiketto"


def _locale_dir() -> Path:
    """Translations: <prefix>/share/locale (pip, install.sh, Flatpak) or a checkout's etiketto/locale."""
    for candidate in (
        Path(__file__).parent / "locale",
        Path(sys.prefix) / "share" / "locale",
        Path("/app/share/locale"),
    ):
        if any(candidate.glob("*/LC_MESSAGES/etiketto.mo")):
            return candidate
    return Path(__file__).parent / "locale"


LOCALE_DIR = _locale_dir()
TRANSLATION = gettext.translation("etiketto", LOCALE_DIR, fallback=True)
_ = TRANSLATION.gettext


def pgettext(context: str, message: str) -> str:
    """A translation with context, for words with several meanings ("May")."""
    return TRANSLATION.pgettext(context, message)


RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


def translation_is_rtl(translation=TRANSLATION) -> bool:
    """True if the loaded catalog is a right-to-left language. GTK only takes
    the direction from the system locale, not from LANGUAGE."""
    language = translation.info().get("language", "") if hasattr(translation, "info") else ""
    return language.split("_")[0].split("-")[0].lower() in RTL_LANGUAGES


def isolate(text: str) -> str:
    """Wrap left-to-right content (links, codes) so it stays intact in RTL text."""
    return "\u2068" + text + "\u2069"


CONFIG_DIR = Path(GLib.get_user_config_dir()) / "etiketto"
TEMPLATE_DIR = Path(GLib.get_user_data_dir()) / "etiketto" / "templates"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

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
TEXT_ALIGNS = [("left", _("Left")), ("center", _("Centre")), ("right", _("Right"))]
SYMBOLOGIES = [("qr", _("QR code")), ("code128", "Code 128"), ("ean13", "EAN-13")]

# Preview: screen pixels per mm at zoom 1
PREVIEW_PX_PER_MM = 6.0


# --- small helpers ----------------------------------------------------------


# settings of Etiketto 0.1 that belong to the VC-500W
LEGACY_PRINTER_KEYS = ("host", "safe_mm", "cut", "mode")


def migrate_settings(settings: dict) -> dict:
    """Move the printer settings of Etiketto 0.1 to settings["printers"]["vc500w"]."""
    legacy = {key: settings.pop(key) for key in LEGACY_PRINTER_KEYS if key in settings}
    if not legacy:
        return settings
    if not isinstance(settings.get("printers"), dict):
        settings["printers"] = {}
    entry = settings["printers"].setdefault(DEFAULT_BACKEND, {})
    if legacy.get("mode") == "normal":  # labelprinter's name for "color"
        legacy["mode"] = "color"
    for key, value in legacy.items():
        entry.setdefault(key, value)
    return settings


def load_settings() -> dict:
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return migrate_settings(settings) if isinstance(settings, dict) else {}


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=1))
    tmp.replace(SETTINGS_FILE)


def rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "#%02x%02x%02x" % (
        round(rgba.red * 255),
        round(rgba.green * 255),
        round(rgba.blue * 255),
    )


def hex_to_rgba(value: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not rgba.parse(value):
        rgba.parse("#000000")
    return rgba


def string_list(items) -> Gtk.StringList:
    return Gtk.StringList.new([label for _key, label in items])


def combo_row(title: str, items, current: str, on_change) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, model=string_list(items))
    keys = [key for key, _label in items]
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
    return row


def color_button(value: str, on_change) -> Gtk.ColorDialogButton:
    button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
    button.set_rgba(hex_to_rgba(value))
    button.set_valign(Gtk.Align.CENTER)
    button.connect("notify::rgba", lambda b, _p: on_change(rgba_to_hex(b.get_rgba())))
    return button


def spin_row(title, value, lower, upper, step, digits, on_change, subtitle=None) -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lower, upper, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_digits(digits)
    row.set_value(value)
    row.connect("notify::value", lambda r, _p: on_change(r.get_value()))
    return row


def image_from_path(path) -> ImageElement:
    """Read an image file into an element; ValueError if it is not an image."""
    from PIL import Image

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


def warning_text(warning: RenderWarning) -> str:
    texts = {
        "code_empty": _("Element %(n)d: the code is empty"),
        "ean13_digits": _("Element %(n)d: EAN-13 needs exactly 12 digits"),
        "ean13_check": _("Element %(n)d: the EAN-13 check digit is wrong"),
        "code128_charset": _(
            "Element %(n)d: Code 128 only accepts Latin letters, digits and punctuation"
        ),
        "image_unreadable": _("Element %(n)d: the image cannot be read"),
        "element_failed": _("Element %(n)d cannot be drawn"),
        "no_room": _("Safety margin, border and edge spacing leave no room – reduce them"),
        "fixed_too_short": _("The fixed length is shorter than the spacing – increase it"),
        "background_unreadable": _("The background image cannot be read"),
    }
    return texts.get(warning.key, warning.key) % warning.params


def printer_error_text(error: Exception, unit: Unit) -> str:
    """What went wrong and what to do, for any error from a backend."""
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
        "generic": _(
            "The printer reported a problem. Switch it off and on again, then try once more."
        ),
    }
    if not isinstance(error, PrinterError):
        return texts["generic"]
    text = texts.get(error.code, texts["generic"])
    if error.code == "too_long":
        text %= {"max": unit.format(float(error.params.get("max_mm", 0)))}
    return text


def stage_text(stage: str) -> str | None:
    """Printer stages worth telling the user about."""
    return {
        "PREHEATING": _("Warming up…"),
        "COOLDOWN": _("Cooling down…"),
        "CLEANING": _("Cleaning…"),
        "READY_FOR_CUT": _("Swipe the printer's panel to cut the label"),
    }.get(stage)


def label_error_text(error: LabelError, name: str) -> str:
    if error.code == "newer":
        return _("This label needs a newer version of Etiketto")
    if error.code == "not_label":
        return _("%s is not an Etiketto label") % name
    return _("The file is damaged: %s") % name


def save_error_text(error: OSError, name: str) -> str:
    if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return _("Cannot save %s: no permission to write there") % name
    if error.errno in (errno.ENOSPC, errno.EDQUOT):
        return _("Cannot save %s: the disk is full") % name
    return _("Cannot save %s") % name


DOCS_URL = "https://etiketto.joshuahirsig.xyz"
# languages the documentation is available in (site/po/<lang>.po)
DOCS_LANGUAGES = ("en", "de")


def docs_url(translation=TRANSLATION) -> str:
    """The documentation in the interface language, English otherwise."""
    language = translation.info().get("language", "") if hasattr(translation, "info") else ""
    short = language.split("_")[0].split("-")[0].lower()
    return "%s/%s/" % (DOCS_URL, short if short in DOCS_LANGUAGES else "en")


def month_year(month: int, year: int) -> str:
    # translators: a month and a year, e.g. "May 2018"; reorder as your language needs
    return _("%(month)s %(year)d") % {"month": month_name(month), "year": year}


def month_name(month: int) -> str:
    return (
        pgettext("month name", "January"),
        pgettext("month name", "February"),
        pgettext("month name", "March"),
        pgettext("month name", "April"),
        pgettext("month name", "May"),
        pgettext("month name", "June"),
        pgettext("month name", "July"),
        pgettext("month name", "August"),
        pgettext("month name", "September"),
        pgettext("month name", "October"),
        pgettext("month name", "November"),
        pgettext("month name", "December"),
    )[month - 1]


def cassette_age_hint(info: PrinterInfo | None, today: date | None = None) -> str | None:
    """A hint for cassettes that seem to be more than two years old."""
    made = info.cassette.made if info and info.cassette else None
    if made is None or ((today or date.today()) - made).days <= 730:
        return None
    return _(
        "This cassette seems to be from %(month)s %(year)d. Old ZINK paper prints paler colours."
    ) % {"month": month_name(made.month), "year": made.year}


# --- window -----------------------------------------------------------------


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Etiketto")
        self.set_default_size(1200, 760)
        self.settings = load_settings()
        self.label = Label(elements=[TextElement(text=_("Label"))])
        self.path: Path | None = None
        self.dirty = False
        self.rendered: Rendered | None = None
        self.printer_info: PrinterInfo | None = None
        self.printing = False
        self._render_source = 0
        self._render_error: str | None = None
        self._query_generation = 0  # answers of older printer queries are dropped
        self.zoom = 1.0
        self.fit_zoom = True
        self._last_dialog = None
        self._chooser: Adw.AlertDialog | None = None  # the open "Choose a printer" dialog

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        self.banner = Adw.Banner(revealed=False)  # printer stage while printing
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self._build_body())
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self._install_actions()
        self._install_drop_target()
        self.connect("close-request", self._on_close_request)

        self.rebuild_sidebar()
        self._update_title()
        self.schedule_render()
        if not GLib.getenv("ETIKETTO_SCREENSHOT"):
            self.refresh_printer()

    # --- layout --------------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Etiketto")
        header.set_title_widget(self.window_title)

        open_button = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Open"))
        open_button.set_action_name("win.open")
        header.pack_start(open_button)

        self.template_menu = Gio.Menu()
        templates = Gtk.MenuButton(
            icon_name="view-list-bullet-symbolic",
            tooltip_text=_("Templates"),
            menu_model=self.template_menu,
        )
        templates.connect("notify::active", self._on_templates_menu)
        header.pack_start(templates)

        save_button = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save"))
        save_button.set_action_name("win.save")
        header.pack_start(save_button)

        menu = Gio.Menu()
        menu.append(_("New label"), "win.new")
        menu.append(_("Save as…"), "win.save-as")
        menu.append(_("Save as template…"), "win.save-template")
        menu.append(_("Export PNG…"), "win.export")
        menu.append(_("Help"), "win.help")
        menu.append(_("Report a problem…"), "win.report")
        menu.append(_("About Etiketto"), "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        self.print_button = Gtk.Button(label=_("Print"))
        self.print_button.add_css_class("suggested-action")
        self.print_button.set_action_name("win.print")
        header.pack_end(self.print_button)
        return header

    def _build_body(self) -> Gtk.Widget:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(720)

        # preview
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.preview_scroller = scroller
        self.picture = Gtk.Picture(
            can_shrink=True,
            content_fit=Gtk.ContentFit.CONTAIN,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        for adjustment in (scroller.get_hadjustment(), scroller.get_vadjustment()):
            adjustment.connect("notify::page-size", self._on_page_size)
        self.picture.set_margin_top(24)
        self.picture.set_margin_bottom(24)
        self.picture.set_margin_start(24)
        self.picture.set_margin_end(24)
        self.picture.add_css_class("label-preview")
        scroller.set_child(self.picture)
        preview_box.append(scroller)

        status = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.size_label = Gtk.Label(xalign=0, hexpand=True)
        self.size_label.add_css_class("dim-label")
        status.append(self.size_label)
        fit = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text=_("Fit to window"))
        fit.add_css_class("flat")
        fit.connect("clicked", lambda _b: self.set_zoom(None))
        status.append(fit)
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text=_("Zoom out"))
        zoom_out.add_css_class("flat")
        zoom_out.connect("clicked", lambda _b: self.set_zoom(self.zoom / 1.25))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text=_("Zoom in"))
        zoom_in.add_css_class("flat")
        zoom_in.connect("clicked", lambda _b: self.set_zoom(self.zoom * 1.25))
        status.append(zoom_out)
        status.append(zoom_in)
        preview_box.append(Gtk.Separator())
        preview_box.append(status)
        paned.set_start_child(preview_box)

        # sidebar
        side_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroller.set_size_request(420, -1)
        self.sidebar = Adw.PreferencesPage()
        side_scroller.set_child(self.sidebar)
        paned.set_end_child(side_scroller)
        return paned

    # --- sidebar ---------------------------------------------------------------

    def _on_templates_menu(self, button, _pspec) -> None:
        if button.get_active():
            self._fill_templates()

    def _on_page_size(self, *_args) -> None:
        if self.fit_zoom:
            self._apply_zoom()

    def _on_printer_chosen(self, _row, dialog, host: str) -> None:
        dialog.close()
        self.use_printer(host)

    def rebuild_sidebar(self) -> None:
        for group in list(getattr(self, "_groups", [])):
            self.sidebar.remove(group)
        self._groups = [self._printer_group(), self._label_group(), self._elements_group()]
        for group in self._groups:
            self.sidebar.add(group)

    # --- printer backend and its settings -----------------------------------------

    @property
    def backend(self) -> Backend:
        try:
            return get_backend(self.label.printer)
        except KeyError:  # a plugin that went away since the label was opened
            return get_backend(DEFAULT_BACKEND)

    @property
    def printer_settings(self) -> dict:
        """Settings of the current printer model (settings["printers"][id])."""
        printers = self.settings.get("printers")
        if not isinstance(printers, dict):
            printers = self.settings["printers"] = {}
        entry = printers.get(self.backend.id)
        if not isinstance(entry, dict):
            entry = printers[self.backend.id] = {}
        return entry

    def save_printer_setting(self, **values) -> None:
        self.printer_settings.update(values)
        save_settings(self.settings)

    @property
    def color_adjust(self) -> ColorAdjust:
        return ColorAdjust.from_dict(self.printer_settings.get("color_adjust"))

    @property
    def data_format(self) -> str:
        """The backend's default format (rawrgb for the VC-500W) unless
        settings.json names another supported one ("data_format": "jpeg")."""
        formats = self.backend.capabilities.data_formats
        value = self.printer_settings.get("data_format")
        return value if value in formats else formats[0]

    def _backend_row(self) -> Adw.ComboRow:
        items = [(b.id, b.name) for b in backends()]
        return combo_row(_("Printer model"), items, self.backend.id, self._on_backend)

    def _on_backend(self, backend_id: str) -> None:
        if backend_id == self.label.printer:
            return
        self.label.printer = backend_id
        widths = self.backend.capabilities.tape_widths_mm
        if self.label.tape_mm not in widths:
            self.label.tape_mm = min(widths, key=lambda w: abs(w - self.label.tape_mm))
        self.printer_info = None
        self.rebuild_sidebar()
        self.mark_changed()
        self.refresh_printer()

    def _printer_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Printer"))
        if len(backends()) > 1:
            group.add(self._backend_row())
        self.printer_row = Adw.ActionRow(title=_("Searching…"))
        self.printer_row.set_subtitle_lines(3)
        self.details_button = Gtk.Button(
            icon_name="dialog-information-symbolic", tooltip_text=_("Printer details")
        )
        self.details_button.add_css_class("flat")
        self.details_button.set_valign(Gtk.Align.CENTER)
        self.details_button.connect("clicked", lambda _b: self.show_printer_details())
        self.printer_row.add_suffix(self.details_button)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.add_css_class("flat")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda _b: self.refresh_printer())
        self.printer_row.add_suffix(refresh)
        search = Gtk.Button(
            icon_name="system-search-symbolic", tooltip_text=_("Search for printers")
        )
        search.add_css_class("flat")
        search.set_valign(Gtk.Align.CENTER)
        search.connect("clicked", lambda _b: self.discover_printers(choose=True))
        self.printer_row.add_suffix(search)
        group.add(self.printer_row)
        self.cassette_hint = Adw.ActionRow(visible=False, activatable=False)
        self.cassette_hint.set_title_lines(0)
        self.cassette_hint.add_css_class("dim-label")
        self.cassette_hint.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        group.add(self.cassette_hint)
        self._update_printer_row()

        host = Adw.EntryRow(title=_("Host name or IP address"))
        host.set_text(self.host or "")
        host.set_show_apply_button(True)
        host.connect("apply", self._on_host_apply)
        group.add(host)

        group.add(
            self.length_row(
                _("Safety margin"),
                self.safe_mm,
                0,
                3,
                0.1,
                self._on_safe_margin,
                _(
                    "%(unit)s kept free for content and border; the background is printed edge to edge"
                ),
            )
        )

        auto_name = _("Millimetres") if default_unit() == "mm" else _("Inches")
        group.add(
            combo_row(
                _("Units"),
                [
                    ("auto", _("Automatic (%s)") % auto_name),
                    ("mm", _("Millimetres")),
                    ("in", _("Inches")),
                ],
                self.settings.get("units", "auto"),
                self._on_units,
            )
        )
        return group

    @property
    def unit(self):
        return unit_for(self.settings.get("units"))

    def _on_units(self, value: str) -> None:
        self.settings["units"] = value
        save_settings(self.settings)
        self.rebuild_sidebar()
        self.schedule_render()
        self._update_printer_row()

    def length_row(self, title, mm, lower_mm, upper_mm, step_mm, on_change_mm, subtitle=None):
        """A spin row that shows a millimetre value in the chosen unit."""
        unit = self.unit
        digits = unit.digits if step_mm < 1 or unit.key == "in" else 0
        row = spin_row(
            title,
            unit.to_display(mm, digits),
            unit.to_display(lower_mm, digits),
            unit.to_display(upper_mm, digits),
            unit.step(step_mm),
            digits,
            lambda v: on_change_mm(round(unit.from_display(v), 3)),
            subtitle=(subtitle % {"unit": unit.symbol}) if subtitle else unit.symbol,
        )
        return row

    @property
    def safe_mm(self) -> float:
        value = self.printer_settings.get("safe_mm", DEFAULT_SAFE_MM)
        return float(value) if isinstance(value, (int, float)) else DEFAULT_SAFE_MM

    def _on_safe_margin(self, value: float) -> None:
        # a printer setting, not part of the label: no dirty flag
        self.save_printer_setting(safe_mm=round(value, 1))
        self.schedule_render()

    def _label_group(self) -> Adw.PreferencesGroup:
        lab = self.label
        group = Adw.PreferencesGroup(title=_("Label"))

        widths = [
            (str(w), self.unit.tape_name(w)) for w in self.backend.capabilities.tape_widths_mm
        ]
        self.tape_row = combo_row(
            _("Tape width"), widths, str(lab.tape_mm), lambda v: self.change(tape_mm=int(v))
        )
        group.add(self.tape_row)

        if self.backend.capabilities.continuous:
            self._add_length_rows(group)

        group.add(
            self.length_row(
                _("Spacing"),
                lab.margin_mm,
                0,
                20,
                0.5,
                lambda v: self.change(margin_mm=v),
                _("%(unit)s at the ends and between elements"),
            )
        )
        group.add(
            self.length_row(
                _("Edge spacing"),
                lab.edge_mm,
                0,
                10,
                0.5,
                lambda v: self.change(edge_mm=v),
                _("extra %(unit)s along the tape edges"),
            )
        )

        bg = Adw.ActionRow(title=_("Background colour"))
        bg.add_suffix(color_button(lab.background, lambda v: self.change(background=v)))
        group.add(bg)

        bg_image = Adw.ActionRow(title=_("Background image"))
        bg_image.set_subtitle(lab.background_image.name if lab.background_image else _("None"))
        choose = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Choose image"))
        choose.add_css_class("flat")
        choose.set_valign(Gtk.Align.CENTER)
        choose.connect("clicked", lambda _b: self.choose_image(self._set_background_image))
        bg_image.add_suffix(choose)
        if lab.background_image:
            clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text=_("Remove"))
            clear.add_css_class("flat")
            clear.set_valign(Gtk.Align.CENTER)
            clear.connect("clicked", lambda _b: self._set_background_image(None))
            bg_image.add_suffix(clear)
        group.add(bg_image)

        border = self.length_row(
            _("Border"),
            lab.border_mm,
            0,
            5,
            0.1,
            lambda v: self.change(border_mm=v),
            _("%(unit)s, 0 = none"),
        )
        border.add_suffix(color_button(lab.border_color, lambda v: self.change(border_color=v)))
        group.add(border)
        return group

    def _add_length_rows(self, group: Adw.PreferencesGroup) -> None:
        """Length, automatic length and alignment: only for endless tape."""
        lab, caps = self.label, self.backend.capabilities
        auto = Adw.SwitchRow(title=_("Automatic length"), subtitle=_("As long as the content"))
        auto.set_active(lab.auto_length)
        group.add(auto)

        lower = max(15.0, caps.min_length_mm)  # 15 mm: the smallest length a file may have
        upper = float(math.floor(min(1000.0, caps.max_length_mm or 1000.0)))
        if not lower <= lab.length_mm <= upper:
            # a file made for another printer: show and keep the same value
            lab.length_mm = min(max(lab.length_mm, lower), upper)
        length = self.length_row(
            _("Length"),
            min(max(lab.length_mm, lower), upper),
            lower,
            upper,
            1,
            lambda v: self.change(length_mm=v),
        )
        align = combo_row(_("Alignment"), ALIGNS, lab.align, lambda v: self.change(align=v))
        length.set_sensitive(not lab.auto_length)
        align.set_sensitive(not lab.auto_length)

        def on_auto(row, _p):
            self.change(auto_length=row.get_active())
            length.set_sensitive(not row.get_active())
            align.set_sensitive(not row.get_active())

        auto.connect("notify::active", on_auto)
        group.add(length)
        group.add(align)

    def _elements_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Content"),
            description=_("Elements are placed one after another along the tape"),
        )
        add_menu = Gio.Menu()
        add_menu.append(_("Text"), "win.add-text")
        add_menu.append(_("Image…"), "win.add-image")
        add_menu.append(_("QR code / barcode"), "win.add-code")
        add = Gtk.MenuButton(
            icon_name="list-add-symbolic", menu_model=add_menu, tooltip_text=_("Add")
        )
        add.add_css_class("flat")
        group.set_header_suffix(add)

        if not self.label.elements:
            empty = Adw.ActionRow(
                title=_("No content yet"), subtitle=_("Add text, an image or a code")
            )
            empty.add_css_class("dim-label")
            group.add(empty)
        for index, element in enumerate(self.label.elements):
            group.add(self._element_row(index, element))
        return group

    def _element_row(self, index: int, element) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(element_title(element)))
        row.add_prefix(Gtk.Image(icon_name=element_icon(element)))
        row.set_expanded(index == getattr(self, "_expand_index", -1))

        def retitle():
            row.set_title(GLib.markup_escape_text(element_title(element)))

        # ExpanderRow packs suffixes from the end: add them right to left
        delete = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text=_("Remove"), valign=Gtk.Align.CENTER
        )
        delete.add_css_class("flat")
        delete.connect("clicked", lambda _b, i=index: self.remove_element(i))
        row.add_suffix(delete)
        for icon, tip, delta in (
            ("go-down-symbolic", _("Move down"), 1),
            ("go-up-symbolic", _("Move up"), -1),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            target = index + delta
            button.set_sensitive(0 <= target < len(self.label.elements))
            button.connect("clicked", lambda _b, i=index, t=target: self.move_element(i, t))
            row.add_suffix(button)

        def set_attr(**kwargs):
            for key, value in kwargs.items():
                setattr(element, key, value)
            retitle()
            self.mark_changed()

        if isinstance(element, TextElement):
            view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
            view.set_top_margin(8)
            view.set_bottom_margin(8)
            view.set_left_margin(12)
            view.set_right_margin(12)
            view.get_buffer().set_text(element.text)
            view.get_buffer().connect(
                "changed",
                lambda buf: set_attr(
                    text=buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                ),
            )
            frame = Gtk.Frame(
                child=view, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8
            )
            text_row = Adw.PreferencesRow(activatable=False, child=frame)
            row.add_row(text_row)

            font_row = Adw.ActionRow(title=_("Font"))
            font_button = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER)
            font_button.set_level(Gtk.FontLevel.FAMILY)
            font_button.set_font_desc(Pango.FontDescription.from_string(element.font))
            font_button.connect(
                "notify::font-desc",
                lambda b, _p: set_attr(font=b.get_font_desc().get_family() or "Sans"),
            )
            font_row.add_suffix(font_button)
            for icon, attr in (
                ("format-text-bold-symbolic", "bold"),
                ("format-text-italic-symbolic", "italic"),
            ):
                toggle = Gtk.ToggleButton(
                    icon_name=icon, valign=Gtk.Align.CENTER, active=getattr(element, attr)
                )
                toggle.add_css_class("flat")
                toggle.connect("toggled", lambda t, a=attr: set_attr(**{a: t.get_active()}))
                font_row.add_suffix(toggle)
            row.add_row(font_row)

            size = spin_row(
                _("Size"),
                element.size_pt,
                0,
                300,
                1,
                0,
                lambda v: set_attr(size_pt=v),
                subtitle=_("pt, 0 = fill tape height"),
            )
            size.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
            row.add_row(size)
            row.add_row(
                combo_row(
                    _("Line alignment"), TEXT_ALIGNS, element.align, lambda v: set_attr(align=v)
                )
            )

        elif isinstance(element, ImageElement):
            image_row = Adw.ActionRow(title=_("Image file"), subtitle=element.name or _("None"))
            choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)

            def picked(new):
                if new:
                    element.data, element.name = new.data, new.name
                    image_row.set_subtitle(new.name)
                    retitle()
                    self.mark_changed()

            choose.connect("clicked", lambda _b: self.choose_image(picked))
            image_row.add_suffix(choose)
            row.add_row(image_row)

        else:
            row.add_row(
                combo_row(
                    _("Type"), SYMBOLOGIES, element.symbology, lambda v: set_attr(symbology=v)
                )
            )
            data = Adw.EntryRow(title=_("Content (link, text, number)"))
            data.set_text(element.data)
            data.connect("changed", lambda e: set_attr(data=e.get_text()))
            row.add_row(data)
            color = Adw.ActionRow(title=_("Colour"))
            color.add_suffix(color_button(element.color, lambda v: set_attr(color=v)))
            row.add_row(color)
        return row

    # --- model changes -----------------------------------------------------------

    def change(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self.label, key, value)
        self.mark_changed()

    def mark_changed(self) -> None:
        self.dirty = True
        self._update_title()
        self.schedule_render()

    def add_element(self, element) -> None:
        self.label.elements.append(element)
        self._expand_index = len(self.label.elements) - 1
        self.rebuild_sidebar()
        self.mark_changed()

    def move_element(self, index: int, target: int) -> None:
        items = self.label.elements
        items[index], items[target] = items[target], items[index]
        self._expand_index = target
        self.rebuild_sidebar()
        self.mark_changed()

    def remove_element(self, index: int) -> None:
        del self.label.elements[index]
        self._expand_index = -1
        self.rebuild_sidebar()
        self.mark_changed()

    def _set_background_image(self, image) -> None:
        self.label.background_image = image
        self.rebuild_sidebar()
        self.mark_changed()

    def set_label(self, label: Label, path: Path | None) -> None:
        self.label = label
        self.path = path
        self.dirty = False
        self._expand_index = -1
        self.rebuild_sidebar()
        self._update_title()
        self.schedule_render()

    def _update_title(self) -> None:
        name = self.path.name if self.path else _("Unsaved label")
        self.window_title.set_subtitle(("• " if self.dirty else "") + name)

    # --- preview -------------------------------------------------------------------

    def schedule_render(self) -> None:
        if self._render_source:
            GLib.source_remove(self._render_source)
        self._render_source = GLib.timeout_add(120, self._do_render)

    def _do_render(self) -> bool:
        self._render_source = 0
        backend = self.backend
        caps = backend.capabilities
        try:
            self.rendered = render(
                self.label,
                dpi=caps.dpi,
                safe_mm=self.safe_mm,
                min_length_mm=caps.min_length_mm,
            )
            preview = backend.preview(self.rendered.image, self.print_mode)
        except Exception as e:  # noqa: BLE001 - keep the app usable, offer a report
            self._render_failed(e)
            return False
        self._render_error = None
        buf = io.BytesIO()
        preview.save(buf, format="PNG")
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))
        self.picture.set_paintable(texture)
        self._apply_zoom()

        info = self.unit.label_size(self.label.tape_mm, self.rendered.length_mm)
        if self.rendered.overflow:
            info += " · " + _("content scaled down to fit")
        if caps.max_length_mm and self.rendered.length_mm > caps.max_length_mm + 0.01:
            info += " · " + _("Too long for this printer: at most %s") % self.unit.format(
                caps.max_length_mm
            )
        if self.rendered.warnings:
            problems = "; ".join(warning_text(w) for w in self.rendered.warnings)
            info += " · " + _("Problem: %s") % problems
        self.size_label.set_text(info)
        return False

    def _render_failed(self, error: Exception) -> None:
        self.rendered = None
        self.picture.set_paintable(None)
        message = _("The preview cannot be drawn")
        self.size_label.set_text(message)
        details = "%s: %s" % (type(error).__name__, error)
        if details != self._render_error:  # once, not on every keystroke
            self._render_error = details
            self.error(message, details)

    def _apply_zoom(self) -> None:
        if not self.rendered:
            return
        length, tape = self.rendered.length_mm, self.label.tape_mm
        if self.fit_zoom:
            avail_w = self.preview_scroller.get_hadjustment().get_page_size() - 56
            avail_h = self.preview_scroller.get_vadjustment().get_page_size() - 56
            if avail_w > 0 and avail_h > 0:
                px_per_mm = min(avail_w / length, avail_h / tape, PREVIEW_PX_PER_MM * 4)
                self.zoom = px_per_mm / PREVIEW_PX_PER_MM
        px_per_mm = PREVIEW_PX_PER_MM * self.zoom
        self.picture.set_size_request(round(length * px_per_mm), round(tape * px_per_mm))

    def set_zoom(self, zoom: float | None) -> None:
        """None = fit to the window."""
        self.fit_zoom = zoom is None
        if zoom is not None:
            self.zoom = min(8.0, max(0.1, zoom))
        self._apply_zoom()

    # --- printer ---------------------------------------------------------------------

    @property
    def host(self) -> str | None:
        return self.printer_settings.get("host") or self.backend.configured_host()

    def _on_host_apply(self, row: Adw.EntryRow) -> None:
        self.save_printer_setting(host=row.get_text().strip())
        self.refresh_printer()

    def discover_printers(self, choose: bool = False) -> None:
        """Look for printers of the current model on the network."""
        self._query_generation += 1  # a pending status answer or search is outdated now
        generation = self._query_generation
        self._update_printer_row(_("Searching for printers…"), "")
        backend = self.backend

        def work():
            found = backend.discover()
            GLib.idle_add(self._printers_discovered, found, choose, generation)

        threading.Thread(target=work, daemon=True).start()

    def _printers_discovered(self, found, choose: bool, generation: int | None = None) -> bool:
        if self._outdated(generation):
            return False
        if not found:
            self._update_printer_row(
                _("No printer found"),
                _(
                    "Make sure the printer is switched on and in the same network, or enter its address below"
                ),
            )
            return False
        if len(found) == 1 or not choose:
            self.use_printer(found[0].host)
            self.toast(_("Found %s") % found[0].name)
            return False
        if self._chooser is not None:  # never two choosers at once
            self._chooser.force_close()
        dialog = self._chooser = Adw.AlertDialog(heading=_("Choose a printer"))
        dialog.connect("closed", self._on_chooser_closed)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        for printer in found:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(printer.name),
                subtitle="%s · %s" % (isolate(printer.host), isolate(printer.address)),
                activatable=True,
            )
            row.connect("activated", self._on_printer_chosen, dialog, printer.host)
            box.append(row)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.present(self)
        if self.host:
            self.refresh_printer()
        else:
            # refresh_printer() would search again and take the first printer,
            # overriding the choice the user is about to make
            self._update_printer_row(_("Choose a printer"), "")
        return False

    def _on_chooser_closed(self, dialog) -> None:
        if self._chooser is dialog:
            self._chooser = None

    def use_printer(self, host: str) -> None:
        self.save_printer_setting(host=host)
        self.rebuild_sidebar()
        self.refresh_printer()

    def refresh_printer(self) -> None:
        host = self.host
        if not host:
            # nothing configured yet: look for one on the network
            self.discover_printers()
            return
        self._query_generation += 1
        generation = self._query_generation
        backend = self.backend
        self._update_printer_row(_("Connecting…"), isolate(host))

        def work():
            try:
                info = backend.query(host)
                GLib.idle_add(self._printer_found, info, generation)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._printer_failed, e, generation)

        threading.Thread(target=work, daemon=True).start()

    def _outdated(self, generation: int | None) -> bool:
        return generation is not None and generation != self._query_generation

    def _printer_found(self, info: PrinterInfo, generation: int | None = None) -> bool:
        if self._outdated(generation):
            return False
        self.printer_info = info
        self._update_printer_row()
        widths = self.backend.capabilities.tape_widths_mm
        if (
            info.tape_mm in widths
            and info.tape_mm != self.label.tape_mm
            and self.path is None
            and not self.dirty
        ):
            self.label.tape_mm = info.tape_mm
            self.tape_row.set_selected(widths.index(info.tape_mm))
            self.dirty = False
            self._update_title()
        return False

    def _printer_failed(self, error: Exception, generation: int | None = None) -> bool:
        if self._outdated(generation):
            return False
        self.printer_info = None
        self._update_printer_row(_("Printer not reachable"), printer_error_text(error, self.unit))
        return False

    def _remaining_text(self, info: PrinterInfo) -> str | None:
        if not self.backend.capabilities.reports_remaining:
            return None
        if info.remaining_mm is None or not info.total_mm:
            return None
        return _("%(left)s of %(total)s left") % {
            "left": self.unit.format_long(info.remaining_mm),
            "total": self.unit.format_long(info.total_mm),
        }

    def _printer_summary(self, info: PrinterInfo) -> str:
        if not info.tape_mm:
            return _("No cassette inserted")
        parts = [_("%s cassette") % isolate(self.unit.tape_name(info.tape_mm))]
        remaining = self._remaining_text(info)
        if remaining:
            parts.append(remaining)
        return " · ".join(parts)

    def _update_printer_row(self, title: str | None = None, subtitle: str | None = None) -> None:
        if not hasattr(self, "printer_row"):
            return
        info = self.printer_info
        connected = False
        if title is None and info is not None:
            # the printer itself reports a codename (the VC-500W says "Wedge")
            title, subtitle, connected = self.backend.name, self._printer_summary(info), True
        self.printer_row.set_title(GLib.markup_escape_text(title or _("Searching…")))
        self.printer_row.set_subtitle(GLib.markup_escape_text(subtitle or ""))
        self.details_button.set_sensitive(connected)
        hint = cassette_age_hint(info) if connected else None
        self.cassette_hint.set_title(GLib.markup_escape_text(hint or ""))
        self.cassette_hint.set_visible(hint is not None)

    def printer_details(self) -> list[tuple[str, str]]:
        """(title, value) pairs for the details dialog; empty values left out."""
        info = self.printer_info
        if info is None:
            return []
        unit = self.unit
        rows = [
            (_("Model"), self.backend.name),
            (_("Address"), isolate(self.host or "")),
            (_("Firmware"), info.firmware),
        ]
        if info.tape_mm:
            rows.append((_("Cassette"), isolate(unit.tape_name(info.tape_mm))))
        rows.append((_("Remaining tape"), self._remaining_text(info) or ""))
        cassette = info.cassette
        if cassette:
            printable = unit.format(cassette.printable_mm) if cassette.printable_mm else ""
            made = cassette.made
            rows += [
                (_("Printable width"), printable),
                (_("Lot number"), isolate(cassette.lot) if cassette.lot else ""),
                (_("Serial number"), isolate(cassette.serial) if cassette.serial else ""),
                # the production date is read from the serial number: a guess
                (_("Probably made"), month_year(made.month, made.year) if made else ""),
            ]
        return [(title, value) for title, value in rows if value and value != isolate("")]

    def show_printer_details(self) -> None:
        details = self.printer_details()
        if not details:
            return
        dialog = Adw.AlertDialog(heading=_("Printer details"))
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        for title, value in details:
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value))
            row.add_css_class("property")
            row.set_subtitle_selectable(True)
            box.append(row)
        hint = cassette_age_hint(self.printer_info)
        if hint:
            dialog.set_body(hint)
        dialog.set_extra_child(box)
        dialog.add_response("close", _("Close"))
        self._last_dialog = dialog
        dialog.present(self)

    # --- printing ----------------------------------------------------------------------

    def on_print(self, *_args) -> None:
        if self.printing or not self.rendered:
            return
        if not self.label.elements and not self.label.background_image:
            self.toast(_("The label is empty"))
            return
        info = self.printer_info
        if info and info.tape_mm and info.tape_mm != self.label.tape_mm:
            dialog = Adw.AlertDialog(
                heading=_("Wrong cassette"),
                body=_(
                    "The label is designed for %(label)s tape, but a %(tape)s cassette is "
                    "inserted. The printer would scale it to the tape."
                )
                % {
                    "label": isolate(self.unit.tape_name(self.label.tape_mm)),
                    "tape": isolate(self.unit.tape_name(info.tape_mm)),
                },
            )
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("print", _("Print anyway"))
            dialog.set_response_appearance("print", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", self._on_wrong_cassette_response)
            dialog.present(self)
            return
        self._print_dialog()

    def _on_wrong_cassette_response(self, _dialog, response: str) -> None:
        if response == "print":
            self._print_dialog()

    @property
    def print_mode(self) -> str:
        """The print mode of the last print (what the preview shows)."""
        return self._print_choices()["mode"]

    def _on_print_mode(self, choices: dict, mode: str) -> None:
        choices.update(mode=mode)
        self.save_printer_setting(mode=mode)
        self.schedule_render()  # black and white shows grey

    def _print_choices(self) -> dict:
        """Copies, cut and mode for the print dialog, from the last print."""
        caps = self.backend.capabilities
        cut = self.printer_settings.get("cut", "full")
        modes = [m.id for m in self.backend.modes(self.printer_info)]
        mode = self.printer_settings.get("mode")
        return {
            "copies": 1,
            "cut": cut if cut in caps.cut_modes else (caps.cut_modes or ("",))[0],
            "mode": mode if mode in modes else modes[0],
        }

    def _print_rows(self, choices: dict) -> list[Gtk.Widget]:
        """Only what the printer can do: no cut row for printers without a
        cutter, no mode row with a single mode, colour correction if supported."""
        caps = self.backend.capabilities
        rows: list[Gtk.Widget] = [
            spin_row(_("Copies"), 1, 1, 99, 1, 0, lambda v: choices.update(copies=int(v)))
        ]
        if caps.cut_modes:
            cuts = [(key, name) for key, name in CUT_MODES if key in caps.cut_modes]
            rows.append(combo_row(_("Cut"), cuts, choices["cut"], lambda v: choices.update(cut=v)))
        modes = [(m.id, MODE_NAMES.get(m.id, m.id)) for m in self.backend.modes(self.printer_info)]
        if len(modes) > 1:
            rows.append(
                combo_row(
                    _("Colours"), modes, choices["mode"], lambda v: self._on_print_mode(choices, v)
                )
            )
        if caps.supports_color_adjust:
            rows.append(self._color_adjust_row())
        return rows

    def _color_adjust_row(self) -> Adw.ExpanderRow:
        """Brightness and colour balance, a printer setting kept in settings.json."""
        row = Adw.ExpanderRow(title=_("Colour adjustment"))
        adjust = self.color_adjust
        spins = []
        reset = Gtk.Button(label=_("Reset"), valign=Gtk.Align.CENTER)
        for name, title in COLOR_ADJUST_ROWS:
            spin = spin_row(
                title,
                getattr(adjust, name),
                -ColorAdjust.STEPS,
                ColorAdjust.STEPS,
                1,
                0,
                lambda v, n=name: self._set_color_adjust(row, reset, n, int(v)),
            )
            spins.append(spin)
            row.add_row(spin)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda _b: [spin.set_value(0) for spin in spins])
        row.add_suffix(reset)
        self._show_color_adjust(row, reset)
        return row

    def _show_color_adjust(self, row: Adw.ExpanderRow, reset: Gtk.Button) -> None:
        neutral = self.color_adjust.is_neutral()
        row.set_subtitle(_("Not adjusted") if neutral else _("Adjusted"))
        reset.set_sensitive(not neutral)

    def _set_color_adjust(self, row, reset, name: str, value: int) -> None:
        values = dict(self.color_adjust.to_dict(), **{name: value})
        self.save_printer_setting(color_adjust=ColorAdjust.from_dict(values).to_dict())
        self._show_color_adjust(row, reset)

    def _print_dialog(self) -> None:
        if self.rendered is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("Print label"),
            body=self.unit.label_size(self.label.tape_mm, self.rendered.length_mm),
        )
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        choices = self._print_choices()
        for row in self._print_rows(choices):
            box.append(row)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("print", _("Print"))
        dialog.set_response_appearance("print", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("print")

        def response(_d, result):
            if result == "print":
                self.save_printer_setting(cut=choices["cut"], mode=choices["mode"])
                self._start_print(
                    PrintOptions(
                        copies=choices["copies"],
                        cut=choices["cut"],
                        mode=choices["mode"],
                        color=self.color_adjust,
                        data_format=self.data_format,
                    )
                )

        dialog.connect("response", response)
        self._last_dialog = dialog
        dialog.present(self)

    def _start_print(self, options: PrintOptions) -> None:
        host = self.host
        if not host:
            self.toast(_("No printer yet – search for one or enter its address"))
            return
        backend, label, safe_mm = self.backend, copy.deepcopy(self.label), self.safe_mm
        self.printing = True
        self.print_button.set_sensitive(False)
        self.print_button.set_label(_("Printing…"))

        def progress(event: Progress):
            GLib.idle_add(self._print_progress, event)

        def work():
            # rendering a long label takes a moment: not in the main loop
            try:
                image = backend.prepare(label, options, safe_mm)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._print_done, e, _("The label cannot be prepared for printing."))
                return
            try:
                result = backend.print_label(image, host, options, progress)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._print_done, e)
                return
            GLib.idle_add(self._print_done, None, None, options, result)

        threading.Thread(target=work, daemon=True).start()

    def _print_progress(self, event: Progress) -> bool:
        if self.printing:
            self.print_button.set_label(
                _("Printing %(n)d/%(total)d…") % {"n": event.copy, "total": event.copies}
            )
            text = stage_text(event.stage)
            self.banner.set_title(GLib.markup_escape_text(text or ""))
            self.banner.set_revealed(text is not None)
        return False

    def _print_done(
        self,
        error: Exception | None,
        message: str | None = None,
        options: PrintOptions | None = None,
        result: PrintResult | None = None,
    ) -> bool:
        self.printing = False
        self.print_button.set_sensitive(True)
        self.print_button.set_label(_("Print"))
        self.banner.set_revealed(False)
        if error:
            self.print_failed(error, message or printer_error_text(error, self.unit))
        elif options and result and result.mode and result.mode != options.mode:
            self.toast(
                _(
                    "Colour mode “%(wanted)s” is not available on this printer, printed with “%(used)s”"
                )
                % {
                    "wanted": MODE_NAMES.get(options.mode, options.mode),
                    "used": MODE_NAMES.get(result.mode, result.mode),
                }
            )
        else:
            self.toast(_("Printed"))
        self.refresh_printer()
        return False

    def print_failed(self, error: Exception, message: str) -> None:
        """A dialog: what happened and what to do, the original text on request."""
        details = getattr(error, "details", "") or "%s: %s" % (type(error).__name__, error)
        dialog = Adw.AlertDialog(heading=_("Printing failed"), body=message)
        expander = Gtk.Expander(label=_("Details"))
        expander.set_child(Gtk.Label(label=details, wrap=True, selectable=True, xalign=0))
        dialog.set_extra_child(expander)
        dialog.add_response("report", _("Report"))
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        problem = message + "\n\n" + details

        def response(_dialog, result):
            if result == "report":
                self.report_problem(problem)

        dialog.connect("response", response)
        self._last_dialog = dialog
        dialog.present(self)

    # --- files -------------------------------------------------------------------------

    def _label_filter(self) -> Gio.ListStore:
        filters = Gio.ListStore.new(Gtk.FileFilter)
        flt = Gtk.FileFilter(name=_("Etiketto labels"))
        flt.add_pattern("*.etiketto")
        filters.append(flt)
        return filters

    def choose_image(self, callback) -> None:
        dialog = Gtk.FileDialog(title=_("Choose image"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        flt = Gtk.FileFilter(name=_("Images"))
        flt.add_pixbuf_formats()
        flt.add_suffix("webp")
        filters.append(flt)
        dialog.set_filters(filters)

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                callback(image_from_path(file.get_path()))
            except ValueError as e:
                self.toast(str(e))

        dialog.open(self, None, done)

    def open_file(self, path: Path) -> None:
        try:
            self.set_label(Label.load(path), path)
        except LabelError as e:
            self.error(label_error_text(e, path.name), str(e))
        except OSError as e:
            self.error(_("Cannot open %s") % path.name, str(e))

    def on_open(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Open label"), filters=self._label_filter())

        def done(d, result):
            try:
                self.open_file(Path(d.open_finish(result).get_path()))
            except GLib.Error:
                pass

        self._confirm_discard(lambda: dialog.open(self, None, done))

    def on_save(self, *_args) -> None:
        if self.path:
            self._write(self.path)
        else:
            self.on_save_as()

    def on_save_as(self, *_args, folder: Path | None = None) -> None:
        dialog = Gtk.FileDialog(title=_("Save label"), filters=self._label_filter())
        dialog.set_initial_name((self.path.stem if self.path else _("label")) + ".etiketto")
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

        def done(d, result):
            try:
                path = Path(d.save_finish(result).get_path())
            except GLib.Error:
                return
            if path.suffix != ".etiketto":
                path = path.with_suffix(".etiketto")
            self._write(path)

        dialog.save(self, None, done)

    def on_save_template(self, *_args) -> None:
        """Store a copy as template; the open file and its state stay as they are."""
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        dialog = Gtk.FileDialog(title=_("Save as template…"), filters=self._label_filter())
        dialog.set_initial_folder(Gio.File.new_for_path(str(TEMPLATE_DIR)))
        dialog.set_initial_name((self.path.stem if self.path else _("label")) + ".etiketto")

        def done(d, result):
            try:
                path = Path(d.save_finish(result).get_path())
            except (GLib.Error, TypeError):
                return
            if path.suffix != ".etiketto":
                path = path.with_suffix(".etiketto")
            try:
                self.label.save(path)
            except OSError as e:
                self.error(save_error_text(e, path.name), str(e))
                return
            self.toast(_("Saved %s") % path.name)

        dialog.save(self, None, done)

    def _write(self, path: Path) -> None:
        try:
            self.label.save(path)
        except OSError as e:
            self.error(save_error_text(e, path.name), str(e))
            return
        self.path = path
        self.dirty = False
        self._update_title()
        self.toast(_("Saved %s") % path.name)

    def on_export(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Export PNG"))
        dialog.set_initial_name((self.path.stem if self.path else _("label")) + ".png")

        def done(d, result):
            try:
                path = Path(d.save_finish(result).get_path())
            except (GLib.Error, TypeError):
                return
            if path.suffix.lower() != ".png":
                path = path.with_suffix(".png")
            self.export_png(path)

        dialog.save(self, None, done)

    def export_png(self, path: Path) -> None:
        backend = self.backend
        caps = backend.capabilities
        dpi = caps.dpi
        try:
            rendered = render(
                self.label, dpi=dpi, safe_mm=self.safe_mm, min_length_mm=caps.min_length_mm
            )
            image = backend.preview(rendered.image, self.print_mode)
        except Exception as e:  # noqa: BLE001
            self.error(_("The preview cannot be drawn"), "%s: %s" % (type(e).__name__, e))
            return
        try:
            image.save(path, format="PNG", dpi=(dpi, dpi))
        except OSError as e:
            self.error(save_error_text(e, path.name), str(e))
            return
        self.toast(_("Exported"))

    def _fill_templates(self) -> None:
        self.template_menu.remove_all()
        files = sorted(TEMPLATE_DIR.glob("*.etiketto")) if TEMPLATE_DIR.is_dir() else []
        if not files:
            self.template_menu.append(_("No templates yet – use “Save as template…”"), None)
            return
        for path in files:
            item = Gio.MenuItem.new(path.stem, None)
            item.set_action_and_target_value(
                "win.open-template", GLib.Variant.new_string(str(path))
            )
            self.template_menu.append_item(item)

    def on_open_template(self, _action, value) -> None:
        path = Path(value.get_string())

        def load():
            try:
                label = Label.load(path)
            except (OSError, LabelError) as e:
                self.error(_("Cannot open template %(name)s") % {"name": path.stem}, str(e))
                return
            # a template starts a new, unsaved label
            self.set_label(label, None)

        self._confirm_discard(load)

    def on_new(self, *_args) -> None:
        def reset():
            widths = self.backend.capabilities.tape_widths_mm
            inserted = self.printer_info.tape_mm if self.printer_info else None
            tape = inserted if inserted in widths else (50 if 50 in widths else widths[-1])
            label = Label(tape_mm=tape, printer=self.backend.id)
            label.elements = [TextElement(text=_("Label"))]
            self.set_label(label, None)

        self._confirm_discard(reset)

    def _confirm_discard(self, then) -> None:
        if not self.dirty:
            then()
            return
        dialog = Adw.AlertDialog(
            heading=_("Discard changes?"), body=_("The current label has unsaved changes.")
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("discard", _("Discard"))
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "discard" and then())
        dialog.present(self)

    def _on_close_request(self, _window) -> bool:
        if self.printing:
            self.toast(_("Please wait until printing has finished"))
            return True
        if not self.dirty:
            return False

        def close():
            self.dirty = False
            self.close()

        self._confirm_discard(close)
        return True

    # --- misc ----------------------------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def error(self, text: str, details: str | None = None) -> None:
        """A toast for problems, with a button to report them; the details
        (original error text) only go into the report."""
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=8)
        toast.set_button_label(_("Report"))
        problem = text + ("\n\n" + details if details else "")
        toast.connect("button-clicked", lambda _t: self.report_problem(problem))
        self.toasts.add_toast(toast)

    def printer_description(self) -> str | None:
        """For bug reports: model, cassette, state and firmware (in English)."""
        info = self.printer_info
        if not info:
            return None
        parts = [
            self.backend.name,
            "%s cassette" % ("%d mm" % info.tape_mm if info.tape_mm else "no"),
            info.state,
        ]
        if info.firmware:
            parts.append("firmware " + info.firmware)
        return ", ".join(parts)

    def report_problem(self, problem: str | None = None) -> None:
        toolkit = "GTK %d.%d, libadwaita %d.%d" % (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Adw.get_major_version(),
            Adw.get_minor_version(),
        )
        self.open_url(report.issue_url(problem, self.printer_description(), toolkit))

    def open_url(self, url: str) -> None:
        """Open a web page; if no browser can be started, offer the link."""

        def launched(launcher, result):
            try:
                ok = launcher.launch_finish(result)
            except GLib.Error:
                ok = False
            if not ok:
                self.offer_link(url)

        Gtk.UriLauncher.new(url).launch(self, None, launched)

    def offer_link(self, url: str) -> None:
        """No browser could be opened: let the user copy the address."""
        toast = Adw.Toast(title=_("The browser cannot be opened"), timeout=0)
        toast.set_button_label(_("Copy link"))
        toast.connect("button-clicked", lambda _t: self.get_clipboard().set(url))
        self.toasts.add_toast(toast)

    def _install_actions(self) -> None:
        actions = {
            "open": self.on_open,
            "save": self.on_save,
            "save-as": self.on_save_as,
            "save-template": self.on_save_template,
            "export": self.on_export,
            "report": lambda *_a: self.report_problem(),
            "help": lambda *_a: self.open_url(docs_url()),
            "print": self.on_print,
            "new": self.on_new,
            "add-text": lambda *_a: self.add_element(TextElement(text=_("Text"))),
            "add-image": lambda *_a: self.choose_image(self.add_element),
            "add-code": lambda *_a: self.add_element(CodeElement()),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        template = Gio.SimpleAction.new("open-template", GLib.VariantType.new("s"))
        template.connect("activate", self.on_open_template)
        self.add_action(template)

        app = self.get_application()
        app.set_accels_for_action("win.open", ["<Control>o"])
        app.set_accels_for_action("win.save", ["<Control>s"])
        app.set_accels_for_action("win.save-as", ["<Control><Shift>s"])
        app.set_accels_for_action("win.print", ["<Control>p"])
        app.set_accels_for_action("win.help", ["F1"])
        app.set_accels_for_action("win.new", ["<Control>n"])

    def _install_drop_target(self) -> None:
        target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)

        def dropped(_t, file, _x, _y):
            if not file.get_path():
                self.toast(_("Only local files can be dropped"))
                return False
            path = Path(file.get_path())
            if path.suffix == ".etiketto":
                self._confirm_discard(lambda: self.open_file(path))
                return True
            try:
                self.add_element(image_from_path(path))
            except ValueError as e:
                self.toast(str(e))
                return False
            return True

        target.connect("drop", dropped)
        self.add_controller(target)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)

    def do_startup(self):
        Adw.Application.do_startup(self)
        if translation_is_rtl():
            Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        css = Gtk.CssProvider()
        css.load_from_string(
            ".label-preview { border-radius: 0; box-shadow: 0 1px 4px alpha(black, 0.5); }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        window = self.get_active_window() or Window(self)
        window.present()
        self._maybe_screenshot(window)

    # development aid only, driven by tools/screenshots.sh: not part of the tests
    def _maybe_screenshot(self, window):  # pragma: no cover
        """Development aid for README screenshots:
        ETIKETTO_SCREENSHOT=out.png [ETIKETTO_SCREENSHOT_STATE=expand|print]"""
        shot = GLib.getenv("ETIKETTO_SCREENSHOT")
        if not shot:
            return
        state = GLib.getenv("ETIKETTO_SCREENSHOT_STATE") or ""
        delay = int(GLib.getenv("ETIKETTO_SCREENSHOT_DELAY") or "3000")
        # GNOME defaults, whatever the developer's desktop uses
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-decoration-layout", ":close")
        settings.set_property("gtk-font-name", "Sans 11")

        def prepare():
            # a believable printer instead of the network
            window.printer_info = PrinterInfo("Wedge", window.label.tape_mm, 4120, 5000, "IDLE")
            window._update_printer_row()
            if state == "expand":
                window._expand_index = len(window.label.elements) - 1
                window.rebuild_sidebar()

                def scroll_down():
                    child = window.sidebar.get_first_child()
                    while child and not isinstance(child, Gtk.ScrolledWindow):
                        child = child.get_first_child()
                    if child:
                        adj = child.get_vadjustment()
                        adj.set_value(adj.get_upper())
                    return False

                GLib.timeout_add(500, scroll_down)
            elif state == "print":
                window._print_dialog()

                # once the dialog is mapped, clear the selected text in the copies field
                def unselect(widget=None):
                    widget = widget or window._last_dialog
                    child = widget.get_first_child() if widget else None
                    while child:
                        if isinstance(child, Gtk.Text):
                            child.select_region(0, 0)
                        unselect(child)
                        child = child.get_next_sibling()
                    return False

                GLib.timeout_add(400, unselect)
            return False

        GLib.timeout_add(max(500, delay - 1500), prepare)
        GLib.timeout_add(delay, self._screenshot, window, shot)

    def _screenshot(self, window, path):  # pragma: no cover - see _maybe_screenshot
        paintable = Gtk.WidgetPaintable.new(window)
        width, height = window.get_width(), window.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node:
            texture = window.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(path)
        self.quit()
        return False

    def do_open(self, files, _n_files, _hint):
        window = self.get_active_window() or Window(self)
        if files and files[0].get_path():
            path = Path(files[0].get_path())
            window._confirm_discard(lambda: window.open_file(path))
        window.present()
        self._maybe_screenshot(window)

    def on_about(self, *_args):
        Adw.AboutDialog(
            application_name="Etiketto",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Joshua Hirsig",
            license_type=Gtk.License.AGPL_3_0,
            comments=_("Label designer for the Brother VC-500W"),
            website="https://git.joshuahirsig.xyz/joshii/etiketto",
            issue_url="https://git.joshuahirsig.xyz/joshii/etiketto/issues",
            copyright="© 2026 Joshua Hirsig",
            # translators: replace with your name to be credited in the About dialog
            translator_credits=_("translator-credits"),
        ).present(self.get_active_window())


# entry points: running the real application is not part of the tests
def main() -> int:  # pragma: no cover
    return App().run(sys.argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
