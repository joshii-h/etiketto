"""Create po/<lang>.po from po/tavletta.pot and tools/strings_<lang>.py.

python3 tools/fill_po.py          # all languages in PLURALS
python3 tools/fill_po.py de fr   # only these

Strings with a context (pgettext("month name", "May")) are keyed
"month name\x04May" in strings_<lang>.py, as gettext stores them.
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLURALS = {
    "de": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "it": "nplurals=2; plural=(n != 1);",
    "es": "nplurals=2; plural=(n != 1);",
    "nl": "nplurals=2; plural=(n != 1);",
    "sv": "nplurals=2; plural=(n != 1);",
    "ja": "nplurals=1; plural=0;",
    "zh_TW": "nplurals=1; plural=0;",
    "zh_HK": "nplurals=1; plural=0;",
    "zh_CN": "nplurals=1; plural=0;",
    "pl": "nplurals=3; plural=(n == 1 ? 0 : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 1 : 2);",
    "pt": "nplurals=2; plural=(n != 1);",
    "pt_BR": "nplurals=2; plural=(n > 1);",
    "ru": "nplurals=3; plural=(n % 10 == 1 && n % 100 != 11 ? 0 : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 1 : 2);",
    "uk": "nplurals=3; plural=(n % 10 == 1 && n % 100 != 11 ? 0 : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 1 : 2);",
    "tr": "nplurals=2; plural=(n != 1);",
    "ko": "nplurals=1; plural=0;",
    "fi": "nplurals=2; plural=(n != 1);",
    "hu": "nplurals=2; plural=(n != 1);",
    "et": "nplurals=2; plural=(n != 1);",
    "id": "nplurals=1; plural=0;",
    "ms": "nplurals=1; plural=0;",
    "th": "nplurals=1; plural=0;",
    "vi": "nplurals=1; plural=0;",
    "hi": "nplurals=2; plural=(n > 1);",
    "ar": "nplurals=6; plural=(n == 0 ? 0 : n == 1 ? 1 : n == 2 ? 2 : n % 100 >= 3 && n % 100 <= 10 ? 3 : n % 100 >= 11 ? 4 : 5);",
}
# languages built from another language's word list (Hong Kong uses the
# Traditional Chinese strings of Taiwan)
SOURCES = {"zh_HK": "zh_TW"}


def load(lang):
    lang = SOURCES.get(lang, lang)
    spec = importlib.util.spec_from_file_location("s", ROOT / "tools" / ("strings_%s.py" % lang))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TRANSLATIONS


def unquote(lines):
    raw = "".join(re.findall(r'"(.*)"', lines))
    return raw.encode().decode("unicode_escape").encode("latin-1").decode("utf-8")


def build(lang):
    table = load(lang)
    pot = (ROOT / "po" / "tavletta.pot").read_text(encoding="utf-8")
    date = re.search(r'"POT-Creation-Date: (.*?)\\n"', pot).group(1)
    out = [
        'msgid ""\nmsgstr ""\n"Project-Id-Version: tavletta\\n"\n"PO-Revision-Date: %s\\n"\n'
        '"Last-Translator: %s\\n"\n"Language-Team: %s\\n"\n"Language: %s\\n"\n'
        '"MIME-Version: 1.0\\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Content-Transfer-Encoding: 8bit\\n"\n"Plural-Forms: %s\\n"\n'
        % (date, table.get("translator-credits", ""), lang, lang, PLURALS[lang])
    ]
    missing = []
    for block in pot.split("\n\n")[1:]:
        m = re.search(r'msgid ((?:".*"\n?)+)msgstr', block)
        if not m:
            continue
        context = re.search(r'msgctxt ((?:".*"\n?)+)msgid', block)
        key = unquote(m.group(1))
        ctxt_line = ""
        if context:
            key = unquote(context.group(1)) + "\x04" + key
            ctxt_line = "msgctxt " + context.group(1)
        if key not in table:
            missing.append(key)
        esc = table.get(key, "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        comments = "\n".join(line for line in block.splitlines() if line.startswith("#"))
        out.append('%s\n%smsgid %smsgstr "%s"\n' % (comments, ctxt_line, m.group(1), esc))
    (ROOT / "po" / ("%s.po" % lang)).write_text("\n".join(out), encoding="utf-8")
    return missing


if __name__ == "__main__":
    for lang in sys.argv[1:] or PLURALS:
        print(lang, "missing:", build(lang))
