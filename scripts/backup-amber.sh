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
if [[ -z "${AMBER_BACKUP_STAGING+x}" ]]; then
  STAGING_ROOT="${HOME}/.local/share/amber/backup-staging"
elif [[ -z "$AMBER_BACKUP_STAGING" ]]; then
  die "AMBER_BACKUP_STAGING is empty"
else
  STAGING_ROOT="$AMBER_BACKUP_STAGING"
fi
DEST="${AMBER_BACKUP_DEST:-boxj:amber-backups}"
KEEP="${AMBER_BACKUP_KEEP:-30}"
# Local rsync. On Dell this is the user-space binary, not DSM's.
RSYNC_BIN="${AMBER_RSYNC:-rsync}"
# Remote rsync. boxj's shell runs this path; it is not executed on Dell.
# sync-boxj.sh is not in this repo. amber-reader scripts/sync-from-mac.sh
# invokes the remote PATH rsync and does not pass --rsync-path. The default
# here is the user-space binary, $HOME/bin/rsync, which that shell expands.
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

resolve_dir() {
  local target="$1"
  if [[ -d "$target" ]]; then
    (cd "$target" && pwd -P)
  else
    local parent base
    parent="$(dirname "$target")"
    base="$(basename "$target")"
    if [[ -d "$parent" ]]; then
      printf '%s/%s\n' "$(cd "$parent" && pwd -P)" "$base"
    else
      printf '%s\n' "$target"
    fi
  fi
}
stage_root="$(resolve_dir "$STAGING_ROOT")"
home_real="$(resolve_dir "$HOME")"
library_real="$(resolve_dir "$LIBRARY")"
state_real="$(resolve_dir "$STATE")"
[[ "$stage_root" != "/" ]] || die "refusing to use / as the staging root"
[[ "$stage_root" != "$home_real" ]] || die "refusing to use the home directory as the staging root"
refuse_stage() {
  local live="$1" label="$2"
  [[ -n "$live" ]] || return 0
  [[ "$stage_root" != "$live" ]] || die "refusing to use $label as the staging root"
  case "$stage_root" in
    "$live"/*) die "refusing to stage inside $label" ;;
  esac
  case "$live" in
    "$stage_root"/*) die "refusing to stage above $label" ;;
  esac
}
refuse_stage "$library_real" "the library"
refuse_stage "$state_real" "app-state"
# Only this directory is ever deleted. Dated names here are previous runs.
stage_base="$STAGING_ROOT/amber-backup-stage"
stage="$stage_base/current"
mkdir -p "$stage_base"
find "$stage_base" -mindepth 1 -maxdepth 1 \( -name current -o -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' \) -exec rm -rf {} +
mkdir -p "$stage/state"
backup_ok=0
cleanup_stage() {
  if [[ "$backup_ok" -eq 1 ]]; then
    rm -rf "$stage"
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

def table_counts(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts = {}
    for name in names:
        if not name.replace("_", "").isalnum():
            raise SystemExit(f"unexpected table name: {name}")
        counts[name] = db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    db.close()
    return counts

library_tables = table_counts(stage / "amber.sqlite3")
app_tables = table_counts(stage / "state" / "app.sqlite3")
db = sqlite3.connect(f"file:{stage / 'amber.sqlite3'}?mode=ro", uri=True)
ids = []
if "snapshots" in library_tables:
    ids = [row[0] for row in db.execute("SELECT id FROM snapshots ORDER BY id LIMIT 5")]
db.close()
articles = []
for sid in ids:
    path = library / "snaps" / sid / "article.txt"
    if not path.is_file():
        continue
    raw = path.read_bytes()
    sample = raw.decode("utf-8", errors="replace").strip()[:40]
    if not sample:
        continue
    articles.append({"id": sid, "sha256": hashlib.sha256(raw).hexdigest(), "sample": sample})
if not articles:
    raise SystemExit("no article.txt files to record")
snaps = library / "snaps"
snaps_files = sum(1 for path in snaps.rglob("*") if path.is_file()) if snaps.is_dir() else 0
sources_doc = json.loads((stage / "state" / "sources.json").read_text(encoding="utf-8"))
sources = sources_doc.get("sources")
if not isinstance(sources, list):
    raise SystemExit("sources.json has no sources list")
if "candidates" not in app_tables:
    raise SystemExit("app-state has no candidates table")
(stage / "manifest.json").write_text(
    json.dumps(
        {
            "library_tables": library_tables,
            "app_tables": app_tables,
            "articles": articles,
            "snaps_files": snaps_files,
            "sources": len(sources),
            "candidates": app_tables["candidates"],
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

# Fills DAYS (complete) and INCOMPLETE. One remote command lists both.
DAYS=()
INCOMPLETE=()
list_days() {
  local raw kind name
  local -a complete=() incomplete=()
  DAYS=()
  INCOMPLETE=()
  if [[ "$remote" -eq 0 ]]; then
    if ! raw="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -printf '%f\n')"; then
      die "could not list $DEST"
    fi
    while IFS= read -r name; do
      [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
      if [[ -f "$DEST/$name/.complete" ]]; then
        complete+=("$name")
      else
        incomplete+=("$name")
      fi
    done <<<"$raw"
  else
    local script
    script=$(cat <<EOF
cd $(printf '%q' "$path") || exit 1
for d in *; do
  case "\$d" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      if [ -f "\$d/.complete" ]; then
        printf 'C %s\n' "\$d"
      else
        printf 'I %s\n' "\$d"
      fi
      ;;
  esac
done
EOF
)
    if ! raw="$("${SSH[@]}" "$host" "$script")"; then
      die "could not list $DEST"
    fi
    while read -r kind name; do
      [[ -n "$kind" ]] || continue
      case "$kind" in
        C) complete+=("$name") ;;
        I) incomplete+=("$name") ;;
      esac
    done <<<"$raw"
  fi
  if ((${#complete[@]})); then
    mapfile -t DAYS < <(printf '%s\n' "${complete[@]}" | sort)
  fi
  if ((${#incomplete[@]})); then
    mapfile -t INCOMPLETE < <(printf '%s\n' "${incomplete[@]}" | sort)
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
  rm -f "$DEST/$TODAY/.complete"
else
  "${SSH[@]}" "$host" "rm -f $(printf '%q' "$path/$TODAY/.complete")" \
    || die "could not clear the completion marker for $TODAY"
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
  list_days
fi
# Unmarked days older than the oldest kept complete backup, or older than
# two days, are not versions. Drop those directories whole.
oldest="${DAYS[0]:-$TODAY}"
cutoff="$(date -u -d "$TODAY - 2 days" +%Y-%m-%d)"
for name in "${INCOMPLETE[@]}"; do
  [[ "$name" == "$TODAY" ]] && continue
  if [[ "$name" < "$oldest" || "$name" < "$cutoff" ]]; then
    log "pruning incomplete $name"
    if [[ "$remote" -eq 0 ]]; then
      rm -rf "$DEST/$name"
    else
      "${SSH[@]}" "$host" "rm -rf $(printf '%q' "$path/$name")" || die "could not prune $name"
    fi
  fi
done

mkdir -p "$(dirname "$STAMP")"
date -u +%s >"$STAMP"
backup_ok=1
log "backup $TODAY complete"
