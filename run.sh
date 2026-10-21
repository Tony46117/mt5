#!/usr/bin/env bash

set -uo pipefail
cd "$(dirname "$0")"

OS_UNAME="$(uname -s 2>/dev/null || echo Windows)"
RT=""
if [ "$OS_UNAME" != "Linux" ] && [ "$OS_UNAME" != "Darwin" ]; then
  echo "detected OS: $OS_UNAME - the wine/MT5 stack runs in a Linux container."
  command -v docker >/dev/null 2>&1 && RT=docker
  [ -z "$RT" ] && command -v podman >/dev/null 2>&1 && RT=podman
  [ -z "$RT" ] && echo "ERROR: Docker Desktop (or Podman in WSL) is required on $OS_UNAME - https://www.docker.com/products/docker-desktop" && exit 1
else

  if ! command -v wine >/dev/null 2>&1 && ! command -v wine64 >/dev/null 2>&1 \
     && [ "${MT5_RUN_ENGINE:-}" != native ]; then
    if [ -n "${MT5_RUN_ENGINE:-}" ]; then RT="$MT5_RUN_ENGINE"
    elif command -v podman >/dev/null 2>&1; then RT=podman
    elif command -v docker >/dev/null 2>&1; then RT=docker
    fi
    if [ -n "$RT" ] && ! command -v "$RT" >/dev/null 2>&1; then
      echo "ERROR: $RT was requested (MT5_RUN_ENGINE) but is not installed."
      exit 1
    fi
    [ -n "$RT" ] && echo "wine not found - using the $RT container stack."
  fi
fi

if [ -z "$RT" ] && ! command -v wine >/dev/null 2>&1 \
   && ! command -v wine64 >/dev/null 2>&1 \
   && [ "${MT5_RUN_ENGINE:-}" != native ]; then
  echo "ERROR: neither Wine nor Docker/Podman was found on this computer."
  echo "  easiest:  install Docker, then run:  docker compose up --build"
  echo "  native:   run  bash install.sh  first (installs Wine + MT5 + venv)"
  exit 1
fi

if [ -n "$RT" ]; then
  COMPOSE_FILE=docker-compose.yml
  mkdir -p data
  if $RT compose version >/dev/null 2>&1; then
    echo "starting via $RT compose (web panel :8000)..."
    exec $RT compose -f "$COMPOSE_FILE" up --build
  fi
  echo "building image with $RT..."
  $RT build -t mt5-bridge:latest . || exit 1
  RUN_IPC=""; RUN_SELINUX=""
  if [ "$RT" = podman ]; then

    RUN_IPC="--ipc=host"
    RUN_SELINUX=":Z"
  fi
  if [ -f "$(pwd)/.env" ]; then
    set -a; . "$(pwd)/.env"; set +a
  fi
  exec $RT run --rm -it -p 8000:8000 \
    $RUN_IPC \
    -v "mt5-data:/data${RUN_SELINUX}" \
    -v "$(pwd)/data:/data/seed:ro${RUN_SELINUX}" \
    -e MT5_MACHINE_KEY="${MT5_MACHINE_KEY:-please-change-me}" \
    mt5-bridge:latest all
fi

PY="$HOME/python312/bin/python"
[ -x "$PY" ] || PY="$(command -v python3.12 || command -v python3)"
APP_LOG="app_run.log"

HERE="$(cd "$(dirname "$0")" && pwd)"

# Startup timing: every phase prints how far into the boot it happened, so a
# slow start is visible instead of a blank terminal.  "Lightning fast" means
# the panel answers in about a second and the MT5 terminals are only touched
# when they are NOT already live (see terminals_warm below).
T0_NS="$(date +%s%N)"
elapsed() { awk -v a="$T0_NS" -v b="$(date +%s%N)" 'BEGIN{printf "%.1f", (b-a)/1e9}'; }
note() { printf '  %5ss  %s\n' "$(elapsed)" "$*"; }

ensure_python() {
  # Native runs want a working venv with flask+waitress.  install.sh
  # builds ~/python312, but a fresh checkout on another computer usually
  # has neither - so build (or repair) the venv right here instead of
  # failing with an import error in the log.
  local venv="$HOME/python312" base
  if [ -x "$venv/bin/python" ]; then
    PY="$venv/bin/python"
  else
    for base in python3.12 python3.11 python3.10 python3; do
      command -v "$base" >/dev/null 2>&1 && break
      base=""
    done
    if [ -z "$base" ]; then
      echo "ERROR: no python3 found - install Python 3.10+ (or use Docker)."
      exit 1
    fi
    echo "creating python venv at $venv (one-time, using $base)..."
    if ! "$base" -m venv "$venv" 2>/dev/null; then
      echo "ERROR: could not create a venv with $base."
      echo "  Debian/Ubuntu:  sudo apt-get install python3-venv"
      echo "  Fedora/RHEL:    sudo dnf install python3"
      exit 1
    fi
    PY="$venv/bin/python"
  fi
  # ONE import check: the old code ran the same interpreter twice (and the
  # whole venv build path twice on a fresh box) before it ever started the app.
  if ! "$PY" -c "import flask, waitress" >/dev/null 2>&1; then
    echo "installing python requirements into $venv ..."
    "$PY" -m pip install --disable-pip-version-check --prefer-binary \
      -r "$HERE/requirements.txt" >/dev/null 2>&1 \
      || "$PY" -m pip install --disable-pip-version-check --prefer-binary \
           flask waitress psycopg2-binary
    if ! "$PY" -c "import flask, waitress" >/dev/null 2>&1; then
      echo "ERROR: python dependencies missing for $PY."
      echo "  fix:  $PY -m pip install -r $HERE/requirements.txt"
      exit 1
    fi
  fi
}

kill_ours() {
  pkill -"$1" -u "$(id -u)" -f "$HERE/$2" 2>/dev/null
}

# factory.py may have been started by hand from this directory ("python3
# factory.py"), so its argv is not the absolute $HERE path.  Match both
# the absolute and relative forms without catching unrelated *factory.py
# names such as notfactory.py.
kill_factory() {
  pkill -"$1" -u "$(id -u)" -f "factory\.py" 2>/dev/null
}

# Both terminals already live (process up, EA feed fresh, reporting the
# stored login)?  Then booting them again is pure downtime: a cold wine
# start is 40-90 s per terminal, and the panel/EA were fine without it.
# The bridge adopts the running terminals instead (algo trading stays ON).
terminals_warm() {
  "$PY" - <<'EOF' >/dev/null 2>&1
import sys
from spot import (term_running, feed_age, read_accounts, read_header,
                  pick_terminal)
# Same 5 s freshness the bridge's own adopt check uses (bridge._adoptable),
# so "warm here" can never disagree with "adopted there".
for inst in (1, 2):
    login = str(read_accounts().get(inst, {}).get("login", "")).strip()
    if not login or login in ("0", "?"):
        sys.exit(1)
    if not term_running(inst, max_age=0):
        sys.exit(1)
    if feed_age(inst) >= 5.0:
        sys.exit(1)
    term = pick_terminal(login)
    if not term:
        sys.exit(1)
    head = read_header(term["trades_path"])
    if str(head.get("login", "")) != login:
        sys.exit(1)
sys.exit(0)
EOF
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

}

purge_exec_files() {

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
  echo "stopping bridge / app / factory / terminals..."
  kill_ours TERM bridge.py
  kill_ours TERM app.py
  kill_factory TERM
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
  kill_factory TERM
  sleep 1
  [ -n "${APP_PID:-}" ]    && kill -9 "$APP_PID"    2>/dev/null
  [ -n "${BRIDGE_PID:-}" ] && kill -9 "$BRIDGE_PID" 2>/dev/null
  kill_factory KILL
  if [ "${MT5_RUN_KEEP_TERMINALS:-0}" != "1" ]; then
    stop_terminals
  else
    echo "(terminals left running - MT5_RUN_KEEP_TERMINALS=1)"
  fi
  echo "stopped.  web app log: $APP_LOG"
}
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 0' EXIT

kill_ours TERM bridge.py
kill_ours TERM app.py
kill_factory TERM
# give a dying instance a moment to release :8000 / the exec file channel
# (bounded - normally it is already gone before the first check)
for _ in $(seq 1 40); do
  pgrep -f "$HERE/(bridge|app)\\.py" >/dev/null 2>&1 || break
  sleep 0.05
done
if terminals_warm; then
  note "terminals already live - adopted (ALGO ON kept, no reboot)"
else
  note "stopping stale MT5 terminals for a clean boot..."
  stop_terminals
  purge_exec_files
fi

for pid in $(pgrep -f "$HERE/(bridge|app)\\.py" 2>/dev/null); do
  owner=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$owner" ] && [ "$owner" != "$(id -un)" ]; then
    echo "ERROR: an instance is already running as user '$owner' (pid $pid)."
    echo "  Kill it first (e.g. close that terminal window, or: sudo kill $pid)."
    echo "  Running this script with sudo is NOT supported - run it as yourself."
    exit 1
  fi
done

if curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "ERROR: something is already serving on port 8000."
  echo "  Find it with:  ss -tlnp | grep 8000"
  echo "  Then stop it (or change web_port in config.py) and run again."
  exit 1
fi

ensure_python

echo "starting web app on :8000 (log: $APP_LOG, production WSGI)..."
: > "$APP_LOG"
"$PY" -u "$HERE/app.py" --production >>"$APP_LOG" 2>&1 &
APP_PID=$!

# The bridge does not need the web app, so both start at once: a cold MT5
# boot (40-90 s) now overlaps the panel's own startup instead of queueing
# behind it.  Ctrl+C stops EVERYTHING, including a hand-started factory.py.
echo "starting bridge supervisor in parallel - adopts live terminals, otherwise"
echo "boots both MT5 terminals into the stored accounts (ALGO ON, one chart each)."
"$PY" -u "$HERE/bridge.py" &
BRIDGE_PID=$!

for _ in $(seq 1 400); do
  curl -s --max-time 0.2 http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  kill -0 "$APP_PID" 2>/dev/null || break
  sleep 0.05
done
if ! kill -0 "$APP_PID" 2>/dev/null; then
  echo "ERROR: the web app exited during startup - last log lines:"
  tail -8 "$APP_LOG"
  exit 1
fi
if curl -s --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  note "web panel ready:  http://127.0.0.1:8000"
else
  note "(web panel not answering yet - see $APP_LOG; app pid $APP_PID is running)"
fi

wait "$BRIDGE_PID"
RC=$?
cleanup
exit "${RC:-0}"
