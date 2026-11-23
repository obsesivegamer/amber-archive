#!/usr/bin/env bash
# Loopback health for the four Amber processes, plus a check that funnel is off.
# Does not change serve config. Exits nonzero on the first failure after logging it.
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "health failed: $*"; exit 1; }

READER="${AMBER_READER_HEALTH_URL:-http://127.0.0.1:8081/healthz}"
INGEST="${AMBER_INGEST_HEALTH_URL:-http://127.0.0.1:8082/healthz}"
IMPORT="${AMBER_IMPORT_HEALTH_URL:-http://127.0.0.1:8083/healthz}"
WEB="${AMBER_WEB_HEALTH_URL:-http://127.0.0.1:8084/healthz}"
USER_NAME="${AMBER_HEALTH_USER:-amber}"
PASSWORD="${AMBER_HEALTH_PASSWORD:-}"
FUNNEL_CMD="${AMBER_TAILSCALE:-tailscale}"

check() {
  local url="$1" auth="${2:-}"
  local -a args=(-fsS --max-time 5)
  if [[ -n "$auth" ]]; then
    args+=(-u "$auth")
  fi
  if ! curl "${args[@]}" "$url" >/dev/null; then
    die "$url did not answer"
  fi
  log "ok $url"
}

[[ -n "$PASSWORD" ]] || die "set AMBER_HEALTH_PASSWORD (the reader password)"
check "$READER" "$USER_NAME:$PASSWORD"
check "$INGEST"
check "$IMPORT"
check "$WEB" "$USER_NAME:$PASSWORD"

status="$("$FUNNEL_CMD" funnel status 2>&1 || true)"
if printf '%s\n' "$status" | grep -Eiq 'funnel on'; then
  die "tailscale funnel is on"
fi
log "tailscale funnel is off"
