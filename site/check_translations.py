#!/usr/bin/env python3
"""Check the documentation translations.

    python3 site/check_translations.py [--strict] [LANG ...]

Errors (exit code 1):
- [[ui:…]] placeholders whose text is not a msgid of the app (po/etiketto.pot)
- translations that drop, add or change [[ui:…]] placeholders
- code blocks that are fuzzy (changed in English, the translation not) or
  whose translation has another number of lines than the English

Reported per language (errors only with --strict):
- paragraphs that are untranslated or fuzzy (changed in English since the
  translation; they appear in English until updated)
- a catalog that is not up to date with the English pages
  (run `python3 site/build.py --update-po`)
- interface terms the app itself has not translated (shown in English)

Code blocks need no translation and are not counted as paragraphs.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402


def placeholders(text: str) -> list[str]:
    return sorted(" ".join(m.group(1).split()) for m in build.UI_RE.finditer(text))


def needs_translation(entry: dict) -> bool:
    return bool(entry["msgid"]) and "code block" not in entry.get("comment", "")


def code_block_errors(lang: str, entries) -> int:
    """Code blocks of a catalog that would show something else than the
    English page: fuzzy, or translated with lines missing or added."""
    errors = 0
    for e in entries:
        if not e["msgid"] or "code block" not in e.get("comment", ""):
            continue
        first = e["msgid"].splitlines()[0][:70]
        if "fuzzy" in e["flags"]:
            print("ERROR %s: fuzzy code block: %s" % (lang, first))
            errors += 1
        elif e["msgstr"] and e["msgstr"].count("\n") != e["msgid"].count("\n"):
            print("ERROR %s: code block with other lines than the English: %s" % (lang, first))
            errors += 1
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("langs", nargs="*", help="languages (default: all)")
    parser.add_argument("--strict", action="store_true", help="fail on missing translations")
    args = parser.parse_args()

    errors = 0
    warnings = 0
    app = build.app_msgids()

    # placeholders in the English pages
    used: set[str] = set()
    for page in build.pages():
        for term in placeholders(page.read_text(encoding="utf-8")):
            used.add(term)
            if term not in app:
                print("ERROR %s: [[ui:%s]] is not a string of the app" % (page.name, term))
                errors += 1

    langs = [lang for lang in (args.langs or build.languages()) if lang != "en"]
    if not langs:
        print("No translations (site/po/*.po).")
        return 1 if errors else 0

    # merge the current English pages into copies of the catalogs
    with tempfile.TemporaryDirectory() as tmp:
        tmp_po = Path(tmp) / "po"
        shutil.copytree(build.DOC_PO, tmp_po)
        build.po4a(langs, update=True, po_dir=tmp_po, out=Path(tmp) / "out")
        for lang in langs:
            committed = {
                e["msgid"]: e
                for e in build.read_po(build.DOC_PO / (lang + ".po"))
                if not e.get("obsolete")
            }
            current = [
                e
                for e in build.read_po(tmp_po / (lang + ".po"))
                if needs_translation(e) and not e.get("obsolete")
            ]
            untranslated = [e for e in current if not e["msgstr"]]
            fuzzy = [e for e in current if e["msgstr"] and "fuzzy" in e["flags"]]
            stale = [e for e in current if e["msgid"] not in committed]
            done = len(current) - len(untranslated) - len(fuzzy)
            print(
                "%s: %d/%d paragraphs translated, %d fuzzy, %d untranslated"
                % (lang, done, len(current), len(fuzzy), len(untranslated))
            )
            for e in fuzzy + untranslated:
                kind = "fuzzy" if e in fuzzy else "untranslated"
                print("  %s: %s" % (kind, e["msgid"].splitlines()[0][:70]))
            if stale:
                print("  catalog out of date: run python3 site/build.py --update-po")
            warnings += len(fuzzy) + len(untranslated) + (1 if stale else 0)

            for e in current:
                translated = e["msgstr"] and "fuzzy" not in e["flags"]
                if translated and placeholders(e["msgstr"]) != placeholders(e["msgid"]):
                    print("ERROR %s: placeholders changed in: %s" % (lang, e["msgid"][:70]))
                    errors += 1

            errors += code_block_errors(lang, committed.values())

            terms = build.catalog(build.APP_PO / (lang + ".po"))
            missing = sorted(t for t in used if t in app and t not in terms)
            if missing:
                print("  app has no %s translation for: %s" % (lang, ", ".join(missing)))
                warnings += len(missing)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
