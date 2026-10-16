#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HTTP_PORT="${NPFLOW_WEB_PORT:-18088}"
HTTPS_PORT="${NPFLOW_WEB_HTTPS_PORT:-18443}"

pids="$(python3 - "$APP_DIR" <<'PY'
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
if [[ -n "$pids" ]]; then
  echo "NPFlow web process:"
  ps -o pid,ppid,pgid,stat,pcpu,pmem,etime,cmd -p "$(printf "%s" "$pids" | paste -sd, -)"
else
  echo "NPFlow web process: not running"
fi

echo
echo "Listening sockets:"
ss -ltnp 2>/dev/null | grep -E ":(${HTTP_PORT}|${HTTPS_PORT})" || true

echo
echo "Health checks:"
curl -fsS --connect-timeout 3 "http://127.0.0.1:${HTTP_PORT}/health" || true
echo
curl -k -fsS --connect-timeout 3 "https://127.0.0.1:${HTTPS_PORT}/health" || true
echo
