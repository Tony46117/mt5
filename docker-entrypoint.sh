#!/usr/bin/env bash

set -uo pipefail

DATA=/data
PREFIX="$DATA/prefix"
LOG=/dev/null
export WINEPREFIX="$PREFIX" WINEDEBUG=-all DISPLAY="${DISPLAY:-:99}"

export MT5_UPDATE_SKIP=1

b="\033[1m"; g="\033[92m"; y="\033[93m"; r="\033[91m"; n="\033[0m"
step() { echo -e "${b}==> ${n}$*"; }
ok()   { echo -e "  ${g}OK${n}  $*"; }
warn() { echo -e "  ${y}WARN${n}  $*"; }
die()  { echo -e "${r}ERROR:${n} $*" >&2; exit 1; }

if [ "$(id -u)" = "0" ]; then
  die "run this container as the built-in 'mt5' user (see README) - root breaks wine perms"
fi

detect_os_env() {
  local os_env="docker"
  [ -e /run/.containerenv ] && os_env="podman"
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

init_data() {
  mkdir -p "$PREFIX" "$DATA/db" "$DATA/logs"

  ln -sfn "$DATA/db/trades.db" /app/trades.db

  if [ -f "$DATA/seed/acc.env" ] && [ ! -f /app/acc.env ]; then
    cp "$DATA/seed/acc.env" /app/acc.env
    chmod 600 /app/acc.env
    ok "acc.env found in ./data - will auto-seed the session"
  fi
  if [ ! -f "$PREFIX/system.reg" ]; then
    if command -v wineboot >/dev/null 2>&1; then
      step "first boot: initialising wineprefix (one-time, ~1 min)"
      timeout 200 wineboot -i >/dev/null 2>&1 || timeout 60 wineboot >/dev/null 2>&1 || true
      timeout 60 wineserver -w 2>/dev/null || true
      [ -f "$PREFIX/system.reg" ] && ok "wineprefix initialised at $PREFIX" \
        || warn "wineprefix not fully ready - it will heal on next start"
    elif [ -d /mt5/prefix/drive_c ]; then

      step "first boot: wineboot unavailable (no userns) - using build-time prefix"

      (cd /mt5/prefix && tar cf - --exclude=./wineserver .) | tar xf - -C "$PREFIX/"
      ok "wineprefix restored from image"
    else
      warn "no wineboot and no baked prefix - terminals cannot start"
    fi
  fi

  local mt1="$PREFIX/drive_c/Program Files/MetaTrader 5"
  local mt2="$PREFIX/drive_c/Program Files/MetaTrader 5-2"
  if [ ! -f "$mt1/terminal64.exe" ]; then
    step "first boot: seeding MetaTrader 5 from image"
    mkdir -p "$(dirname "$mt1")"
    (cd /mt5/master && tar cf - . 2>/dev/null) | tar xf - -C "$mt1/" 2>/dev/null || true
  fi
  if [ ! -f "$mt1/terminal64.exe" ]; then

    if [ -d "$DATA/seed/mt5-master" ] && [ -f "$DATA/seed/mt5-master/terminal64.exe" ]; then
      step "seeding MetaTrader 5 from host-provided ./data/mt5-master"
      cp -a "$DATA/seed/mt5-master/." "$mt1/"
    fi
  fi
  if [ ! -f "$mt1/terminal64.exe" ]; then
    step "MT5 not in image - downloading + extracting at runtime (6 min cap)"
    curl -fL --retry 3 --retry-all-errors -o "$DATA/mt5setup.exe" \
      https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe \
      && { 7z x -y -o"$mt1" "$DATA/mt5setup.exe" >/dev/null 2>&1 || true; }
    if [ ! -f "$mt1/terminal64.exe" ] && command -v wine64 >/dev/null 2>&1; then

      timeout "${MT5_RUNTIME_INSTALL_CAP:-300}" xvfb-run -a \
        wine64 "$DATA/mt5setup.exe" /auto >/dev/null 2>&1 || true
      timeout 60 wineserver -w 2>/dev/null || true
    fi
    rm -f "$DATA/mt5setup.exe"
  fi
  if [ -f "$mt1/terminal64.exe" ]; then

    touch "$mt1/.update"
    ok "terminal 1 ready at $mt1"
  else
    warn "terminal 1 has no terminal64.exe - bridge will report it unhealthy"
  fi
  if [ ! -f "$mt2/terminal64.exe" ] && [ -f "$mt1/terminal64.exe" ]; then
    step "first boot: creating terminal 2 (copy of terminal 1)"
    mkdir -p "$mt2"
    (cd "$mt1" && tar cf - .) | tar xf - -C "$mt2/" 2>/dev/null \
      || warn "terminal 2 copy failed - re-run later"
    touch "$mt2/.update" 2>/dev/null
  fi

  for t in "$mt1" "$mt2"; do
    [ -d "$t" ] || continue
    mkdir -p "$t/MQL5/Experts"
    [ -f "$t/MQL5/Experts/SpotDump.ex5" ] || cp /mt5/SpotDump.ex5 "$t/MQL5/Experts/" 2>/dev/null || true
  done
}

seed_session() {

  if python3 -c 'import sys; sys.path.insert(0,"/app"); import session; sys.exit(0 if session.load() else 1)' 2>/dev/null; then
    ok "trading session loaded"
  else
    warn "no session: log in via the web panel, or exec the container and run: python session.py --seed"
  fi

  if [ "${MT5_AUTO_LOGIN:-1}" = "1" ]; then
    export MT5_NO_LOGIN_PROMPT=1
    ok "auto-login enabled - booting into the stored accounts"
  else
    warn "auto-login disabled (MT5_AUTO_LOGIN=0) - the bridge will ask for logins"
  fi
}

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

run_bridges() { run_bridge; }

main() {
  case "${1:-all}" in
    bash|sh) exec bash ;;
    *) ;;
  esac
  detect_os_env
  start_display
  init_data
  cd /app
  seed_session
  case "${1:-all}" in
    app)    run_app; sleep 2; run_bridges ;;
    bridge) run_bridge ;;
    all)    run_app; sleep 2; run_bridges ;;
    *) exec "$@" ;;
  esac
  echo -e "${b}web panel: http://localhost:8000  (logs in /data/logs)${n}"
  wait -n
  cleanup
}

main "$@"
