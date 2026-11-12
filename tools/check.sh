#!/bin/sh
# Run the same checks as the Forgejo CI (.forgejo/workflows/ci.yml), locally.
# Keep both in sync. Exit code != 0 on failure.
# One command per line: with `set -e` a failure inside `a && b` does not stop
# the script, which once let a formatting error through.
set -eu
cd "$(dirname "$0")/.."
PY=.venv/bin
echo "== ruff"
$PY/ruff format --check .
$PY/ruff check --output-format=full .
echo "== tests"
# The GUI tests need a display; without them the coverage gate fails. With
# none (or HEADLESS=1) they run in a headless Broadway session of their own,
# on a D-Bus session of their own (no window and no message reaches the
# desktop), else under xvfb-run, else the check stops here and says why.
PYTEST="$PY/python -m pytest -q --cov=tavletta --cov-report=term-missing"
if [ "${HEADLESS:-0}" != 1 ] && [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
  $PYTEST
elif command -v gtk4-broadwayd > /dev/null && command -v dbus-run-session > /dev/null; then
  echo "(no display or HEADLESS=1: GUI tests in a headless Broadway session)"
  # shellcheck disable=SC2016
  env -u WAYLAND_DISPLAY -u DISPLAY dbus-run-session -- sh -c '
    n=$(( $$ % 1000 + 100 ))
    while [ -e "${XDG_RUNTIME_DIR:-/tmp}/broadway$((n + 1)).socket" ]; do n=$((n + 1)); done
    gtk4-broadwayd ":$n" > /dev/null 2>&1 &
    broadway=$!
    trap "kill $broadway 2> /dev/null" EXIT
    for _ in $(seq 50); do
      [ -S "${XDG_RUNTIME_DIR:-/tmp}/broadway$((n + 1)).socket" ] && break
      sleep 0.1
    done
    GDK_BACKEND=broadway BROADWAY_DISPLAY=":$n" $0' "$PYTEST"
elif command -v xvfb-run > /dev/null; then
  echo "(no display: GUI tests under xvfb-run)"
  # shellcheck disable=SC2086
  env -u WAYLAND_DISPLAY xvfb-run -a $PYTEST
else
  echo "error: no display for the GUI tests, so the coverage gate would fail." >&2
  echo "Run in a graphical session, or install gtk4-broadwayd (GTK 4) and dbus-run-session, or xvfb-run." >&2
  exit 1
fi
echo "== mypy";      $PY/mypy
echo "== bandit";    $PY/bandit -c pyproject.toml -r tavletta -ll
echo "== complexity"
$PY/radon cc -s -n C tavletta
$PY/xenon --max-absolute C --max-modules A --max-average A tavletta
echo "== dependencies"; $PY/pip-audit --progress-spinner off .
echo "== translations"
# the app's catalogs and the documentation's (site/po)
for po in po/*.po site/po/*.po; do msgfmt --check -o /dev/null "$po"; done
# every string translated, nothing fuzzy; ALLOW_UNTRANSLATED=1 skips this
# locally (e.g. before a translation round), the CI always checks it
if [ "${ALLOW_UNTRANSLATED:-0}" = 1 ]; then
  echo "(completeness skipped: ALLOW_UNTRANSLATED=1)"
else
  incomplete=0
  for po in po/*.po; do
    stats="$(msgfmt --statistics -o /dev/null "$po" 2>&1)"
    case "$stats" in
      *untranslated*|*fuzzy*) echo "$po: $stats"; incomplete=1 ;;
    esac
  done
  if [ "$incomplete" = 1 ]; then
    echo "Untranslated strings (ALLOW_UNTRANSLATED=1 skips this check locally)."
    exit 1
  fi
fi
# the documentation: nothing fuzzy (a fuzzy entry, even a code block, shows
# the English of before); a paragraph not translated yet shows in English
for po in site/po/*.po; do
  stats="$(msgfmt --statistics -o /dev/null "$po" 2>&1)"
  case "$stats" in
    *fuzzy*) echo "$po: $stats (run python3 site/check_translations.py)"; exit 1 ;;
  esac
done
# its placeholders and code blocks against the English pages (needs po4a;
# the CI's "Docs check" workflow runs it too)
if command -v po4a > /dev/null; then
  $PY/python site/check_translations.py
else
  echo "(documentation check skipped: po4a not installed)"
fi
echo "== metadata"
appstreamcli validate --no-net --explain data/*.metainfo.xml
desktop-file-validate data/*.desktop
echo "All checks passed."
