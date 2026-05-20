#!/usr/bin/env bash
# start.sh — launch hermes-agent gateway + dashboard + workspace UI
# Usage: ./start.sh        — start all services
#        ./start.sh -k     — kill all running services
#        ./start.sh stop   — same as -k
set -euo pipefail

HERMES="$HOME/.local/bin/hermes"
WORKSPACE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.hermes/logs"
HERMES_ENV="$HOME/.hermes/.env"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
mkdir -p "$LOG_DIR"

stop_all() {
  echo "Stopping hermes services..."
  pkill -f "hermes gateway run"  2>/dev/null || true
  pkill -f "hermes dashboard"    2>/dev/null || true
  pkill -f "vite"                2>/dev/null || true
  pkill -f "hermes-workspace"    2>/dev/null || true
  echo "Done."
  exit 0
}

[[ "${1:-}" == "stop" || "${1:-}" == "-k" ]] && stop_all

# ── Guard: hai LiteLLM proxy must be running ──────────────────────────────────
echo "Checking hai LiteLLM proxy on :6655..."
if ! curl -fsS http://localhost:6655/ > /dev/null 2>&1; then
  echo ""
  echo "  ✗ hai LiteLLM proxy is not running."
  echo "  Start it first, then re-run this script:"
  echo ""
  echo "    hai proxy start"
  echo ""
  exit 1
fi
echo "  LiteLLM proxy running ✓"

# ── Snapshot workspace custom files into ~/.hermes/runtime/ and push ──────────
echo "Archiving workspace to ~/.hermes/runtime/..."
RUNTIME_DIR="$HOME/.hermes/runtime"
mkdir -p "$RUNTIME_DIR"
cp "$WORKSPACE_DIR/start.sh"            "$RUNTIME_DIR/start.sh"
cp "$WORKSPACE_DIR/pnpm-workspace.yaml" "$RUNTIME_DIR/pnpm-workspace.yaml"
cp "$WORKSPACE_DIR/.npmrc"              "$RUNTIME_DIR/.npmrc"
[[ -f "$WORKSPACE_DIR/.env" ]] && cp "$WORKSPACE_DIR/.env" "$RUNTIME_DIR/workspace.env"
[[ -d "$WORKSPACE_DIR/hermes-config" ]] && cp -r "$WORKSPACE_DIR/hermes-config" "$RUNTIME_DIR/hermes-config"
(
  cd "$HOME/.hermes"
  git add -A
  if ! git diff --cached --quiet; then
    git commit -m "snapshot: $(date '+%Y-%m-%d %H:%M')"
    git push origin main 2>/dev/null || true
  fi
) 2>/dev/null || true
echo "  Archive synced ✓"

# ── Sync current hai API key into hermes .env ─────────────────────────────────
echo "Syncing hai API key..."
hai configure claude-code > /dev/null 2>&1 || true
CURRENT_KEY=$(python3 -c "
import json, sys
try:
    d = json.load(open('$CLAUDE_SETTINGS'))
    print(d['env'].get('ANTHROPIC_AUTH_TOKEN', ''))
except Exception:
    print('')
" 2>/dev/null)
if [[ -n "$CURRENT_KEY" ]]; then
  python3 -c "
import re
path = '$HERMES_ENV'
with open(path) as f:
    content = f.read()
updated = re.sub(r'^OPENAI_API_KEY=.*$', 'OPENAI_API_KEY=$CURRENT_KEY', content, flags=re.MULTILINE)
with open(path, 'w') as f:
    f.write(updated)
"
  echo "  API key synced ✓"
else
  echo "  Warning: could not read API key from $CLAUDE_SETTINGS — using existing key"
fi

# ── Kill any existing instances before starting fresh ─────────────────────
echo "Cleaning up any existing instances..."
pkill -f "hermes gateway run" 2>/dev/null || true
pkill -f "hermes dashboard"   2>/dev/null || true
pkill -f "vite dev"           2>/dev/null || true
# Brief pause to let ports free up
sleep 2

# ── Guard: hermes must be installed ───────────────────────────────────────
if [[ ! -x "$HERMES" ]]; then
  echo "Error: hermes not found at $HERMES"
  echo "Run the Nous installer first:"
  echo "  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
  exit 1
fi

# ── Start gateway (non-blocking, logs to file) ────────────────────────────
echo "Starting hermes gateway on :8642..."
"$HERMES" gateway run --replace > "$LOG_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!

# Wait for gateway health check
for i in {1..45}; do
  if curl -fsS http://127.0.0.1:8642/health > /dev/null 2>&1; then
    echo "  Gateway healthy ✓"
    break
  fi
  [[ $i -eq 45 ]] && { echo "Gateway did not start in time. Check $LOG_DIR/gateway.log"; exit 1; }
  sleep 1
done

# ── Start dashboard ────────────────────────────────────────────────────────
echo "Starting hermes dashboard on :9119..."
"$HERMES" dashboard > "$LOG_DIR/dashboard.log" 2>&1 &
DASHBOARD_PID=$!

for i in {1..30}; do
  if curl -fsS http://127.0.0.1:9119/api/status > /dev/null 2>&1; then
    echo "  Dashboard healthy ✓"
    break
  fi
  [[ $i -eq 30 ]] && { echo "Dashboard did not start in time. Check $LOG_DIR/dashboard.log"; exit 1; }
  sleep 1
done

# ── Start workspace UI (foreground — Ctrl-C stops everything) ─────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Hermes Workspace starting at http://localhost:1972"
echo " Gateway:   http://localhost:8642"
echo " Dashboard: http://localhost:9119"
echo " Logs:      $LOG_DIR/"
echo ""
echo " To stop:  ./start.sh stop  OR  Ctrl-C"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$GATEWAY_PID"   2>/dev/null || true
  kill "$DASHBOARD_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

cd "$WORKSPACE_DIR"
PORT=1972 pnpm dev
