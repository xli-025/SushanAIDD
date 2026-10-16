#!/usr/bin/env bash
set -euo pipefail

export NPFLOW_ROOT="${NPFLOW_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$NPFLOW_ROOT/.venv/bin:$PATH"
export NPFLOW_WEB_PORT="${NPFLOW_WEB_PORT:-18088}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-/data/ollama}"

cd "$(dirname "$0")"
exec "$NPFLOW_ROOT/.venv/bin/python" server.py
