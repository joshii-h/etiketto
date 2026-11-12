#!/usr/bin/env python3
"""Turn a release of Material Design Icons into etiketto/symbols/mdi.json.gz.

    curl -LO https://registry.npmjs.org/@mdi/svg/-/svg-7.4.47.tgz
    mkdir mdi && tar -xzf svg-7.4.47.tgz -C mdi
    python3 -I tools/mdi_symbols.py mdi/package

The package (@mdi/svg, or a checkout of Templarian/MaterialDesign-SVG) has
one SVG per icon, each a single path in a 24 × 24 view box, and meta.json
with the names, aliases and categories ("tags"). Etiketto keeps the name,
the path data, the categories and the aliases of every icon, sorted by name.
Left out: deprecated icons (they disappear in the next major release) and
the "Brand / Logo" category, trademarks of third parties that do not belong
on printed labels without their owners' permission.

The output depends only on the package: icons sorted by name, compact JSON,
gzip without a timestamp. The license (etiketto/symbols/LICENSE.mdi) is kept
by hand; this script stops if the package's license text is not in it, so a
change of license is noticed.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "etiketto" / "symbols"
HIDDEN = frozenset({"Brand / Logo"})
# every icon is <svg … viewBox="0 0 24 24"><path d="…" /></svg>
SVG = re.compile(r'^<svg [^>]*viewBox="0 0 24 24"[^>]*><path d="([^"]+)" ?/></svg>\s*$')


def icon_path(svg: Path) -> str:
    match = SVG.match(svg.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("%s: not a single path in a 24 × 24 view box" % svg.name)
    return match.group(1)


def convert(package: Path) -> dict:
    meta = json.loads((package / "meta.json").read_text(encoding="utf-8"))
    version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
    kept = sorted(
        (icon for icon in meta if not icon["deprecated"] and not HIDDEN & set(icon["tags"])),
        key=lambda icon: icon["name"],
    )
    categories = sorted({tag for icon in kept for tag in icon["tags"]})
    index = {tag: n for n, tag in enumerate(categories)}
    icons = [
        [
            icon["name"],
            icon_path(package / "svg" / ("%s.svg" % icon["name"])),
            sorted(index[tag] for tag in icon["tags"]),
            sorted(icon["aliases"]),
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
