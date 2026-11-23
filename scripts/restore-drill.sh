#!/usr/bin/env bash
# Check the latest complete backup against the manifest written at backup time,
# then start a reader on a temp copy and fetch the same articles.
# Live library and app-state paths are never written.
#
# The reader is the checkout named by AMBER_READER_DIR. On Dell that is
# amber-reader (~/apps/amber-reader), not this amber-archive tree. The drill
# starts it as:
#   cd "$AMBER_READER_DIR"
#   env -i ... "$AMBER_READER_PYTHON" -m uvicorn app.main:app \
#     --host 127.0.0.1 --port "$AMBER_RESTORE_PORT"
# AMBER_READER_PYTHON defaults to python3. AMBER_READER_CMD replaces that
# command in tests. The drill does not ask the reader for a count.
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
checks="$(python3 - "$copy" <<'PY'
import json, sys
from pathlib import Path
copy = Path(sys.argv[1])
manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
articles = manifest.get("articles") or []
if not articles:
    raise SystemExit("manifest has no articles")
rows = []
for table, count in sorted(manifest["library_tables"].items()):
    rows.append(json.dumps({"op": "library", "table": table, "count": count}))
for table, count in sorted(manifest["app_tables"].items()):
    rows.append(json.dumps({"op": "app", "table": table, "count": count}))
for item in articles:
    rows.append(json.dumps({"op": "article", "id": item["id"], "sha256": item["sha256"], "sample": item["sample"]}))
rows.append(json.dumps({"op": "snaps", "count": manifest["snaps_files"]}))
rows.append(json.dumps({"op": "sources", "count": manifest["sources"]}))
rows.append(json.dumps({"op": "candidates", "count": manifest["candidates"]}))
print("\n".join(rows))
PY
)" || die "backup manifest cannot be read"
articles=0
library_snapshots=""
while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  op="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["op"])' "$line")"
  case "$op" in
    library|app)
      table="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["table"])' "$line")"
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["count"])' "$line")"
      [[ "$table" =~ ^[A-Za-z0-9_]+$ ]] || die "bad table name in manifest"
      if [[ "$op" == library ]]; then
        db="$copy/amber.sqlite3"
      else
        db="$copy/state/app.sqlite3"
      fi
      got="$(sqlite3 "file:${db}?mode=ro" "SELECT COUNT(*) FROM \"${table}\"")" \
        || die "could not count $table"
      [[ "$got" == "$expected" ]] || die "$op $table count $got != manifest $expected"
      if [[ "$op" == library && "$table" == snapshots ]]; then
        library_snapshots="$got"
      fi
      ;;
    article)
      sid="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$line")"
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha256"])' "$line")"
      got="$(sha256sum "$copy/snaps/$sid/article.txt" | awk '{print $1}')" \
        || die "could not hash $sid"
      [[ "$got" == "$expected" ]] || die "article $sid hash does not match the manifest"
      articles=$((articles + 1))
      ;;
    snaps)
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["count"])' "$line")"
      got="$(find "$copy/snaps" -type f | wc -l | tr -d ' ')"
      [[ "$got" == "$expected" ]] || die "snaps file count $got != manifest $expected"
      ;;
    sources)
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["count"])' "$line")"
      got="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8")).get("sources", [])))' "$copy/state/sources.json")"
      [[ "$got" == "$expected" ]] || die "source count $got != manifest $expected"
      ;;
    candidates)
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["count"])' "$line")"
      got="$(sqlite3 "file:${copy}/state/app.sqlite3?mode=ro" 'SELECT COUNT(*) FROM "candidates"')" \
        || die "could not count candidates"
      [[ "$got" == "$expected" ]] || die "candidate count $got != manifest $expected"
      ;;
  esac
done <<<"$checks"
[[ "$articles" -ge 1 ]] || die "manifest has no articles"
log "manifest matched for $latest"
printf 'manifest ok library_snapshots=%s candidates=%s snaps_files=%s articles=%s\n' \
  "${library_snapshots:-0}" \
  "$(sqlite3 "file:${copy}/state/app.sqlite3?mode=ro" 'SELECT COUNT(*) FROM "candidates"')" \
  "$(find "$copy/snaps" -type f | wc -l | tr -d ' ')" \
  "$articles"
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
import base64, json, os, sys, urllib.error, urllib.request
manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
port = sys.argv[2]
base = f"http://127.0.0.1:{port}"
headers = {}
password = os.environ.get("AMBER_READER_PASSWORD", "")
if password:
    token = base64.b64encode(f"amber:{password}".encode()).decode()
    headers["Authorization"] = f"Basic {token}"
with urllib.request.urlopen(f"{base}/healthz", timeout=10) as response:
    if response.status != 200 or not response.read().strip():
        raise SystemExit("healthz was empty")
for item in manifest["articles"]:
    request = urllib.request.Request(f"{base}/{item['id']}/article.txt", headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"article {item['id']} returned {exc.code}") from exc
    with response:
        if response.status != 200:
            raise SystemExit(f"article {item['id']} returned {response.status}")
        body = response.read().decode("utf-8", errors="replace")
    if not body.strip():
        raise SystemExit(f"article {item['id']} was empty")
    if item["sample"] not in body:
        raise SystemExit(f"article {item['id']} did not contain its snapshot text")
print(f"served {len(manifest['articles'])} articles", flush=True)
PY
log "served articles match the manifest"
