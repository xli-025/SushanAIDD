#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${NPFLOW_WEB_PORT:-18088}"
BIND_HOST="${NPFLOW_HTTP_BIND_HOST:-127.0.0.1}"
PID_FILE="$APP_DIR/webapp.pid"
LOG_FILE="$APP_DIR/webapp.log"

find_existing_pid() {
  python3 - "$APP_DIR" <<'PY'
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
        break
PY
}

if curl -fsS --connect-timeout 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  existing="$(find_existing_pid)"
  if [[ -n "${existing}" ]]; then
    echo "${existing}" > "$PID_FILE"
    echo "NPFlow web is already listening on ${BIND_HOST}:${PORT} (pid ${existing})."
  else
    echo "NPFlow web health check is OK on local port ${PORT}."
  fi
  exit 0
fi

cd "$APP_DIR"
nohup ./run_web.sh >> "$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" > "$PID_FILE"
sleep 2

if ! kill -0 "$pid" >/dev/null 2>&1; then
  echo "NPFlow web failed to start. See $LOG_FILE" >&2
  exit 1
fi

curl -fsS --connect-timeout 5 "http://127.0.0.1:${PORT}/health" >/dev/null
echo "NPFlow web started; HTTP health listener is ${BIND_HOST}:${PORT} (pid ${pid})."
