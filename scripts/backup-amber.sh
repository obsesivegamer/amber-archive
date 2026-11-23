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
# Local rsync. On Dell this is the user-space binary, not DSM's.
RSYNC_BIN="${AMBER_RSYNC:-rsync}"
# Remote rsync. boxj's user-space binary, the one sync-boxj.sh runs there.
# $HOME is expanded by the remote shell only if the path is passed quoted;
# rsync execs this path, so set a full path when the home directory differs.
RSYNC_PATH="${AMBER_RSYNC_PATH:-\$HOME/bin/rsync}"
if [[ -z "${AMBER_BACKUP_SSH+x}" ]]; then
  SSH=(ssh -p 3003 -o BatchMode=yes)
else
  # shellcheck disable=SC2206
  SSH=(${AMBER_BACKUP_SSH})
fi
TODAY="${AMBER_BACKUP_DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${AMBER_BACKUP_STAMP:-${HOME}/.local/share/amber/backup-last-success}"
[[ "$TODAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "bad backup date: $TODAY"
[[ "$KEEP" =~ ^[0-9]+$ ]] || die "AMBER_BACKUP_KEEP must be a number"
((KEEP >= 1)) || die "AMBER_BACKUP_KEEP must be at least 1"

remote=0
case "$DEST" in
  *:*) remote=1 ;;
esac
if [[ "$remote" -eq 1 && ${#SSH[@]} -eq 0 ]]; then
  die "AMBER_BACKUP_DEST is remote but AMBER_BACKUP_SSH is empty"
fi

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is not on PATH"
command -v "$RSYNC_BIN" >/dev/null 2>&1 || die "rsync not found: $RSYNC_BIN"
[[ -f "$LIBRARY/amber.sqlite3" ]] || die "library database missing: $LIBRARY/amber.sqlite3"
[[ -f "$STATE/app.sqlite3" ]] || die "app-state database missing: $STATE/app.sqlite3"
for name in sources.json homepage.json; do
  [[ -f "$STATE/$name" ]] || die "missing $STATE/$name"
done

stage="$STAGING_ROOT/current"
# One staging directory. Drop anything left from the previous run, and
# drop this one only after a successful copy so a failure keeps at most
# that single directory.
rm -rf "$STAGING_ROOT"
mkdir -p "$stage/state"
backup_ok=0
cleanup_stage() {
  if [[ "$backup_ok" -eq 1 ]]; then
    rm -rf "$STAGING_ROOT"
  fi
}
trap cleanup_stage EXIT
log "staging $TODAY under $stage"

sqlite3 "$LIBRARY/amber.sqlite3" ".backup '$stage/amber.sqlite3'" \
  || die "sqlite3 backup of the library failed"
sqlite3 "$STATE/app.sqlite3" ".backup '$stage/state/app.sqlite3'" \
  || die "sqlite3 backup of app-state failed"

# Whole app-state tree except the live database and its sidecars.
while IFS= read -r -d '' path; do
  rel="${path#"$STATE"/}"
  case "$rel" in
    app.sqlite3*) continue ;;
  esac
  mkdir -p "$stage/state/$(dirname "$rel")"
  cp -p "$path" "$stage/state/$rel"
done < <(find "$STATE" -mindepth 1 -type f -print0)
[[ -f "$stage/state/sources.json" && -f "$stage/state/homepage.json" ]] \
  || die "staged app-state is missing sources.json or homepage.json"

python3 - "$stage" "$LIBRARY" <<'PY' || die "could not write manifest"
import hashlib, json, sqlite3, sys
from pathlib import Path
stage, library = Path(sys.argv[1]), Path(sys.argv[2])
db = sqlite3.connect(stage / "amber.sqlite3")
count = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
ids = [
    row[0]
    for row in db.execute(
        "SELECT id FROM snapshots WHERE status = 'complete' ORDER BY id LIMIT 5"
    )
]
articles = []
for sid in ids:
    path = library / "snaps" / sid / "article.txt"
    if not path.is_file():
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    articles.append({"id": sid, "sha256": digest})
state = sqlite3.connect(stage / "state" / "app.sqlite3")
candidates = state.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
sources = json.loads((stage / "state" / "sources.json").read_text(encoding="utf-8"))
(stage / "manifest.json").write_text(
    json.dumps(
        {
            "snapshots": count,
            "articles": articles,
            "candidates": candidates,
            "sources": len(sources.get("sources", [])),
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

host=""
path=""
if [[ "$remote" -eq 1 ]]; then
  host="${DEST%%:*}"
  path="${DEST#*:}"
  "${SSH[@]}" "$host" "mkdir -p $(printf '%q' "$path")" || die "could not create $DEST"
else
  mkdir -p "$DEST"
fi

# Fills DAYS in the current shell. A failed ssh listing exits the script.
DAYS=()
list_days() {
  local raw name code
  local -a found=()
  DAYS=()
  if [[ "$remote" -eq 0 ]]; then
    if ! raw="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -printf '%f\n')"; then
      die "could not list $DEST"
    fi
  else
    if ! raw="$("${SSH[@]}" "$host" "ls -1 $(printf '%q' "$path")")"; then
      die "could not list $DEST"
    fi
  fi
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
    if [[ "$remote" -eq 0 ]]; then
      [[ -f "$DEST/$name/.complete" ]] || continue
      found+=("$name")
    else
      set +e
      "${SSH[@]}" "$host" "test -f $(printf '%q' "$path/$name/.complete")"
      code=$?
      set -e
      if [[ "$code" -eq 0 ]]; then
        found+=("$name")
      elif [[ "$code" -ne 1 ]]; then
        die "could not check $DEST/$name"
      fi
    fi
  done <<<"$raw"
  if ((${#found[@]})); then
    mapfile -t DAYS < <(printf '%s\n' "${found[@]}" | sort)
  fi
}

list_days
prev=""
for name in "${DAYS[@]}"; do
  [[ "$name" == "$TODAY" ]] && continue
  prev="$name"
done
log "previous complete version: ${prev:-none}"

rsync_to() {
  local src="$1" dst="$2" link_dest="$3"
  local -a args=(-a)
  if [[ "$remote" -eq 1 ]]; then
    args+=(-e "${SSH[*]}" --rsync-path="$RSYNC_PATH")
  fi
  if [[ -n "$link_dest" ]]; then
    args+=(--link-dest="$link_dest")
  fi
  set +e
  "$RSYNC_BIN" "${args[@]}" "$src" "$dst"
  local code=$?
  set -e
  if [[ "$code" -eq 24 ]]; then
    log "warning: rsync reported vanished files: $src"
    return 0
  fi
  if [[ "$code" -ne 0 ]]; then
    die "rsync failed ($code): $src -> $dst"
  fi
}

if [[ "$remote" -eq 1 ]]; then
  "${SSH[@]}" "$host" "mkdir -p $(printf '%q' "$path/$TODAY/state") $(printf '%q' "$path/$TODAY/snaps") $(printf '%q' "$path/$TODAY/blobs")" \
    || die "could not create $DEST/$TODAY"
fi

root_link=""
state_link=""
snaps_link=""
blobs_link=""
if [[ -n "$prev" ]]; then
  if [[ "$remote" -eq 0 ]]; then
    root_link="$DEST/$prev"
    state_link="$DEST/$prev/state"
    snaps_link="$DEST/$prev/snaps"
    blobs_link="$DEST/$prev/blobs"
  else
    root_link="../$prev"
    state_link="../../$prev/state"
    snaps_link="../../$prev/snaps"
    blobs_link="../../$prev/blobs"
  fi
fi
if [[ "$remote" -eq 0 ]]; then
  mkdir -p "$DEST/$TODAY"
fi
rsync_to "$stage/amber.sqlite3" "$DEST/$TODAY/amber.sqlite3" "$root_link"
rsync_to "$stage/manifest.json" "$DEST/$TODAY/manifest.json" "$root_link"
rsync_to "$stage/state/" "$DEST/$TODAY/state/" "$state_link"
if [[ -d "$LIBRARY/snaps" ]]; then
  if [[ "$remote" -eq 0 ]]; then
    mkdir -p "$DEST/$TODAY/snaps"
  fi
  rsync_to "$LIBRARY/snaps/" "$DEST/$TODAY/snaps/" "$snaps_link"
fi
if [[ -d "$LIBRARY/blobs" ]]; then
  if [[ "$remote" -eq 0 ]]; then
    mkdir -p "$DEST/$TODAY/blobs"
  fi
  rsync_to "$LIBRARY/blobs/" "$DEST/$TODAY/blobs/" "$blobs_link"
fi

if [[ "$remote" -eq 0 ]]; then
  printf '%s\n' "$TODAY" >"$DEST/$TODAY/.complete"
else
  "${SSH[@]}" "$host" "printf '%s\n' $(printf '%q' "$TODAY") > $(printf '%q' "$path/$TODAY/.complete")" \
    || die "could not mark $TODAY complete"
fi

list_days
if ((${#DAYS[@]} > KEEP)); then
  drop=$((${#DAYS[@]} - KEEP))
  for ((i = 0; i < drop; i++)); do
    name="${DAYS[$i]}"
    [[ "$name" == "$TODAY" ]] && die "refusing to prune today's backup"
    log "pruning $name"
    if [[ "$remote" -eq 0 ]]; then
      rm -rf "$DEST/$name"
    else
      "${SSH[@]}" "$host" "rm -rf $(printf '%q' "$path/$name")" || die "could not prune $name"
    fi
  done
fi

mkdir -p "$(dirname "$STAMP")"
date -u +%s >"$STAMP"
backup_ok=1
log "backup $TODAY complete"
