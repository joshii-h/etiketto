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


def test_check_sh_runs_what_the_ci_runs():
    """tools/check.sh is the CI locally: the same commands, the same flags."""
    check = (ROOT / "tools" / "check.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".forgejo" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in (
        "ruff format --check .",
        "ruff check --output-format=full .",
        "pytest -q --cov=etiketto --cov-report=term-missing",
        "mypy",
        "bandit -c pyproject.toml -r etiketto -ll",
        "radon cc -s -n C etiketto",
        "xenon --max-absolute C --max-modules A --max-average A etiketto",
        "pip-audit --progress-spinner off .",
        "msgfmt --check",
        "appstreamcli validate --no-net --explain data/*.metainfo.xml",
        "desktop-file-validate data/*.desktop",
    ):
        assert command in check and command in ci, command
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = " ".join(project["dependency-groups"]["dev"])
    for tool in ("pytest", "ruff", "mypy", "bandit", "radon", "xenon", "pip-audit"):
        assert tool in dev, tool


def test_the_flatpak_ships_every_licence_once():
    """Each bundled component's licence in share/licenses/<app id>/, Zint's
    once; what the app does not use is cleaned up; every downloaded source
    can be checked for updates."""
    import pytest

    yaml = pytest.importorskip("yaml")
    path = ROOT / "build-aux" / "flatpak" / "xyz.joshuahirsig.Etiketto.yml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    commands = "\n".join(
        command
        for module in manifest["modules"]
        for command in module.get("build-commands", []) + module.get("post-install", [])
    )
    target = "${FLATPAK_DEST}/share/licenses/${FLATPAK_ID}/"
    for name in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSE.glabels",
        "LICENSE.mdi",
        "LICENSE.zint",
        "LICENSE.zint-bindings",
        "LICENSE.segno",
        "LICENSE.python-barcode",
        "LICENSE.pillow",
        "LICENSE.labelprinter",
        "poppler ",
        "poppler-data ",
    ):
        assert target + name in commands + " ", name
    assert commands.count("LICENSE.zint ") == 1
    cleanup = [c for module in manifest["modules"] for c in module.get("cleanup", [])]
    assert "/share/man/man1/segno.1" in cleanup
    assert any(c.endswith("labelprinter/test") for c in cleanup)
    for module in manifest["modules"]:
        for source in module.get("sources", []):
            if source.get("type") in ("file", "archive") and "url" in source:
                assert "x-checker-data" in source, (module["name"], source["url"])
