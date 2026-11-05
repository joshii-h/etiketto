"""GTK4 / libadwaita user interface."""

from __future__ import annotations

import copy
import gettext
import io
import json
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from etiketto import __version__, device  # noqa: E402
from etiketto.model import (  # noqa: E402
    TAPE_WIDTHS_MM,
    CodeElement,
    ImageElement,
    Label,
    TextElement,
)
from etiketto.render import DEFAULT_SAFE_MM, print_image, render  # noqa: E402

APP_ID = "xyz.joshuahirsig.Etiketto"
LOCALE_DIR = Path(__file__).parent / "locale"
_ = gettext.translation("etiketto", LOCALE_DIR, fallback=True).gettext

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "etiketto"
TEMPLATE_DIR = Path(GLib.get_user_data_dir()) / "etiketto" / "templates"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

CUT_MODES = [("full", _("Full cut")), ("half", _("Half cut")), ("none", _("No cut"))]
PRINT_MODES = [("vivid", _("Vivid")), ("normal", _("Normal"))]
ALIGNS = [("start", _("Start")), ("center", _("Centre")), ("end", _("End"))]
TEXT_ALIGNS = [("left", _("Left")), ("center", _("Centre")), ("right", _("Right"))]
SYMBOLOGIES = [("qr", _("QR code")), ("code128", "Code 128"), ("ean13", "EAN-13")]

# Preview: screen pixels per mm at zoom 1
PREVIEW_PX_PER_MM = 6.0


# --- small helpers ----------------------------------------------------------


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


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
    return dict(SYMBOLOGIES).get(element.symbology, element.symbology) + ": " + element.data[:30]


def element_icon(element) -> str:
    if isinstance(element, TextElement):
        return "format-text-bold-symbolic"
    if isinstance(element, ImageElement):
        return "image-x-generic-symbolic"
    return "view-grid-symbolic"


# --- window -----------------------------------------------------------------


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Etiketto")
        self.set_default_size(1200, 760)
        self.settings = load_settings()
        self.label = Label(elements=[TextElement(text=_("Label"))])
        self.path: Path | None = None
        self.dirty = False
        self.rendered = None
        self.printer_info: device.PrinterInfo | None = None
        self.printing = False
        self._render_source = 0
        self.zoom = 1.0
        self.fit_zoom = True

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        toolbar.set_content(self._build_body())
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self._install_actions()
        self._install_drop_target()
        self.connect("close-request", self._on_close_request)

        self.rebuild_sidebar()
        self._update_title()
        self.schedule_render()
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
        templates.connect("notify::active", lambda b, _p: b.get_active() and self._fill_templates())
        header.pack_start(templates)

        save_button = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save"))
        save_button.set_action_name("win.save")
        header.pack_start(save_button)

        menu = Gio.Menu()
        menu.append(_("New label"), "win.new")
        menu.append(_("Save as…"), "win.save-as")
        menu.append(_("Save as template…"), "win.save-template")
        menu.append(_("Export PNG…"), "win.export")
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
            adjustment.connect(
                "notify::page-size", lambda *_a: self.fit_zoom and self._apply_zoom()
            )
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

    def rebuild_sidebar(self) -> None:
        for group in list(getattr(self, "_groups", [])):
            self.sidebar.remove(group)
        self._groups = [self._printer_group(), self._label_group(), self._elements_group()]
        for group in self._groups:
            self.sidebar.add(group)

    def _printer_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Printer"))
        self.printer_row = Adw.ActionRow(title=_("Searching…"))
        self.printer_row.set_subtitle_lines(2)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.add_css_class("flat")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda _b: self.refresh_printer())
        self.printer_row.add_suffix(refresh)
        group.add(self.printer_row)
        self._update_printer_row()

        host = Adw.EntryRow(title=_("Host name or IP address"))
        host.set_text(self.host or "")
        host.set_show_apply_button(True)
        host.connect("apply", self._on_host_apply)
        group.add(host)

        group.add(
            spin_row(
                _("Safety margin"),
                self.safe_mm,
                0,
                3,
                0.1,
                1,
                self._on_safe_margin,
                subtitle=_(
                    "mm kept free for content and border; the background is printed edge to edge"
                ),
            )
        )
        return group

    @property
    def safe_mm(self) -> float:
        return float(self.settings.get("safe_mm", DEFAULT_SAFE_MM))

    def _on_safe_margin(self, value: float) -> None:
        # a printer setting, not part of the label: no dirty flag
        self.settings["safe_mm"] = round(value, 1)
        save_settings(self.settings)
        self.schedule_render()

    def _label_group(self) -> Adw.PreferencesGroup:
        lab = self.label
        group = Adw.PreferencesGroup(title=_("Label"))

        widths = [(str(w), "%d mm" % w) for w in TAPE_WIDTHS_MM]
        self.tape_row = combo_row(
            _("Tape width"), widths, str(lab.tape_mm), lambda v: self.change(tape_mm=int(v))
        )
        group.add(self.tape_row)

        auto = Adw.SwitchRow(title=_("Automatic length"), subtitle=_("As long as the content"))
        auto.set_active(lab.auto_length)
        group.add(auto)

        length = spin_row(
            _("Length"),
            lab.length_mm,
            15,
            1000,
            1,
            0,
            lambda v: self.change(length_mm=v),
            subtitle=_("mm"),
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

        group.add(
            spin_row(
                _("Spacing"),
                lab.margin_mm,
                0,
                20,
                0.5,
                1,
                lambda v: self.change(margin_mm=v),
                subtitle=_("mm at the ends and between elements"),
            )
        )

        group.add(
            spin_row(
                _("Edge spacing"),
                lab.edge_mm,
                0,
                10,
                0.5,
                1,
                lambda v: self.change(edge_mm=v),
                subtitle=_("extra mm along the tape edges"),
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

        border = spin_row(
            _("Border"),
            lab.border_mm,
            0,
            5,
            0.1,
            1,
            lambda v: self.change(border_mm=v),
            subtitle=_("mm, 0 = none"),
        )
        border.add_suffix(color_button(lab.border_color, lambda v: self.change(border_color=v)))
        group.add(border)
        return group

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
        try:
            self.rendered = render(self.label, safe_mm=self.safe_mm)
        except Exception as e:  # noqa: BLE001 - keep the app usable, show the reason
            self.rendered = None
            self.picture.set_paintable(None)
            self.size_label.set_text(_("Problem: %s") % e)
            return False
        buf = io.BytesIO()
        self.rendered.image.save(buf, format="PNG")
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))
        self.picture.set_paintable(texture)
        self._apply_zoom()

        info = _("%(width)d × %(length).0f mm") % {
            "width": self.label.tape_mm,
            "length": self.rendered.length_mm,
        }
        if self.rendered.overflow:
            info += " · " + _("content scaled down to fit")
        if self.rendered.warnings:
            info += " · " + _("Problem: %s") % "; ".join(self.rendered.warnings)
        self.size_label.set_text(info)
        return False

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
        return self.settings.get("host") or device.configured_host()

    def _on_host_apply(self, row: Adw.EntryRow) -> None:
        self.settings["host"] = row.get_text().strip()
        save_settings(self.settings)
        self.refresh_printer()

    def refresh_printer(self) -> None:
        host = self.host
        if not host:
            self.printer_info = None
            self._update_printer_row(_("No printer configured"), _("Enter its host name below"))
            return
        self._update_printer_row(_("Connecting…"), host)

        def work():
            try:
                info = device.query(host)
                GLib.idle_add(self._printer_found, info)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._printer_failed, host, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _printer_found(self, info: device.PrinterInfo) -> bool:
        self.printer_info = info
        self._update_printer_row()
        if (
            info.tape_mm
            and info.tape_mm != self.label.tape_mm
            and self.path is None
            and not self.dirty
        ):
            self.label.tape_mm = info.tape_mm
            self.tape_row.set_selected(TAPE_WIDTHS_MM.index(info.tape_mm))
            self.dirty = False
            self._update_title()
        return False

    def _printer_failed(self, host: str, error: str) -> bool:
        self.printer_info = None
        self._update_printer_row(
            _("Printer not reachable"), "%s: %s" % (host, (error.splitlines() or [""])[0])
        )
        return False

    def _update_printer_row(self, title: str | None = None, subtitle: str | None = None) -> None:
        if not hasattr(self, "printer_row"):
            return
        info = self.printer_info
        if title is None and info:
            title = "Brother VC-500W"  # the printer reports its codename ("Wedge")
            if info.tape_mm:
                parts = [_("%d mm cassette") % info.tape_mm]
                if info.remaining_mm is not None and info.total_mm:
                    parts.append(
                        _("%(left)d of %(total)d mm left")
                        % {"left": info.remaining_mm, "total": info.total_mm}
                    )
                subtitle = " · ".join(parts)
            else:
                subtitle = _("No cassette inserted")
        self.printer_row.set_title(GLib.markup_escape_text(title or _("Searching…")))
        self.printer_row.set_subtitle(GLib.markup_escape_text(subtitle or ""))

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
                    "The label is designed for %(label)d mm tape, but a %(tape)d mm cassette is "
                    "inserted. The printer would scale it to the tape."
                )
                % {"label": self.label.tape_mm, "tape": info.tape_mm},
            )
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("print", _("Print anyway"))
            dialog.set_response_appearance("print", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _d, r: r == "print" and self._print_dialog())
            dialog.present(self)
            return
        self._print_dialog()

    def _print_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Print label"),
            body=_("%(width)d × %(length).0f mm")
            % {"width": self.label.tape_mm, "length": self.rendered.length_mm},
        )
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        options = {
            "copies": 1,
            "cut": self.settings.get("cut", "full"),
            "mode": self.settings.get("mode", "vivid"),
        }
        box.append(spin_row(_("Copies"), 1, 1, 99, 1, 0, lambda v: options.update(copies=int(v))))
        box.append(combo_row(_("Cut"), CUT_MODES, options["cut"], lambda v: options.update(cut=v)))
        box.append(
            combo_row(_("Colours"), PRINT_MODES, options["mode"], lambda v: options.update(mode=v))
        )
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("print", _("Print"))
        dialog.set_response_appearance("print", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("print")

        def response(_d, result):
            if result == "print":
                self.settings.update(cut=options["cut"], mode=options["mode"])
                save_settings(self.settings)
                self._start_print(options["copies"], options["cut"], options["mode"])

        dialog.connect("response", response)
        dialog.present(self)

    def _start_print(self, copies: int, cut: str, mode: str) -> None:
        host = self.host
        if not host:
            self.toast(_("No printer configured"))
            return
        image = print_image(render(copy.deepcopy(self.label), safe_mm=self.safe_mm))
        self.printing = True
        self.print_button.set_sensitive(False)
        self.print_button.set_label(_("Printing…"))

        def progress(n, total):
            GLib.idle_add(
                self.print_button.set_label,
                _("Printing %(n)d/%(total)d…") % {"n": n, "total": total},
            )

        def work():
            try:
                device.print_label(
                    image, host, mode=mode, cut=cut, copies=copies, progress=progress
                )
                GLib.idle_add(self._print_done, None)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._print_done, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _print_done(self, error: str | None) -> bool:
        self.printing = False
        self.print_button.set_sensitive(True)
        self.print_button.set_label(_("Print"))
        self.toast(_("Printing failed: %s") % error if error else _("Printed"))
        self.refresh_printer()
        return False

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
        except (OSError, ValueError, TypeError) as e:
            self.toast(_("Cannot open %(name)s: %(error)s") % {"name": path.name, "error": e})

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
                self.toast(_("Cannot save: %s") % e)
                return
            self.toast(_("Saved %s") % path.name)

        dialog.save(self, None, done)

    def _write(self, path: Path) -> None:
        try:
            self.label.save(path)
        except OSError as e:
            self.toast(_("Cannot save: %s") % e)
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
            try:
                render(self.label, safe_mm=self.safe_mm).image.save(
                    path, format="PNG", dpi=(317, 317)
                )
            except Exception as e:  # noqa: BLE001
                self.toast(_("Cannot save: %s") % e)
                return
            self.toast(_("Exported"))

        dialog.save(self, None, done)

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
            except (OSError, ValueError, TypeError) as e:
                self.toast(str(e))
                return
            # a template starts a new, unsaved label
            self.set_label(label, None)

        self._confirm_discard(load)

    def on_new(self, *_args) -> None:
        def reset():
            tape = (
                self.printer_info.tape_mm if self.printer_info and self.printer_info.tape_mm else 50
            )
            self.set_label(Label(tape_mm=tape, elements=[TextElement(text=_("Label"))]), None)

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

    def _install_actions(self) -> None:
        actions = {
            "open": self.on_open,
            "save": self.on_save,
            "save-as": self.on_save_as,
            "save-template": self.on_save_template,
            "export": self.on_export,
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

    def _maybe_screenshot(self, window):
        """Development aid for README screenshots:
        ETIKETTO_SCREENSHOT=out.png [ETIKETTO_SCREENSHOT_STATE=expand|print]"""
        shot = GLib.getenv("ETIKETTO_SCREENSHOT")
        if not shot:
            return
        state = GLib.getenv("ETIKETTO_SCREENSHOT_STATE") or ""

        def prepare():
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
            return False

        GLib.timeout_add(1500, prepare)
        GLib.timeout_add(3000, self._screenshot, window, shot)

    def _screenshot(self, window, path):
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
            developer_name="joshii-h",
            license_type=Gtk.License.AGPL_3_0,
            comments=_("Label designer for the Brother VC-500W"),
            website="https://git.joshuahirsig.xyz/joshii/etiketto",
        ).present(self.get_active_window())


def main() -> int:
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
