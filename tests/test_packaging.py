"""What the package and install.sh ship: the licence files are there."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_license_files_exist_and_install_sh_copies_them():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    for name in project["license-files"]:
        assert (ROOT / name).is_file(), name
        top = name.split("/")[0]
        assert '"$SRC/%s"' % top in install, name  # the build copy has it


def test_the_sdist_carries_what_the_notices_link_to():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\(((?:build-aux|etiketto)/[^)]+)\)", notices):
        assert (ROOT / link).is_file(), link
        assert link.startswith("etiketto/") or link in manifest, link
