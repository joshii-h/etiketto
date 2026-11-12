# Etiketto

Label designer for the Linux desktop. Design labels with text, images,
symbols and codes, see them exactly as they will be printed, and print them over the
network on endless tape or on die-cut labels. Etiketto talks to each
printer in its own language, finds it on the network and explains printer
problems in plain words.

GTK 4 and libadwaita, in 27 languages: English, German, French, Italian,
Spanish, Portuguese (Portugal and Brazil), Dutch, Swedish, Finnish,
Estonian, Polish, Hungarian, Russian, Ukrainian, Turkish, Arabic, Hindi,
Thai, Vietnamese, Indonesian, Malay, Korean, Japanese and Chinese
(Traditional for Taiwan and Hong Kong, Simplified).

User documentation (English, German): <https://etiketto.joshuahirsig.xyz>

## Supported printers

| Printer | Labels | What Etiketto does with it | Protocol |
|---|---|---|---|
| Brother VC-500W (ColAura) | Colour on ZINK tape cassettes, 9 to 50 mm wide, any length | Detects the cassette, remaining tape and the cassette's age; vivid, normal or black-and-white colours; full, half or no cut; colour adjustment | Brother's native protocol on TCP port 9100 or USB ([docs/vc500w.md](docs/vc500w.md)) |
| cab EOS2, EOS5, SQUIX 4, MACH 4S | Black on die-cut labels up to 105.6 mm wide, 203, 300 or 600 dpi, thermal direct or transfer | Reads the resolution and the measured label distance and warns if the roll does not match; cancels a job stopped by a problem | JScript on TCP port 9100 or USB ([docs/cab.md](docs/cab.md)) |
| System printer (CUPS, IPP) | Paper or labels in any size the printer takes: office printers, printers set up in CUPS (USB too), IPP Everywhere printers on the network | Lists the CUPS queues and the IPP printers on the network; prints the label as one page at exactly its size, or on label sheets (A4, US Letter, over 1700 sheets); explains paper, toner and cover problems | IPP to CUPS on this computer or straight to the printer ([docs/system.md](docs/system.md)) |

Connections: network (LAN or Wi-Fi) for all of them; USB too, through the
kernel's printer driver: cab printers and the VC-500W are found on USB by
themselves, and the system printer reaches every printer CUPS has, USB
included. On USB, the VC-500W and cab printers have not been tried on a
printer yet. Your user needs to be in the group `lp`
(`sudo usermod -aG lp $USER`, then log in again). The Flatpak has
`--device=all` for this: `/dev/usb/lp*` is not covered by `--device=usb`.

Each printer family is a backend; adding one does not touch the rest of
the app, see [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-printer).
Developed with a VC-500W, an EOS2/300 and, for the system printer,
CUPS's simulated IPP Everywhere printer and an HP LaserJet Pro 4002. Not supported yet: continuous
media and the cutter of cab printers.

## Screenshots

<picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/main-dark.png"><img src="docs/screenshots/main.png" alt="Main window: QR code and text on a 50 mm label"></picture>

| | |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/colour-dark.png"><img src="docs/screenshots/colour.png" alt="Full-colour label with a background image"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/sticker-dark.png"><img src="docs/screenshots/sticker.png" alt="Square sticker with an image and border"></picture> |
| Full colour: background image printed edge to edge | Sticker: fixed 50 × 50 mm, image and border |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/expand-dark.png"><img src="docs/screenshots/expand.png" alt="Editing a text element"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/print-dark.png"><img src="docs/screenshots/print.png" alt="Print dialog with cut and colour mode"></picture> |
| Text with any font, colour and size | Copies, cut mode, vivid or normal colours |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/cable-de-dark.png"><img src="docs/screenshots/cable-de.png" alt="German interface, 12 mm cable label"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/die-cut-dark.png"><img src="docs/screenshots/die-cut.png" alt="Die-cut 57 × 32 mm label for a cab printer"></picture> |
| German interface (27 languages in total), 12 mm cable label | Die-cut label on a cab EOS2: QR code and text on 57 × 32 mm |

Screenshots use the default GNOME style; Etiketto follows your desktop's light or dark mode and accent colour.

The labels shown are in [`docs/examples`](docs/examples); open them with Etiketto.

## Features

- Finds the printer on the network by itself and shows its state
- Endless tape: as long as the content, or a fixed length
- Die-cut labels: common or custom sizes, content across or along the roll
- Label sheets on office printers: over 1700 sheets (Avery, Zweckform,
  Herma …) found by part number or size, start at any position on a partly
  used sheet, outlines for a test print; warns about the printer's margins
- Text with any installed font, bold/italic, colour, multiple lines, size
  fitted to the label or fixed in pt; emoji and font fallback via Pango
- Images and photos, background colour, background image, border
- Codes: QR code, Data Matrix, PDF417, Aztec; Code 128, Code 39, Code 93,
  EAN-13, EAN-8, UPC-A, UPC-E, ITF-14 (with bearer bars); GS1-128 and GS1
  DataMatrix with the application identifiers checked (check digits, dates,
  lengths) and in brackets in the text below the bars
- Every module of a code a whole number of printer dots, with the quiet
  zone its standard asks for, pure black and white, never resampled on printers with
  square dots (except on a colour page too large to send without loss,
  over 8 MB: then JPEG); the VC-500W prints its vivid and colour modes at
  another number of lines per inch along the tape, so there the label is
  resampled along the tape
- Symbol library: over 7000 pictograms (Material Design Icons) found by
  name, alias or category (in English, as the icon set names them),
  recently used ones at hand; drawn as vectors at the printer's resolution,
  in colour, or on black-and-white label printers in black or white with
  sharp edges
- Live preview at print resolution: what you see is what is printed
- Millimetres or inches, chosen from the system language
- Printer problems explained in plain words, with what to do;
  *Report a problem* prepares an issue with all the details
- Save labels as `.etiketto` files (images and symbols embedded) and reuse them as templates
- Drag and drop images or `.etiketto` files onto the window
- Export as PNG

What each printer adds is listed under [Supported printers](#supported-printers).

## Install

Requirements from your distribution: Python 3.11+, PyGObject with GTK 4
and libadwaita, pycairo, gettext. Everything else goes into a private
virtual environment.

```sh
git clone https://git.joshuahirsig.xyz/joshii/etiketto.git
cd etiketto
./install.sh                # per user, no root; ./install.sh --uninstall
```

| Distribution | system packages |
|---|---|
| Debian / Ubuntu | `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi-cairo python3-venv gettext` |
| Fedora | `python3-gobject gtk4 libadwaita python3-cairo gettext` |
| Arch | `python-gobject gtk4 libadwaita python-cairo gettext` |
| Gentoo | `dev-python/pygobject gui-libs/gtk:4 gui-libs/libadwaita dev-python/pycairo sys-devel/gettext` |

Then start **Etiketto** from the app menu and choose the **Printer model**.
Etiketto looks for the printer on the network; otherwise enter its host
name, e.g. `VC-500W1234.local` or `EOS2.local` (needs mDNS / nss-mdns), or
its IP address. For the VC-500W, outside Flatpak, the host is also read from
`~/.config/labelprinter/config.json` if you use the `labelprinter` CLI.
In Flatpak, `.local` names are resolved through the system's Avahi daemon.

### Flatpak (build locally)

```sh
flatpak install --user flathub org.gnome.Sdk//51 org.gnome.Platform//51
flatpak-builder --user --install --force-clean build-dir \
    build-aux/flatpak/xyz.joshuahirsig.Etiketto.yml
flatpak run xyz.joshuahirsig.Etiketto
```

Check it like Flathub does:

```sh
flatpak install --user flathub org.flatpak.Builder
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest build-aux/flatpak/xyz.joshuahirsig.Etiketto.yml
```

## How labels are laid out

Elements (text, image, symbol, code) are placed one after another along the label,
each filling the label's height unless a text or symbol has a fixed size.

- **Endless tape**: the label is either exactly as long as its content plus
  the spacing (*Automatic length*) or has a fixed length; the content is
  then aligned and, if needed, scaled down. Printers with a cutter add a
  few millimetres of unprintable margin at the ends.
- **Die-cut labels** have the size of the labels on the roll. The content
  runs across the roll, as the label is read when it comes out of the
  printer, or along it like on tape, and is aligned and scaled to fit.

## Development

```sh
uv venv --system-site-packages .venv
uv pip install --python .venv/bin/python -e . pytest hypothesis ruff
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
LANGUAGE=de .venv/bin/python -m etiketto.app
```

Translations: `xgettext` (see CONTRIBUTING.md) → `po/etiketto.pot`, strings in
`tools/strings_<lang>.py`, `python3 tools/fill_po.py` writes the
`.po` files; `install.sh` compiles them.

## Contributing

Contributions are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Issues and
pull requests on [GitHub](https://github.com/joshii-h/etiketto) are welcome,
no Forgejo account needed: they are carried over automatically to the
[Forgejo repository](https://git.joshuahirsig.xyz/joshii/etiketto), where
review and merge happen.

## Credits

The Brother VC-500W protocol was reverse engineered by
[m7i.org](https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/);
Etiketto prints through its descendant
[labelprinter](https://github.com/sgrimee/labelprinter-vc500w)
(m7i.org → lenchan139 → sgrimee).

The VC-500W on USB follows the observations of
[barnguard](https://github.com/JulianaGaibler/barnguard) (Juliana Gaibler,
MPL-2.0), written anew; no barnguard code is included.

Apple Raster (URF) for AirPrint printers follows the byte layout of
[libcups](https://github.com/OpenPrinting/libcups),
[PAPPL](https://github.com/michaelrsweet/pappl) and
[libcupsfilters](https://github.com/OpenPrinting/libcupsfilters)
(OpenPrinting, Apple Inc., Michael R Sweet, Till Kamppeter; Apache-2.0),
written anew; no code of theirs is included.

Printing on cab printers follows cab's public
[JScript Programming Manual](https://www.cab.de/en/programming); no
reverse engineering and no cab code are involved.

Most code types are encoded by [Zint](https://zint.org.uk) (libzint,
Copyright (C) 2009-2025 Robin Stuart and contributors, BSD-3-Clause) through
[zint-bindings](https://github.com/bindreams/zint-bindings) (Anna Zhukova,
Apache-2.0); Etiketto takes Zint's modules and draws them itself. The
Flatpak ships Zint's license as `share/licenses/xyz.joshuahirsig.Etiketto/zint/LICENSE`.
QR codes come from [segno](https://github.com/heuer/segno) (BSD-3-Clause),
Code 128 and EAN-13 from
[python-barcode](https://github.com/WhyNotHugo/python-barcode) (MIT).

The label sheet templates come from the template database of
[gLabels](https://github.com/j-evins/glabels-qt) by Jaye Evins, MIT license,
Copyright (C) 2001-2026 Jaye Evins
([`etiketto/sheets/LICENSE.glabels`](etiketto/sheets/LICENSE.glabels)).

The symbols are the icons of
[Material Design Icons](https://pictogrammers.com/library/mdi/) 7.4.47 by
[Pictogrammers](https://pictogrammers.com) (Austin Andrews and contributors),
Apache License 2.0
([`etiketto/symbols/LICENSE.mdi`](etiketto/symbols/LICENSE.mdi)); some are
based on Google's Material Icons (Apache-2.0). The "Brand / Logo" category
and further icons showing brands or trademarks are left out
([`tools/mdi_symbols.py`](tools/mdi_symbols.py)). Etiketto draws the icons
with its own SVG path parser.

## License

AGPL-3.0-or-later, like labelprinter which it builds on; the label sheet
templates (`etiketto/sheets/templates.json`) are MIT licensed, the symbols
(`etiketto/symbols/mdi.json.gz`) Apache-2.0 licensed, see Credits. Not affiliated with
Brother Industries or cab Produkttechnik GmbH & Co. KG; "Brother",
"ColAura", "VC-500W", "cab", "EOS", "SQUIX" and "MACH" are trademarks of their
owners.
