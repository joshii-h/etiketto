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

```sh
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/python -m pytest --cov=etiketto --cov-report=term-missing
```

- All tests pass and total coverage stays **at or above 90 %**.
- New behaviour comes with tests that check the actual result, not only
  that nothing crashed.
- Tests never print on a real printer and never contact the network: mock
  `etiketto.device` instead.
- Changes to rendering keep the label geometry intact: the background is
  printed edge to edge, content and border stay inside the safety margin.

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

Etiketto is translated into German, French and Italian.

- Wrap every user-facing string in `_()`. Use named placeholders
  (`%(width)d`) so translators can reorder them.
- After changing strings:

  ```sh
  xgettext --language=Python --keyword=_ --from-code=UTF-8 \
      --package-name=etiketto -o po/etiketto.pot etiketto/*.py
  ```

  then add the translations to `tools/strings_<lang>.py` and run
  `python3 tools/fill_po.py de fr it`. Missing strings are listed.
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
