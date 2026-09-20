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

# ---------------------------------------------------------------------------
# OS AUTO-DETECT: `bash run.sh` means the same thing on every OS - start web
# panel + bridge + both MT5 terminals.  Non-Linux (Windows/macOS) and Linux
# boxes without wine both go through a container runtime:
#   * podman  - preferred on Linux (rootless, daemonless, SELinux-native)
#   * docker  - fallback everywhere else (Docker Desktop on Windows/macOS)
# ---------------------------------------------------------------------------
OS_UNAME="$(uname -s 2>/dev/null || echo Windows)"
RT=""
if [ "$OS_UNAME" != "Linux" ] && [ "$OS_UNAME" != "Darwin" ]; then
  echo "detected OS: $OS_UNAME - the wine/MT5 stack runs in a Linux container."
  command -v docker >/dev/null 2>&1 && RT=docker
  [ -z "$RT" ] && command -v podman >/dev/null 2>&1 && RT=podman
  [ -z "$RT" ] && echo "ERROR: Docker Desktop (or Podman in WSL) is required on $OS_UNAME - https://www.docker.com/products/docker-desktop" && exit 1
else
  # native Linux: no wine installed -> containers are the only way to run.
  # If BOTH runtimes exist, prefer podman (rootless, no daemon holding the
  # trading stack); MT5_RUN_ENGINE=docker overrides the pick explicitly.
  if ! command -v wine >/dev/null 2>&1 && ! command -v wine64 >/dev/null 2>&1 \
     && [ "${MT5_RUN_ENGINE:-}" != native ]; then
    if [ -n "${MT5_RUN_ENGINE:-}" ]; then RT="$MT5_RUN_ENGINE"
    elif command -v podman >/dev/null 2>&1; then RT=podman
    elif command -v docker >/dev/null 2>&1; then RT=docker
    fi
    [ -n "$RT" ] && echo "wine not found - using the $RT container stack."
  fi
fi

if [ -n "$RT" ]; then
  COMPOSE_FILE=docker-compose.yml
  if $RT compose version >/dev/null 2>&1; then
    echo "starting via $RT compose (web panel :8000)..."
    exec $RT compose -f "$COMPOSE_FILE" up --build
  fi
  echo "building image with $RT..."
  $RT build -t mt5-bridge:latest . || exit 1
  exec $RT run --rm -it -p 8000:8000 \
    -v mt5-data:/data -v "$(pwd)/data:/data/seed:ro" \
    -e MT5_MACHINE_KEY="${MT5_MACHINE_KEY:-please-change-me}" \
    mt5-bridge:latest all
fi

PY="$HOME/python312/bin/python"
[ -x "$PY" ] || PY="$(command -v python3.12 || command -v python3)"
APP_LOG="app_run.log"

# Everything below targets ONLY this checkout.  The previous version ran
# `pkill -f app.py` / `pkill -f bridge.py` / `pkill -9 -f terminal64.exe`,
# which matched any process anywhere on the machine with those strings in
# its command line - including the sibling mt5_v2 project that shares this
# wine prefix (see config.py), and any unrelated `app.py` (or a shell
# whose command line merely MENTIONED those strings - it killed its own
# invoker).
HERE="$(cd "$(dirname "$0")" && pwd)"

# pkill restricted to this user AND to command lines rooted in this dir
kill_ours() {  # kill_ours <signal> <script name>
  pkill -"$1" -u "$(id -u)" -f "$HERE/$2" 2>/dev/null
}

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
  # spot.stop_terminal() targets each terminal by its own install path; a
  # blanket `pkill -9 -f terminal64.exe` would also kill MT5 terminals this
  # stack does not own, so it is deliberately NOT done here.
}

purge_exec_files() {
  # leftover exec_in/exec_next command files from a previous run must be gone
  # BEFORE the terminals boot: a dying terminal leaves queued orders behind
  # and the next boot's EA would execute them as phantom trades (the executor
  # janitor also sweeps, but only after 15 s + a grace period).
  "$PY" - <<'EOF' 2>/dev/null
from pathlib import Path
import os
base = Path.home() / ".mt5" / "drive_c" / "Program Files"
for term in ("MetaTrader 5", "MetaTrader 5-2"):
    d = base / term / "MQL5" / "Files"
    if not d.is_dir():
        continue
    for pat in ("exec_in.*.txt", "exec_next*.txt", "exec_next*.tmp"):
        for p in d.glob(pat):
            try:
                p.unlink()
            except OSError:
                pass
EOF
}

if [ "${1:-}" = "stop" ]; then
  echo "stopping bridge / app / terminals..."
  kill_ours TERM bridge.py
  kill_ours TERM app.py
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

# leftovers of a previous run must not hold :8000, double-supervise, or
# race the boot (a terminal still exiting when the bridge launches its own
# copy double-boots MT5 and the EA can end up detached - observed live)
kill_ours TERM bridge.py
kill_ours TERM app.py
stop_terminals
purge_exec_files
sleep 1

# a stack started by ANOTHER USER (e.g. `sudo bash run.sh` in some window)
# cannot be killed from here and will silently fight this one for the port
# and the terminals - refuse to start instead of half-working.
for pid in $(pgrep -f "$HERE/(bridge|app)\\.py" 2>/dev/null); do
  owner=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$owner" ] && [ "$owner" != "$(id -un)" ]; then
    echo "ERROR: an instance is already running as user '$owner' (pid $pid)."
    echo "  Kill it first (e.g. close that terminal window, or: sudo kill $pid)."
    echo "  Running this script with sudo is NOT supported - run it as yourself."
    exit 1
  fi
done

# port 8000 must be free BEFORE we start our own app on it
if curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "ERROR: something is already serving on port 8000."
  echo "  Find it with:  ss -tlnp | grep 8000"
  echo "  Then stop it (or change web_port in config.py) and run again."
  exit 1
fi

echo "starting web app on :8000 (log: $APP_LOG)..."
: > "$APP_LOG"
"$PY" -u "$HERE/app.py" >>"$APP_LOG" 2>&1 &
APP_PID=$!

for _ in $(seq 1 30); do
  curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 0.5
done
if ! kill -0 "$APP_PID" 2>/dev/null; then
  echo "ERROR: the web app exited during startup - last log lines:"
  tail -8 "$APP_LOG"
  exit 1
fi
if curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "  web panel ready:  http://127.0.0.1:8000"
else
  echo "  (web panel not answering yet - see $APP_LOG; app pid $APP_PID is running)"
fi

echo "starting bridge supervisor - boots both MT5 terminals into the stored"
echo "accounts (ALGO ON, one EURUSD chart each).  Ctrl+C stops EVERYTHING."
"$PY" -u "$HERE/bridge.py" &
BRIDGE_PID=$!

wait "$BRIDGE_PID"
RC=$?
cleanup
exit "${RC:-0}"
