#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${NPFLOW_WEB_PORT:-18088}"
LOG_FILE="$APP_DIR/webapp.log"

if curl -fsS --connect-timeout 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  exit 0
fi

{
  printf '[%s] NPFlow web health check failed; starting service\n' "$(date -Is)"
  cd "$APP_DIR"
  ./start_web.sh
} >> "$LOG_FILE" 2>&1
