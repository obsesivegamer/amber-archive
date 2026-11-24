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

cfg="$(mktemp)"
chmod 0600 "$cfg"
trap 'rm -f "$cfg"' EXIT

curl_quote() {
  local text="$1"
  text="${text//\\/\\\\}"
  text="${text//\"/\\\"}"
  printf '"%s"' "$text"
}

check() {
  local url="$1" use_auth="${2:-}"
  local -a args=(-fsS --max-time 5)
  if [[ -n "$use_auth" ]]; then
    case "$PASSWORD" in
      *$'\n'*) die "password contains a newline" ;;
    esac
    printf 'user = %s\n' "$(curl_quote "${USER_NAME}:${PASSWORD}")" >"$cfg"
    args+=(-K "$cfg")
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
# amber-web stays disabled until cutover. Port 8084 is required only once
# the unit is enabled. A missing systemctl is a failure: the check cannot
# tell the two states apart.
if [[ -n "${AMBER_WEB_ENABLED+x}" ]]; then
  case "$AMBER_WEB_ENABLED" in
    1) web_enabled=1 ;;
    0) web_enabled=0 ;;
    *) die "AMBER_WEB_ENABLED must be 0 or 1" ;;
  esac
else
  command -v systemctl >/dev/null 2>&1 || die "systemctl is not available"
  set +e
  systemctl --user is-enabled amber-web.service >/dev/null 2>&1
  web_code=$?
  set -e
  if [[ "$web_code" -eq 0 ]]; then
    web_enabled=1
  elif [[ "$web_code" -eq 1 || "$web_code" -eq 4 ]]; then
    web_enabled=0
  else
    die "could not tell whether amber-web.service is enabled"
  fi
fi
if [[ "$web_enabled" -eq 1 ]]; then
  check "$WEB" yes
else
  log "amber-web disabled, pre-cutover"
fi

# User jerem must be able to run this without root. A permission failure
# here fails the check every time.
err="$(mktemp)"
pyerr="$(mktemp)"
if ! status="$("$FUNNEL_CMD" serve status --json 2>"$err")"; then
  detail="$(tr '\n' ' ' <"$err" | sed 's/[[:space:]]*$//')"
  rm -f "$err" "$pyerr"
  die "tailscale serve status failed${detail:+: $detail}"
fi
rm -f "$err"
set +e
python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if raw in ("", "null"):
    sys.exit(0)
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(2)
def funnel_on(node):
    if isinstance(node, dict):
        if "AllowFunnel" in node and not isinstance(node["AllowFunnel"], dict):
            raise RuntimeError("AllowFunnel is not an object")
        allow = node.get("AllowFunnel")
        if isinstance(allow, dict) and any(value is True for value in allow.values()):
            return True
        return any(funnel_on(value) for value in node.values())
    if isinstance(node, list):
        return any(funnel_on(value) for value in node)
    return False
if funnel_on(data):
    sys.exit(10)
' <<<"$status" 2>"$pyerr"
code=$?
set -e
detail="$(tr '\n' ' ' <"$pyerr" | sed 's/[[:space:]]*$//')"
rm -f "$pyerr"
if [[ "$code" -eq 10 ]]; then
  die "tailscale funnel is on"
elif [[ "$code" -ne 0 ]]; then
  die "tailscale funnel check failed${detail:+: $detail}"
fi
log "tailscale funnel is off"

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
