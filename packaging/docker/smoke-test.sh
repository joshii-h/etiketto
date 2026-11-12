#!/bin/sh
# Smoke test of a built server image (.github/workflows/docker.yml runs it
# before anything is pushed):
#
#   docker build -f packaging/docker/Dockerfile -t tavletta-server .
#   packaging/docker/smoke-test.sh tavletta-server
#
# Starts `tavletta serve` as compose.yaml does (read-only, no capabilities,
# not root) with a throwaway token and a printers.toml whose printer is never
# reached, then checks: /api/v1/health answers, a request without the token
# is refused, a preview of docs/examples/shelf.tavletta comes back as a PNG,
# and Docker's healthcheck reports the container healthy.
# EXPECT_VERSION=1.2.0 also checks the version the server reports.
set -eu

IMAGE="${1:?usage: smoke-test.sh IMAGE}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAME="tavletta-smoke-$$"
WORK="$(mktemp -d)"
cleanup() {
  docker rm -f "$NAME" > /dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $*" >&2
  docker logs "$NAME" >&2 2>&1 || true
  exit 1
}

# the image's own user, never root
user="$(docker image inspect -f '{{.Config.User}}' "$IMAGE")"
case "$user" in
  "" | root | 0 | 0:*) fail "the image runs as root (USER '$user')" ;;
esac
docker image inspect -f '{{.Config.Healthcheck}}' "$IMAGE" | grep -q CMD ||
  fail "the image has no HEALTHCHECK"

mkdir "$WORK/config" "$WORK/labels"
cp "$ROOT/docs/examples/shelf.tavletta" "$WORK/labels/"
# 192.0.2.0/24 is TEST-NET-1: a preview never connects, nothing is printed
cat > "$WORK/config/printers.toml" << 'EOF'
[printers.smoke]
backend = "cab"
host = "192.0.2.10"
description = "Smoke test, never reached"
default = true
EOF
chmod -R a+rX "$WORK"

# passed through the environment, so it is on no command line
TAVLETTA_TOKEN="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
export TAVLETTA_TOKEN
docker run -d --name "$NAME" -e TAVLETTA_TOKEN \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --health-interval 2s --health-start-period 30s \
  -v "$WORK/config:/config:ro" -v "$WORK/labels:/labels:ro" \
  -p 127.0.0.1::8790 \
  "$IMAGE" --mcp --printers /config/printers.toml > /dev/null

uid="$(docker exec "$NAME" id -u)" || fail "the container did not start"
[ "$uid" != 0 ] || fail "the server runs as root"

port="$(docker port "$NAME" 8790/tcp | head -n 1 | sed 's/.*://')"
base="http://127.0.0.1:$port/api/v1"

i=0
until health="$(curl -fsS "$base/health" 2> /dev/null)"; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || fail "no answer from $base/health"
  docker inspect -f '{{.State.Running}}' "$NAME" | grep -qx true || fail "the server ended"
  sleep 1
done
echo "health: $health"
case "$health" in
  *'"status": "ok"'* | *'"status":"ok"'*) ;;
  *) fail "unexpected health answer" ;;
esac
if [ -n "${EXPECT_VERSION:-}" ]; then
  case "$health" in
    *"\"$EXPECT_VERSION\""*) ;;
    *) fail "the server is not version $EXPECT_VERSION" ;;
  esac
fi

request='{"label": "shelf", "printer": "smoke", "set": {}}'
status="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "$request" "$base/preview")"
[ "$status" = 401 ] || fail "a preview without the token answered $status, not 401"

curl -fsS -H "Authorization: Bearer $TAVLETTA_TOKEN" -H 'Content-Type: application/json' \
  -d "$request" -o "$WORK/preview.png" "$base/preview" || fail "the preview failed"
magic="$(head -c 8 "$WORK/preview.png" | od -An -tx1 | tr -d ' \n')"
[ "$magic" = 89504e470d0a1a0a ] || fail "the preview is not a PNG"
echo "preview: PNG, $(wc -c < "$WORK/preview.png") bytes"

i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ]; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "the healthcheck did not report healthy"
  sleep 1
done
echo "healthcheck: healthy"
echo "Smoke test passed: $IMAGE"
