#!/bin/sh
# Take the README / Flathub screenshots in the default GNOME style, light
# and dark, 1000x700, with rounded corners and a soft shadow.
# Needs niri (to float and size the window) and ImageMagick.
set -eu
cd "$(dirname "$0")/.."
OUT=docs/screenshots
CONF="$(mktemp -d)"
trap 'rm -rf "$CONF"' EXIT
mkdir -p "$CONF/etiketto"
echo '{"host": "VC-500W.local"}' > "$CONF/etiketto/settings.json"

shoot() { # name example state language scheme
    case " ${ONLY:-$1} " in *" $1 "*) ;; *) return 0 ;; esac
    XDG_CONFIG_HOME="$CONF" ADW_DISABLE_PORTAL=1 ADW_DEBUG_COLOR_SCHEME="$5" LANGUAGE="$4" \
    ETIKETTO_SCREENSHOT="$OUT/$1.raw.png" ETIKETTO_SCREENSHOT_STATE="$3" ETIKETTO_SCREENSHOT_DELAY=6000 \
        .venv/bin/python -m etiketto.app "docs/examples/$2.etiketto" >/dev/null 2>&1 &
    pid=$!
    id=""
    for _ in $(seq 40); do
        id=$(niri msg --json windows | python3 -c 'import json,sys
w=[x for x in json.load(sys.stdin) if x.get("app_id")=="xyz.joshuahirsig.Etiketto"]
print(w[-1]["id"] if w else "")')
        [ -n "$id" ] && break
        sleep 0.1
    done
    niri msg action toggle-window-floating --id "$id"
    niri msg action set-window-width --id "$id" 1000
    niri msg action set-window-height --id "$id" 700
    niri msg action focus-window --id "$id"
    wait "$pid" || true
    # rounded corners (12 px) and a soft shadow, as in GNOME
    size=$(magick identify -format '%wx%h' "$OUT/$1.raw.png")
    w=${size%x*}; h=${size#*x}
    magick -size "$size" xc:black -fill white -draw "roundrectangle 0,0 $((w-1)),$((h-1)) 12,12" "$CONF/mask.png"
    magick "$OUT/$1.raw.png" "$CONF/mask.png" -alpha off -compose CopyOpacity -composite -compose Over \
        \( +clone -background black -shadow 35x14+0+4 \) +swap -background none -layers merge +repage \
        "$OUT/$1.png"
    [ -n "${KEEP_RAW:-}" ] || rm -f "$OUT/$1.raw.png"
}

for scheme in light dark; do
    sfx=""; cs=prefer-light
    if [ "$scheme" = dark ]; then sfx="-dark"; cs=prefer-dark; fi
    shoot "main$sfx" spice "" C "$cs"
    shoot "expand$sfx" spice expand C "$cs"
    shoot "print$sfx" spice print C "$cs"
    shoot "colour$sfx" party "" C "$cs"
    shoot "sticker$sfx" sticker "" C "$cs"
    shoot "cable-de$sfx" cable "" de "$cs"
done
