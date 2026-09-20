#!/usr/bin/env python3.12
"""Shared MT5-on-Wine infrastructure + live FX spot-only dashboard.

BRIDGE
    The MQL5 EA SpotDump.mq5 (running inside each terminal) writes
    MQL5/Files/spots.csv (bid/ask for EVERY Market Watch symbol,
    ms-precise tick time), MQL5/Files/trades.csv (all open positions +
    an account header row) and MQL5/Files/candles.csv (M1 OHLC of
    EURUSD+GBPUSD) every 50 ms.  The EA is auto-attached on terminal
    launch via an MT5 start config (/config:... [StartUp]
    Expert=SpotDump.ex5).  Since v1.40 it also consumes exec_in.csv, an
    order channel python uses to place/close trades (see executor.py).

ACCOUNTS
    Accounts are the RUNTIME SESSION (session.py): bridge.py asks the
    operator for each terminal's login / password / server, logs the
    terminals in with exactly those credentials and stores them
    obfuscated.  No acc.env auto-login anymore - every module (monitor,
    info, executor, close, web) follows whoever is actually logged in,
    so everything stays connected to the logged-in trades.

THIS SCRIPT (run directly) shows the spot prices of every symbol the
terminal has a quote for - no open positions, no account state.
Positions/account monitoring lives in monitor.py.

Usage:
    python spot.py              # live spot-only dashboard (static screen)
    python spot.py --once       # single frame
    python spot.py --accounts   # show which account each terminal is in
    python spot.py --restart    # bounce the terminal to (re)attach the EA
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import datetime as dt
from pathlib import Path

import config
import session
from config import CONFIG, setup_logging

log = setup_logging(__name__, level=logging.WARNING)

WINEPREFIX = CONFIG.wineprefix
MT5_DIR = CONFIG.mt5_dir
MT5_DIR2 = CONFIG.mt5_dir2
APPDATA = WINEPREFIX / "drive_c" / "users"
SCRIPT_SRC = CONFIG.spot_dump_src
STARTUP_INI = MT5_DIR / "config_spot.ini"
ENV_FILE = CONFIG.env_file

# terminal instances: one account per terminal, so account2 needs its own copy.
# Start configs live at drive_c root: MT5's /config: parser mangles paths
# containing spaces ("Program Files" -> config silently not loaded).
# ini names come from config.py (config_bridge*.ini) so this project and
# mt5_v2 (config_spot*.ini) never overwrite each other's start configs.
TERMINALS = {1: dict(config.TERMINALS[1]), 2: dict(config.TERMINALS[2])}

PAIRS = config.CLASSIC_PAIRS
COLORS = {"EURUSD": "\033[96m", "GBPUSD": "\033[95m", "XAUUSD": "\033[93m"}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
BLUE = "\033[94m"

# legacy tab-less rows from old SpotDump builds (bid/ask have fixed digits)
_NOTAB_RES = {
    "EURUSD": re.compile(r"^EURUSD(\d+\.\d{5})(\d+\.\d{5})\s*(.*)$"),
    "GBPUSD": re.compile(r"^GBPUSD(\d+\.\d{5})(\d+\.\d{5})\s*(.*)$"),
    "XAUUSD": re.compile(r"^XAUUSD(\d+\.\d{2})(\d+\.\d{2})\s*(.*)$"),
}


# --------------------------------------------------------------------------
# runtime session accounts (replaces acc.env auto-login)
# --------------------------------------------------------------------------

def read_accounts() -> dict[int, dict[str, str]]:
    """The logged-in accounts {1: {'login','password','server'}, 2: {...}}.

    THE single identity source for every module: bridge.py asks the
    operator for credentials, session.py stores them obfuscated, and from
    then on everything (executor, info, monitor, metrics, close, web)
    follows the session - so everything is connected to the logged-in
    trades.  Empty entries = that terminal is not logged in yet.
    """
    return session.load()


# --------------------------------------------------------------------------
# data-folder / bridge helpers
# --------------------------------------------------------------------------

def data_roots() -> list[Path]:
    """All candidate MT5 data-folder roots.

    Covers the portable install dir, any sibling install copies (e.g. a
    second terminal in "MetaTrader 5-2" for another account) and all
    AppData instance folders.
    """
    roots: list[Path] = []
    pf = WINEPREFIX / "drive_c" / "Program Files"
    for term_dir in sorted(pf.glob("MetaTrader 5*")):
        if (term_dir / "MQL5").is_dir():
            roots.append(term_dir / "MQL5")
    for user in APPDATA.glob("*"):
        for term in (user / "AppData" / "Roaming" / "MetaQuotes" / "Terminal").glob("*"):
            if (term / "MQL5").is_dir():
                roots.append(term / "MQL5")
    return roots


def spots_csv_paths() -> list[Path]:
    """ALL terminal spot feeds - read_spots() MERGES them per symbol.

    (This used to exclude terminal2 so the spot dashboard could not
    flip-flop between two terminals; but excluding it made every
    symbol that only terminal 2 has selected look like 'no live quote'
    for account 2's orders whenever terminal 1's file was caught
    mid-rewrite.  Merging newest-first per symbol keeps every Market
    Watch represented and is strictly more robust.)"""
    return [root / "Files" / "spots.csv" for root in data_roots()]


def trades_csv_paths() -> list[Path]:
    return [root / "Files" / "trades.csv" for root in data_roots()]


def candles_csv_paths() -> list[Path]:
    """M1 OHLC feed paths (one per terminal data root)."""
    return [root / "Files" / "candles.csv" for root in data_roots()]


def exec_in_path(inst: int = 1) -> Path:
    """Order-channel directory of terminal `inst`: executor.py drops atomic
    exec_in.<id>.cmd files here (see SpotDump.mq5 v1.40).
    Resolves the actual data directory (may be in AppData, not install dir)."""
    return _exec_dir(inst)


def exec_out_path(inst: int = 1) -> Path:
    """Order-channel results file of terminal `inst`."""
    return _exec_dir(inst) / "exec_out.csv"


def _exec_dir(inst: int) -> Path:
    """Resolve the actual MQL5/Files directory for terminal `inst`.
    Uses pick_terminal with the expected login from the runtime session."""
    login = read_accounts().get(inst, {}).get("login", "")
    if login:
        term = pick_terminal(login)
        if term:
            return term["root"] / "Files"
    # Fallback: install directory (for terminal 2 /portable, or if bridge not ready)
    return TERMINALS[inst]["dir"] / "MQL5" / "Files"


def compiled_paths(inst: int | None = None) -> list[Path]:
    if inst:
        return [TERMINALS[inst]["dir"] / "MQL5" / "Experts" / "SpotDump.ex5"]
    return [root / "Experts" / "SpotDump.ex5" for root in data_roots()]


# --------------------------------------------------------------------------
# shared TTL caches - one bridge read serves every consumer (spot.py,
# monitor.py, info.py, ms.py, hft.py, app.py, bridge.py).  The EA rewrites
# the CSVs every 50 ms, so a 100 ms cache keeps every reader coherent while
# cutting repeated file parses (and repeated pgrep forks) to near zero.
# --------------------------------------------------------------------------

_SPOTS_CACHE: tuple[float, dict] | None = None          # (monotonic_ts, dict)
_HEADER_CACHE: dict[Path, tuple[float, int, int, dict]] = {}  # path -> (ts, mtime, size, dict)
_TERM_RUNNING_CACHE: dict[int, tuple[float, bool]] = {}  # inst -> (ts, bool)


def read_header(path: Path) -> dict:
    """Parse the NONE header row of a trades.csv (EA v1.20+).

    The EA rewrites the file every 50 ms (briefly truncating it), so a
    read can catch an empty/partial file: retry a few times before
    giving up.  Returns {} when the file/EA predates the header fields.
    Results are cached until the file's mtime/size changes (cheap stat).
    """
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return {}
    cached = _HEADER_CACHE.get(path)
    if cached and cached[0] > mtime and cached[1] == mtime and cached[2] == size:
        return cached[3]
    h = _read_header_uncached(path)
    _HEADER_CACHE[path] = (time.monotonic() + 0.05, mtime, size, h)
    return h


def _read_header_uncached(path: Path) -> dict:
    if not path.exists():
        return {}
    for _ in range(5):
        try:
            raw = path.read_text(encoding="cp1252", errors="replace")
        except OSError:
            return {}
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if parts and parts[0] == "NONE":
                h: dict = {"positions": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0}
                if len(parts) > 4:
                    h["login"] = parts[2]
                    h["server"] = parts[3]
                if len(parts) > 5:
                    h["balance"] = parts[4]
                    h["equity"] = parts[5]
                if len(parts) > 15:   # SpotDump v1.30 extended header
                    h["currency"] = parts[6]
                    h["leverage"] = parts[7]
                    h["margin"] = parts[8]
                    h["margin_free"] = parts[9]
                    h["margin_level"] = parts[10]
                    h["profit"] = parts[11]
                    h["broker"] = parts[12]
                    h["holder"] = parts[13]
                    h["margin_mode"] = parts[14]
                    h["trade_mode"] = parts[15]
                if len(parts) > 18:   # SpotDump v1.50+ trade-allowed diagnostics
                    h["trade_allowed"] = parts[16]           # terminal algo button
                    h["mql_allowed"] = parts[17]             # EA trading allowed
                    h["account_trade_allowed"] = parts[18]   # account trade mode
                return h
        time.sleep(0.05)   # caught mid-rewrite; try again
    return {}


def scan_terminals() -> list[dict]:
    """One entry per data root that has a trades.csv: path, mtime, header."""
    out: list[dict] = []
    for root in data_roots():
        tp = root / "Files" / "trades.csv"
        if not tp.exists():
            continue
        entry = {"root": root, "trades_path": tp, "spots_path": root / "Files" / "spots.csv",
                 "mtime": 0.0, "header": {}}
        try:
            entry["mtime"] = tp.stat().st_mtime
        except OSError:
            pass
        entry["header"] = read_header(tp)
        out.append(entry)
    return out


def pick_terminal(login: str) -> dict | None:
    """Data root whose trades.csv header login matches; fallback: any.

    When no live header proves the login yet, the runtime session's
    terminal order acts as the hint (terminal 1 = account 1, etc.) so the
    exec channel lands in the right terminal while the bridge warms up."""
    terms = scan_terminals()
    for t in terms:
        if t["header"].get("login") == login:
            return t
    if login:
        for inst in (1, 2):
            if session.expected_login(inst) == login:
                hint = TERMINALS[inst]["dir"] / "MQL5"
                for t in terms:
                    if t["root"] == hint:
                        return t
                break
    return terms[0] if terms else None


def bridge_age(extra_paths: list[Path] | None = None) -> float:
    """Seconds since the freshest bridge file was written (999 = stale)."""
    files = spots_csv_paths() + trades_csv_paths() + (extra_paths or [])
    newest = 0.0
    now = time.time()
    for p in files:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return now - newest if newest else 999.0


def wait_for_bridge(inst: int = 1, timeout: float = 120) -> bool:
    """Wait until THIS terminal's EA feed is fresh (written within the last 5 s)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if feed_age(inst) < 5:
            return True
        time.sleep(1)
    return False


def feed_age(inst: int = 1) -> float:
    """Seconds since THIS terminal's freshest bridge file was written.
    999 = terminal's data folder has no bridge files yet (stale/never).
    
    Cached for 1 second to avoid repeated stat() calls during polling."""
    global _FEED_AGE_CACHE
    now = time.time()
    cached = _FEED_AGE_CACHE.get(inst)
    if cached and now - cached[0] < 1.0:
        return cached[1]
    if inst == 1:
        files = spots_csv_paths() + trades_csv_paths()  # incl. legacy AppData roots
    else:
        base = MT5_DIR2 / "MQL5" / "Files"
        files = [base / "spots.csv", base / "trades.csv", base / "candles.csv"]
    newest = 0.0
    for p in files:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    age = now - newest if newest else 999.0
    _FEED_AGE_CACHE[inst] = (now, age)
    return age


_FEED_AGE_CACHE: dict[int, tuple[float, float]] = {}  # inst -> (ts, age)


# --------------------------------------------------------------------------
# terminal control (Wine)
# --------------------------------------------------------------------------

def wine_bin() -> str:
    if os.path.exists("/usr/bin/wine"):
        return "/usr/bin/wine"
    if os.path.exists("/usr/bin/wine64"):
        return "/usr/bin/wine64"
    return "wine"


def wineserver_bin() -> str:
    """Host wineserver binary (NOT a Windows exe - never run via wine)."""
    ws = shutil.which("wineserver")
    if ws:
        return ws
    return "/usr/bin/wineserver"


def term_running(inst: int = 1, max_age: float = 1.0) -> bool:
    """True if terminal instance `inst` is running (1 s TTL shared cache -
    avoids one pgrep fork per poller per second).

    The cmdline may use unix or windows separators depending on who
    launched it (wine shows 'C:\\Program Files\\MetaTrader 5\\terminal64.exe'),
    so both slash styles match; the install-dir name keeps terminal 1 and
    terminal 2 from cross-matching each other.
    """
    cached = _TERM_RUNNING_CACHE.get(inst)
    now = time.monotonic()
    if cached and now - cached[0] < max_age:
        return cached[1]
    pat = re.escape(TERMINALS[inst]["dir"].name) + r"[\\/]+terminal64\.exe"
    try:
        r = subprocess.run(["pgrep", "-f", pat],
                           capture_output=True, text=True, timeout=5)
        running = r.returncode == 0
    except Exception:
        running = False
    _TERM_RUNNING_CACHE[inst] = (now, running)
    return running


def check_terminal_running() -> bool:   # legacy alias (terminal 1)
    return term_running(1)


# MINIMAL TERMINALS: exactly ONE chart window per terminal - EURUSD H1 with
# the SpotDump EA already attached.  MT5 restores whatever the active profile
# folder contains, so this canonical chart01.chr (known-good XML profile
# format) is rewritten into EVERY profile before each launch: the terminal
# can then only ever come up with a single chart, no matter how many windows
# a previous session (or a wrong profile selection) left behind.
MINIMAL_CHART_XML = """<chart>
  <chart_settings>
    <symbol>EURUSD</symbol>
    <period>60</period>
    <chart_type>1</chart_type>
    <chart_mode>0</chart_mode>
    <chart_shift>0</chart_shift>
    <chart_autoscroll>1</chart_autoscroll>
    <chart_scale>5</chart_scale>
    <chart_scalefix>0</chart_scalefix>
    <chart_scalefix_11>0</chart_scalefix_11>
    <chart_scale_percent>0</chart_scale_percent>
    <chart_points_per_bar>0</chart_points_per_bar>
    <chart_show_ohlc>1</chart_show_ohlc>
    <chart_show_grid>1</chart_show_grid>
    <chart_show_volumes>1</chart_show_volumes>
    <chart_show_line_break>0</chart_show_line_break>
    <chart_show_bid_line>1</chart_show_bid_line>
    <chart_show_ask_line>0</chart_show_ask_line>
    <chart_show_last_line>0</chart_show_last_line>
    <chart_show_period_sep>1</chart_show_period_sep>
    <chart_show_time_scale>1</chart_show_time_scale>
    <chart_color_background>16777215</chart_color_background>
    <chart_color_foreground>0</chart_color_foreground>
    <chart_color_grid>8421504</chart_color_grid>
    <chart_color_volumes>8421504</chart_color_volumes>
    <chart_color_bull>255</chart_color_bull>
    <chart_color_bear>16711680</chart_color_bear>
    <chart_color_line>0</chart_color_line>
    <chart_color_ask>8421504</chart_color_ask>
    <chart_color_stop>16711680</chart_color_stop>
    <chart_color_profit>255</chart_color_profit>
    <chart_color_last>0</chart_color_last>
    <chart_color_bid_line>0</chart_color_bid_line>
    <chart_color_ask_line>8421504</chart_color_ask_line>
    <chart_color_last_line>0</chart_color_last_line>
    <chart_color_period_sep>8421504</chart_color_period_sep>
    <chart_shift_size>10</chart_shift_size>
    <chart_scalefix_ratio>0</chart_scalefix_ratio>
    <chart_scalefix_11_ratio>0</chart_scalefix_11_ratio>
  </chart_settings>
  <experts>
    <expert>
      <name>Experts\\SpotDump.ex5</name>
      <flags>339</flags>
      <window_num>0</window_num>
    </expert>
  </experts>
  <indicators/>
  <graphical_objects/>
</chart>
"""


def sanitize_charts(inst: int = 1) -> None:
    """Force terminal `inst` to boot MINIMAL: every profile is pruned to ZERO
    restored charts.  The single window comes from the start config itself
    ([StartUp] Symbol/Period + Expert + Template=Bridge) so the boot can only
    ever produce ONE chart with the EA attached - no matter how many windows
    a previous session left behind.

    Called from launch_terminal while the terminal is stopped - MT5 saves its
    open charts into the active profile on exit and restores them on start,
    which is how windows piled up session after session (each restart added
    the config chart on top of everything the last exit had saved).  Pruning
    at launch makes 'one chart, nothing else' the permanent boot state.
    """
    charts_root = TERMINALS[inst]["dir"] / "Profiles" / "Charts"
    try:
        if not charts_root.exists():
            charts_root.mkdir(parents=True, exist_ok=True)
        for prof in charts_root.iterdir():
            if not prof.is_dir():
                continue
            for old in prof.glob("chart*.chr"):
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError as exc:
        log.debug(f"terminal {inst}: chart sanitize skipped: {exc}")


def force_profile(inst: int = 1) -> None:
    """Pin [Charts] ProfileLast=SpotBridge{inst} in the terminal's common.ini
    (while stopped) so the boot can never restore some other profile the
    operator last clicked around in - the sanitized, empty SpotBridge profile
    is the ONLY one that ever opens.  ATOMIC write."""
    ini = TERMINALS[inst]["dir"] / "Config" / "common.ini"
    want = f"SpotBridge{inst}"
    text = _read_ini(ini)
    if not text:
        _write_ini_atomic(ini, "\ufeff[Common]\r\nAutoTrading=1\r\n"
                                f"[Charts]\r\nProfileLast={want}\r\n")
        return
    new = text
    if re.search(r"(?im)^ProfileLast\s*=", new):
        new = re.sub(r"(?im)^ProfileLast\s*=\S*", f"ProfileLast={want}", new)
    else:
        new = new.rstrip("\r\n") + f"\r\n[Charts]\r\nProfileLast={want}\r\n"
    if new != text:
        _write_ini_atomic(ini, new)


def _write_start_cfg(inst: int, with_login: bool = True) -> None:
    """Write the MT5 start config for a terminal instance.

    With with_login=True (the default) BOTH terminals are auto-LOGINED
    from the runtime session ([Common] Login/Password/Server) - bridge.py
    collected those credentials interactively, so whoever logged in is
    exactly who boots.  Symbol/Period guarantee a chart exists for the EA
    to attach to.
    With with_login=False (bridge.py's PRE-LOGIN boot) no [Common] block
    is written: the terminal boots WITHOUT auto-login so it can never
    silently connect the stored/old account while the operator is about
    to type fresh credentials.  The EA still auto-attaches.
    [Experts] AllowLiveTrading=1 boots the terminal with EA automated
    trading allowed (MQL_TRADE_ALLOWED=1) - without it every OrderSend
    dies with retcode 10027 even when the algo button is on.
    The login block is scrubbed again after boot (see scrub_start_cfg) so
    credentials never sit in a plaintext ini.
    """
    ini = TERMINALS[inst]["ini"]
    try:
        common = ""
        if with_login:
            a = read_accounts().get(inst, {})
            common = ("[Common]\r\n"
                      f"Login={a.get('login', '')}\r\n"
                      f"Password={a.get('password', '')}\r\n"
                      f"Server={a.get('server', '')}\r\n"
                      f"Profile=SpotBridge{inst}\r\n")
        body = ("[Experts]\r\n"
                "AllowLiveTrading=1\r\n"
                "Enabled=1\r\n"
                "Account=0\r\n"
                f"Profile=SpotBridge{inst}\r\n"
                "[StartUp]\r\nExpert=SpotDump.ex5\r\n"
                "Template=Bridge\r\n"
                "Symbol=EURUSD\r\nPeriod=H1\r\n"
                f"Profile=SpotBridge{inst}\r\n")
        ini.write_text(common + body, encoding="ascii")
    except OSError:
        pass


def scrub_start_cfg(inst: int) -> None:
    """ANTIDETECT: wipe the login block from terminal `inst`'s start config.

    Called once the terminal has booted (it reads the config at startup),
    so credentials never sit in a plaintext file while the terminal runs.
    Callers must wait until the bridge feed is live - scrubbing too early
    would make a RE-launch lose its login (bridge.py does this).
    """
    ini = TERMINALS[inst]["ini"]
    try:
        if not ini.exists():
            return
        lines = [l for l in ini.read_text(encoding="ascii", errors="replace").splitlines()
                 if not l.strip().lower().startswith(("login=", "password=", "server="))]
        ini.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    except OSError as exc:
        log.debug(f"terminal {inst}: start-config scrub skipped: {exc}")


# --------------------------------------------------------------------------
# common.ini writers - ATOMIC + SERIALIZED
#
# Every terminal's common.ini is read-modify-written by several helpers
# (autotrading force, login scrub, profile pin).  A non-atomic write here once
# CORRUPTED terminal 2's common.ini (torn file: truncated [Charts], stray
# fragments) because the two concurrent boot threads both wrote it - terminal
# 2 then booted with a mangled config, MT5 failed to open its chart
# ('open charts limit reached') and the EA never attached.  All writes now go
# through one lock + tmp-file + atomic rename, and each helper only ever
# touches the file of the terminal being launched.
# --------------------------------------------------------------------------
_INI_LOCK = threading.Lock()


def _read_ini(path: Path) -> str:
    """Read a UTF-16 ini (empty string when missing/unreadable)."""
    try:
        return path.read_text(encoding="utf-16", errors="replace")
    except (OSError, UnicodeError):
        return ""


def _write_ini_atomic(path: Path, text: str) -> None:
    """Write a UTF-16 ini atomically (tmp + os.replace) under the global lock
    so concurrent boot threads can never tear the file."""
    with _INI_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".ini.tmp")
            tmp.write_text(text, encoding="utf-16")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug(f"ini write failed for {path.name}: {exc}")


def _drop_ini_lines(text: str, prefixes: tuple[str, ...]) -> str:
    """Remove every line whose lowercase stripped form starts with one of
    `prefixes` (used for the credential scrubs)."""
    return "\r\n".join(
        l for l in text.splitlines()
        if not l.strip().lower().startswith(prefixes)) + "\r\n"


def scrub_terminal2_credentials() -> None:
    """Remove account1's stored auto-login from terminal2's copied common.ini.

    If terminal2's start config ever fails to load, without this scrub it
    would silently fall back to logging into account1 (copied settings).
    ATOMIC write - this file used to be torn by concurrent boot threads."""
    ini = MT5_DIR2 / "Config" / "common.ini"
    if not ini.exists():
        return
    text = _read_ini(ini)
    if not text:
        return
    cleaned = _drop_ini_lines(text, ("login=", "server="))
    if cleaned != text:
        _write_ini_atomic(ini, cleaned)


def scrub_common_ini_login(inst: int) -> None:
    """Remove any saved login/password/server from terminal's common.ini.
    
    MT5 saves the last logged-in account to common.ini on exit. If we don't
    scrub this before launch, the terminal will silently auto-login to the
    OLD account BEFORE reading the start config, causing a race where the
    EA briefly reports the wrong account. This must be done while terminal
    is NOT running (it rewrites common.ini on exit)."""
    ini = TERMINALS[inst]["dir"] / "Config" / "common.ini"
    if not ini.exists():
        return
    text = _read_ini(ini)
    if not text:
        return
    cleaned = _drop_ini_lines(text, ("login=", "password=", "server="))
    if cleaned != text:
        _write_ini_atomic(ini, cleaned)


def repair_common_ini(inst: int) -> None:
    """Heal a corrupted common.ini before launch.

    A torn write once left terminal 2's ini with garbage (a stray 'on]'
    fragment and keys BEFORE the first [section] header).  MT5's parser then
    mis-reads the file: the start-config chart fails with
    "open charts limit reached", the EA never attaches and the terminal
    looks broken.  Every boot this keeps only well-formed lines:
      * content before the first [section] header is dropped
      * lines that are neither [section] nor key=value are dropped
      * duplicate keys are collapsed (first occurrence wins)
      * AutoTrading=1 and ProfileLast=SpotBridge{inst} are ensured
    Runs while the terminal is stopped; write is atomic."""
    ini = TERMINALS[inst]["dir"] / "Config" / "common.ini"
    text = _read_ini(ini)
    if not text:
        _write_ini_atomic(ini, "\ufeff[Common]\r\nAutoTrading=1\r\n"
                                f"[Charts]\r\nProfileLast=SpotBridge{inst}\r\n")
        return
    lines = text.splitlines()
    out: list[str] = []
    seen_section = False
    seen: set[str] = set()
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            seen_section = True
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        elif re.match(r"^[A-Za-z0-9_]+\s*=", s):
            if not seen_section:
                continue                     # key before any [section] = garbage
            key = s.split("=", 1)[0].strip().lower()
            if key in seen:
                continue                     # duplicate key - first wins
            seen.add(key)
            out.append(s)
        # anything else = torn-write fragment - dropped
    body = "\r\n".join(out)
    if not re.search(r"(?im)^AutoTrading\s*=", body):
        body = "[Common]\r\nAutoTrading=1\r\n" + body
    elif not re.search(r"(?im)^\[Common\]", body):
        body = "[Common]\r\n" + body
    want = f"SpotBridge{inst}"
    if re.search(r"(?im)^ProfileLast\s*=", body):
        body = re.sub(r"(?im)^ProfileLast\s*=\S*", f"ProfileLast={want}", body)
    else:
        body += f"\r\n[Charts]\r\nProfileLast={want}\r\n"
    repaired = "\ufeff" + body + "\r\n"
    if repaired != text:
        _write_ini_atomic(ini, repaired)
        log.info(f"terminal {inst}: repaired corrupted {ini.name}")


def ensure_autotrading(inst: int = 1) -> bool:
    """Force AutoTrading=1 in the terminal's common.ini so the very first
    OrderSend can never die with retcode 10027 (algotrading disabled).

    MT5 has no CLI switch for this - the algo button state lives in
    common.ini ([Common] AutoTrading=0|1), and a fresh/unknown data folder
    defaults to 0, which is exactly how scheduled and manual orders used to
    die with 'retcode 10027'.  common.ini is UTF-16-LE with a BOM; the file
    is only rewritten when the flag is missing or 0 (and the terminal must
    not be running while we touch it - it rewrites the file on exit).
    ATOMIC write (see _write_ini_atomic - a torn write here once killed
    terminal 2's boot entirely)."""
    ini = TERMINALS[inst]["dir"] / "Config" / "common.ini"
    text = _read_ini(ini)
    if not text:
        _write_ini_atomic(ini, "\ufeff[Common]\r\nAutoTrading=1\r\n")
        return True
    m = re.search(r"(?im)^AutoTrading\s*=\s*(\S+)", text)
    if m and m.group(1).strip() == "1":
        return True
    if m:
        text = re.sub(r"(?im)^AutoTrading\s*=\s*\S+", "AutoTrading=1", text)
    else:
        text = text.rstrip("\r\n") + "\r\nAutoTrading=1\r\n"
    _write_ini_atomic(ini, text)
    log.info(f"terminal {inst}: forced AutoTrading=1 in {ini.name}")
    return True


def ensure_bridge_template(inst: int) -> None:
    """Make sure the Bridge.tpl template (expertmode=1 + SpotDump EA) exists
    for terminal `inst` - [StartUp] Template=Bridge only works when the file
    is there.  Idempotent + cheap (skips when already installed)."""
    try:
        import make_bridge_tpl            # lazy: avoids the config/spot cycle at import
        make_bridge_tpl.install()
    except Exception as exc:
        log.debug(f"terminal {inst}: Bridge template ensure skipped: {exc}")


def pin_window_geometry(inst: int) -> None:
    """Force the main window to a SMALL rectangle in the top-left of the
    screen by rewriting Config/terminal.ini's [Window] block BEFORE launch.

    MT5 saves its window frame in terminal.ini on exit; a maximized (or
    user-resized) session is then restored covering the whole screen on
    every boot.  Everything here runs headless - the two terminals just
    need to exist for the EA, not hog the operator's desktop.  Left/Top/
    Right/Bottom are client-area coordinates in METATRADER's own logic;
    the LSave/TSave/RSave/BSave "saved maximized" markers are pinned to
    the same small rect so Windows' restore-from-maximized logic cannot
    resurrect a full-screen frame."""
    ini = TERMINALS[inst]["dir"] / "Config" / "terminal.ini"
    L, T, R, B = 10 + (inst - 1) * 30, 10 + (inst - 1) * 24, 460, 330
    try:
        text = _read_ini(ini)
        if not text:
            return                        # no terminal.ini yet - MT5 writes defaults
        lines = text.splitlines()
        out: list[str] = []
        in_win = False
        replaced = {"Left": False, "Top": False, "Right": False,
                    "Bottom": False, "Fullscreen": False}
        for ln in lines:
            if ln.strip().startswith("["):
                in_win = ln.strip().lower() == "[window]"
                out.append(ln)
                continue
            if in_win and "=" in ln:
                key = ln.split("=", 1)[0].strip()
                if key == "Fullscreen":
                    out.append("Fullscreen=0")
                    replaced["Fullscreen"] = True
                    continue
                if key == "LSave" or key == "TSave" or key == "RSave" or key == "BSave":
                    out.append(ln)        # keep, then overwrite below
                    continue
                if key in replaced and not replaced[key]:
                    if key == "Left":
                        out.append(f"Left={L}")
                    elif key == "Top":
                        out.append(f"Top={T}")
                    elif key == "Right":
                        out.append(f"Right={R}")
                    elif key == "Bottom":
                        out.append(f"Bottom={B}")
                    replaced[key] = True
                    continue
            out.append(ln)
        # inject keys that the saved file did not contain
        if not all(replaced.values()):
            new_lines: list[str] = []
            injected = False
            for ln in out:
                new_lines.append(ln)
                if not injected and ln.strip().lower() == "[window]":
                    if not replaced["Fullscreen"]:
                        new_lines.append("Fullscreen=0")
                    if not replaced["Left"]:
                        new_lines.append(f"Left={L}")
                    if not replaced["Top"]:
                        new_lines.append(f"Top={T}")
                    if not replaced["Right"]:
                        new_lines.append(f"Right={R}")
                    if not replaced["Bottom"]:
                        new_lines.append(f"Bottom={B}")
                    injected = True
            out = new_lines
        _write_ini_atomic(ini, "\r\n".join(out) + "\r\n")
        log.debug(f"terminal {inst}: window pinned to {R - L}x{B - T} at ({L},{T})")
    except OSError as exc:
        log.debug(f"terminal {inst}: window pin skipped: {exc}")


def purge_exec_channel(inst: int) -> None:
    """Delete leftover exec_in/exec_next command files of terminal `inst`
    BEFORE it boots.

    When a terminal dies with queued commands (crash, pkill, wine stall), the
    files survive on disk; the NEXT boot's EA reads the pointer file and
    executes the STALE order - one user-visible symptom was phantom trades
    right after a restart.  The executor's janitor sweeps these too, but only
    after 15 s AND it used to react to a sweep by RESTARTING the just-booted
    terminal (restart storm); purging at launch removes the problem at the
    source while the EA cannot possibly have consumed anything yet."""
    files_dir = TERMINALS[inst]["dir"] / "MQL5" / "Files"
    try:
        if not files_dir.exists():
            return
        for pat in ("exec_in.*.txt", "exec_next*.txt", "exec_next*.tmp"):
            for p in files_dir.glob(pat):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError as exc:
        log.debug(f"terminal {inst}: exec purge skipped: {exc}")


def launch_terminal(inst: int = 1, jitter: float | None = None,
                    with_login: bool = True) -> None:
    """Start a terminal instance in portable mode so its data (and the
    common.ini that holds AutoTrading=1) always lives in the install dir -
    never a per-user AppData data folder that defaults to autotrading OFF
    (the root cause of retcode 10027 on first orders).

    with_login=False boots WITHOUT the auto-login block (bridge.py's
    pre-login launch - the operator is about to type fresh credentials;
    the stored/old account must not be silently connected).

    ANTI-DETECT launch cadence: a small randomized delay (jitter)
    de-syncs the two terminals' boot so their logins/connection bursts do
    not look machine-simultaneous to the broker.  Set MT5_NO_LAUNCH_JITTER=1
    (or pass jitter=0) to disable - CI and tests do.

    CRITICAL: scrubs any saved login from common.ini BEFORE launch so the
    terminal CANNOT fall back to a previous account - it MUST use the
    start config credentials."""
    scrub_common_ini_login(inst)        # REMOVE any saved credentials first
    repair_common_ini(inst)             # heal torn/corrupted ini BEFORE boot
    _write_start_cfg(inst, with_login=with_login)
    ensure_autotrading(inst)            # kill 10027 before the terminal boots
    scrub_terminal2_credentials()       # stale auto-login out of the copied common.ini
    sanitize_charts(inst)               # ZERO restored charts - config opens the ONE window
    force_profile(inst)                 # boot profile is ALWAYS the sanitized one
    ensure_bridge_template(inst)        # Template=Bridge must exist (expertmode=1)
    pin_window_geometry(inst)           # small window top-left, never fullscreen
    purge_exec_channel(inst)            # no stale order commands can fire on boot
    if jitter is None:
        jitter = 0.0 if os.getenv("MT5_NO_LAUNCH_JITTER") else random.uniform(0.05, 0.6)
    if jitter > 0:
        time.sleep(jitter)
    d = TERMINALS[inst]["dir"]
    env = os.environ.copy()
    env["WINEPREFIX"] = str(WINEPREFIX)
    env["WINEDEBUG"] = "-all"
    # esync/fsync: wine's fast event/futex sync primitives - cuts timer
    # wake-up latency (the EA's 1 ms tick + OnTick order pickup depend on
    # it).  Ignored gracefully by wine builds without support.
    env["WINEESYNC"] = "1"
    env["WINEFSYNC"] = "1"
    cfg_win = str(TERMINALS[inst]["ini"].relative_to(WINEPREFIX / "drive_c")).replace("/", "\\")
    cmd = [wine_bin(), str(d / "terminal64.exe"), "/portable",
           f"/config:C:\\{cfg_win}"]
    subprocess.Popen(
        cmd, cwd=d, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_terminal(inst: int = 1) -> bool:
    exe = TERMINALS[inst]["dir"] / "terminal64.exe"
    if not exe.exists():
        log.error(f"{exe} does not exist." +
              ("" if inst == 1 else
               "  Create it once with:  python monitor.py --setup2"))
        return False
    if term_running(inst):
        return True
    log.info(f"MT5 terminal {inst} not running - starting it...")
    launch_terminal(inst)
    deadline = time.time() + 120
    while time.time() < deadline:
        if term_running(inst):
            log.info(f"terminal {inst} is up")
            return True
        time.sleep(0.5)
    log.error(f"terminal {inst} did not start within 120 s - launch it manually")
    return False


def stop_terminal(inst: int = 1) -> bool:
    """Stop ONE terminal instance - never touches the other one."""
    pat = TERMINALS[inst]["dir"].name + "/terminal64.exe"
    try:
        subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
    except Exception:
        pass
    # fast path: wine tear-down usually completes in ~1-2 s (0.2 s polls)
    for _ in range(25):
        if not term_running(inst):
            return True
        time.sleep(0.2)
    try:
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(2)
    return not term_running(inst)


def restart_terminal(inst: int = 1) -> bool:
    """Stop + relaunch ONE terminal instance (EA auto-attaches)."""
    log.info(f"restarting MT5 terminal {inst}...")
    stop_terminal(inst)
    if term_running(inst):
        log.error(f"could not stop terminal {inst} - close it manually and retry")
        return False
    launch_terminal(inst)
    deadline = time.time() + 120
    while time.time() < deadline:
        if term_running(inst):
            log.info(f"terminal {inst} is up")
            return True
        time.sleep(0.5)
    log.error(f"terminal {inst} did not start")
    return False


def setup_terminal2() -> bool:
    """Create the second MT5 install (one-time copy) for account2.

    Excludes the heavy/skippable folders (price history re-downloads, logs,
    tester data) and the live bridge CSVs (being rewritten while we copy).
    Profiles and Config ARE copied so charts exist for the EA auto-attach
    and the MetaQuotes-Demo server is known.
    """
    d2 = TERMINALS[2]["dir"]
    if (d2 / "terminal64.exe").exists():
        return True
    if not MT5_DIR.exists():
        log.error("primary MT5 install not found - nothing to copy")
        return False
    log.info("creating second MT5 install (one-time copy)...")
    t0 = time.time()
    shutil.copytree(
        MT5_DIR, d2,
        ignore=shutil.ignore_patterns("logs", "Logs", "Tester", "Bases",
                                      "*.log", "Temp", "temp", "Files",
                                      "liveupdate"),
    )
    log.info(f"copy done in {time.time() - t0:.0f}s")
    return True


def install_script(inst: int = 1) -> None:
    """Copy SpotDump.mq5 into the terminal's MQL5/Experts; compile if changed."""
    experts_dir = TERMINALS[inst]["dir"] / "MQL5" / "Experts"
    experts_dir.mkdir(parents=True, exist_ok=True)
    if not SCRIPT_SRC.exists():
        log.error(f"{SCRIPT_SRC} not found next to spot.py")
        return
    dst = experts_dir / "SpotDump.mq5"
    changed = True
    if dst.exists():
        changed = dst.read_text(encoding="cp1252", errors="replace") != SCRIPT_SRC.read_text()
    # also recompile when the compiled .ex5 is missing or older than the source
    stale_ex5 = False
    ex5 = dst.with_suffix(".ex5")
    if ex5.exists() and dst.exists():
        stale_ex5 = ex5.stat().st_mtime < dst.stat().st_mtime
    elif not ex5.exists():
        stale_ex5 = True
    if not changed and not stale_ex5:
        return
    shutil.copy2(SCRIPT_SRC, dst)
    compile_mq5(dst, inst)


def compile_mq5(dst_mq5: Path, inst: int = 1) -> bool:
    """Compile via MetaEditor64.exe (works when cwd is the MT5 folder).

    MetaEditor is a GUI app that Wine renders as an X11 window.  To avoid
    visible windows popping up:
      * WINEDEBUG=-all  kills all Wine console/debug windows
      * /skin:0         tells MetaEditor to use the minimal skinless mode
      * start /wait      keeps cmd quiet until MetaEditor exits
    """
    if not os.path.exists(wine_bin()):
        return False
    mt5_dir = TERMINALS[inst]["dir"]
    rel = dst_mq5.relative_to(mt5_dir).as_posix()
    win_path = rel.replace("/", "\\")
    env = os.environ.copy()
    env["WINEPREFIX"] = str(WINEPREFIX)
    env["WINEDEBUG"] = "-all"
    t0 = time.time()
    ex5 = dst_mq5.with_suffix(".ex5")
    old_mtime = ex5.stat().st_mtime if ex5.exists() else 0.0
    try:
        subprocess.run(
            [wine_bin(), "cmd", "/c",
             f"start /wait MetaEditor64.exe /skin:0 /portable /compile:{win_path} /log"],
            cwd=mt5_dir, env=env, timeout=45,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error(f"compile attempt failed: {exc}")
        return False
    # MetaEditor under Wine can exit before the .ex5 is fully written:
    # wait until it is newer than when we started (max 15 s).
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if ex5.exists() and ex5.stat().st_mtime > old_mtime:
                break
        except OSError:
            pass
        time.sleep(0.5)
    log_path = dst_mq5.with_suffix(".log")
    if log_path.exists():
        text = log_path.read_text(encoding="utf-16", errors="replace")
        result = [l for l in text.splitlines() if "Result" in l or "error" in l.lower()]
        if result:
            log.info(" | ".join(result))
    try:
        return ex5.exists() and ex5.stat().st_mtime > old_mtime
    except OSError:
        return False


# --------------------------------------------------------------------------
# bridge file readers
# --------------------------------------------------------------------------

def _newest_text(paths: list[Path]) -> str | None:
    files = [p for p in paths if p.exists()]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    try:
        return files[0].read_text(encoding="cp1252", errors="replace")
    except OSError:
        return None


def read_spots(max_age: float = 0.1) -> dict[str, tuple[str, str, str]]:
    """Every symbol ANY terminal has a quote for (v1.40+ EA dumps the whole
    Market Watch, not just the classic trio).

    All terminals' spots.csv files are MERGED oldest->newest so a fresher
    file's quote wins per symbol.  Shared 100 ms TTL cache: the EA rewrites
    spots.csv every 50 ms, so callers polling at 10-20 Hz (ms.py, hft.py,
    web APIs) share one read instead of each reparsing.  Pass max_age=0 to
    force a refresh.  (Reading ONE 'newest' file used to lose every symbol
    the other terminal had selected - and a file caught mid-rewrite turned
    into a bogus 'no live quote' for live orders.)"""
    global _SPOTS_CACHE
    now = time.monotonic()
    if max_age > 0 and _SPOTS_CACHE and now - _SPOTS_CACHE[0] < max_age:
        return _SPOTS_CACHE[1]
    spots: dict[str, tuple[str, str, str]] = {}
    files: list[Path] = []
    for p in spots_csv_paths():
        try:
            if p.exists():
                p.stat()                  # touch mtime for the sort below
                files.append(p)
        except OSError:
            continue
    files.sort(key=lambda p: p.stat().st_mtime)   # oldest first: newest wins
    for p in files:
        try:
            raw = p.read_text(encoding="cp1252", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            parts = [q.strip() for q in line.split("\t")]
            if len(parts) < 4:
                # fallback: tab-less rows from legacy SpotDump builds
                # (e.g. "EURUSD1.156791.156802026.09.14 08:50:49")
                s = line.strip()
                for sym, rx in _NOTAB_RES.items():
                    m = rx.match(s)
                    if m:
                        parts = [sym, m.group(1), m.group(2), m.group(3)]
                        break
                else:
                    continue
            try:
                if float(parts[1]) > 0.0:
                    spots[parts[0]] = (parts[1], parts[2], parts[3])
            except ValueError:
                continue
    for s in PAIRS:                      # classic trio always present
        spots.setdefault(s, ("", "", ""))
    _SPOTS_CACHE = (now, spots)
    return spots


# --------------------------------------------------------------------------
# M1 candles (ms.py chart + hft.py features)
# --------------------------------------------------------------------------

def _parse_msc(s: str) -> int:
    """'2026.09.14 08:50:49.123' (EA MscToTime) -> epoch seconds."""
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return 0


# candles.csv is rewritten each M1 bar; memoizing every snapshot we ever
# saw stitches a continuous history (no gaps between rewrites).
CANDLE_MEMO: dict[str, dict[int, dict]] = {}


def candles(symbol: str = "EURUSD", limit: int = 300) -> list[dict]:
    """Stitched M1 history for `symbol`, OLDEST first, up to `limit` bars.
    Each bar: {'t': epoch_minutes, 'o','h','l','c','v'}.
    The forming bar appears as soon as the terminal opens it and is
    corrected by the next snapshot."""
    memo = CANDLE_MEMO.setdefault(symbol, {})
    raw = _newest_text(candles_csv_paths())
    if raw:
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 7 or parts[0] != symbol:
                continue
            try:
                t = _parse_msc(parts[1]) // 60
                if not t:
                    continue
                memo[t] = {"t": t, "o": float(parts[2]), "h": float(parts[3]),
                           "l": float(parts[4]), "c": float(parts[5]),
                           "v": int(float(parts[6]))}
            except ValueError:
                continue
    return [memo[k] for k in sorted(memo)[-limit:]]


def read_trades(login: str | None = None) -> list[dict]:
    """Parse trades.csv written by the EA (tab separated, per-position rows).

    With `login` given, only the terminal whose header reports that login
    is read; otherwise the most recently written one (legacy behaviour).
    """
    if login:
        term = pick_terminal(login)
        if not term:
            return []
        try:
            raw = term["trades_path"].read_text(encoding="cp1252", errors="replace")
        except OSError:
            return []
    else:
        raw = _newest_text(trades_csv_paths())
        if raw is None:
            return []
    rows: list[dict] = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 2 and parts[0] == "NONE":
            continue  # header row
        if len(parts) >= 11:
            rows.append({
                "ticket": parts[0], "symbol": parts[1], "side": parts[2],
                "volume": parts[3], "open": parts[4], "cur": parts[5],
                "pl": parts[6], "swap": parts[7], "magic": parts[8],
                "time": parts[9], "comment": parts[10],
            })
    return rows


# --------------------------------------------------------------------------
# spot-only rendering (positions/account state are monitor.py's job)
# --------------------------------------------------------------------------

def render_line(sym: str, bid: str, ask: str, ts: str, prev: dict) -> str:
    digits = 2 if sym == "XAUUSD" else (3 if sym == "XAGUSD" else 5)
    try:
        b, a = float(bid), float(ask)
        b_prev = float(prev[sym][0]) if prev.get(sym, ("",))[0] else b
        arrow = "\u25b2" if b > b_prev else ("\u25bc" if b < b_prev else " ")
        return (f"{COLORS.get(sym, CYAN)}{sym:<8}{RESET} "
                f"{BOLD}{b:>10.{digits}f}{RESET} / {a:>10.{digits}f}{RESET} "
                f"{arrow}  {DIM}{ts}{RESET}")
    except ValueError:
        return f"{sym:<8} {bid or 'no data'} / {ask or 'no data'}  {ts}"


def render_dashboard(spots: dict, prev: dict) -> str:
    order = [s for s in PAIRS if s in spots] + sorted(
        s for s in spots if s not in PAIRS)
    out = [f"{BOLD}FX SPOT{RESET} {DIM}live - every symbol with a quote "
           f"({len(order)} symbols){RESET}  {DIM}{dt.datetime.now():%H:%M:%S}{RESET}", ""]
    for sym in order:
        bid, ask, ts = spots[sym]
        if bid:
            out.append(render_line(sym, bid, ask, ts, prev))
        else:
            out.append(f"{COLORS.get(sym, CYAN)}{sym:<8}{RESET} {DIM}no data{RESET}")
    return "\n".join(out)


def print_accounts() -> None:
    accs = read_accounts()
    print(f"{BOLD}acc.env{RESET}")
    for n in (1, 2):
        a = accs.get(n, {})
        print(f"  account{n}  login={a.get('login', '?'):<12} "
              f"server={a.get('server', '?')}")
    print(f"\n{BOLD}terminals / bridge files{RESET}")
    terms = scan_terminals()
    if not terms:
        print(f"  {DIM}no trades.csv found - run spot.py --restart{RESET}")
    for t in terms:
        h = t["header"]
        rel = t["root"]
        try:
            rel = t["root"].relative_to(WINEPREFIX)
        except ValueError:
            pass
        print(f"  {rel}  mtime={t['mtime']:.0f}  "
              f"login={h.get('login', '? (EA < v1.20)')}  server={h.get('server', '?')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Live FX spot-only dashboard via MT5 (Wine)")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--list", action="store_true",
                    help="print one frame and exit (alias of --once)")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="refresh interval seconds (default 0.1)")
    ap.add_argument("--restart", action="store_true",
                    help="restart the terminal (re-attaches the SpotDump EA)")
    ap.add_argument("--accounts", action="store_true",
                    help="show acc.env entries and which account each terminal is in")
    args = ap.parse_args()

    if args.accounts:
        print_accounts()
        return 0

    if args.list:
        args.once = True

    log.info("FX Spot Dashboard starting (spot prices only - positions are monitor.py's job)")

    if args.restart:
        if not restart_terminal():
            return 1
    elif not ensure_terminal():
        return 1
    install_script()

    if not any(p.exists() for p in compiled_paths()):
        log.error("SpotDump.ex5 not found in MQL5/Experts - compilation failed.")
        return 1

    log.info("waiting for the EA bridge...")
    if not wait_for_bridge():
        log.error("EA bridge is not producing data - SpotDump is not attached to any chart.")
        log.error("Run:  python spot.py --restart   (auto-attaches it on relaunch)")
        return 1

    prev: dict[str, tuple[str, str, str]] = {}
    first = True
    try:
        while True:
            spots = read_spots()
            frame = render_dashboard(spots, prev)
            if args.once:
                print(frame)
                break
            if first:
                sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                first = False
            else:
                sys.stdout.write("\033[H" + frame + "\033[J")
            sys.stdout.flush()
            prev = spots
            time.sleep(max(args.interval, 0.05))
    except KeyboardInterrupt:
        log.info("spot dashboard stopped")
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
