#!/usr/bin/env bash
# Compare the latest backup with live data. Live files are opened read-only.
# Nothing here writes the live library or the live app-state directory.
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "restore drill failed: $*"; exit 1; }

LIBRARY="${AMBER_LIBRARY_DIR:-${HOME}/apps/amber-reader/data}"
STATE="${AMBER_APP_STATE:-${HOME}/.local/share/amber/web-state}"
DEST="${AMBER_BACKUP_DEST:-boxj:amber-backups}"
if [[ -z "${AMBER_BACKUP_SSH+x}" ]]; then
  SSH=(ssh -p 3003 -o BatchMode=yes)
else
  # shellcheck disable=SC2206
  SSH=(${AMBER_BACKUP_SSH})
fi
WORK="${AMBER_RESTORE_WORK:-$(mktemp -d)}"
SKIP_SERVER="${AMBER_RESTORE_SKIP_SERVER:-0}"

remote=0
case "$DEST" in
  *:*) remote=1 ;;
esac

if [[ "$remote" -eq 0 ]]; then
  latest="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -n 1)"
  [[ -n "$latest" ]] || die "no dated backup under $DEST"
  copy="$DEST/$latest"
else
  host="${DEST%%:*}"
  path="${DEST#*:}"
  latest="$("${SSH[@]}" "$host" "ls -1 $(printf '%q' "$path")" \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -n 1)"
  [[ -n "$latest" ]] || die "no dated backup under $DEST"
  copy="$WORK/nas"
  mkdir -p "$copy"
  rsync -a -e "${SSH[*]}" "$DEST/$latest/" "$copy/"
fi

live_db="file:${LIBRARY}/amber.sqlite3?mode=ro"
[[ -f "$LIBRARY/amber.sqlite3" ]] || die "live library database missing"
[[ -f "$copy/amber.sqlite3" ]] || die "backup library database missing"
live_snaps="$(sqlite3 "$live_db" 'SELECT COUNT(*) FROM snapshots;')"
bak_snaps="$(sqlite3 "$copy/amber.sqlite3" 'SELECT COUNT(*) FROM snapshots;')"
[[ "$live_snaps" == "$bak_snaps" ]] || die "snapshot counts differ live=$live_snaps backup=$bak_snaps"
log "snapshot count $live_snaps"

mapfile -t ids < <(sqlite3 "$live_db" "SELECT id FROM snapshots WHERE status = 'complete' ORDER BY id LIMIT 5;")
((${#ids[@]} > 0)) || die "live library has no complete snapshots"
for sid in "${ids[@]}"; do
  live_file="$LIBRARY/snaps/$sid/article.txt"
  bak_file="$copy/snaps/$sid/article.txt"
  [[ -f "$live_file" && -f "$bak_file" ]] || die "article.txt missing for $sid"
  live_hash="$(sha256sum "$live_file" | awk '{print $1}')"
  bak_hash="$(sha256sum "$bak_file" | awk '{print $1}')"
  [[ "$live_hash" == "$bak_hash" ]] || die "article hash differs for $sid"
  log "article $sid $live_hash"
done

[[ -f "$STATE/app.sqlite3" && -f "$copy/state/app.sqlite3" ]] || die "app-state database missing"
live_state="file:${STATE}/app.sqlite3?mode=ro"
live_cands="$(sqlite3 "$live_state" 'SELECT COUNT(*) FROM candidates;')"
bak_cands="$(sqlite3 "$copy/state/app.sqlite3" 'SELECT COUNT(*) FROM candidates;')"
[[ "$live_cands" == "$bak_cands" ]] || die "candidate counts differ live=$live_cands backup=$bak_cands"
live_sources="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["sources"]))' "$STATE/sources.json")"
bak_sources="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["sources"]))' "$copy/state/sources.json")"
[[ "$live_sources" == "$bak_sources" ]] || die "source counts differ live=$live_sources backup=$bak_sources"
log "candidates $live_cands sources $live_sources"

if [[ "$SKIP_SERVER" == "1" ]]; then
  log "skipped amber-reader server start"
  exit 0
fi
[[ -n "${AMBER_READER_DIR:-}" ]] || die "set AMBER_READER_DIR to start amber-reader, or AMBER_RESTORE_SKIP_SERVER=1"
port="${AMBER_RESTORE_PORT:-18081}"
reader_copy="$WORK/reader-data"
mkdir -p "$reader_copy"
cp -a "$copy/amber.sqlite3" "$reader_copy/amber.sqlite3"
if [[ -d "$copy/snaps" ]]; then
  cp -a "$copy/snaps" "$reader_copy/snaps"
fi
(
  cd "$AMBER_READER_DIR"
  AMBER_DATA_DIR="$reader_copy" AMBER_HOST=127.0.0.1 AMBER_PORT="$port" \
    "${AMBER_READER_PYTHON:-python3}" -m uvicorn app.main:app --host 127.0.0.1 --port "$port"
) &
reader_pid=$!
trap 'kill "$reader_pid" 2>/dev/null || true' EXIT
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${port}/healthz" >/dev/null; then
    ok=1
    break
  fi
  sleep 0.4
done
[[ "$ok" == "1" ]] || die "amber-reader did not answer on $port"
log "amber-reader answered on $port against the backup copy"
