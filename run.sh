#!/usr/bin/env bash
# run.sh - run the WHOLE MT5 bridge software with one command.
#
#   bash run.sh                       # web app (:8000) + bridge dashboard
#   Ctrl+C                            # stops bridge, app AND the MT5 terminals
#   MT5_RUN_KEEP_TERMINALS=1 bash run.sh   # keep terminals running on exit
#   bash run.sh stop                  # kill every leftover process of the suite
#
# The bridge screen is the live dashboard; the web panel is the trading UI.
# Optional: `~/python312/bin/python monitor.py` in another window shows the
# live position book of both accounts.

set -uo pipefail
cd "$(dirname "$0")"

PY="$HOME/python312/bin/python"
[ -x "$PY" ] || PY="$(command -v python3.12 || command -v python3)"
APP_LOG="app_run.log"

stop_terminals() {
  "$PY" - <<'EOF' 2>/dev/null
try:
    from spot import stop_terminal
    for i in (1, 2):
        try:
            stop_terminal(i)
        except Exception:
            pass
except Exception:
    pass
EOF
  pkill -9 -f "terminal64.exe" 2>/dev/null
}

if [ "${1:-}" = "stop" ]; then
  echo "stopping bridge / app / probes / terminals..."
  pkill -f "bridge.py" 2>/dev/null
  pkill -f "app.py" 2>/dev/null
  pkill -f "burst_probe|minute_probe|probe_schedules" 2>/dev/null
  stop_terminals
  echo "all stopped."
  exit 0
fi

CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  echo
  echo "shutting down..."
  [ -n "${APP_PID:-}" ]    && kill "$APP_PID"    2>/dev/null
  [ -n "${BRIDGE_PID:-}" ] && kill "$BRIDGE_PID" 2>/dev/null
  sleep 1
  [ -n "${APP_PID:-}" ]    && kill -9 "$APP_PID"    2>/dev/null
  [ -n "${BRIDGE_PID:-}" ] && kill -9 "$BRIDGE_PID" 2>/dev/null
  if [ "${MT5_RUN_KEEP_TERMINALS:-0}" != "1" ]; then
    stop_terminals
  else
    echo "(terminals left running - MT5_RUN_KEEP_TERMINALS=1)"
  fi
  echo "stopped.  web app log: $APP_LOG"
}
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# leftovers of a previous run must not hold :8000 or double-supervise
pkill -f "bridge.py" 2>/dev/null
pkill -f "app.py" 2>/dev/null
sleep 1

echo "starting web app on :8000 (log: $APP_LOG)..."
: > "$APP_LOG"
"$PY" -u app.py >>"$APP_LOG" 2>&1 &
APP_PID=$!

for _ in $(seq 1 30); do
  curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "  web panel ready:  http://127.0.0.1:8000"
else
  echo "  (web panel not answering yet - see $APP_LOG)"
fi

echo "starting bridge supervisor - boots both MT5 terminals into the stored"
echo "accounts (ALGO ON, one EURUSD chart each).  Ctrl+C stops EVERYTHING."
"$PY" -u bridge.py &
BRIDGE_PID=$!

wait "$BRIDGE_PID"
RC=$?
cleanup
exit "${RC:-0}"
