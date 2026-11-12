import platform
from urllib.parse import parse_qs, urlparse

import pytest

from tavletta import __version__, report


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    return tmp_path


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_os_name_order(root):
    assert report.os_name(str(root)) == platform.system()
    _write(root, "usr/lib/os-release", 'NAME=x\nPRETTY_NAME="Lib Linux"\n')
    assert report.os_name(str(root)) == "Lib Linux"
    _write(root, "etc/os-release", "ID=gentoo\nPRETTY_NAME=Gentoo Linux\n")
    assert report.os_name(str(root)) == "Gentoo Linux"
    _write(root, "run/host/os-release", 'PRETTY_NAME="Host OS 1"\n')
    assert report.os_name(str(root)) == "Host OS 1"


def test_os_name_without_pretty_name_falls_through(root):
    _write(root, "etc/os-release", "NAME=nothing\n")
    _write(root, "usr/lib/os-release", 'PRETTY_NAME="Fallback"\n')
    assert report.os_name(str(root)) == "Fallback"


def test_install_kind(root, monkeypatch):
    assert report.install_kind(str(root)) == "install.sh / pip"
    monkeypatch.setenv("APPIMAGE", "/tmp/x.AppImage")
    assert report.install_kind(str(root)) == "AppImage"
    (root / ".flatpak-info").write_text("[Application]\n")
    assert report.install_kind(str(root)) == "Flatpak"


def _parse(url):
    parsed = urlparse(url)
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == report.ISSUES_URL
    query = parse_qs(parsed.query, keep_blank_values=True)
    return query["title"][0], query["body"][0]


def test_issue_url_with_problem(root):
    _write(root, "etc/os-release", 'PRETTY_NAME="Test OS"\n')
    title, body = _parse(
        report.issue_url("Printing failed: jam\nmore", "VC-500W, 25 mm", "GTK 4.20", str(root))
    )
    assert title == "Problem: Printing failed: jam"
    assert "**Error message**" in body
    assert "```\nPrinting failed: jam\nmore\n```" in body
    assert "- Tavletta %s (install.sh / pip)" % __version__ in body
    assert "- Test OS, Python %s, GTK 4.20" % platform.python_version() in body
    assert "- Printer: VC-500W, 25 mm" in body


def test_issue_url_without_problem(root):
    title, body = _parse(report.issue_url(None, None, "GTK", str(root)))
    assert title == ""
    assert "Error message" not in body
    assert "- Printer: not connected" in body
    assert body.startswith("**What happened?**")
    assert "Please remove anything private (names, addresses) before sending." in body


def test_issue_url_title_is_shortened(root):
    title, _body = _parse(report.issue_url("x" * 200, None, "GTK", str(root)))
    assert title == "Problem: " + "x" * 80


def test_issue_url_long_problem_is_truncated(root):
    # an umlaut takes 6 characters URL-encoded: 2000 characters exceed MAX_URL
    problem = "".join("Zeile %d: %s\n" % (i, "ä" * 20) for i in range(1000))
    url = report.issue_url(problem, None, "GTK", str(root))
    assert len(url) <= report.MAX_URL
    _title, body = _parse(url)
    assert problem[:400] in body  # the first 500 characters (~16 lines) are kept
    assert "Zeile 15:" in body
    assert "Zeile 17:" not in body
    assert "- Printer: not connected" in body  # system info survives


def test_issue_url_error_text_capped_at_2000(root, monkeypatch):
    monkeypatch.setattr(report, "MAX_URL", 100_000)
    _title, body = _parse(report.issue_url("a" * 3000, None, "GTK", str(root)))
    assert "a" * 2000 + "\n```" in body
    assert "a" * 2001 not in body
