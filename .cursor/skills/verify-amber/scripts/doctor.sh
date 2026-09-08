#!/usr/bin/env bash
# Read-only health check for the Amber verify instance. Exit 0 only if worth driving.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_ROOT/../../.." && pwd)"
SCRATCH_DIR="$SKILL_ROOT/.scratch"
INSTANCE_PATH="$SCRATCH_DIR/instance.json"
REPO_DATA="$REPO_ROOT/data"

fail() {
  echo "DOCTOR FAIL: $*"
  exit 1
}

if [[ ! -f "$INSTANCE_PATH" ]]; then
  fail "No instance.json at $INSTANCE_PATH. Run launch.sh."
fi

read_inst() {
  python3 - "$INSTANCE_PATH" <<'PY'
import json, sys
inst = json.load(open(sys.argv[1], encoding="utf-8"))
print(inst.get("pid") or "")
print(inst.get("url") or "")
print(inst.get("port") or "")
print(inst.get("data_dir") or "")
PY
}

mapfile -t INST < <(read_inst)
PID_VALUE="${INST[0]}"
URL="${INST[1]}"
PORT="${INST[2]}"
DATA_DIR="${INST[3]}"

if [[ "$PORT" == "8080" ]]; then
  fail "Instance port is 8080. That is the user archive. Do not drive it."
fi
if [[ "$URL" == *":8080"* ]]; then
  fail "Instance URL is $URL. Refuse to drive the user archive."
fi

if ! [[ "$PID_VALUE" =~ ^[0-9]+$ ]] || ! kill -0 "$PID_VALUE" 2>/dev/null; then
  fail "pid $PID_VALUE is not running. Run cleanup.sh then launch.sh."
fi

scratch_resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$SCRATCH_DIR")"
data_resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$DATA_DIR")"
repo_data_resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$REPO_DATA")"

python3 - "$data_resolved" "$scratch_resolved" "$repo_data_resolved" <<'PY' || exit 1
import os, sys
data, scratch, repo_data = sys.argv[1], sys.argv[2], sys.argv[3]
scratch_prefix = scratch if scratch.endswith(os.sep) else scratch + os.sep
repo_prefix = repo_data if repo_data.endswith(os.sep) else repo_data + os.sep
if data != scratch and not data.startswith(scratch_prefix):
    print(f"DOCTOR FAIL: data_dir {data} is not under {scratch}.")
    sys.exit(1)
if data == repo_data or data.startswith(repo_prefix):
    print("DOCTOR FAIL: data_dir points at repo data/. Refuse to drive.")
    sys.exit(1)
PY

DB_PATH="$data_resolved/amber.sqlite3"
if [[ ! -f "$DB_PATH" ]]; then
  fail "SQLite missing at $DB_PATH."
fi

home_page="$(curl -sS --max-time 5 -w '\n%{http_code}' "$URL/" 2>/dev/null || true)"
home_code="$(printf '%s\n' "$home_page" | tail -n1)"
home_body="$(printf '%s\n' "$home_page" | sed '$d')"
if [[ "$home_code" != "200" ]]; then
  fail "GET $URL/ returned ${home_code:-failed}"
fi
if [[ "$home_body" != *"time capsule for web pages"* ]]; then
  fail "Homepage did not contain the Amber tagline."
fi

about_code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$URL/about" 2>/dev/null || true)"
if [[ "$about_code" != "200" ]]; then
  fail "GET $URL/about returned ${about_code:-failed}"
fi

PROC_NAME="$(ps -p "$PID_VALUE" -o comm= 2>/dev/null | tr -d ' ' || echo unknown)"
echo "DOCTOR OK"
echo "url=$URL"
echo "pid=$PID_VALUE ($PROC_NAME)"
echo "data_dir=$data_resolved"
echo "db=$DB_PATH"
exit 0
