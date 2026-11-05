#!/bin/sh
# Install Etiketto for the current user (no root needed).
#
#   ./install.sh            install / update
#   ./install.sh --uninstall
#
# Needs from the system: Python 3.11+, PyGObject with GTK 4 and libadwaita,
# pycairo, gettext (msgfmt). Python packages go into a private venv.
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/etiketto/venv"
BIN="$HOME/.local/bin"
APP_ID=xyz.joshuahirsig.Etiketto

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$DATA/etiketto/venv" "$BIN/etiketto" \
        "$DATA/applications/$APP_ID.desktop" \
        "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
        "$DATA/metainfo/$APP_ID.metainfo.xml" \
        "$DATA/mime/packages/$APP_ID.xml"
    update-mime-database "$DATA/mime" 2>/dev/null || true
    update-desktop-database "$DATA/applications" 2>/dev/null || true
    # a stale icon cache would still point at the removed icons
    gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
    echo "Etiketto removed (labels and templates in $DATA/etiketto/templates are kept)"
    exit 0
fi

PYTHON="$(command -v python3)" || { echo "error: python3 not found" >&2; exit 1; }
"$PYTHON" -c 'import gi, cairo; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("PangoCairo", "1.0")' 2>/dev/null || {
    echo "error: PyGObject with GTK 4, libadwaita, PangoCairo and pycairo is missing" >&2
    exit 1
}
command -v msgfmt >/dev/null || { echo "error: msgfmt (gettext) not found" >&2; exit 1; }

# private venv that can see the system's GTK bindings; build from a copy
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
# the build compiles the translations into the venv (setup.py)
cp -r "$SRC/pyproject.toml" "$SRC/setup.py" "$SRC/MANIFEST.in" "$SRC/README.md" "$SRC/LICENSE" \
    "$SRC/etiketto" "$SRC/data" "$SRC/po" "$BUILD/"
rm -rf "$BUILD/etiketto/locale"  # left over from older install.sh versions
if command -v uv >/dev/null; then
    # the system python3, which has the GTK bindings
    uv venv --quiet --allow-existing --system-site-packages --python "$PYTHON" "$VENV"
    uv pip install --quiet --python "$VENV/bin/python" --reinstall-package etiketto "$BUILD"
else
    "$PYTHON" -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade "$BUILD"
fi

mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" \
    "$DATA/icons/hicolor/symbolic/apps" "$DATA/mime/packages" "$DATA/metainfo"
ln -sf "$VENV/bin/etiketto" "$BIN/etiketto"
# absolute Exec: ~/.local/bin is often not in the session PATH
# the package installed these into the venv; make them visible to the desktop
SHARE="$VENV/share"
sed "s|^Exec=etiketto |Exec=$BIN/etiketto |" "$SHARE/applications/$APP_ID.desktop" > "$DATA/applications/$APP_ID.desktop"
cp "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
cp "$SHARE/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" "$DATA/icons/hicolor/symbolic/apps/"
cp "$SHARE/metainfo/$APP_ID.metainfo.xml" "$DATA/metainfo/"
cp "$SHARE/mime/packages/$APP_ID.xml" "$DATA/mime/packages/"
update-mime-database "$DATA/mime" 2>/dev/null || true
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

echo "Etiketto installed: $BIN/etiketto (also in the app menu)"
