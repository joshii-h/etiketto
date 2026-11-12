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
    for link in re.findall(r"\]\(((?:build-aux|tavletta)/[^)]+)\)", notices):
        assert (ROOT / link).is_file(), link
        assert link.startswith("tavletta/") or link in manifest, link


def test_check_sh_runs_what_the_ci_runs():
    """tools/check.sh is the CI locally: the same commands, the same flags."""
    check = (ROOT / "tools" / "check.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".forgejo" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in (
        "ruff format --check .",
        "ruff check --output-format=full .",
        "pytest -q --cov=tavletta --cov-report=term-missing",
        "mypy",
        "bandit -c pyproject.toml -r tavletta -ll",
        "radon cc -s -n C tavletta",
        "xenon --max-absolute C --max-modules A --max-average A tavletta",
        "pip-audit --progress-spinner off .",
        "msgfmt --check",
        "appstreamcli validate --no-net --explain data/*.metainfo.xml",
        "desktop-file-validate data/*.desktop",
    ):
        assert command in check and command in ci, command
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = " ".join(project["dependency-groups"]["dev"])
    for tool in ("pytest", "ruff", "mypy", "bandit", "radon", "xenon", "pip-audit", "pyyaml"):
        assert tool in dev, tool
    for loop in ("for po in po/*.po site/po/*.po; do", "for po in site/po/*.po; do"):
        assert loop in check and loop in ci, loop  # the documentation's catalogs too
    assert ci.count("pip install -q -e . --group dev") == 2  # both jobs: the same tools
    assert ci.count('pip install -q --upgrade "pip>=25.1"') == 2  # --group needs it
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "-e . --group dev" in readme


def test_the_flatpak_ships_every_licence_once():
    """One layout: share/licenses/<app id>/<module>/, where flatpak-builder
    copies a module's own LICENSE*/COPYING* by itself; installed by hand
    only what it does not find (licences inside wheels, Tavletta's notices
    and data licences), into the same folder, never a second copy at the
    top. What the app does not use is cleaned up; every downloaded source
    can be checked for updates."""
    import re

    import yaml  # in the dev dependency group: the test runs, never skips

    path = ROOT / "build-aux" / "flatpak" / "xyz.joshuahirsig.Tavletta.yml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    target = "${FLATPAK_DEST}/share/licenses/${FLATPAK_ID}/"
    by_hand: dict[str, str] = {}
    for module in manifest["modules"]:
        commands = module.get("build-commands", []) + module.get("post-install", [])
        for command in commands:
            for folder in re.findall(re.escape(target) + r"([^\s/]*)", command):
                assert folder == module["name"], command  # the module's own folder
                by_hand[module["name"]] = command
    assert set(by_hand) == {
        "python3-segno",
        "python3-python-barcode",
        "python3-zint-bindings",
        "poppler-data",
        "poppler",
        "tavletta",
    }
    assert "THIRD_PARTY_NOTICES.md" in by_hand["tavletta"]
    assert "LICENSE.glabels" in by_hand["tavletta"] and "LICENSE.mdi" in by_hand["tavletta"]
    assert "LICENSE.zint" not in "".join(by_hand.values())  # a source: copied by itself
    zint = next(m for m in manifest["modules"] if m["name"] == "python3-zint-bindings")
    assert {"type": "file", "path": "LICENSE.zint"} in zint["sources"]
    cleanup = [c for module in manifest["modules"] for c in module.get("cleanup", [])]
    assert "/share/man/man1/segno.1" in cleanup
    for unused in ("test", "cups_backend.py", "__main__.py", "print_text.py", "queue_*.py"):
        assert any(c.endswith("labelprinter/" + unused) for c in cleanup), unused
    for module in manifest["modules"]:
        for source in module.get("sources", []):
            if source.get("type") in ("file", "archive") and "url" in source:
                assert "x-checker-data" in source, (module["name"], source["url"])


def _workflow(name: str) -> dict:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML 1.1 reads `on` as true
    return workflow


def test_github_workflows_pin_actions_and_run_only_on_github():
    """Forgejo reads .github/workflows too: every job is guarded. Actions are
    pinned to a full commit SHA, with the version as a comment."""
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for line in re.findall(r"uses:\s*(\S.*)", text):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+# v\d+(\.\d+)*", line), (
                path.name,
                line,
            )
        for name, job in _workflow(path.name)["jobs"].items():
            assert job["if"].startswith("github.server_url == 'https://github.com'"), name


def test_the_container_workflow():
    workflow = _workflow("docker.yml")
    text = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    on = workflow["on"]
    assert on["push"] == {"tags": ["v*"], "branches": ["main"]}
    assert on["pull_request"] == {"branches": ["main"]} and "workflow_dispatch" in on
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"]["IMAGE"] == "ghcr.io/joshii-h/tavletta"
    check, publish = workflow["jobs"]["check"], workflow["jobs"]["publish"]
    assert "permissions" not in check  # read-only, also for pull requests
    assert publish["needs"] == "check" and "github.event_name != 'pull_request'" in publish["if"]
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }

    def step(job: dict, uses: str = "", run: str = "") -> dict:
        found = [
            s
            for s in job["steps"]
            if (uses and s.get("uses", "").startswith(uses + "@"))
            or (run and run in s.get("run", ""))
        ]
        assert len(found) == 1, uses or run
        return found[0]

    smoke = step(check, run="packaging/docker/smoke-test.sh")
    assert smoke["env"]["EXPECT_VERSION"]  # a release reports its tag's version
    script = ROOT / "packaging" / "docker" / "smoke-test.sh"
    assert script.stat().st_mode & 0o111  # executable
    trivy = step(check, uses="aquasecurity/trivy-action")["with"]
    assert trivy["severity"] == "CRITICAL,HIGH" and trivy["ignore-unfixed"] is True
    assert trivy["exit-code"] == "${{ env.RELEASE == 'true' && '1' || '0' }}"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", trivy["version"])  # not whatever is newest
    test_build = step(check, uses="docker/build-push-action")["with"]
    assert test_build["platforms"] == "linux/amd64" and test_build["load"] is True
    # a release has today's security updates, scanned as they are pushed
    assert test_build["no-cache"] == "${{ env.RELEASE == 'true' }}"

    build = step(publish, uses="docker/build-push-action")["with"]
    assert build["file"] == "packaging/docker/Dockerfile"
    assert build["platforms"] == "linux/amd64,linux/arm64"
    assert build["push"] == "${{ env.PUSH == 'true' }}"
    assert publish["env"]["PUSH"] == "${{ github.event_name == 'push' }}"
    assert build["sbom"] is True and build["provenance"] == "mode=max"
    assert build["cache-from"].splitlines() == [
        "type=gha,scope=check",
        "${{ env.RELEASE != 'true' && 'type=gha,scope=publish' || '' }}",
    ]
    assert build["cache-to"] == "type=gha,scope=publish,mode=max"
    assert step(publish, uses="docker/setup-qemu-action")["with"]["platforms"] == "arm64"
    attest = step(publish, uses="actions/attest-build-provenance")
    assert attest["with"]["push-to-registry"] is True
    assert attest["with"]["subject-digest"] == "${{ steps.build.outputs.digest }}"
    assert "cosign sign --yes" in step(publish, run="cosign sign")["run"]
    login = step(publish, uses="docker/login-action")
    assert login["with"]["password"] == "${{ github.token }}"  # no long-lived token
    for writing in (login, attest, step(publish, run="cosign sign")):
        assert writing["if"] == "env.PUSH == 'true'"

    meta = step(publish, uses="docker/metadata-action")["with"]
    for tag in (
        "type=semver,pattern={{version}}",
        "type=semver,pattern={{major}}.{{minor}}",
        "type=edge,branch=main",
    ):
        assert tag in meta["tags"], tag
    assert "type=raw,value=latest," in meta["tags"] and meta["flavor"] == "latest=false"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for field in ("labels", "annotations"):
        assert "org.opencontainers.image.licenses=%s\n" % project["license"] in meta[field]
        assert (
            "org.opencontainers.image.source=https://github.com/joshii-h/tavletta\n" in meta[field]
        )
        assert (
            "org.opencontainers.image.url=https://git.joshuahirsig.xyz/joshii/tavletta\n"
            in (meta[field])
        )
    assert "secrets." not in text  # GITHUB_TOKEN only


def test_the_container_image_and_compose_files():
    import yaml

    from tavletta import __version__

    docker = ROOT / "packaging" / "docker"
    dockerfile = (docker / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER tavletta$", dockerfile, re.M)
    assert "HEALTHCHECK" in dockerfile and "/api/v1/health" in dockerfile
    assert re.search(r"^COPY .*THIRD_PARTY_NOTICES\.md \./$", dockerfile, re.M)  # setuptools
    assert "/src/THIRD_PARTY_NOTICES.md /usr/share/doc/tavletta/" in dockerfile
    assert "gir1.2-poppler-0.18" in dockerfile  # PDF pages of shipping labels
    assert "apt-get upgrade -y" in dockerfile  # fixes newer than the base image
    compose = yaml.safe_load((docker / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["tavletta"]
    assert "build" not in service
    minor = ".".join(__version__.split(".")[:2])
    assert service["image"] == "ghcr.io/joshii-h/tavletta:" + minor  # bumped with the version
    build = yaml.safe_load((docker / "compose.build.yaml").read_text(encoding="utf-8"))
    built = build["services"]["tavletta"]
    assert (docker / built["build"]["context"] / built["build"]["dockerfile"]).is_file()
    assert built["pull_policy"] == "build"
