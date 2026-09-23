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
# do not let the terminals self-update on boot - that opens a GUI dialog on
# Xvfb and stalls the boot; MT5 is pinned to the version baked at build time
export MT5_UPDATE_SKIP=1

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
  [ -e /run/.containerenv ] && os_env="podman"          # podman/shutdown marker
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
    if command -v wineboot >/dev/null 2>&1; then
      step "first boot: initialising wineprefix (one-time, ~1 min)"
      timeout 200 wineboot -i >/dev/null 2>&1 || timeout 60 wineboot >/dev/null 2>&1 || true
      timeout 60 wineserver -w 2>/dev/null || true
      [ -f "$PREFIX/system.reg" ] && ok "wineprefix initialised at $PREFIX" \
        || warn "wineprefix not fully ready - it will heal on next start"
    elif [ -d /mt5/prefix/drive_c ]; then
      # podman/CentOS-family roots: no unprivileged userns -> wineboot cannot
      # run inside the container.  Fall back to the prefix baked at build time.
      step "first boot: wineboot unavailable (no userns) - using build-time prefix"
      # tar, not cp -a: files must end up owned by the RUNNING user, and the
      # baked prefix's wineserver socket (root 0700) must be skipped
      (cd /mt5/prefix && tar cf - --exclude=./wineserver .) | tar xf - -C "$PREFIX/"
      ok "wineprefix restored from image"
    else
      warn "no wineboot and no baked prefix - terminals cannot start"
    fi
  fi

  # Terminal 1 = real install dir inside the prefix; Terminal 2 = copy.
  # MT5 self-extracts/updates on first launch, so /mt5/master seed is optional.
  local mt1="$PREFIX/drive_c/Program Files/MetaTrader 5"
  local mt2="$PREFIX/drive_c/Program Files/MetaTrader 5-2"
  if [ ! -f "$mt1/terminal64.exe" ]; then
    step "first boot: seeding MetaTrader 5 from image"
    mkdir -p "$(dirname "$mt1")"
    (cd /mt5/master && tar cf - . 2>/dev/null) | tar xf - -C "$mt1/" 2>/dev/null || true
  fi
  if [ ! -f "$mt1/terminal64.exe" ]; then
    # host-provided fallback: drop a copy of a working MetaTrader 5 program
    # dir at ./data/mt5-master on the host (Windows users can copy theirs
    # from C:\Program Files\MetaTrader 5) - fastest way to a running stack
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
      # stub installer: run its silent /auto mode (wine downloads the real
      # payload itself; capped so a slow link cannot stall the boot forever)
      timeout "${MT5_RUNTIME_INSTALL_CAP:-300}" xvfb-run -a \
        wine64 "$DATA/mt5setup.exe" /auto >/dev/null 2>&1 || true
      timeout 60 wineserver -w 2>/dev/null || true
    fi
    rm -f "$DATA/mt5setup.exe"
  fi
  if [ -f "$mt1/terminal64.exe" ]; then
    # pin the version: a first-launch self-update opens a GUI dialog on the
    # virtual display and stalls the boot forever
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
  # MT5_AUTO_LOGIN=1 (default): the bridge boots STRAIGHT into the stored
  # session - both terminals auto-logged into exactly those accounts, no
  # interactive login prompts.  MT5_AUTO_LOGIN=0 restores the old ask-on-boot
  # behaviour.  Hot switching still works: any new login made on a terminal
  # (UI or panel) is adopted into the session live.
  if [ "${MT5_AUTO_LOGIN:-1}" = "1" ]; then
    export MT5_NO_LOGIN_PROMPT=1
    ok "auto-login enabled - booting into the stored accounts"
  else
    warn "auto-login disabled (MT5_AUTO_LOGIN=0) - the bridge will ask for logins"
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
# all/app modes ALSO run the supervisor (in the background): it is what
# auto-logs the terminals into the stored accounts and keeps them there -
# without it the web panel trades against dead terminals.
run_bridges() { run_bridge; }

main() {
  case "${1:-all}" in
    bash|sh) exec bash ;;           # one-off shell: skip the whole init
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
