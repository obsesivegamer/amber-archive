#!/usr/bin/env bash
# Daily backup of the Dell-PLX library and amber-web app-state.
# Stages consistent sqlite copies, then rsyncs them plus snaps/ and blobs/
# to boxj over SSH. Re-running on the same UTC day updates that day's
# directory. Older days are removed only as whole directories.
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "backup failed: $*"; exit 1; }

LIBRARY="${AMBER_LIBRARY_DIR:-${HOME}/apps/amber-reader/data}"
STATE="${AMBER_APP_STATE:-${HOME}/.local/share/amber/web-state}"
STAGING_ROOT="${AMBER_BACKUP_STAGING:-${HOME}/.local/share/amber/backup-staging}"
DEST="${AMBER_BACKUP_DEST:-boxj:amber-backups}"
KEEP="${AMBER_BACKUP_KEEP:-30}"
RSYNC_BIN="${AMBER_RSYNC:-rsync}"
# Unset means the boxj SSH command. Set it to empty for a local destination.
if [[ -z "${AMBER_BACKUP_SSH+x}" ]]; then
  SSH=(ssh -p 3003 -o BatchMode=yes)
else
  # shellcheck disable=SC2206
  SSH=(${AMBER_BACKUP_SSH})
fi
TODAY="${AMBER_BACKUP_DATE:-$(date -u +%Y-%m-%d)}"
[[ "$TODAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "bad backup date: $TODAY"
[[ "$KEEP" =~ ^[0-9]+$ ]] || die "AMBER_BACKUP_KEEP must be a number"

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is not on PATH"
command -v "$RSYNC_BIN" >/dev/null 2>&1 || die "rsync not found: $RSYNC_BIN"
[[ -f "$LIBRARY/amber.sqlite3" ]] || die "library database missing: $LIBRARY/amber.sqlite3"
[[ -f "$STATE/app.sqlite3" ]] || die "app-state database missing: $STATE/app.sqlite3"
for name in sources.json homepage.json; do
  [[ -f "$STATE/$name" ]] || die "missing $STATE/$name"
done

stage="$STAGING_ROOT/$TODAY"
rm -rf "$stage"
mkdir -p "$stage/state"
log "staging $TODAY under $stage"

sqlite3 "$LIBRARY/amber.sqlite3" ".backup '$stage/amber.sqlite3'" \
  || die "sqlite3 backup of the library failed"
sqlite3 "$STATE/app.sqlite3" ".backup '$stage/state/app.sqlite3'" \
  || die "sqlite3 backup of app-state failed"
# The rest of the app-state folder, without the live database or its sidecars.
while IFS= read -r -d '' path; do
  cp -p "$path" "$stage/state/$(basename "$path")"
done < <(find "$STATE" -mindepth 1 -maxdepth 1 -type f ! -name 'app.sqlite3*' -print0)
[[ -f "$stage/state/sources.json" && -f "$stage/state/homepage.json" ]] \
  || die "staged app-state is missing sources.json or homepage.json"

remote=0
case "$DEST" in
  *:*) remote=1 ;;
esac

list_days() {
  if [[ "$remote" -eq 0 ]]; then
    find "$DEST" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
      | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort || true
  else
    local host="${DEST%%:*}" path="${DEST#*:}"
    "${SSH[@]}" "$host" "ls -1 $(printf '%q' "$path") 2>/dev/null" \
      | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort || true
  fi
}

prev="$(list_days | grep -v "^${TODAY}$" | tail -n 1 || true)"
log "previous version: ${prev:-none}"

rsync_to() {
  local src="$1" dst="$2" link_dest="$3"
  local -a args=(-a)
  if [[ "$remote" -eq 1 && ${#SSH[@]} -gt 0 ]]; then
    args+=(-e "${SSH[*]}")
  fi
  if [[ -n "$link_dest" ]]; then
    args+=(--link-dest="$link_dest")
  fi
  "$RSYNC_BIN" "${args[@]}" "$src" "$dst" || die "rsync failed: $src -> $dst"
}

if [[ "$remote" -eq 0 ]]; then
  mkdir -p "$DEST/$TODAY"
  root_link=""
  state_link=""
  snaps_link=""
  blobs_link=""
  if [[ -n "$prev" ]]; then
    root_link="$DEST/$prev"
    state_link="$DEST/$prev/state"
    snaps_link="$DEST/$prev/snaps"
    blobs_link="$DEST/$prev/blobs"
  fi
  rsync_to "$stage/amber.sqlite3" "$DEST/$TODAY/amber.sqlite3" "$root_link"
  rsync_to "$stage/state/" "$DEST/$TODAY/state/" "$state_link"
  if [[ -d "$LIBRARY/snaps" ]]; then
    mkdir -p "$DEST/$TODAY/snaps"
    rsync_to "$LIBRARY/snaps/" "$DEST/$TODAY/snaps/" "$snaps_link"
  fi
  if [[ -d "$LIBRARY/blobs" ]]; then
    mkdir -p "$DEST/$TODAY/blobs"
    rsync_to "$LIBRARY/blobs/" "$DEST/$TODAY/blobs/" "$blobs_link"
  fi
else
  root_link=""
  state_link=""
  snaps_link=""
  blobs_link=""
  if [[ -n "$prev" ]]; then
    root_link="../$prev"
    state_link="../../$prev/state"
    snaps_link="../../$prev/snaps"
    blobs_link="../../$prev/blobs"
  fi
  rsync_to "$stage/amber.sqlite3" "$DEST/$TODAY/amber.sqlite3" "$root_link"
  rsync_to "$stage/state/" "$DEST/$TODAY/state/" "$state_link"
  if [[ -d "$LIBRARY/snaps" ]]; then
    rsync_to "$LIBRARY/snaps/" "$DEST/$TODAY/snaps/" "$snaps_link"
  fi
  if [[ -d "$LIBRARY/blobs" ]]; then
    rsync_to "$LIBRARY/blobs/" "$DEST/$TODAY/blobs/" "$blobs_link"
  fi
fi

mapfile -t days < <(list_days)
if ((${#days[@]} > KEEP)); then
  drop=$((${#days[@]} - KEEP))
  for ((i = 0; i < drop; i++)); do
    name="${days[$i]}"
    [[ "$name" == "$TODAY" ]] && die "refusing to prune today's backup"
    log "pruning $name"
    if [[ "$remote" -eq 0 ]]; then
      rm -rf "$DEST/$name"
    else
      host="${DEST%%:*}"
      path="${DEST#*:}"
      "${SSH[@]}" "$host" "rm -rf $(printf '%q' "$path/$name")" || die "could not prune $name"
    fi
  done
fi

log "backup $TODAY complete"
