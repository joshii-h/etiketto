# Contributing to Etiketto

Contributions are welcome: bug reports, fixes, features, translations,
documentation and testing with other cassettes or printers.

## Where

Issues and pull requests are welcome on **GitHub**
(<https://github.com/joshii-h/etiketto>); you do not need a Forgejo account.
The main repository is on Forgejo
(<https://git.joshuahirsig.xyz/joshii/etiketto>); if you have an account
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
6. **Breaking changes**, e.g. to the `.etiketto` file format or settings.

## Development setup

You need Python 3.11+, PyGObject with GTK 4 and libadwaita, pycairo and
gettext from your distribution (see README for package names).

```sh
git clone https://git.joshuahirsig.xyz/joshii/etiketto.git
cd etiketto
uv venv --system-site-packages .venv
uv pip install --python .venv/bin/python -e . pytest pytest-cov hypothesis ruff zxing-cpp
.venv/bin/python -m etiketto.app
```

## Before you open a pull request

Run `tools/check.sh`: it runs the same checks as the CI (formatting, lint,
tests with coverage, mypy, bandit, complexity, translations, metadata).
It also requires every string to be translated; `ALLOW_UNTRANSLATED=1
tools/check.sh` skips that locally, the CI does not. If you cannot translate
new strings, say so in the pull request and a maintainer helps. In detail:

```sh
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/python -m pytest --cov=etiketto --cov-report=term-missing
```

- All tests pass and total coverage stays **at or above 90 %**.
- New behaviour comes with tests that check the actual result, not only
  that nothing crashed.
- Tests never print on a real printer and never contact the network: use
  the fake printers in `tests/conftest.py` and `tests/fake_backends.py`.
- Changes to rendering keep the label geometry intact: the background is
  printed edge to edge, content and border stay inside the safety margin.

## Adding a printer

Etiketto talks to printers through backends in `etiketto/backends/`. A new
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

Not supported yet: printers connected by USB or Bluetooth (the interface
asks for a host name or IP address). That needs an extension of the
interface first; please open an issue before you start.

1. Create `etiketto/backends/<name>.py` with a subclass of
   `etiketto.backends.base.Backend`:

   ```python
   class MyPrinter(Backend):
       id = "myprinter"  # stored in .etiketto files, never change it
       name = "Maker Model"  # shown in the interface
       capabilities = Capabilities(
           tape_widths_mm=(12, 24),  # empty for die-cut printers
           dpi=300,  # resolution across the tape
           modes=(PrintMode("standard", lpi=300),),
           color_model="mono",  # "color", "mono" or "two-color"
           cut_modes=("full",),  # empty: the printer has no cutter
           reports_remaining=False,
           supports_color_adjust=False,
           continuous=True,  # endless tape, False: die-cut labels (see above)
           label_sizes=(),  # die-cut: LabelSize(width_mm, height_mm), default first
           max_width_mm=None,  # die-cut: widest label
           data_formats=("jpeg",),  # the first one is the default
           min_length_mm=0,  # shorter labels are extended
           max_length_mm=None,  # longer labels are refused
       )

       def discover(self, timeout=3.0): ...  # -> list[FoundPrinter]
       def query(self, host): ...  # -> PrinterInfo
       def print_label(self, image, host, options, progress=None): ...
   ```

   - `discover` returns printers announced on the network, `[]` if there is
     no mDNS; `etiketto.backends.mdns.browse()` does the Avahi part.
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
   - Override `raster()` if the printer needs a different resolution, the
     exact tape height in pixels or background beyond the tape edges (bleed),
     `configured_host()` if there is a CLI configuration to read, and
     `display_name(info)` if the printer reports its exact model.
   - `modes(info)` returns the modes the connected printer reports
     (`PrinterInfo.modes`), falling back to `capabilities.modes`.
   - Override `preview(image, mode)` if a mode changes the result (the
     VC-500W shows black and white as grey).

2. Register it in `etiketto/backends/__init__.py` (`_load()`), or ship it as
   a separate package with an entry point in the group `etiketto.backends`
   that names the class.

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
   `docs/<name>.md`, and the trademark in the README and the metainfo.
   Nothing else names printers, so no existing sentence needs a new
   translation.

Labels store the backend id in the `printer` field; files without it belong
to the VC-500W. Die-cut labels also store `gap_mm` and `orientation`;
labels on a label sheet (page printers) store `sheet`: its id, the
positions on the first sheet and whether the content is turned. Printer settings (host, safety margin, cut, mode, colour
adjustment) are kept per backend in `settings.json` under `printers.<id>`.

Codes store their type in `symbology` (`model.SYMBOLOGIES`). A label with a
type added after the first versions (`model.BARCODES`: Data Matrix, PDF417,
Aztec, Code 39, Code 93, EAN-8, UPC-A, UPC-E, ITF-14, GS1-128, GS1
DataMatrix) is written as format version 2 with `"features": ["barcodes"]`:
version 1 readers refuse it as "newer" instead of calling it damaged, and a
reader refuses features it does not know. A label without features stays
version 1 and opens everywhere. A new feature adds a name to
`model.FEATURES`, not a version.

TODO at integration: the branches `variables-cli` and `mail-merge` bring the
same `features` list (`"serial"`, `"variables"`). When they meet this one,
keep a single `FORMAT_VERSION = 2`, `FEATURES = {"barcodes", "serial",
"variables"}` and one `Label.features()` that lists all three; the tests for
each feature stay.

## Codes

`etiketto/codes.py` encodes, `etiketto/render.py` draws. The encoders only
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
text in `ui/texts.warning_text()`.

## Label sheets

The label sheets (`etiketto/sheets/templates.json`) are converted from the
template database of [gLabels](https://github.com/j-evins/glabels-qt) (MIT
license, Copyright (C) 2001-2026 Jaye Evins). To update them, check out
gLabels and run the converter on its `templates` folder:

```sh
git clone --depth 1 https://github.com/j-evins/glabels-qt.git /tmp/glabels-qt
python3 tools/glabels_templates.py /tmp/glabels-qt/templates
```

It rewrites `templates.json` and copies the license to
`etiketto/sheets/LICENSE.glabels`; check the number of sheets it reports and
run the tests (`tests/test_sheets.py` checks that every sheet is usable).
A sheet that is missing or wrong belongs in gLabels, so that both projects
profit; fixes to the converter belong here. Sheet ids (`"Avery L7160"`, brand
and part number) are stored in `.etiketto` files: do not rename them.

## Code style

- Formatting and linting with ruff, configuration in `pyproject.toml`.
- Small, readable functions; comments explain *why*, not *what*.
- Commit messages in English, imperative mood, a short first line
  ("Fix cut mode for multiple copies"), details in the body.

## User interface

- Follow the [GNOME Human Interface Guidelines](https://developer.gnome.org/hig/)
  and use libadwaita widgets where they exist.
- No emoji in the interface. Use symbolic icons from the icon theme.
- The app follows the system's light/dark mode and accent colour; do not
  hard-code colours in the interface.
- Screenshots for the README and Flathub use the default GNOME style:
  `ETIKETTO_SCREENSHOT=out.png ADW_DISABLE_PORTAL=1 ADW_DEBUG_COLOR_SCHEME=prefer-dark`
  with an empty `XDG_CONFIG_HOME`.

## Translations

- Keep printer names out of translatable sentences ("Design and print
  labels", not "… on the Brother VC-500W"): a sentence that lists printers
  has to be translated again into every language for each new printer.
  Printer names belong in headings, tables and lists of their own, and in
  messages about a feature only that printer has.

German uses Swiss spelling ("ss", not "ß") on purpose, for everyone. A
separate `de_DE` with "ß" is welcome as its own catalog.

Etiketto is translated into German (`de`), French (`fr`), Italian (`it`),
Spanish (`es`), Portuguese (`pt`, Brazil: `pt_BR`), Dutch (`nl`), Swedish
(`sv`), Finnish (`fi`), Estonian (`et`), Polish (`pl`), Hungarian (`hu`),
Russian (`ru`), Ukrainian (`uk`), Turkish (`tr`), Arabic (`ar`), Hindi (`hi`),
Thai (`th`), Vietnamese (`vi`), Indonesian (`id`), Malay (`ms`), Korean
(`ko`), Japanese (`ja`) and Chinese (Traditional: `zh_TW`, also built as
`zh_HK` from the same strings; Simplified: `zh_CN`).

- Wrap every user-facing string in `_()`. Use named placeholders
  (`%(width)d`) so translators can reorder them.
- After changing strings:

  ```sh
  xgettext --language=Python --keyword=_ --keyword=pgettext:1c,2 --add-comments=translators: --from-code=UTF-8 \
      --package-name=etiketto -o po/etiketto.pot etiketto/*.py etiketto/ui/*.py \
      etiketto/backends/*.py etiketto/sheets/*.py
  ```

  then add the translations to `tools/strings_<lang>.py` and run
  `python3 tools/fill_po.py` (all languages, or name some: `de fr`).
  Missing strings are listed.
- New languages are welcome: add `tools/strings_<lang>.py`, the plural rule
  in `tools/fill_po.py`, and the language to the desktop and metainfo files.

## Reporting bugs

Please include the Etiketto version, your distribution, how you installed
it (Flatpak, install.sh), the printer firmware (shown in the printer's web
interface) and the cassette width. For print problems a photo of the label
helps a lot.

## License

By contributing you agree that your contribution is licensed under the
AGPL-3.0-or-later, the license of this project.

## Conduct

Be kind and assume good intent. Harassment or personal attacks are not
tolerated in issues, pull requests or reviews.
