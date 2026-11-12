"""Build hook: desktop integration and translations.

The project metadata lives in pyproject.toml. This file only adds the files
that belong outside the Python package. Their paths are relative to the
install prefix, so `pip install --prefix=/app .` puts them under /app/share
and a venv install under <venv>/share. They are part of the wheel, so
installing a prebuilt wheel brings them along too.

Building needs msgfmt (gettext). A broken translation stops the build.
"""

import shutil
import subprocess  # nosec B404 - runs msgfmt on our own po files
from pathlib import Path

from setuptools import setup

# isort: split
# after setuptools: it provides distutils (no longer in the standard library)
from distutils.command.install_data import install_data

APP_ID = "xyz.joshuahirsig.Tavletta"

DATA_FILES = [
    ("share/applications", [f"data/{APP_ID}.desktop"]),
    ("share/metainfo", [f"data/{APP_ID}.metainfo.xml"]),
    ("share/icons/hicolor/scalable/apps", [f"data/{APP_ID}.svg"]),
    ("share/icons/hicolor/symbolic/apps", [f"data/{APP_ID}-symbolic.svg"]),
    ("share/mime/packages", [f"data/{APP_ID}.xml"]),
]


class InstallData(install_data):
    """Compile po/<lang>.po and install it as share/locale/<lang>/LC_MESSAGES/tavletta.mo."""

    def run(self):
        msgfmt = shutil.which("msgfmt")
        if msgfmt is None:
            raise SystemExit("error: msgfmt (gettext) is needed to build the translations")
        po_files = sorted(Path("po").glob("*.po"))
        if not po_files:
            raise SystemExit("error: no translations found in po/")
        out = Path(self.get_finalized_command("build").build_base) / "mo"
        for po in po_files:
            mo = out / po.stem / "tavletta.mo"
            mo.parent.mkdir(parents=True, exist_ok=True)
            # check=True: a broken translation stops the build
            subprocess.run([msgfmt, "--check-format", "-o", str(mo), str(po)], check=True)  # nosec B603
            self.data_files.append((f"share/locale/{po.stem}/LC_MESSAGES", [str(mo)]))
        super().run()


setup(data_files=DATA_FILES, cmdclass={"install_data": InstallData})
