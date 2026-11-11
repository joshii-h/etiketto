#!/bin/sh
# Take the screenshots listed in tools/screenshots.list in the default GNOME
# style, light and dark, 1000x700, with rounded corners and a soft shadow.
#
#   tools/screenshots.sh                README images   -> docs/screenshots/
#   tools/screenshots.sh --docs [LL…]   documentation   -> site/build/screenshots/<lang>/
#                                       (default: en and every site/po/<lang>.po)
#   ONLY="main print" tools/screenshots.sh …   only these images
#
# Window size: niri (local desktop) or xdotool (X11, e.g. xvfb-run in CI).
# Needs ImageMagick and gettext. PYTHON defaults to .venv/bin/python.
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
LIST=tools/screenshots.list
CONF="$(mktemp -d)"
trap 'rm -rf "$CONF"' EXIT
mkdir -p "$CONF/etiketto"
echo '{"host": "VC-500W.local", "printers": {"cab": {"host": "EOS2.local"}}}' > "$CONF/etiketto/settings.json"

# ImageMagick 7 is `magick`, ImageMagick 6 (some distributions) `convert`
if command -v magick >/dev/null; then IM=magick; IDENTIFY="magick identify"; else IM=convert; IDENTIFY=identify; fi

# the app reads its translations from etiketto/locale
for po in po/*.po; do
    lang="$(basename "$po" .po)"
    mkdir -p "etiketto/locale/$lang/LC_MESSAGES"
    msgfmt -o "etiketto/locale/$lang/LC_MESSAGES/etiketto.mo" "$po"
done

size_window() { # pid
    if [ -n "${NIRI_SOCKET:-}" ] && command -v niri >/dev/null; then
        id=""
        for _ in $(seq 40); do
            id=$(niri msg --json windows | python3 -c 'import json,sys
w=[x for x in json.load(sys.stdin) if x.get("app_id")=="xyz.joshuahirsig.Etiketto"]
print(w[-1]["id"] if w else "")')
            [ -n "$id" ] && break
            sleep 0.1
        done
        [ -n "$id" ] || return 0
        niri msg action toggle-window-floating --id "$id"
        niri msg action set-window-width --id "$id" 1000
        niri msg action set-window-height --id "$id" 700
        niri msg action focus-window --id "$id"
    elif [ -n "${DISPLAY:-}" ] && command -v xdotool >/dev/null; then
        for _ in $(seq 50); do
            if xdotool search --sync --onlyvisible --pid "$1" >/dev/null 2>&1; then
                for w in $(xdotool search --onlyvisible --pid "$1"); do
                    xdotool windowsize "$w" 1000 700
                done
                return 0
            fi
            sleep 0.1
        done
    fi
}

shoot() { # output example state language scheme
    out="$1"
    XDG_CONFIG_HOME="$CONF" ADW_DISABLE_PORTAL=1 ADW_DEBUG_COLOR_SCHEME="$5" LANGUAGE="$4" \
    ETIKETTO_SCREENSHOT="$out.raw.png" ETIKETTO_SCREENSHOT_STATE="$3" ETIKETTO_SCREENSHOT_DELAY=6000 \
        "$PYTHON" -m etiketto.app "docs/examples/$2.etiketto" </dev/null >/dev/null 2>&1 &
    pid=$!
    size_window "$pid"
    wait "$pid" || true
    [ -f "$out.raw.png" ] || { echo "error: no screenshot $out" >&2; return 1; }
    # rounded corners (12 px) and a soft shadow, as in GNOME
    size=$($IDENTIFY -format '%wx%h' "$out.raw.png")
    w=${size%x*}; h=${size#*x}
    $IM -size "$size" xc:black -fill white -draw "roundrectangle 0,0 $((w-1)),$((h-1)) 12,12" "$CONF/mask.png"
    $IM "$out.raw.png" "$CONF/mask.png" -alpha off -compose CopyOpacity -composite -compose Over \
        \( +clone -background black -shadow 35x14+0+4 \) +swap -background none -layers merge +repage \
        "$out.png"
    [ -n "${KEEP_RAW:-}" ] || rm -f "$out.raw.png"
    echo "$out.png"
}

# every light/dark pair of one list entry
pair() { # dir name example state language
    for scheme in light dark; do
        sfx=""; cs=prefer-light
        if [ "$scheme" = dark ]; then sfx="-dark"; cs=prefer-dark; fi
        state="$4"; [ "$state" = - ] && state=""
        shoot "$1/$2$sfx" "$3" "$state" "$5" "$cs"
    done
}

entries() { # target: lines "name example state" whose targets contain it
    grep -v '^[[:space:]]*\(#\|$\)' "$LIST" | while read -r name example state targets; do
        case " ${ONLY:-$name} " in *" $name "*) ;; *) continue ;; esac
        for t in $targets; do
            case "$t" in "$1") echo "$name $example $state" ;; esac
        done
    done
}

if [ "${1:-}" = --docs ]; then
    shift
    langs="$*"
    [ -n "$langs" ] || langs="en $(for p in site/po/*.po; do [ -e "$p" ] && basename "$p" .po; done)"
    for lang in $langs; do
        dir="site/build/screenshots/$lang"
        mkdir -p "$dir"
        app_lang="$lang"; [ "$lang" = en ] && app_lang=C
        entries docs | while read -r name example state; do
            pair "$dir" "$name" "$example" "$state" "$app_lang"
        done
    done
else
    entries readme | while read -r name example state; do
        pair docs/screenshots "$name" "$example" "$state" C
    done
    for t in $(grep -o 'readme:[A-Za-z_]*' "$LIST" | sort -u); do
        entries "$t" | while read -r name example state; do
            pair docs/screenshots "$name" "$example" "$state" "${t#readme:}"
        done
    done
fi
