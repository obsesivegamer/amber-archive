#!/usr/bin/env bash
# Start or reuse the isolated Amber verify instance (Linux/macOS).
# Isolation: port 18080 (or AMBER_VERIFY_PORT, never 8080) and
# AMBER_DATA_DIR under this skill's .scratch/. Never touch repo data/.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_ROOT/../../.." && pwd)"
SCRATCH_DIR="$SKILL_ROOT/.scratch"
DATA_DIR="$SCRATCH_DIR/data"
INSTANCE_PATH="$SCRATCH_DIR/instance.json"
LOG_OUT="$SCRATCH_DIR/uvicorn.out.log"
LOG_ERR="$SCRATCH_DIR/uvicorn.err.log"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  VENV_PY="$REPO_ROOT/.venv/bin/python"
elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
  VENV_PY="$REPO_ROOT/.venv/Scripts/python.exe"
else
  echo "Missing $REPO_ROOT/.venv/bin/python. Create the venv from the README (python -m venv .venv) then install requirements and Playwright Chromium." >&2
  exit 1
fi

HOST_NAME="127.0.0.1"
PORT="${AMBER_VERIFY_PORT:-18080}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "AMBER_VERIFY_PORT must be an integer, got: $PORT" >&2
  exit 1
fi
if [[ "$PORT" -eq 8080 ]]; then
  echo "Refuse to launch verification on port 8080 (user archive). Unset AMBER_VERIFY_PORT or pick another port." >&2
  exit 1
fi

pid_alive() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

http_ready() {
  local url="$1"
  local body
  body="$(curl -sS --max-time 3 "$url/" 2>/dev/null || true)"
  [[ "$body" == *"time capsule"* ]]
}

port_listening() {
  local host="$1"
  local port="$2"
  "$VENV_PY" - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket()
s.settimeout(0.4)
try:
    s.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

if [[ -f "$INSTANCE_PATH" ]]; then
  existing="$("$VENV_PY" - "$INSTANCE_PATH" <<'PY'
import json, sys
inst = json.load(open(sys.argv[1], encoding="utf-8"))
print(inst.get("pid") or "")
print(inst.get("url") or "")
PY
)"
  existing_pid="$(printf '%s\n' "$existing" | sed -n '1p')"
  existing_url="$(printf '%s\n' "$existing" | sed -n '2p')"
  alive=0
  ready=0
  if pid_alive "$existing_pid"; then alive=1; fi
  if [[ -n "$existing_url" ]] && http_ready "$existing_url"; then ready=1; fi
  if [[ "$alive" -eq 1 && "$ready" -eq 1 ]]; then
    echo "Amber verify instance already running at $existing_url (pid $existing_pid)"
    echo "Amber verify instance ready at $existing_url"
    exit 0
  fi
  echo "Stale instance.json (alive=$alive ready=$ready). Starting a new process."
fi

mkdir -p "$DATA_DIR"
rm -f "$LOG_OUT" "$LOG_ERR"

if port_listening "$HOST_NAME" "$PORT"; then
  echo "Port $PORT is already in use and is not a healthy verify instance. Pick AMBER_VERIFY_PORT or run cleanup.sh if you started that process." >&2
  exit 1
fi

export AMBER_DATA_DIR="$DATA_DIR"
export AMBER_HOST="$HOST_NAME"
export AMBER_PORT="$PORT"
unset AMBER_ALLOW_PRIVATE || true

(
  cd "$REPO_ROOT"
  nohup "$VENV_PY" -m uvicorn app.main:app --host "$HOST_NAME" --port "$PORT" \
    >"$LOG_OUT" 2>"$LOG_ERR" &
  echo $! >"$SCRATCH_DIR/.launch.pid"
)
PROC_PID="$(cat "$SCRATCH_DIR/.launch.pid")"
rm -f "$SCRATCH_DIR/.launch.pid"
URL="http://${HOST_NAME}:${PORT}"

deadline=$((SECONDS + 30))
ready=0
while (( SECONDS < deadline )); do
  if ! pid_alive "$PROC_PID"; then
    err=""
    [[ -f "$LOG_ERR" ]] && err="$(cat "$LOG_ERR")"
    echo "uvicorn exited during launch." >&2
    echo "$err" >&2
    exit 1
  fi
  if http_ready "$URL"; then
    ready=1
    break
  fi
  sleep 0.3
done

if [[ "$ready" -ne 1 ]]; then
  kill "$PROC_PID" 2>/dev/null || true
  err=""
  [[ -f "$LOG_ERR" ]] && err="$(cat "$LOG_ERR")"
  echo "Timed out waiting for $URL." >&2
  echo "$err" >&2
  exit 1
fi

"$VENV_PY" - "$INSTANCE_PATH" "$PROC_PID" "$HOST_NAME" "$PORT" "$URL" "$DATA_DIR" "$REPO_ROOT" <<'PY'
import json, sys
from datetime import datetime, timezone
path, pid, host, port, url, data_dir, repo = sys.argv[1:]
payload = {
    "pid": int(pid),
    "host": host,
    "port": int(port),
    "url": url,
    "data_dir": data_dir,
    "repo": repo,
    "started": datetime.now(timezone.utc).isoformat(),
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY

echo "Amber verify instance ready at $URL"
echo "pid=$PROC_PID data_dir=$DATA_DIR"
