#!/usr/bin/env bash
# Check the latest complete backup against the manifest written at backup time,
# then start a reader on a temp copy and fetch the same articles.
# Live library and app-state paths are never written.
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
RSYNC_BIN="${AMBER_RSYNC:-rsync}"
RSYNC_PATH="${AMBER_RSYNC_PATH:-\$HOME/bin/rsync}"
own_work=0
if [[ -z "${AMBER_RESTORE_WORK:-}" ]]; then
  WORK="$(mktemp -d)"
  own_work=1
else
  WORK="$AMBER_RESTORE_WORK"
fi
reader_pid=""
cleanup() {
  if [[ -n "$reader_pid" ]]; then
    kill "$reader_pid" 2>/dev/null || true
    wait "$reader_pid" 2>/dev/null || true
  fi
  if [[ "$own_work" -eq 1 ]]; then
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

refuse_live() {
  local path="$1"
  local real live_library live_state
  real="$(readlink -f "$path" 2>/dev/null || printf '%s' "$path")"
  live_library="$(readlink -f "$LIBRARY" 2>/dev/null || printf '%s' "$LIBRARY")"
  live_state="$(readlink -f "$STATE" 2>/dev/null || printf '%s' "$STATE")"
  case "$real" in
    "$live_library"|"$live_library"/*|"$live_state"|"$live_state"/*)
      die "refusing to use live path $path"
      ;;
  esac
}

remote=0
case "$DEST" in
  *:*) remote=1 ;;
esac
if [[ "$remote" -eq 1 && ${#SSH[@]} -eq 0 ]]; then
  die "AMBER_BACKUP_DEST is remote but AMBER_BACKUP_SSH is empty"
fi

if [[ "$remote" -eq 0 ]]; then
  [[ -d "$DEST" ]] || die "no backup destination $DEST"
  names=()
  while IFS= read -r name; do
    [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
    if [[ -f "$DEST/$name/.complete" ]]; then
      names+=("$name")
    fi
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
  latest=""
  if ((${#names[@]})); then
    latest="$(printf '%s\n' "${names[@]}" | sort | tail -n 1)"
  fi
  [[ -n "$latest" ]] || die "no complete backup under $DEST"
  copy="$DEST/$latest"
  refuse_live "$copy"
else
  host="${DEST%%:*}"
  path="${DEST#*:}"
  script=$(cat <<EOF
cd $(printf '%q' "$path") || exit 1
for d in *; do
  case "\$d" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      if [ -f "\$d/.complete" ]; then
        printf '%s\n' "\$d"
      fi
      ;;
  esac
done
EOF
)
  if ! listing="$("${SSH[@]}" "$host" "$script")"; then
    die "could not list $DEST"
  fi
  names=()
  while IFS= read -r name; do
    [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
    names+=("$name")
  done <<<"$listing"
  latest=""
  if ((${#names[@]})); then
    latest="$(printf '%s\n' "${names[@]}" | sort | tail -n 1)"
  fi
  [[ -n "$latest" ]] || die "no complete backup under $DEST"
  copy="$WORK/nas"
  refuse_live "$copy"
  mkdir -p "$copy"
  "$RSYNC_BIN" -a --exclude 'blobs/' --exclude 'blobs' -e "${SSH[*]}" --rsync-path="$RSYNC_PATH" \
    "$DEST/$latest/" "$copy/" || die "could not copy $latest"
fi

[[ -f "$copy/manifest.json" ]] || die "backup $latest has no manifest"
[[ -f "$copy/.complete" ]] || die "backup $latest is not marked complete"
python3 - "$copy" <<'PY' || die "backup does not match its manifest"
import hashlib, json, sqlite3, sys
from pathlib import Path
copy = Path(sys.argv[1])
manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
db = sqlite3.connect(f"file:{copy / 'amber.sqlite3'}?mode=ro", uri=True)
count = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
if count != manifest["snapshots"]:
    raise SystemExit(f"snapshot count {count} != manifest {manifest['snapshots']}")
state = sqlite3.connect(f"file:{copy / 'state' / 'app.sqlite3'}?mode=ro", uri=True)
candidates = state.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
if candidates != manifest["candidates"]:
    raise SystemExit(f"candidate count {candidates} != manifest {manifest['candidates']}")
sources = json.loads((copy / "state" / "sources.json").read_text(encoding="utf-8"))
if len(sources.get("sources", [])) != manifest["sources"]:
    raise SystemExit("source count does not match the manifest")
if not manifest.get("articles"):
    raise SystemExit("manifest has no articles")
if "total_usable" not in manifest:
    raise SystemExit("manifest has no total_usable")
for item in manifest["articles"]:
    path = copy / "snaps" / item["id"] / "article.txt"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit(f"article {item['id']} hash does not match the manifest")
print(
    f"manifest ok snapshots={count} total_usable={manifest['total_usable']} candidates={candidates} articles={len(manifest['articles'])}",
    flush=True,
)
PY
log "manifest matched for $latest"
if [[ -f "$LIBRARY/amber.sqlite3" ]]; then
  live="$(sqlite3 "file:${LIBRARY}/amber.sqlite3?mode=ro" "SELECT COUNT(*) FROM snapshots" 2>/dev/null || true)"
  if [[ -n "$live" ]]; then
    log "live snapshot count (not checked): $live"
  fi
fi

if [[ "${AMBER_RESTORE_SKIP_SERVER:-0}" == "1" ]]; then
  log "skipped reader server start"
  exit 0
fi
port="${AMBER_RESTORE_PORT:-18081}"
reader_copy="$WORK/reader-data"
refuse_live "$reader_copy"
mkdir -p "$reader_copy"
cp -a "$copy/amber.sqlite3" "$reader_copy/amber.sqlite3"
if [[ -d "$copy/snaps" ]]; then
  cp -a "$copy/snaps" "$reader_copy/snaps"
fi
# env replaces itself with the server, so the recorded pid is the server.
cmd=()
if [[ -n "${AMBER_READER_CMD:-}" ]]; then
  # shellcheck disable=SC2206
  cmd=(${AMBER_READER_CMD})
else
  reader_dir="${AMBER_READER_DIR:-}"
  [[ -n "$reader_dir" ]] || die "set AMBER_READER_DIR to the amber-reader checkout"
  cd "$reader_dir"
  cmd=("${AMBER_READER_PYTHON:-python3}" -m uvicorn app.main:app --host 127.0.0.1 --port "$port")
fi
env_args=(env -i "PATH=$PATH" "HOME=$HOME" "AMBER_DATA_DIR=$reader_copy" "AMBER_HOST=127.0.0.1" "AMBER_PORT=$port" "AMBER_ALLOWED_HOSTS=127.0.0.1:${port}")
if [[ -n "${AMBER_READER_PASSWORD:-}" ]]; then
  env_args+=("AMBER_READER_PASSWORD=$AMBER_READER_PASSWORD")
fi
"${env_args[@]}" "${cmd[@]}" &
reader_pid=$!
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${port}/healthz" >/dev/null; then
    ok=1
    break
  fi
  sleep 0.3
done
[[ "$ok" == "1" ]] || die "reader did not answer on $port"
log "reader answered on $port"
python3 - "$copy/manifest.json" "$port" <<'PY' || die "reader did not serve the backup"
import base64, hashlib, json, os, sys, urllib.request
manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
port = sys.argv[2]
base = f"http://127.0.0.1:{port}"
headers = {}
password = os.environ.get("AMBER_READER_PASSWORD", "")
if password:
    token = base64.b64encode(f"amber:{password}".encode()).decode()
    headers["Authorization"] = f"Basic {token}"
# total_usable is the full usable count. limit only slices the snapshots
# array (this app clamps it to 200), so the page length is not the count.
count_url = f"{base}/api/snapshots?since_days=365&limit=1"
with urllib.request.urlopen(urllib.request.Request(count_url, headers=headers), timeout=5) as response:
    payload = json.loads(response.read().decode())
if "total_usable" not in payload:
    raise SystemExit("reader did not report total_usable")
if payload["total_usable"] != manifest["total_usable"]:
    raise SystemExit(
        f"reader total_usable {payload['total_usable']} != manifest {manifest['total_usable']}"
    )
print(f"reader total_usable {payload['total_usable']}", flush=True)
for item in manifest["articles"]:
    request = urllib.request.Request(f"{base}/{item['id']}/article.txt", headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
    digest = hashlib.sha256(body).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit(f"served article {item['id']} does not match the manifest")
print(f"served {len(manifest['articles'])} articles", flush=True)
PY
log "served articles match the manifest"
