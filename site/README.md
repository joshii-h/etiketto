# User documentation

Published at <https://etiketto.joshuahirsig.xyz> with every release
(`.forgejo/workflows/docs.yml`), built with [Zensical](https://zensical.org).

| Path | Content |
|---|---|
| `docs/*.md` | English pages, the source for every language |
| `po/<lang>.po` | translations, one entry per paragraph |
| `po/etiketto-docs.pot` | template, generated |
| `zensical.toml` | configuration, shared by all languages |
| `overrides/`, `assets/` | language selector that stays on the page, colours |
| `build.py` | builds everything into `build/public/` |
| `check_translations.py` | reports missing, fuzzy and broken translations |
| `build/` | output, not committed |

## Build

Needs po4a and gettext (`apt install po4a`, `emerge app-text/po4a`, …) and
Zensical:

```sh
python3 -m venv /tmp/zensical && /tmp/zensical/bin/pip install -r site/requirements.txt
/tmp/zensical/bin/python site/build.py          # all languages
python3 -m http.server -d site/build/public     # open http://localhost:8000
```

Screenshots are taken from the app for every language (list in
`tools/screenshots.list`): `tools/screenshots.sh --docs [lang …]`. Without
them the build uses the English README screenshots.

## Writing

- Short: bullet points, one fact per line. The app is translated into 27
  languages; the documentation is in English and German so far, and every
  sentence is to be translated into each language added.
- Prefer a screenshot (add it to `tools/screenshots.list`) over a description.
- Name buttons and settings as `[[ui:Safety margin]]` with the exact English
  text of the app (`po/etiketto.pot`). The build inserts the app's own
  translation, so translators skip these words.
- Use explicit heading ids for links between pages: `## Wi-Fi setup { #wifi }`.
- No printer names in sentences that are about every printer: what one
  printer can do goes into its section of `printer.md`. A new printer then
  adds paragraphs instead of changing translated ones.
- After changing English text: `python3 site/build.py --update-po`, commit the
  updated `po/` files. Changed paragraphs become fuzzy in every language and
  stay English there until translated.

## Translating

- Existing language: translate the empty and fuzzy entries in `po/<lang>.po`
  (any PO editor, e.g. Poedit, GNOME Translation Editor), remove the `fuzzy`
  flag. Keep `[[ui:…]]`, links, `{ #id }` and code exactly as they are.
- New language: `msginit -i site/po/etiketto-docs.pot -o site/po/<lang>.po -l <lang>`.
  Nothing else to register: the build, the language selector and the
  screenshots pick up every `po/*.po`. Use the same code as the app (`pt_BR`,
  `zh_TW`, …).
- `python3 site/check_translations.py` lists what is left to do.
