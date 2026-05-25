#!/usr/bin/env bash
# debug.sh — Run Python scripts or inline commands using the hermes-agent venv.
# Safe to allow without human-in-the-loop for debugging hermes internals.
#
# Usage:
#   ./debug.sh path/to/script.py [args...]
#   ./debug.sh -c "import json; print(json.dumps({'x': 1}))"
#   ./debug.sh debug_truncation.py flutter_dev

set -euo pipefail

HERMES_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="${HERMES_DIR}/hermes-agent/.venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "ERROR: venv python not found at $VENV_PYTHON" >&2
    exit 1
fi

cd "$HERMES_DIR"
PYTHONPATH="${HERMES_DIR}/hermes-agent:${PYTHONPATH:-}" exec "$VENV_PYTHON" "$@"
