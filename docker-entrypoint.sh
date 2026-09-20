#!/usr/bin/env bash
# docker-entrypoint.sh - container bootstrap with OS/display auto-detection.
#
# Auto-detection performed on every start:
#   * OS / init environment  (dockerd, containerd, k8s, WSL, plain docker)
#   * X display availability (X11 forward, Wayland, VNC, or headless Xvfb)
#   * terminal64.exe presence in the volume (first-boot MT5 install + copy)
#
# Usage:
#   docker run ... mt5-bridge           # web app + bridge + terminals (default)
#   docker run ... mt5-bridge app       # web app only
#   docker run ... mt5-bridge bridge    # bridge supervisor only
#   docker run ... mt5-bridge bash      # shell inside the container

set -uo pipefail

DATA=/data
PREFIX="$DATA/prefix"
LOG=/dev/null
export WINEPREFIX="$PREFIX" WINEDEBUG=-all DISPLAY="${DISPLAY:-:99}"

b="\033[1m"; g="\033[92m"; y="\033[93m"; r="\033[91m"; n="\033[0m"
step() { echo -e "${b}==> ${n}$*"; }
ok()   { echo -e "  ${g}OK${n}  $*"; }
warn() { echo -e "  ${y}WARN${n}  $*"; }
die()  { echo -e "${r}ERROR:${n} $*" >&2; exit 1; }

if [ "$(id -u)" = "0" ]; then
  die "run this container as the built-in 'mt5' user (see README) - root breaks wine perms"
fi

# --------------------------------------------------------------------------
# 1. OS / environment auto-detect (informational + WSL special case)
# --------------------------------------------------------------------------
detect_os_env() {
  local os_env="docker"
  if grep -qiE 'microsoft|WSL' /proc/version 2>/dev/null; then
    os_env="wsl2"
  elif [ -e /run/secrets/kubernetes.io ] || [ -n "${KUBERNETES_SERVICE_HOST:-}" ]; then
    os_env="kubernetes"
  elif grep -q containerd /proc/1/cgroup 2>/dev/null; then
    os_env="containerd"
  fi
  ok "detected environment: $os_env ($(uname -m))"
  [ "$os_env" = "wsl2" ] && warn "WSL2: pass --gpus all only for GPU wine; audio disabled"
}

# --------------------------------------------------------------------------
# 2. X display auto-detection: reuse a forwarded/host display when present,
#    otherwise start our own Xvfb on :99.  MT5 is a GUI app - wine needs X.
# --------------------------------------------------------------------------
start_display() {
  if [ -S /tmp/.X11-unix/X"${DISPLAY#:}" ] || xdpyinfo >/dev/null 2>&1; then
    ok "X display ${DISPLAY} detected (X11 forward / host screen) - reusing it"
    return 0
  fi
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    warn "Wayland detected - using XWayland bridge on ${DISPLAY}"
  fi
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
  Xvfb :99 -screen 0 1600x900x24 -nolisten tcp &
  XPID=$!
  for _ in $(seq 1 20); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.25; done
  xdpyinfo >/dev/null 2>&1 || die "Xvfb failed to start on :99"
  ok "headless Xvfb started on :99 (1600x900x24)"
}

# --------------------------------------------------------------------------
# 3. Persistent state + first-boot MT5 install into the volume
# --------------------------------------------------------------------------
init_data() {
  mkdir -p "$PREFIX" "$DATA/db" "$DATA/logs"
  # /app/trades.db is a symlink into the volume so schedules survive rebuilds
  ln -sfn "$DATA/db/trades.db" /app/trades.db
  # acc.env dropped by the user into ./data on the host (mounted read-only
  # at /data/seed) is copied next to the code; session.load() auto-seeds
  # from it when the session store is empty.
  if [ -f "$DATA/seed/acc.env" ] && [ ! -f /app/acc.env ]; then
    cp "$DATA/seed/acc.env" /app/acc.env
    chmod 600 /app/acc.env
    ok "acc.env found in ./data - will auto-seed the session"
  fi
  if [ ! -f "$PREFIX/system.reg" ]; then
    step "first boot: initialising wineprefix (one-time, ~1 min)"
    wineboot -i >/dev/null 2>&1 || wineboot >/dev/null 2>&1
    wineserver -w 2>/dev/null || true
    [ -f "$PREFIX/system.reg" ] && ok "wineprefix initialised at $PREFIX" \
      || warn "wineprefix not fully ready - it will heal on next start"
  fi

  # Terminal 1 = real install dir inside the prefix; Terminal 2 = copy.
  # MT5 self-extracts/updates on first launch, so /mt5/master seed is optional.
  local mt1="$PREFIX/drive_c/Program Files/MetaTrader 5"
  local mt2="$PREFIX/drive_c/Program Files/MetaTrader 5-2"
  if [ ! -f "$mt1/terminal64.exe" ]; then
    step "first boot: seeding MetaTrader 5 from image"
    mkdir -p "$(dirname "$mt1")"
    cp -a /mt5/master/. "$mt1/"
    ok "seeded terminal 1 at $mt1"
  fi
  if [ ! -f "$mt2/terminal64.exe" ] && [ -f "$mt1/terminal64.exe" ]; then
    step "first boot: creating terminal 2 (copy of terminal 1)"
    cp -a "$mt1" "$mt2" 2>/dev/null || warn "terminal 2 copy failed - re-run later"
  fi

  # Pre-compiled EA from the build stage; spot.install_script recompiles
  # only when MetaEditor succeeds, so dropping the .ex5 in is the base case.
  for t in "$mt1" "$mt2"; do
    [ -d "$t" ] || continue
    mkdir -p "$t/MQL5/Experts"
    [ -f "$t/MQL5/Experts/SpotDump.ex5" ] || cp /mt5/SpotDump.ex5 "$t/MQL5/Experts/" 2>/dev/null || true
  done
}

# --------------------------------------------------------------------------
# 4. Session bootstrap: seed the obfuscated store from acc.env on first boot
# --------------------------------------------------------------------------
seed_session() {
  # session.load() auto-seeds from a legacy acc.env when the store is empty
  if python3 -c 'import sys; sys.path.insert(0,"/app"); import session; sys.exit(0 if session.load() else 1)' 2>/dev/null; then
    ok "trading session loaded"
  else
    warn "no session: log in via the web panel, or exec the container and run: python session.py --seed"
  fi
}

# --------------------------------------------------------------------------
# 5. Services
# --------------------------------------------------------------------------
PIDS=()
cleanup() {
  echo; echo "shutting down..."
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
  sleep 1
  for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null; done
  python3 - <<'EOF' 2>/dev/null || true
from spot import stop_terminal
for i in (1, 2):
    try: stop_terminal(i)
    except Exception: pass
EOF
  [ -n "${XPID:-}" ] && kill "$XPID" 2>/dev/null
  echo "stopped."
}
trap 'cleanup; exit 143' INT TERM

run_app()    { python3 -u /app/app.py --production >>"$DATA/logs/app.log" 2>&1 & PIDS+=($!); }
run_bridge() { python3 -u /app/bridge.py        >>"$DATA/logs/bridge.log" 2>&1 & PIDS+=($!); }

main() {
  detect_os_env
  start_display
  init_data
  cd /app
  seed_session
  case "${1:-all}" in
    app)    run_app ;;
    bridge) run_bridge ;;
    all)    run_app; sleep 2; run_bridge ;;
    bash|sh) exec bash ;;
    *) exec "$@" ;;
  esac
  echo -e "${b}web panel: http://localhost:8000  (logs in /data/logs)${n}"
  wait -n
  cleanup
}

main "$@"
