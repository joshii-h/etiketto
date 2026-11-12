#!/usr/bin/env python3
"""Turn a release of Material Design Icons into etiketto/symbols/mdi.json.gz.

    curl -LO https://registry.npmjs.org/@mdi/svg/-/svg-7.4.47.tgz
    mkdir mdi && tar -xzf svg-7.4.47.tgz -C mdi
    python3 -I tools/mdi_symbols.py mdi/package

The package (@mdi/svg, or a checkout of Templarian/MaterialDesign-SVG) has
one SVG per icon, each a single path in a 24 × 24 view box, and meta.json
with the names, aliases and categories ("tags"). Etiketto keeps the name,
the path data, the categories and the aliases of every icon, sorted by name.
Left out: deprecated icons (they disappear in the next major release), the
"Brand / Logo" category and further icons showing brands, trademarks or
designs from games and films (DENIED): they do not belong on printed labels
without their owners' permission. Aliases naming a brand ("lego", "apple",
"twitter-retweet") are dropped (BRAND_WORDS), the icon stays. Kept on
purpose: marks of technical standards that label a port or a feature, such
as bluetooth, usb, nfc, hdmi, cast or contactless payment, and the key
symbols of apple-keyboard-* (command, option, shift: standard key symbols
in Unicode, not a logo).

The output depends only on the package: icons sorted by name, compact JSON,
gzip without a timestamp. The license (etiketto/symbols/LICENSE.mdi) is kept
by hand; this script stops if the package's license text is not in it, so a
change of license is noticed.
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "etiketto" / "symbols"
HIDDEN = frozenset({"Brand / Logo"})
# icons outside "Brand / Logo" that show a brand, a product or a design from
# a game or film (fnmatch patterns)
DENIED = (
    "camera-gopro",
    "death-star*",
    "deathly-hallows",
    "ev-plug-tesla",
    "file-excel*",
    "file-powerpoint*",
    "file-word*",
    "firewire",
    "ghost*",
    "hololens",
    "ipod",
    "klingon",
    "microsoft-*",
    "minidisc",
    "nintendo-*",
    "one-up",
    "pac-man",
    "pokeball",
    "polaroid",
    "raspberry-pi",
    "space-invaders",
    "triforce",
)
# an alias with one of these words is dropped
BRAND_WORDS = frozenset(
    {
        "acrobat", "adobe", "airplay", "apple", "blinky", "chrome", "clyde", "fortnite",
        "harry", "inky", "ios", "lego", "linux", "microsoft", "nest", "olympics", "pinky",
        "potter", "roomba", "tesla", "twitter", "vespa", "windows", "xbox", "youtube", "zelda",
    }
)  # fmt: skip
# every icon is <svg … viewBox="0 0 24 24"><path d="…" /></svg>
SVG = re.compile(r'^<svg [^>]*viewBox="0 0 24 24"[^>]*><path d="([^"]+)" ?/></svg>\s*$')


def icon_path(svg: Path) -> str:
    match = SVG.match(svg.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("%s: not a single path in a 24 × 24 view box" % svg.name)
    return match.group(1)


def wanted(icon: dict) -> bool:
    """Not deprecated, no brand, no trademark."""
    if icon["deprecated"] or HIDDEN & set(icon["tags"]):
        return False
    return not any(fnmatch.fnmatchcase(icon["name"], pattern) for pattern in DENIED)


def convert(package: Path) -> dict:
    meta = json.loads((package / "meta.json").read_text(encoding="utf-8"))
    version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
    kept = sorted((icon for icon in meta if wanted(icon)), key=lambda icon: icon["name"])
    categories = sorted({tag for icon in kept for tag in icon["tags"]})
    index = {tag: n for n, tag in enumerate(categories)}
    icons = [
        [
            icon["name"],
            icon_path(package / "svg" / ("%s.svg" % icon["name"])),
            sorted(index[tag] for tag in icon["tags"]),
            sorted(a for a in icon["aliases"] if not BRAND_WORDS & set(a.split("-"))),
        ]
        for icon in kept
    ]
    return {
        "set": "Material Design Icons",
        "version": version,
        "categories": categories,
        "icons": icons,
    }


def check_license(package: Path) -> None:
    upstream = (package / "LICENSE").read_text(encoding="utf-8").strip()
    ours = (OUT / "LICENSE.mdi").read_text(encoding="utf-8")
    if upstream not in ours:
        raise SystemExit("The license of the icons changed: review it, update LICENSE.mdi")


def write(data: dict) -> int:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    target = OUT / "mdi.json.gz"
    with (
        open(target, "wb") as out,
        gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=9, mtime=0) as gz,
    ):
        gz.write(raw)
    return target.stat().st_size


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    package = Path(argv[1])
    check_license(package)
    data = convert(package)
    size = write(data)
    print(
        "Material Design Icons %s: %d icons in %d categories, %d bytes"
        % (data["version"], len(data["icons"]), len(data["categories"]), size)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
