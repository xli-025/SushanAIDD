#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/webapp.pid"

pids=""
if [[ -s "$PID_FILE" ]]; then
  pids="$(cat "$PID_FILE")"
fi

found="$(python3 - "$APP_DIR" <<'PY'
import os, sys
app = os.path.realpath(sys.argv[1])
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        if os.stat(f"/proc/{name}").st_uid != os.getuid():
            continue
        cwd = os.path.realpath(os.readlink(f"/proc/{name}/cwd"))
        cmd = open(f"/proc/{name}/cmdline", "rb").read().decode("utf-8", "ignore")
    except OSError:
        continue
    if cwd == app and "server.py" in cmd:
        print(name)
PY
)"
if [[ -n "$found" ]]; then
  pids="${pids}"$'\n'"${found}"
fi

pids="$(printf "%s\n" "$pids" | awk 'NF && !seen[$1]++')"
if [[ -z "$pids" ]]; then
  echo "NPFlow web is not running."
  rm -f "$PID_FILE"
  exit 0
fi

while read -r pid; do
  [[ -z "$pid" ]] && continue
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" || true
  fi
done <<< "$pids"

sleep 2

while read -r pid; do
  [[ -z "$pid" ]] && continue
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -9 "$pid" || true
  fi
done <<< "$pids"

rm -f "$PID_FILE"
echo "NPFlow web stopped."
