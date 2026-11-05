"""Build a pre-filled issue on the project's Forgejo tracker.

No account data or token is involved: the browser opens the "new issue" form
with title and description filled in, and the user decides what to send.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from urllib.parse import urlencode

from etiketto import __version__

ISSUES_URL = "https://git.joshuahirsig.xyz/joshii/etiketto/issues/new"
MAX_URL = 6000  # stay well below URL length limits of browsers and servers


def os_name(root: str = "/") -> str:
    """Pretty name of the (host) distribution; inside Flatpak via /run/host."""
    for path in ("run/host/os-release", "etc/os-release", "usr/lib/os-release"):
        try:
            for line in (Path(root) / path).read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            continue
    return platform.system()


def install_kind(root: str = "/") -> str:
    if (Path(root) / ".flatpak-info").exists():
        return "Flatpak"
    if os.environ.get("APPIMAGE"):
        return "AppImage"
    return "install.sh / pip"


def issue_url(problem: str | None, printer: str | None, toolkit: str, root: str = "/") -> str:
    title = "Problem: " + problem.splitlines()[0][:80] if problem else ""
    lines = [
        "**What happened?**",
        "",
        "<!-- Describe what you did and what you expected. -->",
        "",
        "_Please remove anything private (names, addresses) before sending._",
        "",
    ]
    if problem:
        lines += ["**Error message**", "", "```", problem.strip()[:2000], "```", ""]
    lines += [
        "**System**",
        "",
        "- Etiketto %s (%s)" % (__version__, install_kind(root)),
        "- %s, Python %s, %s" % (os_name(root), platform.python_version(), toolkit),
        "- Printer: %s" % (printer or "not connected"),
    ]
    query = urlencode({"title": title, "body": "\n".join(lines)})
    url = ISSUES_URL + "?" + query
    if len(url) > MAX_URL:  # very long error text: keep only the first part
        return issue_url(problem[:500] if problem else None, printer, toolkit, root)
    return url
