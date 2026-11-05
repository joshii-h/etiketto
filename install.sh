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

PYTHON="$(command -v python3)" || { echo "error: python3 not found" >&2; exit 1; }
"$PYTHON" -c 'import gi, cairo; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("PangoCairo", "1.0")' 2>/dev/null || {
    echo "error: PyGObject with GTK 4, libadwaita, PangoCairo and pycairo is missing" >&2
    exit 1
}
command -v msgfmt >/dev/null || { echo "error: msgfmt (gettext) not found" >&2; exit 1; }

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
    # the system python3, which has the GTK bindings
    uv venv --quiet --allow-existing --system-site-packages --python "$PYTHON" "$VENV"
    uv pip install --quiet --python "$VENV/bin/python" --reinstall-package zinklabel "$BUILD"
else
    "$PYTHON" -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade "$BUILD"
fi

mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" "$DATA/mime/packages"
ln -sf "$VENV/bin/zinklabel" "$BIN/zinklabel"
# absolute Exec: ~/.local/bin is often not in the session PATH
sed "s|^Exec=zinklabel |Exec=$BIN/zinklabel |" "$SRC/data/$APP_ID.desktop" > "$DATA/applications/$APP_ID.desktop"
cp "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
cp "$SRC/data/$APP_ID.xml" "$DATA/mime/packages/"
update-mime-database "$DATA/mime" 2>/dev/null || true
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

echo "zinklabel installed: $BIN/zinklabel (also in the app menu)"
