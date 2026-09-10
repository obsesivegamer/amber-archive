#!/usr/bin/env bash
# Stop the launched verify PID and delete scratch only. Keeps evidence/.
# Never kills by process name. Never touches repo data/ or port 8080.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
SCRATCH_DIR="$SKILL_ROOT/.scratch"
INSTANCE_PATH="$SCRATCH_DIR/instance.json"
EVIDENCE_DIR="$SKILL_ROOT/evidence"

if [[ -f "$INSTANCE_PATH" ]]; then
  PID_VALUE="$(python3 - "$INSTANCE_PATH" <<'PY'
import json, sys
inst = json.load(open(sys.argv[1], encoding="utf-8"))
print(inst.get("pid") or 0)
PY
)"
  if [[ "$PID_VALUE" =~ ^[0-9]+$ && "$PID_VALUE" -gt 0 ]]; then
    if kill -0 "$PID_VALUE" 2>/dev/null; then
      PROC_NAME="$(ps -p "$PID_VALUE" -o comm= 2>/dev/null | tr -d ' ' || echo unknown)"
      echo "Stopping pid $PID_VALUE ($PROC_NAME)"
      # Kill the recorded PID and any remaining children (Playwright), not by name.
      pids="$PID_VALUE"
      children="$(pgrep -P "$PID_VALUE" 2>/dev/null || true)"
      if [[ -n "$children" ]]; then
        pids="$PID_VALUE $children"
      fi
      kill $pids 2>/dev/null || true
      sleep 0.4
      kill -9 $pids 2>/dev/null || true
    else
      echo "pid $PID_VALUE already gone"
    fi
  fi
else
  echo "No instance.json; nothing to kill"
fi

if [[ -d "$SCRATCH_DIR" ]]; then
  rm -rf "$SCRATCH_DIR"
  echo "Removed $SCRATCH_DIR"
fi

if [[ -d "$EVIDENCE_DIR" ]]; then
  echo "Kept evidence at $EVIDENCE_DIR"
else
  echo "No evidence directory yet"
fi

echo "Cleanup done"
