# Etiketto

Label designer for the Linux desktop: colour labels on the **Brother
VC-500W** (also sold as ColAura; ZINK paper) and die-cut labels on **cab**
label printers (EOS2, EOS5, SQUIX 4, MACH 4S). GTK 4 and libadwaita, in
27 languages: English, German, French, Italian, Spanish, Portuguese
(Portugal and Brazil), Dutch, Swedish, Finnish, Estonian, Polish,
Hungarian, Russian, Ukrainian, Turkish, Arabic, Hindi, Thai, Vietnamese,
Indonesian, Malay, Korean, Japanese and Chinese (Traditional for Taiwan
and Hong Kong, Simplified).

Brother only ships the Color Label Editor for Windows, macOS and phones.
Etiketto brings the same workflow to Linux: the inserted cassette is
detected, the label grows with its content, and it prints over the network
in the printer's own protocol, including the **vivid** colour mode and the
**cut modes** that AirPrint does not offer.

On cab printers Etiketto prints die-cut labels of any size in cab's own
printer language (JScript), checks that the loaded roll matches the label
and explains printer problems such as an open print head or an empty roll.

User documentation (English, German): <https://etiketto.joshuahirsig.xyz>

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

- Finds the printer on the network by itself
- Cassette detection (9, 12, 19, 25, 50 mm), remaining tape and cassette details
- Automatic length (as long as the content) or a fixed length, in mm or inches
- cab printers: die-cut labels in common or custom sizes, content across or
  along the roll, a warning when the loaded labels have another size
- Text with any installed font, bold/italic, colour, multiple lines,
  size fitted to the tape or fixed in pt; emoji and font fallback via Pango
- Images and photos, background colour, background image, border
- QR codes, Code 128 and EAN-13 barcodes
- Live preview at print resolution: what you see is what is printed
- Print with vivid or normal colours or in black and white, full cut, half
  cut or no cut, copies; colour adjustment (brightness, colour balance)
  (VC-500W); in black at 203, 300 or 600 dpi with the printer's own heat and
  speed settings (cab)
- Printer problems explained in plain words, with what to do;
  *Report a problem* prepares an issue with all the details
- Save labels as `.etiketto` files (images embedded) and reuse them as templates
- Drag and drop images or `.etiketto` files onto the window
- Export as PNG
- [User documentation](https://etiketto.joshuahirsig.xyz) in English and German

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
name, e.g. `VC-500W1234.local` or `printer04.local` (needs mDNS / nss-mdns),
or its IP address. Outside Flatpak the host is also read from
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

Elements (text, image, code) are placed one after another along the tape,
each filling the tape height unless a text has a fixed size. With
*Automatic length* the label is exactly as long as its content plus the
spacing; with a fixed length the content is aligned and, if needed, scaled
down. The printer adds a few millimetres of unprintable margin at the ends
for the cutter.

## Printers

| Printer | Labels | Protocol |
|---|---|---|
| Brother VC-500W (ColAura) | ZINK tape cassettes, 9 to 50 mm, colour | Brother's native protocol on TCP port 9100, through [labelprinter](https://github.com/sgrimee/labelprinter-vc500w) ([docs/protocol.md](docs/protocol.md)) |
| cab EOS2, EOS5, SQUIX 4, MACH 4S | die-cut labels up to 105.6 mm wide, black (thermal direct or transfer) | JScript on TCP port 9100 ([docs/cab.md](docs/cab.md)) |

The cab backend was developed with an EOS2/300. Continuous media and the
cutter of cab printers are not supported yet. More printers can be added as
a backend, see [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-printer).

## Development

```sh
uv venv --system-site-packages .venv
uv pip install --python .venv/bin/python -e . pytest ruff
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

The VC-500W protocol was reverse engineered by
[m7i.org](https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/);
Etiketto prints through its descendant
[labelprinter](https://github.com/sgrimee/labelprinter-vc500w)
(m7i.org → lenchan139 → sgrimee).

Printing on cab printers follows cab's public
[JScript Programming Manual](https://www.cab.de/en/programming); no
reverse engineering and no cab code are involved.

## License

AGPL-3.0-or-later, like labelprinter which it builds on. Not affiliated with
Brother Industries or cab Produkttechnik GmbH & Co. KG; "Brother",
"ColAura", "VC-500W", "cab", "EOS", "SQUIX" and "MACH" are trademarks of their
owners.
