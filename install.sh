#!/bin/sh
# Install zinklabel for the current user (no root needed).
#
#   ./install.sh            install / update
#   ./install.sh --uninstall
#
# Needs from the system: Python 3.11+, PyGObject with GTK 4 and libadwaita,
# pycairo, gettext (msgfmt). Python packages go into a private venv.
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/zinklabel/venv"
BIN="$HOME/.local/bin"
APP_ID=xyz.joshuahirsig.Zinklabel

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$DATA/zinklabel/venv" "$BIN/zinklabel" \
        "$DATA/applications/$APP_ID.desktop" \
        "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA/mime/packages/$APP_ID.xml"
    update-mime-database "$DATA/mime" 2>/dev/null || true
    echo "zinklabel removed (labels and templates in $DATA/zinklabel/templates are kept)"
    exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null || {
    echo "error: PyGObject with GTK 4 and libadwaita is missing" >&2
    exit 1
}

# translations
for po in "$SRC"/po/*.po; do
    lang="$(basename "$po" .po)"
    mkdir -p "$SRC/zinklabel/locale/$lang/LC_MESSAGES"
    msgfmt -o "$SRC/zinklabel/locale/$lang/LC_MESSAGES/zinklabel.mo" "$po"
done

# private venv that can see the system's GTK bindings; build from a copy
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
cp -r "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/LICENSE" "$SRC/zinklabel" "$BUILD/"
if command -v uv >/dev/null; then
    uv venv --quiet --allow-existing --system-site-packages "$VENV"
    uv pip install --quiet --python "$VENV/bin/python" --reinstall-package zinklabel "$BUILD"
else
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade "$BUILD"
fi

mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" "$DATA/mime/packages"
ln -sf "$VENV/bin/zinklabel" "$BIN/zinklabel"
cp "$SRC/data/$APP_ID.desktop" "$DATA/applications/"
cp "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
cp "$SRC/data/$APP_ID.xml" "$DATA/mime/packages/"
update-mime-database "$DATA/mime" 2>/dev/null || true
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

echo "zinklabel installed: $BIN/zinklabel (also in the app menu)"
