"""The word lists of tools/strings_<lang>.py hold no strings the app does
not use any more (po/etiketto.pot), so translators work on what counts."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LISTS = sorted((ROOT / "tools").glob("strings_*.py"))


def _fill_po():
    spec = importlib.util.spec_from_file_location("fill_po", ROOT / "tools" / "fill_po.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pot_keys() -> set[str]:
    fill_po = _fill_po()
    pot = (ROOT / "po" / "etiketto.pot").read_text(encoding="utf-8")
    keys = {"translator-credits"}
    for block in pot.split("\n\n")[1:]:
        msgid = re.search(r'msgid ((?:".*"\n?)+)msgstr', block)
        if not msgid:
            continue
        context = re.search(r'msgctxt ((?:".*"\n?)+)msgid', block)
        key = fill_po.unquote(msgid.group(1))
        keys.add(fill_po.unquote(context.group(1)) + "\x04" + key if context else key)
    return keys


@pytest.mark.parametrize("path", LISTS, ids=[p.stem for p in LISTS])
def test_no_unused_strings(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    unused = sorted(set(module.TRANSLATIONS) - _pot_keys())
    assert not unused, "%s: %s" % (path.name, unused[:5])


def _site_check():
    path = ROOT / "site" / "check_translations.py"
    spec = importlib.util.spec_from_file_location("check_translations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_fuzzy_or_changed_code_block_of_the_documentation_is_an_error(capsys):
    """A code block translated with a line missing (or left fuzzy after the
    English changed) would show other commands than the English page."""
    check = _site_check()
    block = "Fenced code block (sh)"
    english = "etiketto print @@ a @@\netiketto print @@ a @@ --data @@ b @@\n"
    entries = [
        {"msgid": english, "msgstr": english, "flags": set(), "comment": block},
        {"msgid": english, "msgstr": "etiketto print @@ a @@\n", "flags": set(), "comment": block},
        {"msgid": english, "msgstr": english, "flags": {"fuzzy"}, "comment": block},
        {"msgid": "Text", "msgstr": "", "flags": {"fuzzy"}, "comment": "Plain text"},
    ]
    assert check.code_block_errors("de", entries) == 2
    out = capsys.readouterr().out
    assert "other lines than the English" in out and "fuzzy code block" in out


def test_the_documentation_catalogs_have_nothing_fuzzy():
    """tools/check.sh and the CI catch it too (msgfmt --statistics)."""
    for path in sorted((ROOT / "site" / "po").glob("*.po")):
        flags = re.findall(r"^#, (.*)$", path.read_text(encoding="utf-8"), re.MULTILINE)
        assert not [f for f in flags if "fuzzy" in f], path.name
