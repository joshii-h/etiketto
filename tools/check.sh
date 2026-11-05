#!/bin/sh
# Run the same checks as the Forgejo CI, locally. Exit code != 0 on failure.
set -eu
cd "$(dirname "$0")/.."
PY=.venv/bin
echo "== ruff";      $PY/ruff format --check . && $PY/ruff check .
echo "== tests";     $PY/python -m pytest -q --cov=etiketto --cov-report=term
echo "== mypy";      $PY/mypy
echo "== bandit";    $PY/bandit -q -c pyproject.toml -r etiketto -ll
echo "== complexity"; $PY/xenon --max-absolute C --max-modules A --max-average A etiketto
echo "== translations"
for po in po/*.po; do msgfmt --check -o /dev/null "$po"; done
echo "== metadata"
appstreamcli validate --no-net data/*.metainfo.xml
desktop-file-validate data/*.desktop
echo "All checks passed."
