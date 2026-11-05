# Contributing to Etiketto

Contributions are welcome: bug reports, fixes, features, translations,
documentation and testing with other cassettes or printers.

## Where

The main repository is on Forgejo:
<https://git.joshuahirsig.xyz/joshii/etiketto>. Open issues and pull requests
there.

The GitHub repository (`joshii-h/etiketto`) is a read-only mirror that is
overwritten on every push, so pull requests cannot be merged there. If you
cannot use Forgejo, open an issue or pull request on GitHub anyway and a
maintainer will carry it over, keeping you as the author.

## Review and merging

- Every change goes through a pull request; nothing is pushed to `main`
  directly by contributors.
- A pull request needs **at least one approving review from a maintainer**
  before it can be merged. Maintainers are listed in `CODEOWNERS` and are
  requested automatically. Currently that is @joshii.
- Maintainers merge; please do not merge your own pull request.
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
uv pip install --python .venv/bin/python -e . pytest pytest-cov ruff
.venv/bin/python -m etiketto.app
```

## Before you open a pull request

Run `tools/check.sh`: it runs the same checks as the CI (formatting, lint,
tests with coverage, mypy, bandit, complexity, translations, metadata).
In detail:

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
continuous-tape (or die-cut) label printer is one new module there; the
interface adapts to what the printer can do.

1. Create `etiketto/backends/<name>.py` with a subclass of
   `etiketto.backends.base.Backend`:

   ```python
   class MyPrinter(Backend):
       id = "myprinter"  # stored in .etiketto files, never change it
       name = "Maker Model"  # shown in the interface
       capabilities = Capabilities(
           tape_widths_mm=(12, 24),
           dpi=300,  # resolution across the tape
           modes=(PrintMode("standard", lpi=300),),
           color_model="mono",  # "color", "mono" or "two-color"
           cut_modes=("full",),  # empty: the printer has no cutter
           reports_remaining=False,
           supports_color_adjust=False,
           continuous=True,  # False for die-cut labels
       )

       def discover(self, timeout=3.0): ...  # -> list[FoundPrinter]
       def query(self, host): ...  # -> PrinterInfo
       def print_label(self, image, host, options, progress=None): ...
   ```

   - `discover` returns printers announced on the network, `[]` if there is
     no mDNS; `etiketto.backends.mdns.browse()` does the Avahi part.
   - `query` and `print_label` raise `PrinterError` subclasses
     (`PrinterNotFound`, `PrinterTimeout`, `PortClosed`, `NoCassette`,
     `CoverOpen`, `PaperJam`, `BadCassette`, `PrinterBusy`, `LabelTooLong`)
     with the original error text as `details`. Do not write messages for
     users in a backend: `app.py` turns the error code into a translated text.
   - `print_label` gets the image from `prepare()`: the label rendered in
     label space (x along the tape) at `raster()`'s resolution, already
     dithered for mono printers. Rotate and encode it the way the printer
     wants. Report each copy and printer stage through `progress(Progress(...))`.
   - Override `raster()` if the printer needs a different resolution, the
     exact tape height in pixels or background beyond the tape edges (bleed),
     and `configured_host()` if there is a CLI configuration to read.
   - `modes(info)` returns the modes the connected printer reports
     (`PrinterInfo.modes`), falling back to `capabilities.modes`.

2. Register it in `etiketto/backends/__init__.py` (`_load()`), or ship it as
   a separate package with an entry point in the group `etiketto.backends`
   that names the class.

3. Tests: add `tests/test_<name>.py` with a fake of the printer's protocol
   (never the real device) and a subclass of
   `tests/backend_contract.BackendContract` that provides a `harness`
   fixture (`backend`, `fail()`, `printed()`); see `TestContract` in
   `tests/test_vc500w.py`. The contract checks the capabilities, the render
   geometry for every tape, mode and data format, mono output, query, printing
   with progress, error handling and discovery.
   `test_every_backend_has_a_contract_test` fails for a registered backend
   without one. Test the protocol details (requests, bytes, error mapping)
   on top of that.

Labels store the backend id in the `printer` field; files without it belong
to the VC-500W. Printer settings (host, safety margin, cut, mode, colour
adjustment) are kept per backend in `settings.json` under `printers.<id>`.

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
  xgettext --language=Python --keyword=_ --from-code=UTF-8 \
      --package-name=etiketto -o po/etiketto.pot etiketto/*.py etiketto/backends/*.py
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
