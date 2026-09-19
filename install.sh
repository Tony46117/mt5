#!/usr/bin/env bash
# install.sh - one-command setup for the MT5 bridge trading system.
#
# Autodetects OS + installed components and installs everything missing:
#   * system packages (python3.12+, wine, xvfb, winetricks, curl...)
#   * the python virtualenv (~/python312) + every library in requirements.txt
#   * MetaTrader 5 into ~/.mt5 (downloads the official installer, installs
#     it under wine, creates the SECOND terminal copy for account 2)
#   * compiles the SpotDump EA bridge inside both terminals
#
# Idempotent: re-run any time, it only fixes what is missing.
# Supported: Debian/Ubuntu (+derivatives), Fedora, Arch. macOS: partial
# (wine via Homebrew; MT5 install works but is less tested).
# Windows is NOT supported (this stack runs MT5 under Wine on Linux).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
VENV_DIR="${VENV_DIR:-$HOME/python312}"
MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
MT5_DIR2="$WINEPREFIX/drive_c/Program Files/MetaTrader 5-2"
MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
MT5_SETUP="$WINEPREFIX/drive_c/mt5setup.exe"

# --- pretty output ---------------------------------------------------------
B="\033[1m"; D="\033[2m"; R="\033[91m"; G="\033[92m"; Y="\033[93m"; N="\033[0m"
step() { echo -e "${B}==> ${N}$*"; }
ok()   { echo -e "  ${G}OK${N}  $*"; }
skip() { echo -e "  ${D}SKIP${N}  $* (already present)"; }
warn() { echo -e "  ${Y}WARN${N}  $*"; }
die()  { echo -e "${R}ERROR:${N} $*" >&2; exit 1; }

# --- auto-retry -------------------------------------------------------------
# retry <attempts> <delay-s> <label> <cmd...>  - reruns a flaky/slow step
# (downloads, wine boot, package transactions) with a short backoff so one
# network hiccup never kills the whole install.
RETRY_MAX="${RETRY_MAX:-3}"
retry() {
  local attempts="$1" delay="$2" label="$3"; shift 3
  local n=1
  until "$@"; do
    if [ "$n" -ge "$attempts" ]; then
      die "$label failed after $attempts attempts"
    fi
    warn "$label failed (attempt $n/$attempts) - retrying in ${delay}s..."
    sleep "$delay"
    delay=$(( delay * 2 ))
    n=$(( n + 1 ))
  done
  ok "$label"
}

# fast-install pip flags: no version check, prebuilt wheels only, real timeouts
PIPFAST=(--disable-pip-version-check --prefer-binary --timeout 30)

# --- OS detection -----------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  *) die "unsupported OS '$OS' - this stack needs Linux (or macOS with wine)" ;;
esac
[ "$ARCH" = "x86_64" ] || warn "non-x86_64 arch ($ARCH): wine + MT5 need 64-bit Intel/AMD"

PKG="unknown"
if [ "$PLATFORM" = "linux" ]; then
  if   command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf     >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum     >/dev/null 2>&1; then PKG="yum"
  elif command -v pacman  >/dev/null 2>&1; then PKG="pacman"
  else die "no known package manager (apt/dnf/yum/pacman) found"
  fi
fi
step "detected: $PLATFORM ($ARCH), package manager: $PKG"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

# --- 1. system packages (batched: ONE transaction = fast) -------------------
step "1/6 system packages"
MISSING_PKGS=()
need_pkg() {  # need_pkg <binary> <apt-name> <dnf-name> <pacman-name>
  local bin="$1" apt="$2" dnf="$3" pacman="$4" name
  command -v "$bin" >/dev/null 2>&1 && { skip "$bin"; return 0; }
  case "$PKG" in
    apt)    name="$apt" ;;
    dnf|yum) name="$dnf" ;;
    pacman) name="$pacman" ;;
    *) name="" ;;
  esac
  if [ -z "$name" ]; then
    warn "cannot auto-install '$bin' - install it manually"
  elif ! printf '%s\n' "${MISSING_PKGS[@]}" | grep -qx "$name"; then
    MISSING_PKGS+=("$name")
  fi
}

if [ "$PLATFORM" = "linux" ]; then
  # python3.12+: distro package may be older; 3.10+ is what the code needs
  if command -v python3 >/dev/null 2>&1 && [ "$(python3 -c 'import sys; print(sys.version_info >= (3,10))')" = "True" ]; then
    skip "python3 ($(python3 -V))"
  else
    case "$PKG" in
      apt)    MISSING_PKGS+=("python3" "python3-venv" "python3-dev") ;;
      dnf|yum) MISSING_PKGS+=("python3" "python3-devel") ;;
      pacman) MISSING_PKGS+=("python") ;;
    esac
  fi
  need_pkg curl    curl      curl       curl
  need_pkg wine    wine      wine       wine
  need_pkg wine64  wine64    wine64     wine
  need_pkg wineserver wine   wine       wine
  # X virtual framebuffer: MT5 is a GUI app; headless servers need xvfb
  need_pkg Xvfb    xvfb      xorg-x11-server-Xvfb xorg-server-xvfb
  need_pkg winetricks winetricks winetricks winetricks

  if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    step "installing ${#MISSING_PKGS[@]} package(s) in one transaction (with retry)"
    case "$PKG" in
      apt)    retry "$RETRY_MAX" 3 "package install" \
                $SUDO apt-get update -y && \
                retry "$RETRY_MAX" 3 "package install" \
                $SUDO apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}" ;;
      dnf)    retry "$RETRY_MAX" 3 "package install" \
                $SUDO dnf install -y "${MISSING_PKGS[@]}" ;;
      yum)    retry "$RETRY_MAX" 3 "package install" \
                $SUDO yum install -y "${MISSING_PKGS[@]}" ;;
      pacman) retry "$RETRY_MAX" 3 "package install" \
                $SUDO pacman -S --noconfirm --needed "${MISSING_PKGS[@]}" ;;
    esac
  else
    ok "all system packages present"
  fi
elif [ "$PLATFORM" = "macos" ]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew required on macOS - install from https://brew.sh"
  brew list wine-stable >/dev/null 2>&1 && skip "wine" || { step "installing wine via Homebrew"; brew install --cask wine-stable; }
fi

# --- 2+3. python venv AND wineprefix in PARALLEL ----------------------------
step "2-3/6 venv + libraries  |  wineprefix (parallel)"
export WINEPREFIX
mkdir -p "$WINEPREFIX"
WINE="wine"; command -v wine64 >/dev/null 2>&1 && WINE="wine64"
export WINEDEBUG="-all"

venv_task() {
  if [ -x "$VENV_DIR/bin/python" ]; then
    skip "venv $VENV_DIR"
  else
    retry "$RETRY_MAX" 3 "venv creation" "$PYBIN" -m venv "$VENV_DIR"
  fi
  retry "$RETRY_MAX" 5 "pip bootstrap" \
    "$VENV_DIR/bin/pip" install "${PIPFAST[@]}" --upgrade pip wheel setuptools
  if [ -f "$HERE/requirements.txt" ]; then
    retry "$RETRY_MAX" 5 "python libraries (requirements.txt)" \
      "$VENV_DIR/bin/pip" install "${PIPFAST[@]}" -r "$HERE/requirements.txt"
  else
    warn "requirements.txt not found - installing the core set only"
    retry "$RETRY_MAX" 5 "core python libraries" \
      "$VENV_DIR/bin/pip" install "${PIPFAST[@]}" flask waitress pytest
  fi
}

prefix_task() {
  if [ -d "$WINEPREFIX/drive_c" ]; then
    skip "wineprefix $WINEPREFIX"
    return 0
  fi
  step "initialising wineprefix (first run, ~1 min)"
  try_boot() {  # -i first; plain wineboot as fallback; never hard-fails
    "$WINE" wineboot -i >/dev/null 2>&1 || "$WINE" wineboot >/dev/null 2>&1
    return 0
  }
  retry "$RETRY_MAX" 5 "wineprefix init" try_boot
  wineserver -w 2>/dev/null || true
  [ -d "$WINEPREFIX/drive_c" ] && ok "wineprefix initialised at $WINEPREFIX" \
    || warn "wineprefix not ready - re-run install.sh"
}

PYBIN="$(command -v python3)"
venv_task & VENV_PID=$!
prefix_task & PREFIX_PID=$!
wait "$VENV_PID" || die "venv step failed"
wait "$PREFIX_PID"

if [ -f "$MT5_DIR/terminal64.exe" ]; then
  skip "MetaTrader 5 (terminal 1)"
else
  step "downloading MetaTrader 5 installer (resume + retry)"
  retry "$RETRY_MAX" 5 "MT5 download" \
    curl -fL --retry 5 --retry-all-errors -C - -o "$MT5_SETUP" "$MT5_SETUP_URL"
  step "installing MT5 under wine (GUI wizard may appear - click through, it remembers)"
  # /auto runs the installer unattended where supported; otherwise the
  # wizard shows once and the user finishes it.  Two attempts: the very
  # first wine run sometimes loses the race against prefix init.  The
  # existence check below (not the exit code) decides success - the user
  # may finish the wizard manually and re-run this script.
  mt5_install() {
    bash -c '"$1" "$2" /auto || "$1" "$2"' _ "$WINE" "$MT5_SETUP" || true
    return 0
  }
  retry 2 10 "MT5 install" mt5_install
  wineserver -w 2>/dev/null || true
  [ -f "$MT5_DIR/terminal64.exe" ] && ok "MetaTrader 5 installed" \
    || warn "MT5 not found at $MT5_DIR - finish the wizard manually, then re-run install.sh"
fi

# --- 4. second terminal (account 2) -----------------------------------------
step "4/6 second terminal (one-time copy)"
if [ -f "$MT5_DIR2/terminal64.exe" ]; then
  skip "terminal 2"
elif [ -f "$MT5_DIR/terminal64.exe" ]; then
  "$VENV_DIR/bin/python" - <<PYEOF
import sys; sys.path.insert(0, "$HERE")
from spot import setup_terminal2
print("terminal 2 created" if setup_terminal2() else "terminal 2 FAILED")
PYEOF
else
  warn "terminal 1 missing - nothing to copy yet (finish step 3, re-run)"
fi

# --- 5. EA bridge compile ----------------------------------------------------
step "5/6 SpotDump EA bridge"
"$VENV_DIR/bin/python" - <<PYEOF
import sys; sys.path.insert(0, "$HERE")
from spot import install_script
for inst in (1, 2):
    try:
        install_script(inst)
        print(f"terminal {inst}: EA installed/compiled")
    except Exception as e:
        print(f"terminal {inst}: EA install skipped ({e})")
PYEOF

# --- 6. chart template + summary --------------------------------------------
step "6/6 chart template (algo trading permissions)"
"$VENV_DIR/bin/python" "$HERE/make_bridge_tpl.py" || warn "template install failed (re-run after first terminal boot)"

cat <<EOF

${B}install complete.${N}
  venv:            $VENV_DIR/bin/python
  wineprefix:      $WINEPREFIX
  terminals:       $MT5_DIR
                   $MT5_DIR2
  next steps:
    1. ${B}python bridge.py${N}   - boots both terminals into the stored session (ALGO ON); logging into any account on any terminal is adopted live
    2. ${B}python app.py${N}      - web terminal (dashboard, panel, broker probe)
    3. ${B}python info.py${N}     - account info incl. spreads + best filling
    4. ${B}python probe_schedules.py${N} - verify scheduled trades execute fast

${D}run everything with the venv python:  ~/python312/bin/python ...${N}
EOF
