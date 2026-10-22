#!/usr/bin/env bash

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="$HOME/python312"
wineprefix="$HOME/.mt5"

step() { echo -e "\033[1m==>\033[0m $*"; }
ok()   { echo -e "  \033[92mOK\033[0m  $*"; }
skip() { echo -e "  \033[2mSKIP\033[0m  $* (already present)"; }
warn() { echo -e "  \033[93mWARN\033[0m  $*"; }
die()  { echo -e "\033[91mERROR:\033[0m $*" >&2; exit 1; }

export PATH="$here:$PATH"

OS="$(uname -s)"
case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  *) die "unsupported OS '$OS' - this stack needs Linux (or macOS with wine)" ;;
esac

step "detected OS: $PLATFORM"

# Install system packages
step "1/5 installing system packages"
if [ "$PLATFORM" = "linux" ]; then
  if command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf     >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum     >/dev/null 2>&1; then PKG="yum"
  elif command -v pacman  >/dev/null 2>&1; then PKG="pacman"
  else die "no known package manager (apt/dnf/yum/pacman) found"
  fi

  need_pkg() {
    local bin="$1" apt="$2" dnf="$3" pacman="$4"
    command -v "$bin" >/dev/null 2>&1 && { skip "$bin"; return 0; }
    case "$PKG" in
      apt)    name="$apt" ;;
      dnf|yum) name="$dnf" ;;
      pacman) name="$pacman" ;;
      *) name="" ;;
    esac
    if [ -z "$name" ]; then
      warn "cannot auto-install '$bin' - install it manually"
    else
      if ! printf '%s\n' "$name" | grep -qx "$name" 2>/dev/null; then
        MISSING_PKGS+=("$name")
      fi
    fi
  }

  MISSING_PKGS=()
  need_pkg curl    curl      curl       curl
  need_pkg wine    wine      wine       wine
  need_pkg wine64  wine64    wine64     wine
  need_pkg wineserver wine   wine       wine
  need_pkg unzip   unzip     unzip      unzip
  need_pkg python3 python3    python3    python3
  need_pkg python3-venv   python3-venv   python3-venv
  need_pkg xvfb    xvfb      xorg-x11-server-Xvfb xorg-server-xvfb
  need_pkg winetricks winetricks winetricks winetricks

  if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    step "installing ${#MISSING_PKGS[@]} package(s)"
    case "$PKG" in
      apt)    apt-get update -y && apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}" ;;
      dnf)    dnf install -y "${MISSING_PKGS[@]}" ;;
      yum)    yum install -y "${MISSING_PKGS[@]}" ;;
      pacman) pacman -S --noconfirm --needed "${MISSING_PKGS[@]}" ;;
    esac
  else
    ok "all system packages present"
  fi
elif [ "$PLATFORM" = "macos" ]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew required on macOS - install from https://brew.sh"
  brew list wine-stable >/dev/null 2>&1 && skip "wine" || { step "installing wine via Homebrew"; brew install --cask wine-stable; }
fi

step "2/5 setting up Python virtual environment"
if [ -x "$venv_dir/bin/python" ]; then
  skip "venv $venv_dir"
else
  python3 -m venv "$venv_dir"
fi
export VENV="$venv_dir"
"$venv_dir/bin/pip" install --upgrade pip wheel setuptools
if [ -f "$here/requirements.txt" ]; then
  "$venv_dir/bin/pip" install -r "$here/requirements.txt"
else
  "$venv_dir/bin/pip" install flask waitress psycopg2-binary
fi

step "3/5 setting up Wine prefix"
export WINEPREFIX="$wineprefix"
mkdir -p "$wineprefix"
WINE="wine"; command -v wine64 >/dev/null 2>&1 && WINE="wine64"
export WINEDEBUG="-all"

if [ -d "$wineprefix/drive_c" ]; then
  skip "wineprefix $wineprefix"
else
  step "initialising wineprefix (first run, ~1 min)"
  "$WINE" wineboot -i >/dev/null 2>&1 || "$WINE" wineboot >/dev/null 2>&1
  wineserver -w 2>/dev/null || true
  [ -d "$wineprefix/drive_c" ] && ok "wineprefix initialised at $wineprefix" \
    || warn "wineprefix not ready - re-run build"
fi

step "4/5 installing MetaTrader 5"
MT5_DIR="$wineprefix/drive_c/Program Files/MetaTrader 5"
if [ -f "$MT5_DIR/terminal64.exe" ]; then
  skip "MetaTrader 5 (terminal 1)"
else
  MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
  MT5_SETUP="$wineprefix/drive_c/mt5setup.exe"
  step "downloading MetaTrader 5 installer"
  curl -fL --retry 5 --retry-all-errors -C - -o "$MT5_SETUP" "$MT5_SETUP_URL"
  step "installing MT5 under wine (GUI wizard may appear - click through)"
  bash -c '"$WINE" "$MT5_SETUP"' || true
  wineserver -w 2>/dev/null || true
  [ -f "$MT5_DIR/terminal64.exe" ] && ok "MetaTrader 5 installed" \
    || warn "MT5 not found at $MT5_DIR - finish the wizard manually, then re-run build"
fi

step "5/5 installing SpotDump EA bridge"
export PATH="$VENV/bin:$PATH"
if [ -f "$MT5_DIR/terminal64.exe" ]; then
  "$VENV/bin/python" - <<PYEOF
import sys; sys.path.insert(0, "$here")
from spot import setup_terminal2, install_script
print("terminal 2 created" if setup_terminal2() else "terminal 2 FAILED")
PYEOF
  for inst in 1 2; do
    "$VENV/bin/python" - <<PY2EOF
import sys; sys.path.insert(0, "$here")
from spot import install_script
try:
    install_script($inst)
    print(f"terminal {inst}: EA installed/compiled")
except Exception as e:
    print(f"terminal {inst}: EA install skipped ({e})")
PY2EOF
  done
else
  warn "terminal 1 missing - nothing to copy yet (finish step 4, re-run)"
fi

cat <<EOF

${B}build complete.${N}
  venv:            $VENV/bin/python
  wineprefix:      $WINEPREFIX
  terminals:       $MT5_DIR
next steps:
  1. ${B}make run${N}   - starts EVERYTHING (web panel :8000 + both terminals)
  2. ${B}python session.py${N} - store the account logins it should boot into
  3. ${B}python info.py${N}    - account info incl. spreads + best filling
  4. ${B}python executor.py --status${N} - scheduled trades + fired log

${D}Prefer the venv python for CLI tools:  $VENV/bin/python ...  ${N}

EOF