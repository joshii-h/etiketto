"""Build hook: desktop integration and translations.

The project metadata lives in pyproject.toml. This file only adds the files
that belong outside the Python package. Their paths are relative to the
install prefix, so `pip install --prefix=/app .` puts them under /app/share
and a venv install under <venv>/share. They are part of the wheel, so
installing a prebuilt wheel brings them along too.

The icon: the scalable SVG for every size, and below 48 px the drawings
made for 16, 20, 22, 24 and 32 px (data/icons/tavletta-<size>.svg, a
thicker frame and fewer details), installed as the hicolor <size>x<size>
icons. 22 px is the size of KDE's panels and menus.

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


# keep in step with SMALL_ICON_SIZES in install.sh
SMALL_ICONS = (16, 20, 22, 24, 32)


class InstallData(install_data):
    """Compile po/<lang>.po and install it as share/locale/<lang>/LC_MESSAGES/tavletta.mo.

    Also installs the small icon drawings under the app ID's name: data_files
    cannot rename, so they are copied to the build folder first."""

    def finalize_options(self):
        super().finalize_options()
        build_base = Path(self.get_finalized_command("build").build_base)
        # (source, staged copy); listed here, once, not on every run
        self.small_icons = []
        for size in SMALL_ICONS:
            icon = build_base / "small-icons" / f"{size}x{size}" / f"{APP_ID}.svg"
            self.small_icons.append((Path(f"data/icons/tavletta-{size}.svg"), icon))
            self.data_files.append((f"share/icons/hicolor/{size}x{size}/apps", [str(icon)]))

    def run(self):
        build_base = Path(self.get_finalized_command("build").build_base)
        for source, icon in self.small_icons:
            icon.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, icon)
        msgfmt = shutil.which("msgfmt")
        if msgfmt is None:
            raise SystemExit("error: msgfmt (gettext) is needed to build the translations")
        po_files = sorted(Path("po").glob("*.po"))
        if not po_files:
            raise SystemExit("error: no translations found in po/")
        out = build_base / "mo"
        for po in po_files:
            mo = out / po.stem / "tavletta.mo"
            mo.parent.mkdir(parents=True, exist_ok=True)
            # check=True: a broken translation stops the build
            subprocess.run([msgfmt, "--check-format", "-o", str(mo), str(po)], check=True)  # nosec B603
            self.data_files.append((f"share/locale/{po.stem}/LC_MESSAGES", [str(mo)]))
        super().run()


setup(data_files=DATA_FILES, cmdclass={"install_data": InstallData})
