"""Create po/<lang>.po from po/etiketto.pot and tools/strings_<lang>.py.

python3 tools/fill_po.py de fr it es nl sv ja zh_TW zh_HK zh_CN
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
    pot = (ROOT / "po" / "etiketto.pot").read_text(encoding="utf-8")
    date = re.search(r'"POT-Creation-Date: (.*?)\\n"', pot).group(1)
    out = [
        'msgid ""\nmsgstr ""\n"Project-Id-Version: etiketto\\n"\n"PO-Revision-Date: %s\\n"\n'
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
        msgid = unquote(m.group(1))
        if msgid not in table:
            missing.append(msgid)
        esc = table.get(msgid, "").replace("\\", "\\\\").replace('"', '\\"')
        comments = "\n".join(line for line in block.splitlines() if line.startswith("#"))
        out.append('%s\nmsgid %smsgstr "%s"\n' % (comments, m.group(1), esc))
    (ROOT / "po" / ("%s.po" % lang)).write_text("\n".join(out), encoding="utf-8")
    return missing


if __name__ == "__main__":
    for lang in sys.argv[1:]:
        print(lang, "missing:", build(lang))
