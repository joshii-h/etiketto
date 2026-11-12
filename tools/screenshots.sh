#!/bin/sh
# Take the screenshots listed in tools/screenshots.list in the default GNOME
# style, light and dark, 1000x700, with rounded corners and a soft shadow.
#
#   tools/screenshots.sh                README images   -> docs/screenshots/
#   tools/screenshots.sh --docs [LL…]   documentation   -> site/build/screenshots/<lang>/
#                                       (default: en and every site/po/<lang>.po)
#   ONLY="main print" tools/screenshots.sh …   only these images
#
# The app runs on a D-Bus session of its own (dbus-run-session), never on the
# desktop's: there another running Tavletta would take the files. Its
# windows go to a headless display of their own, so nothing appears on the
# desktop: a virtual KWin (kwin_wayland --virtual), else gtk4-broadwayd; the
# app sizes its window itself. SHOTS_ON_DESKTOP=1 uses the current display
# instead: niri or xdotool (X11, e.g. xvfb-run in CI) set the window size.
# Needs ImageMagick and gettext. PYTHON defaults to .venv/bin/python.
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
LIST=tools/screenshots.list
if [ -z "${TAVLETTA_SHOTS_SESSION:-}" ]; then
    export TAVLETTA_SHOTS_SESSION=1
    if command -v dbus-run-session >/dev/null; then
        exec dbus-run-session -- "$0" "$@"
    fi
    echo "warning: no dbus-run-session, the app uses the current D-Bus session" >&2
fi
CONF="$(mktemp -d)"
HEADLESS=""
cleanup() {
    [ -z "$HEADLESS" ] || kill "$HEADLESS" 2>/dev/null || true
    rm -rf "$CONF"
}
trap cleanup EXIT
mkdir -p "$CONF/tavletta" "$CONF/compositor"
echo '{"host": "VC-500W.local", "printers": {"cab": {"host": "EOS2.local"}}}' > "$CONF/tavletta/settings.json"

wait_socket() { # path
    for _ in $(seq 100); do
        [ -S "$1" ] && return 0
        sleep 0.1
    done
    echo "error: the headless display did not start ($1)" >&2
    exit 1
}

if [ "${SHOTS_ON_DESKTOP:-0}" != 1 ]; then
    run="${XDG_RUNTIME_DIR:-/tmp}"
    if command -v kwin_wayland >/dev/null; then
        sock="tavletta-shots-$$"
        # its own configuration: KWin writes the virtual outputs there
        XDG_CONFIG_HOME="$CONF/compositor" kwin_wayland --virtual --no-lockscreen --no-global-shortcuts \
            --width 1280 --height 900 --socket "$sock" >/dev/null 2>&1 &
        HEADLESS=$!
        wait_socket "$run/$sock"
        unset DISPLAY NIRI_SOCKET
        export WAYLAND_DISPLAY="$sock" GDK_BACKEND=wayland
    elif command -v gtk4-broadwayd >/dev/null; then
        n=$(( $$ % 1000 + 100 ))
        while [ -e "$run/broadway$((n + 1)).socket" ]; do n=$((n + 1)); done
        gtk4-broadwayd ":$n" >/dev/null 2>&1 &
        HEADLESS=$!
        wait_socket "$run/broadway$((n + 1)).socket"
        unset WAYLAND_DISPLAY DISPLAY NIRI_SOCKET
        export GDK_BACKEND=broadway BROADWAY_DISPLAY=":$n"
    elif [ -n "${WAYLAND_DISPLAY:-}" ]; then
        echo "error: no kwin_wayland or gtk4-broadwayd for a headless display;" >&2
        echo "SHOTS_ON_DESKTOP=1 takes the screenshots on the desktop" >&2
        exit 1
    fi
fi

# ImageMagick 7 is `magick`, ImageMagick 6 (some distributions) `convert`
if command -v magick >/dev/null; then IM=magick; IDENTIFY="magick identify"; else IM=convert; IDENTIFY=identify; fi

# the app reads its translations from tavletta/locale
for po in po/*.po; do
    lang="$(basename "$po" .po)"
    mkdir -p "tavletta/locale/$lang/LC_MESSAGES"
    msgfmt -o "tavletta/locale/$lang/LC_MESSAGES/tavletta.mo" "$po"
done

size_window() { # pid
    if [ -n "${NIRI_SOCKET:-}" ] && command -v niri >/dev/null; then
        id=""
        for _ in $(seq 40); do
            id=$(niri msg --json windows | python3 -c 'import json,sys
w=[x for x in json.load(sys.stdin) if x.get("app_id")=="xyz.joshuahirsig.Tavletta"]
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
    # a snapshot taken while the window is between two frames comes out
    # empty (the app then saves nothing): try again
    for _ in 1 2 3; do
        XDG_CONFIG_HOME="$CONF" ADW_DISABLE_PORTAL=1 ADW_DEBUG_COLOR_SCHEME="$5" LANGUAGE="$4" \
        TAVLETTA_SCREENSHOT="$out.raw.png" TAVLETTA_SCREENSHOT_STATE="$3" TAVLETTA_SCREENSHOT_DELAY=6000 \
            "$PYTHON" -m tavletta.app "docs/examples/$2.tavletta" </dev/null >/dev/null 2>&1 &
        pid=$!
        size_window "$pid"
        wait "$pid" || true
        [ -f "$out.raw.png" ] && break
    done
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
