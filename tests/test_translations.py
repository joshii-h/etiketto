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
