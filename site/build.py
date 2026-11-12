#!/usr/bin/env python3
"""Build the Tavletta documentation in every language.

    python3 site/build.py                 build all languages into site/build/public
    python3 site/build.py --update-po     first update site/po/*.po from the English pages
    python3 site/build.py --no-site de    only prepare the Markdown, e.g. to proofread

English pages live in site/docs. Translations are gettext catalogs per
paragraph (site/po/<lang>.po, managed with po4a): when an English paragraph
changes, only that entry becomes fuzzy. Untranslated or fuzzy paragraphs stay
English in the build.

[[ui:Some text]] is replaced by the app's own translation of "Some text"
(po/<lang>.po of the app), so documentation and interface use the same words.

Screenshots (media/<name>.png) come from site/build/screenshots/<lang>/
(tools/screenshots.sh --docs), falling back to English and then to the
README screenshots in docs/screenshots.

Needs: po4a, and Zensical in the Python running this script.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
REPO = SITE.parent
DOCS = SITE / "docs"
DOC_PO = SITE / "po"
APP_PO = REPO / "po"
BUILD = SITE / "build"
PUBLIC = BUILD / "public"
SCREENSHOTS = BUILD / "screenshots"
BASE_URL = "https://tavletta.joshuahirsig.xyz/"

UI_RE = re.compile(r"\[\[ui:([^\]]+)\]\]")
MEDIA_RE = re.compile(r"\(media/([A-Za-z0-9_.-]+\.png)(?:#[a-z-]+)?\)")

# Names in the language selector, in the language itself
NATIVE_NAMES = {
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "es": "Español",
    "pt": "Português",
    "pt_BR": "Português (Brasil)",
    "nl": "Nederlands",
    "sv": "Svenska",
    "fi": "Suomi",
    "et": "Eesti",
    "pl": "Polski",
    "hu": "Magyar",
    "ru": "Русский",
    "uk": "Українська",
    "tr": "Türkçe",
    "ar": "العربية",
    "hi": "हिन्दी",
    "th": "ไทย",
    "vi": "Tiếng Việt",
    "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "ko": "한국어",
    "ja": "日本語",
    "zh_TW": "繁體中文（台灣）",
    "zh_HK": "繁體中文（香港）",
    "zh_CN": "简体中文",
}
# gettext locale -> Zensical theme language (labels of search, navigation, …)
THEME_LANGUAGE = {"pt_BR": "pt-BR", "zh_TW": "zh-TW", "zh_HK": "zh-Hant", "zh_CN": "zh"}


def languages() -> list[str]:
    """English first, then every language with a catalog in site/po."""
    return ["en"] + sorted(p.stem for p in DOC_PO.glob("*.po"))


def pages() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


# --- gettext catalogs -------------------------------------------------------------


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}


def _unquote(line: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), line[1:-1])


def read_po(path: Path) -> list[dict]:
    """Minimal PO reader: entries with msgid, msgstr, flags, obsolete."""
    entries: list[dict] = []
    entry: dict = {}
    field = None

    def flush():
        nonlocal entry
        if "msgid" in entry:
            entry.setdefault("msgstr", "")
            entry.setdefault("flags", set())
            entries.append(entry)
        entry = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        obsolete = line.startswith("#~")
        if obsolete:
            line = line[2:].strip()
        if not line:
            flush()
            field = None
        elif line.startswith("#,"):
            if "msgid" in entry:
                flush()
            entry.setdefault("flags", set()).update(f.strip() for f in line[2:].split(","))
        elif line.startswith("#"):
            if "msgid" in entry:
                flush()
            if line.startswith("#."):  # e.g. "#. type: Fenced code block (sh)"
                entry["comment"] = entry.get("comment", "") + line[2:].strip()
        elif line.startswith(("msgid ", "msgstr ", "msgctxt ", "msgid_plural ")):
            key, value = line.split(" ", 1)
            if key == "msgid" and "msgid" in entry:
                flush()
            field = "msgstr" if key.startswith("msgstr") else key
            entry[field] = entry.get(field, "") if key.startswith("msgstr[") else ""
            entry[field] += _unquote(value)
            entry["obsolete"] = obsolete
        elif line.startswith('"') and field:
            entry[field] += _unquote(line)
    flush()
    return entries


def catalog(path: Path) -> dict[str, str]:
    """msgid -> msgstr of the translated, non-fuzzy entries."""
    if not path.exists():
        return {}
    return {
        e["msgid"]: e["msgstr"]
        for e in read_po(path)
        if e["msgid"] and e["msgstr"] and "fuzzy" not in e["flags"] and not e.get("obsolete")
    }


def app_msgids() -> set[str]:
    return {e["msgid"] for e in read_po(APP_PO / "tavletta.pot") if e["msgid"]}


def resolve_ui(text: str, lang: str) -> str:
    """[[ui:Term]] -> **translated term** from the app's catalog, else English."""
    terms = catalog(APP_PO / (lang + ".po")) if lang != "en" else {}

    def term(match: re.Match) -> str:
        msgid = ui_msgid(match)
        return "**%s**" % terms.get(msgid, msgid)

    return UI_RE.sub(term, text)


def ui_msgid(match: re.Match) -> str:
    """The app msgid of a placeholder; po4a may have wrapped it over lines."""
    return " ".join(match.group(1).split())


# --- po4a ------------------------------------------------------------------------


def po4a(langs: list[str], update: bool, po_dir: Path = DOC_PO, out: Path = BUILD) -> None:
    """Update the catalogs (update=True) and/or write the translated pages."""
    langs = [lang for lang in langs if lang != "en"]
    if not langs:  # English only: nothing to translate (po4a needs a language)
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    cfg = BUILD / "po4a.cfg"

    def rel(path: Path) -> str:  # relative to site/, so the catalogs hold no local paths
        return os.path.relpath(path, SITE)

    lines = [
        "[po4a_langs] " + " ".join(langs),
        "[po4a_paths] %s $lang:%s/$lang.po" % (rel(po_dir / "tavletta-docs.pot"), rel(po_dir)),
        '[options] opt:"-o markdown"',
    ]
    lines += [
        "[type: text] %s $lang:%s/$lang/docs/%s" % (rel(page), rel(out), page.name)
        for page in pages()
    ]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "po4a",
        "--quiet",
        "--keep",
        "0",  # always write the page, untranslated parts stay English
        "--porefs",
        "file",
        "--wrap-po",
        "newlines",
        "--package-name",
        "tavletta-docs",
        "--copyright-holder",
        "Tavletta contributors",
        "--master-charset",
        "UTF-8",
        "--localized-charset",
        "UTF-8",
    ]
    if not update:
        cmd.append("--no-update")
    subprocess.run(cmd + [str(cfg)], check=True, cwd=SITE)
    if update:  # removed paragraphs: drop them instead of keeping them as #~
        for lang in langs:
            po = po_dir / (lang + ".po")
            subprocess.run(["msgattrib", "--no-obsolete", "-o", str(po), str(po)], check=True)


# --- one language ----------------------------------------------------------------


def stage(lang: str) -> Path:
    """Write build/<lang>/ with Markdown, media, assets, overrides and config."""
    root = BUILD / lang
    docs = root / "docs"
    if lang == "en":
        shutil.rmtree(root, ignore_errors=True)
        docs.mkdir(parents=True)
        for page in pages():
            shutil.copy(page, docs / page.name)
    shutil.rmtree(root / "overrides", ignore_errors=True)
    shutil.copytree(SITE / "overrides", root / "overrides")
    (docs / "assets").mkdir(exist_ok=True)
    shutil.copy(SITE / "assets" / "extra.css", docs / "assets" / "extra.css")
    shutil.copy(REPO / "data" / "xyz.joshuahirsig.Tavletta.svg", docs / "assets" / "logo.svg")
    # the favicon: the icon drawn for 32 px, sharp in a browser tab
    shutil.copy(REPO / "data" / "icons" / "tavletta-32.svg", docs / "assets" / "favicon.svg")

    media = docs / "media"
    media.mkdir(exist_ok=True)
    for page in docs.glob("*.md"):
        text = resolve_ui(page.read_text(encoding="utf-8"), lang)
        page.write_text(text, encoding="utf-8")
        for name in MEDIA_RE.findall(text):
            for folder in (SCREENSHOTS / lang, SCREENSHOTS / "en", REPO / "docs" / "screenshots"):
                if (folder / name).exists():
                    shutil.copy(folder / name, media / name)
                    break
            else:
                print("warning: %s: screenshot %s missing" % (lang, name), file=sys.stderr)

    (root / "zensical.toml").write_text(config(lang), encoding="utf-8")
    return root


def config(lang: str) -> str:
    text = (SITE / "zensical.toml").read_text(encoding="utf-8")
    theme_lang = THEME_LANGUAGE.get(lang, lang)
    text = re.sub(r'(?m)^language = ".*"$', 'language = "%s"' % theme_lang, text, count=1)
    text = re.sub(r'(?m)^site_url = ".*"$', 'site_url = "%s%s/"' % (BASE_URL, lang), text, count=1)
    alternates = ",\n".join(
        '  { name = "%s", link = "/%s/", lang = "%s" }'
        % (NATIVE_NAMES.get(code, code), code, code.replace("_", "-"))
        for code in languages()
    )
    return text + "\n[project.extra]\nalternate = [\n%s,\n]\n" % alternates


def zensical(root: Path, lang: str, strict: bool) -> None:
    cmd = [sys.executable, "-m", "zensical", "build", "--clean", "-f", str(root / "zensical.toml")]
    if strict:
        cmd.append("--strict")
    subprocess.run(cmd, check=True)
    target = PUBLIC / lang
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(root / "site", target)


def landing(langs: list[str]) -> None:
    """/ sends the browser to its preferred language, English otherwise."""
    links = " · ".join('<a href="%s/">%s</a>' % (c, NATIVE_NAMES.get(c, c)) for c in langs)
    known = ", ".join('"%s"' % c for c in langs)
    (PUBLIC / "index.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Tavletta</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="en/assets/favicon.svg" type="image/svg+xml">
<noscript><meta http-equiv="refresh" content="0; url=en/"></noscript>
<script>
(function () {
  var known = [%s];
  var wanted = navigator.languages || [navigator.language || "en"];
  for (var i = 0; i < wanted.length; i++) {
    var tag = wanted[i].replace("-", "_");
    var base = tag.split("_")[0];
    var hit = known.indexOf(tag) >= 0 ? tag : known.indexOf(base) >= 0 ? base : null;
    if (!hit) {
      for (var k = 0; k < known.length; k++) {
        if (known[k].split("_")[0] === base) { hit = known[k]; break; }
      }
    }
    if (hit) { location.replace(hit + "/"); return; }
  }
  location.replace("en/");
})();
</script></head>
<body><p>%s</p></body></html>
"""
        % (known, links),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("langs", nargs="*", help="languages to build (default: all)")
    parser.add_argument("--update-po", action="store_true", help="update site/po from the pages")
    parser.add_argument("--no-site", action="store_true", help="only write the Markdown")
    parser.add_argument("--strict", action="store_true", help="fail on Zensical warnings")
    args = parser.parse_args()

    all_langs = languages()
    langs = args.langs or all_langs
    unknown = sorted(set(langs) - set(all_langs))
    if unknown:
        parser.error("no catalog site/po/%s.po" % unknown[0])

    po4a(all_langs if args.update_po else langs, update=args.update_po)
    for lang in langs:
        root = stage(lang)
        if not args.no_site:
            zensical(root, lang, args.strict)
    if not args.no_site:
        landing(all_langs)
        print("Built %s into %s" % (", ".join(langs), PUBLIC))
    return 0


if __name__ == "__main__":
    sys.exit(main())
