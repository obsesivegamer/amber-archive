#!/usr/bin/env bash
# Health for reader, ingest, import, and amber-web, plus funnel off.
# A missing tool, a bad answer, or an unclear funnel status is a failure.
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
STAMP="${AMBER_BACKUP_STAMP:-${HOME}/.local/share/amber/backup-last-success}"
MAX_AGE="${AMBER_BACKUP_MAX_AGE:-93600}"

netrc="$(mktemp)"
chmod 0600 "$netrc"
trap 'rm -f "$netrc"' EXIT

check() {
  local url="$1" use_auth="${2:-}"
  local host
  host="$(printf '%s\n' "$url" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
  local -a args=(-fsS --max-time 5)
  if [[ -n "$use_auth" ]]; then
    printf 'machine %s login %s password %s\n' "$host" "$USER_NAME" "$PASSWORD" >"$netrc"
    args+=(--netrc-file "$netrc")
  fi
  if ! curl "${args[@]}" "$url" >/dev/null; then
    die "$url did not answer"
  fi
  log "ok $url"
}

[[ -n "$PASSWORD" ]] || die "set AMBER_HEALTH_PASSWORD (the reader password)"
check "$READER" yes
check "$INGEST"
check "$IMPORT"
check "$WEB" yes

if ! status="$("$FUNNEL_CMD" funnel status 2>&1)"; then
  die "tailscale funnel status failed"
fi
if printf '%s\n' "$status" | grep -Eiq 'funnel on'; then
  die "tailscale funnel is on"
fi
trimmed="${status//[[:space:]]/}"
if [[ -z "$trimmed" ]]; then
  log "tailscale funnel is off"
elif printf '%s\n' "$status" | grep -Eiq 'no funnel|funnel is disabled'; then
  log "tailscale funnel is off"
else
  die "tailscale funnel status is not a known off state"
fi

if [[ ! -f "$STAMP" ]]; then
  die "no backup stamp at $STAMP"
fi
now="$(date -u +%s)"
then="$(tr -cd '0-9' <"$STAMP")"
[[ -n "$then" ]] || die "backup stamp is empty"
age=$((now - then))
if ((age > MAX_AGE)); then
  die "newest backup is ${age}s old"
fi
log "backup stamp is ${age}s old"
