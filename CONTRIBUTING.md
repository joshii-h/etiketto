# Contributing to Tavletta

Contributions are welcome: bug reports, fixes, features, translations,
documentation and testing with other cassettes or printers.

## Where

Issues and pull requests are welcome on **GitHub**
(<https://github.com/joshii-h/tavletta>); you do not need a Forgejo account.
The main repository is on Forgejo
(<https://git.joshuahirsig.xyz/joshii/tavletta>); if you have an account
there, contribute there directly.

A bridge (`.github/workflows/bridge.yml`) copies GitHub pull requests and
issues to Forgejo and comments the link on GitHub. Review and merge happen on
Forgejo; your commits are kept, so GitHub then shows the pull request as
merged. The discussion can stay on GitHub.

- Pull requests that change CI workflow files or are based on an outdated
  `main` are not copied: rebase onto the current `main`; workflow changes are
  applied by a maintainer by hand.
- CI for pull requests from contributors starts once a maintainer approves it
  with the label `safe-to-test`.

## Review and merging

- Every change goes through a pull request; nothing is pushed to `main`
  directly by contributors.
- A pull request needs **at least one approving review from a maintainer**
  before it can be merged. Maintainers are listed in `CODEOWNERS` and are
  requested automatically. Currently that is @joshii.
- Maintainers merge on Forgejo, also pull requests that came from GitHub
  (see above); please do not merge your own pull request.
- Keep a pull request to one topic. Unrelated fixes go into their own pull
  request, even when they are small.
- Review comments are part of the process, not a judgement. Please answer or
  resolve each one; if you disagree, say why.

## Describing a pull request

The pull request template asks for the following; please fill in every
section that applies:

1. **What** the change does, in a few sentences.
2. **Why** it is needed: the problem, use case or issue it addresses.
3. **How** it works, if not obvious from the diff: design decisions,
   alternatives you rejected.
4. **How you tested it**: automated tests, manual steps, and if you printed,
   which printer, firmware and cassette width.
5. **Screenshots** for every visible change, in light and dark mode.
6. **Breaking changes**, e.g. to the `.tavletta` file format or settings.

## Development setup

You need Python 3.11+, PyGObject with GTK 4 and libadwaita, pycairo,
gettext and, for the icon tests and `tools/icons.py`, librsvg's GObject
introspection from your distribution (see README for package names).

```sh
git clone https://git.joshuahirsig.xyz/joshii/tavletta.git
cd tavletta
uv venv --system-site-packages .venv
uv pip install --python .venv/bin/python -e . --group dev
.venv/bin/python -m tavletta.app
```

## Before you open a pull request

Run `tools/check.sh`: it runs the same checks with the same flags as the CI
(formatting, lint, tests with coverage, mypy, bandit, complexity, the
dependency audit with pip-audit, translations, metadata; the tools are in
the `dev` dependency group).
It also requires every string to be translated; `ALLOW_UNTRANSLATED=1
tools/check.sh` skips that locally, the CI does not. If you cannot translate
new strings, say so in the pull request and a maintainer helps. In detail:

```sh
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/python -m pytest -q --cov=tavletta --cov-report=term-missing
```

- All tests pass and total coverage stays **at or above 90 %**.
- New behaviour comes with tests that check the actual result, not only
  that nothing crashed.
- Tests never print on a real printer and never contact the network: use
  the fake printers in `tests/conftest.py` and `tests/fake_backends.py`.
- Changes to rendering keep the label geometry intact: the background is
  printed edge to edge, content and border stay inside the safety margin.

## Adding a printer

Tavletta talks to printers through backends in `tavletta/backends/`. A new
**network** printer is one new module there; the interface adapts to what
the printer can do (tape widths or label sizes, modes, cutter, colour,
length and width limits). There are two kinds:

- **Continuous tape** (`continuous=True`, e.g. `vc500w.py`): the user picks
  one of `tape_widths_mm`, the label grows with its content or has a fixed
  length.
- **Die-cut labels** (`continuous=False`, e.g. `cab.py`): the user picks one
  of `label_sizes` or enters width and height (up to `max_width_mm`), the
  gap to the next label and whether the content runs across or along the
  roll. `Label.media_size()` gives width (across the print head) and height
  (along the feed); `PrintOptions.gap_mm` carries the gap. `prepare()` must
  return the label in printer orientation, see `Cab.prepare()`. Report the
  measured label distance as `PrinterInfo.pitch_mm` if the printer knows it:
  the interface then warns when the loaded labels have another size.

Not supported yet: printers connected by Bluetooth. That needs an
extension of the interface first; please open an issue before you start.

1. Create `tavletta/backends/<name>.py` with a subclass of
   `tavletta.backends.base.Backend`:

   ```python
   class MyPrinter(Backend):
       id = "myprinter"  # stored in .tavletta files, never change it
       name = "Maker Model"  # shown in the interface
       capabilities = Capabilities(
           tape_widths_mm=(12, 24),  # empty for die-cut printers
           dpi=300,  # resolution across the tape
           modes=(PrintMode("standard", lpi=300),),
           color_model="mono",  # "color", "mono" or "two-color"
           cut_modes=("full",),  # of "full", "half", "end", "none"; empty: no cutter
           reports_remaining=False,
           supports_color_adjust=False,
           continuous=True,  # endless tape, False: die-cut labels (see above)
           label_sizes=(),  # die-cut: LabelSize(width_mm, height_mm), default first
           max_width_mm=None,  # die-cut: widest label
           data_formats=("jpeg",),  # the first one is the default
           min_length_mm=0,  # shorter labels are extended
           max_length_mm=None,  # longer labels are refused
           pages=False,  # die-cut: the labels are pages (office printers, label sheets)
           media="cassette",  # continuous: "cassette" or "roll" it recognises, "paper"
           settings=(),  # Choice/Level: printer settings shown in the printer group
           qualities=(),  # e.g. ("standard", "high"); one or none: no choice
       )

       def discover(self, timeout=3.0): ...  # -> list[FoundPrinter]
       def query(self, host): ...  # -> PrinterInfo
       def print_label(self, image, host, options, progress=None): ...
   ```

   - `discover` returns printers announced on the network, `[]` if there is
     no mDNS; `tavletta.backends.mdns.browse()` does the Avahi part.
   - `query` returns a `PrinterInfo`; if the printer reports a problem, put
     the code of the matching `PrinterError` into `PrinterInfo.error` (e.g.
     `"cover_open"`), never the printer's own text. `base.open_session()`
     and `base.network_error()` cover connecting and network errors.
   - `query` and `print_label` raise `PrinterError` subclasses
     (`PrinterNotFound`, `PrinterTimeout`, `PortClosed`, `NoCassette`,
     `CoverOpen`, `PaperJam`, `BadCassette`, `PrinterBusy`, `LabelTooLong`,
     `LabelTooWide`, `OutOfLabels`, `HeadOpen`, `LabelNotFound`,
     `RibbonError`, `PrinterPaused`)
     with the original error text as `details`. Do not write messages for
     users in a backend: `ui/texts.py` turns the error code into a translated text.
   - `print_label` gets the image from `prepare()`: the label rendered in
     label space (x along the tape) at `raster()`'s resolution, already
     dithered for mono printers. Rotate and encode it the way the printer
     wants. Check `max_length_mm` with `check_length()` (die-cut:
     `max_width_mm` with `check_width()`) before sending.
     Report each copy and printer stage through `progress(Progress(...))`
     (`report_progress()` from `base.py` does that if a callback was given).
     Return a `PrintResult` if something differed from the request (e.g.
     another mode was used), else `None`.
     Serial labels call `print_label` once per row, one after the other, so
     return only when the printer has taken the job (better: printed it)
     and can take the next one.
   - Override `raster()` if the printer needs a different resolution, the
     exact tape height in pixels or background beyond the tape edges (bleed),
     `configured_host()` if there is a CLI configuration to read, and
     `display_name(info)` if the printer reports its exact model.
   - `modes(info)` returns the modes the connected printer reports
     (`PrinterInfo.modes`), falling back to `capabilities.modes`.
   - `cut_modes(info)` and `extras(info)` narrow `capabilities.cut_modes`
     and `capabilities.extras` to what the connected model can do. Extras
     are settings the print dialog offers on top (`"chain"`, `"high_res"`,
     `"feed"` with `capabilities.feed_mm`); they arrive in
     `PrintOptions.chain`, `high_res` and `feed_mm`. See the P-touch
     backend.
   - One backend instance serves every window: keep no printer state in
     it. `PrintOptions.info` carries the printer as last asked, for a
     `prepare()` that depends on the model; `capabilities.default_tape_mm`
     sets the tape of a new label.
   - Override `preview(image, mode)` if a mode changes the result (the
     VC-500W shows black and white as grey), and `renders_red(mode)` if a
     mono printer prints red too in a mode (the Brother QL's red and
     black): labels for mono printers are rendered black and white part by
     part (`render(mono=True)`: text without anti-aliasing, colours black
     or white against the background, images by their method; with
     `red=True` red parts stay red), so the backend's own conversion
     changes nothing.
   - `settings`: printer settings of the backend's own (resolution, label
     sensor, darkness, ...) as `Choice` (protocol values, the first is the
     default) or `Level` (steps; None keeps the printer's own setting). The
     interface shows a row for each in the printer group, keeps the values
     in `settings.json` and hands them over in `PrintOptions.settings`; read
     them with `Backend.setting()`. Name new keys and values in
     `ui/texts.py` (`SETTING_TITLES`, `CHOICE_NAMES`).
   - A printer that takes the label as a bitmap in a command language
     (like ZPL, TSPL, ESC/POS) can subclass `raw.RawBackend`: it finds the
     printers on USB and by mDNS by their IEEE 1284 command set, connects
     on port 9100 or USB and asks short questions where silence is no
     error. The subclass builds the job (`build_job`) and, if the language
     has a status, reads it (`ask_status`, `problem`); see `zpl.py`.
   - `qualities(info)` returns the print qualities the connected printer
     offers (`PrinterInfo.qualities`), falling back to
     `capabilities.qualities`; the chosen one is `PrintOptions.quality`.
   - A printer that takes continuous rolls and die-cut labels can be two
     backends sharing one module, one per kind (see `brother_ql.py`).

2. Register it in `tavletta/backends/__init__.py` (`_load()`), or ship it as
   a separate package with an entry point in the group `tavletta.backends`
   that names the class.

   - Connections: printers are reached over the network and over USB. If
     the printer language works on USB too, open `usb://` addresses with
     `usb.connect()` (a socket-like object on `/dev/usb/lpN`) and list
     `usb.printers()` of your maker in `discover`; see the cab backend.
   - Printers that are a serial port on USB (CDC ACM, `/dev/ttyACM*`) use
     `serialport.Port` and list `serialport.devices()`; see the NIIMBOT
     backend. Tests talk to a fake printer on a pseudo terminal; conftest
     refuses to open real serial ports.
   - Printer settings (`Capabilities.settings`: label sensor, density,
     darkness, ...) are the one mechanism for what a printer takes per job;
     override `settings(info)` if the connected model takes other values
     (NIIMBOT: its label types and density range), and `migrate_settings()`
     to update keys or values an earlier version stored in settings.json.

3. Tests: add `tests/test_<name>.py` with a fake of the printer's protocol
   (never the real device) and a subclass of
   `tests/backend_contract.BackendContract` that provides a `harness`
   fixture (`backend`, `fail()`, `printed()`); see `TestContract` in
   `tests/test_vc500w.py` and `tests/test_cab.py`. The contract checks the
   capabilities, the render geometry for every tape (or label size and
   orientation), mode and data format, mono output, query, printing with
   progress, error handling and discovery.
   `test_every_backend_has_a_contract_test` fails for a registered backend
   without one. Test the protocol details (requests, bytes, error mapping)
   on top of that.

4. Texts: a row in the "Supported printers" table of the README, an entry
   under "Supported printers" in the metainfo description, a section in
   `site/docs/printer.md` (setup and problems), protocol notes in
   `docs/<name>.md`, and the trademark in the README and the metainfo;
   the printer's names among the `Keywords` of the desktop file and the
   `<keywords>` of the metainfo, so a search finds it. Protocol knowledge
   from other projects goes into `THIRD_PARTY_NOTICES.md` (section 2) and
   `PROTOCOL_SOURCES` in `tavletta/app.py` (a test keeps both in step), code
   or data you ship into section 1 and `LEGAL`. Nothing else names
   printers, so no existing sentence needs a new translation.

Labels store the backend id in the `printer` field; files without it belong
to the VC-500W. Die-cut labels also store `gap_mm` and `orientation`;
labels on a label sheet (page printers) store `sheet`: its id, the
positions on the first sheet and whether the content is turned.
Serial labels store the path of their data file in `data_file` (relative to
the label file if the data file lies in its folder or below, else absolute)
and the sheet in `data_sheet`, never the data itself. Templates never keep
the data file. Printer settings (host, safety margin, cut, mode, colour
adjustment) are kept per backend in `settings.json` under `printers.<id>`.

Format version 2: a file lists the `"features"` it needs, a reader refuses
features it does not know ("needs a newer version") instead of printing the
label wrongly; version 1 readers refuse every version 2 file as "newer". A
label without features stays version 1 and opens everywhere.

The rule: everything added after version 1 that an older Tavletta would
print wrongly, drop when it saves, call damaged or refuse with an unclear
message is a feature, new element kinds included. A new one adds a name to
`model.FEATURES` and a line to `Label.features()`, never a version. The
features (`model.FEATURES`):

- `"barcodes"`: a code of a type added after the first versions
  (`model.BARCODES`: Data Matrix, PDF417, Aztec, Code 39, Code 93, EAN-8,
  UPC-A, UPC-E, ITF-14, GS1-128, GS1 DataMatrix); codes store their type in
  `symbology` (`model.SYMBOLOGIES`).
- `"symbols"`: a symbol element (see Symbols below).
- `"pages"`: a page element, an area of a PDF or image (shipping labels).
- `"sheets"`: printed on a label sheet (`sheet`); older versions would print
  one label of the sheet's size on a page of its own.
- `"serial"`: a linked data file (`data_file`); older versions would print
  `{Name}` as it is.
- `"variables"`: variables (`{=date}`, `{=counter}` …) in texts or codes, or
  a counter other than the default; older versions would print `{=date}` as
  it is and drop the counter when they save.
- `"styles"`: a design option set to anything but its default
  (`model.STYLE_FIELDS`: text decoration, spacing, fitting, rotation, boxes,
  outline and shadow; image mode, adjustments, crop, corners and border;
  every element's `valign` and `gap_mm`; the label's `border_radius_mm`,
  `border_style` and `border_inset_mm`). These fields are written only
  when they differ from their default, which is how earlier versions print,
  so a label without them stays readable everywhere; older versions would
  ignore them and print the label differently.
- `"shapes"`: a line or shape element (kind `shape`, `model.ShapeElement`);
  versions before it also refuse the unknown element kind.
- `"free_layout"`: the free layout (`layout: "free"`, every element at its
  own `x_mm`, `y_mm`; `tavletta/freelayout.py`) and the fields that came
  with it (`model.FREE_FIELDS`: a font weight, a QR code's error
  correction, a code's module size, height and turn, an image's height),
  written only when they differ from their default; older versions would
  place the elements one after another.

A new design option: a field with the old behaviour as its default, its
name in `model.STYLE_FIELDS` (or `LABEL_STYLE_FIELDS`), validation in
`model.py` and a test of the round trip.

## Codes

`tavletta/codes.py` encodes, `tavletta/render.py` draws. The encoders only
deliver modules (rows of `1` and `0`), never a picture: Zint's raw
`encoded_data`, segno's matrix, python-barcode's pattern. The drawing makes
each module a whole number of printer dots and keeps the quiet zone each
standard asks for (`codes.SPECS`) inside the element. QR codes (segno),
Code 128 and EAN-13 (python-barcode) keep their encoders so labels made
before print the same modules; Zint would choose other QR masks and Code 128
code sets. A new symbology needs an entry in `codes.SPECS`, in
`model.BARCODES`, in `ui/texts.SYMBOLOGY_GROUPS` and `SYMBOLOGY_HELP`, a
sample in `tests/test_codes.py` (decoded with zxing-cpp) and its input
checks: content Zint would change silently (lower case in Code 39, a wrong
check digit) is refused before it is encoded, with a warning key and its
text in `ui/texts.warning_text()`. Data Matrix, PDF417 and Aztec carry text beyond
ASCII as UTF-8 with ECI 26: without an ECI scanners guess at the bytes (and
read Latin-1 "37°" as katakana). GS1 data in square brackets
(`[01]…[21]A(B)`) is passed to Zint without its parentheses mode, so values
may contain round brackets.

## Label sheets

The label sheets (`tavletta/sheets/templates.json`) are converted from the
template database of [gLabels](https://github.com/j-evins/glabels-qt) (MIT
license, Copyright (C) 2001-2026 Jaye Evins). To update them, check out
gLabels and run the converter on its `templates` folder:

```sh
git clone --depth 1 https://github.com/j-evins/glabels-qt.git /tmp/glabels-qt
python3 tools/glabels_templates.py /tmp/glabels-qt/templates
```

It rewrites `templates.json` and copies the license to
`tavletta/sheets/LICENSE.glabels`; check the number of sheets it reports
(and the number "over 1700 sheets" in the README and `site/docs/sheets.md`),
keep the copyright years in the README credits, `THIRD_PARTY_NOTICES.md`
and `LEGAL` in `tavletta/app.py` current, and
run the tests (`tests/test_sheets.py` checks that every sheet is usable).
A sheet that is missing or wrong belongs in gLabels, so that both projects
profit; fixes to the converter belong here. Sheet ids (`"Avery L7160"`, brand
and part number) are stored in `.tavletta` files: do not rename them.

## Symbols

The symbols (`tavletta/symbols/mdi.json.gz`) are converted from a release of
[Material Design Icons](https://github.com/Templarian/MaterialDesign-SVG)
(Apache-2.0, Pictogrammers). To update them, download the release from npm
and run the converter on it:

```sh
mkdir /tmp/mdi && cd /tmp/mdi
curl -LO https://registry.npmjs.org/@mdi/svg/-/svg-7.4.47.tgz  # the new version
tar -xzf svg-7.4.47.tgz
cd - && python3 -I tools/mdi_symbols.py /tmp/mdi/package
```

It keeps name, path data, categories and aliases of every icon, without the
deprecated ones, without the category "Brand / Logo" and without further
icons showing brands, trademarks or designs from games and films
(`DENIED` in the converter: e.g. raspberry-pi, nintendo-*, pokeball,
ghost, one-up, death-star, firewire, minidisc, file-word); aliases naming a brand ("lego", "apple",
"twitter-retweet", see `BRAND_WORDS`) are dropped, the icon stays. When
updating, scan the new names and aliases for brands and extend both lists.
Kept on purpose: marks of technical standards that label a port or a
feature (bluetooth, usb, nfc, hdmi, cast, contactless payment), as a label
on a device needs them, and the key symbols apple-keyboard-* (command,
option, shift are standard key symbols in Unicode, not a logo). It stops if the release's license text is no longer the
one in `tavletta/symbols/LICENSE.mdi`: then check the new license and update
that file by hand. Update the version in the README credits,
`THIRD_PARTY_NOTICES.md` and `LICENSE.mdi` (and the holders in `LEGAL` in
`tavletta/app.py` if they change), check the number of icons it reports
and run the tests
(`tests/test_symbols.py` counts the icons and checks that every path
parses). The output depends only on the release (sorted, no timestamp).

A label stores a symbol by its name and a copy of its path data
(`name`, `path`, `size_mm`, `rotation`, `color`); the copy is what is
drawn, so a label looks the same with an icon set where the symbol was
changed, renamed or removed, and the name is what the chooser shows.
Icon names are therefore not renamed by Tavletta, but upstream may; a label
with a symbol that is no longer in the set still prints.

A label with a symbol is written as format version 2 with
`"features": ["symbols"]`. Older Tavletta versions refuse it as "needs a
newer version": those with format version 2 by the unknown feature, the
ones before by the unknown element kind `symbol` (see
`model.ELEMENT_TYPES`).

## App icon

The icon is a small slate tablet in a wooden frame with a label on it
(Tavletta: Romansh for "small tablet"). Its sources are SVG, drawn by hand
in the Adwaita palette:

| File | What |
| --- | --- |
| `data/xyz.joshuahirsig.Tavletta.svg` | the icon, 128 × 128, GNOME HIG style |
| `data/xyz.joshuahirsig.Tavletta-symbolic.svg` | symbolic, 16 × 16, one colour |
| `data/icons/tavletta-16.svg`, `-20.svg`, `-22.svg`, `-24.svg`, `-32.svg` | redrawn on that grid: thicker frame, fewer details; installed as the hicolor icons of these sizes (22 px: KDE's panels and menus, 20 px: Windows at 125 %); 40 and 48 px are the 20 and 24 px drawings at twice the size; the site's favicon is the 32 px one |
| `data/icons/tavletta-macos.svg` | macOS (Big Sur style): the tablet on an 824 × 824 plate in 1024 × 1024; written by `tools/macos_icon.py` from the main icon, do not edit it |
| `data/icons/tavletta-macos-16.svg`, `-32.svg` | the same on a 16 and 32 px grid, a simplified plate |

Keep them to SVG Tiny 1.2, which is what QtSvg (KDE) draws: paths, rects
and circles, solid fills, linear and radial gradients. No filters, masks,
clip paths, `<style>` or `style=`, patterns, images, text (letters as
paths) or arcs in path data; shadows are layered shapes with a low
`fill-opacity`. Colours as `#rrggbb` (`currentColor` only in the symbolic
icon). `tests/test_icons.py` checks this (some of it is a house rule on
top of Tiny 1.2). When the icon changes, change the small drawings with it
and run `python3 tools/macos_icon.py`; the tests fail while the macOS file
is out of date.

PNG, Windows `.ico` and macOS `.icns` files are not in the repository;
`tools/icons.py` renders them (needs librsvg's GObject introspection,
`gir1.2-rsvg-2.0` on Debian; without it the icon tests are skipped
locally and fail in the CI, which sets `CI`):

```sh
python3 tools/icons.py   # build/icons/: hicolor PNGs 16-512, tavletta.ico, tavletta.icns
python3 tools/macos_icon.py   # data/icons/tavletta-macos.svg from the main icon
```

The Swiss cross must not appear on the icon: its use is restricted in
Switzerland (Wappenschutzgesetz).

## Code style

- Formatting and linting with ruff, configuration in `pyproject.toml`.
- Small, readable functions; comments explain *why*, not *what*.
- Commit messages in English, imperative mood, a short first line
  ("Fix cut mode for multiple copies"), details in the body.

## Untrusted input

Label files and data files come from anywhere, so they are read
defensively: within size limits, and every malformed file raises one
exception type with a code (the interface turns the code into a translated
text), never a raw `KeyError`, `zipfile` or XML error.

- `tavletta/model.py`: `.tavletta` label files, `LabelError`.
- `tavletta/merge/`: data rows for serial labels from CSV, ODS and XLSX
  files (`read_table()`, `read_book()` with the sheet names), and the
  `{column}` placeholders in label texts (`fill()`, which never uses
  `str.format()` on user data). It raises `MergeError` with the codes
  `not_table`, `damaged`, `too_large`, `empty`, `no_sheet` (a sheet name
  the file does not have) and `cancelled` (the caller set `cancel`: the window
  stops a read when another file is chosen); the limits (file size,
  number of zip entries, unpacked size of each zip member, rows, columns,
  cells) are in `tavletta/merge/limits.py`. Only plain files are read (a
  named pipe would block). Hidden cells of merged ranges are empty in ODS
  and XLSX alike. XML parts with a document type or entity declaration are
  refused before parsing, and large parts are parsed as a stream. The
  readers use only the standard library (`csv`, `zipfile`, `xml.etree`); no
  third-party code is included or adapted, so nothing needs credit.
- `tavletta/serial.py` fills a copy of the label from a row (texts and code
  contents) and prints a series: one job per row through the backend's
  `print_label`, stoppable between two labels.
- Property tests (`tests/test_properties.py`, `tests/test_merge.py`) feed
  random input to these readers: keep them passing when you change one.

## User interface

- Follow the [GNOME Human Interface Guidelines](https://developer.gnome.org/hig/)
  and use libadwaita widgets where they exist.
- No emoji in the interface. Use symbolic icons from the icon theme.
- The app follows the system's light/dark mode and accent colour; do not
  hard-code colours in the interface.
- Screenshots for the README and Flathub use the default GNOME style:
  `TAVLETTA_SCREENSHOT=out.png ADW_DISABLE_PORTAL=1 ADW_DEBUG_COLOR_SCHEME=prefer-dark`
  with an empty `XDG_CONFIG_HOME`.

## Translations

- Keep printer names out of translatable sentences ("Design and print
  labels", not "… on the Brother VC-500W"): a sentence that lists printers
  has to be translated again into every language for each new printer.
  Printer names belong in headings, tables and lists of their own, and in
  messages about a feature only that printer has.

German uses Swiss spelling ("ss", not "ß") on purpose, for everyone. A
separate `de_DE` with "ß" is welcome as its own catalog.

Tavletta is translated into German (`de`), French (`fr`), Italian (`it`),
Spanish (`es`), Portuguese (`pt`, Brazil: `pt_BR`), Dutch (`nl`), Swedish
(`sv`), Finnish (`fi`), Estonian (`et`), Polish (`pl`), Hungarian (`hu`),
Russian (`ru`), Ukrainian (`uk`), Turkish (`tr`), Arabic (`ar`), Hindi (`hi`),
Thai (`th`), Vietnamese (`vi`), Indonesian (`id`), Malay (`ms`), Korean
(`ko`), Japanese (`ja`) and Chinese (Traditional: `zh_TW`, also built as
`zh_HK` from the same strings; Simplified: `zh_CN`).

- Symbol names, aliases and categories stay English, as Material Design
  Icons names them, and the symbol search is in English: do not translate
  them (there are over 7000, and a label file stores the name).
- Wrap every user-facing string in `_()`. Use named placeholders
  (`%(width)d`) so translators can reorder them.
- After changing strings:

  ```sh
  xgettext --language=Python --keyword=_ --keyword=pgettext:1c,2 --add-comments=translators: --from-code=UTF-8 \
      --package-name=tavletta -o po/tavletta.pot tavletta/*.py tavletta/ui/*.py \
      tavletta/backends/*.py tavletta/merge/*.py tavletta/sheets/*.py \
      tavletta/shipping/*.py tavletta/symbols/*.py tavletta/server/*.py
  ```

  then add the translations to `tools/strings_<lang>.py` and run
  `python3 tools/fill_po.py` (all languages, or name some: `de fr`).
  Missing strings are listed.
- New languages are welcome: add `tools/strings_<lang>.py`, the plural rule
  in `tools/fill_po.py`, and the language to the desktop and metainfo files.

## Reporting bugs

Please include the Tavletta version, your distribution, how you installed
it (Flatpak, install.sh), the printer firmware (shown in the printer's web
interface) and the cassette width. For print problems a photo of the label
helps a lot.

## License

By contributing you agree that your contribution is licensed under the
AGPL-3.0-or-later, the license of this project.

## Conduct

Be kind and assume good intent. Harassment or personal attacks are not
tolerated in issues, pull requests or reviews.
