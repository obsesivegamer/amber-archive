#!/usr/bin/env bash
# POST fixtures/article.html to /import on the verify instance; print snapshot_id=
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
INSTANCE_PATH="$SKILL_ROOT/.scratch/instance.json"
FIXTURE="$SKILL_ROOT/fixtures/article.html"

if [[ ! -f "$INSTANCE_PATH" ]]; then
  echo "No instance.json. Run launch.sh first." >&2
  exit 1
fi
if [[ ! -f "$FIXTURE" ]]; then
  echo "Missing fixture $FIXTURE" >&2
  exit 1
fi

URL="$(python3 - "$INSTANCE_PATH" <<'PY'
import json, sys
inst = json.load(open(sys.argv[1], encoding="utf-8"))
print(inst.get("url") or "")
PY
)"
if [[ -z "$URL" ]]; then
  echo "instance.json has no url" >&2
  exit 1
fi
if [[ "$URL" == *":8080"* ]]; then
  echo "Refuse to import into the user archive on port 8080." >&2
  exit 1
fi

TMP_HEADERS="$(mktemp)"
TMP_BODY="$(mktemp)"
trap 'rm -f "$TMP_HEADERS" "$TMP_BODY"' EXIT

set +e
curl -sS -D "$TMP_HEADERS" -o "$TMP_BODY" --max-redirs 0 \
  -F "file=@${FIXTURE};filename=article.html;type=text/html" \
  "$URL/import"
curl_code=$?
set -e
# 47 is CURLE_TOO_MANY_REDIRECTS when --max-redirs 0 hits a 303
if [[ "$curl_code" -ne 0 && "$curl_code" -ne 47 ]]; then
  echo "curl import failed with exit $curl_code" >&2
  exit 1
fi

LOCATION="$(python3 - "$TMP_HEADERS" <<'PY'
import sys
location = ""
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if line.lower().startswith("location:"):
        location = line.split(":", 1)[1].strip()
        break
print(location)
PY
)"
if [[ -z "$LOCATION" ]]; then
  echo "Import did not return a Location header." >&2
  cat "$TMP_HEADERS" >&2
  exit 1
fi
if ! [[ "$LOCATION" =~ ^/[A-Za-z0-9]{5}$ ]]; then
  echo "Unexpected Location: $LOCATION" >&2
  exit 1
fi

SID="${LOCATION#/}"
echo "snapshot_id=$SID"
echo "url=$URL/$SID"
