#!/usr/bin/env python3.12

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

_NOTAB_RES = {
    "EURUSD": re.compile(r"^EURUSD(\d+\.\d{5})(\d+\.\d{5})\s*(.*)$"),
    "GBPUSD": re.compile(r"^GBPUSD(\d+\.\d{5})(\d+\.\d{5})\s*(.*)$"),
    "XAUUSD": re.compile(r"^XAUUSD(\d+\.\d{2})(\d+\.\d{2})\s*(.*)$"),
}

def read_accounts() -> dict[int, dict[str, str]]:
    return session.load()

def _scan_data_roots() -> list[Path]:
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

_DATA_ROOTS_CACHE: tuple[float, list[Path]] | None = None
DATA_ROOTS_TTL_S = 5.0

def data_roots() -> list[Path]:
    """Every MQL5 data tree under the prefix (memoized).

    This used to walk two directory trees on EVERY call, and the exec
    path resolution calls it several times per order - pure overhead in
    the hot path.  A 5 s memo removes that cost while still picking up a
    freshly created terminal almost immediately.
    """
    global _DATA_ROOTS_CACHE
    now = time.monotonic()
    if _DATA_ROOTS_CACHE and now - _DATA_ROOTS_CACHE[0] < DATA_ROOTS_TTL_S:
        return list(_DATA_ROOTS_CACHE[1])
    roots = _scan_data_roots()
    _DATA_ROOTS_CACHE = (now, roots)
    return list(roots)

def spots_csv_paths() -> list[Path]:
    return [root / "Files" / "spots.csv" for root in data_roots()]

def trades_csv_paths() -> list[Path]:
    return [root / "Files" / "trades.csv" for root in data_roots()]

def candles_csv_paths() -> list[Path]:
    return [root / "Files" / "candles.csv" for root in data_roots()]

def exec_in_path(inst: int = 1) -> Path:
    return _exec_dir(inst)

def exec_out_path(inst: int = 1) -> Path:
    return _exec_dir(inst) / "exec_out.csv"

_EXEC_DIR_CACHE: dict[int, tuple[float, tuple, Path]] = {}
EXEC_DIR_TTL_S = 2.0

def _exec_dir(inst: int) -> Path:
    """Resolve the MQL5\\Files directory the EA of `inst` writes to.

    pick_terminal() walks every terminal tree, and this was resolved on
    EVERY command (open/close/ping) - measurable per-order latency.  The
    answer only changes when the session login changes, which the cache
    keys on, plus a short TTL as a safety net.
    """
    hint = TERMINALS[inst]["dir"] / "MQL5" / "Files"
    login = str(read_accounts().get(inst, {}).get("login", "")).strip()
    try:
        stamp = (login, session.file_stamp())
    except Exception:
        stamp = (login, None)
    now = time.monotonic()
    cached = _EXEC_DIR_CACHE.get(inst)
    if cached and cached[1] == stamp and now < cached[0]:
        return cached[2]
    resolved = hint
    if login:
        term = pick_terminal(login)
        if term:
            resolved = term["root"] / "Files"
    _EXEC_DIR_CACHE[inst] = (now + EXEC_DIR_TTL_S, stamp, resolved)
    return resolved

def compiled_paths(inst: int | None = None) -> list[Path]:
    if inst:
        return [TERMINALS[inst]["dir"] / "MQL5" / "Experts" / "SpotDump.ex5"]
    return [root / "Experts" / "SpotDump.ex5" for root in data_roots()]

_SPOTS_CACHE: tuple[float, dict] | None = None
# (expiry_monotonic, mtime, size, header)
#
# The expiry MUST be compared against time.monotonic(), never against the
# file's st_mtime.  The old code stored time.monotonic()+0.05 and compared
# that with st_mtime (a wall-clock epoch ~1.7e9), so `cached[0] > mtime` was
# false on every single call: read_header() re-opened and re-parsed the whole
# trades.csv hundreds of times a second.  That was the single biggest source
# of the UI/bridge/executor lag.
_HEADER_CACHE: dict[Path, tuple[float, float, int, dict]] = {}
_TERM_RUNNING_CACHE: dict[int, tuple[float, bool]] = {}
HEADER_CACHE_TTL_S = 0.04
HEADER_MISS_TTL_S = 0.01

def read_header(path: Path) -> dict:
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return {}
    cached = _HEADER_CACHE.get(path)
    if cached:
        expiry, c_mtime, c_size, h = cached
        if c_mtime == mtime and c_size == size and time.monotonic() < expiry:
            return h
    h = _read_header_uncached(path)
    # negative results are cached too (very briefly) so a torn read during an
    # EA rewrite does not stampede the file on every caller
    ttl = HEADER_CACHE_TTL_S if h else HEADER_MISS_TTL_S
    _HEADER_CACHE[path] = (time.monotonic() + ttl, mtime, size, h)
    return h

def _read_header_uncached(path: Path) -> dict:
    if not path.exists():
        return {}
    # a couple of quick re-reads only: a torn read (EA mid-rewrite) is
    # retried almost immediately, and a genuinely absent header is now
    # negative-cached by read_header() so we never block in this loop
    for _ in range(3):
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
                if len(parts) > 15:
                    h["currency"] = parts[6] or ""
                    h["leverage"] = parts[7] or ""
                    h["margin"] = parts[8] or ""
                    h["margin_free"] = parts[9] or ""
                    h["margin_level"] = parts[10] or ""
                    h["profit"] = parts[11] or ""
                    h["broker"] = parts[12] or ""
                    h["holder"] = parts[13] or ""
                    h["margin_mode"] = parts[14] or ""
                    h["trade_mode"] = parts[15] or ""
                if len(parts) > 18:
                    h["trade_allowed"] = parts[16] or ""
                    h["mql_allowed"] = parts[17] or ""
                    h["account_trade_allowed"] = parts[18] or ""
                return h
        time.sleep(0.01)
    return {}

def scan_terminals() -> list[dict]:
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
    terms = scan_terminals()
    now = time.time()
    for t in sorted(terms, key=lambda t: t["mtime"], reverse=True):
        if (t["header"].get("login") == login
                and now - t["mtime"] < HEADER_PROOF_MAX_AGE_S):
            return t
    if login:
        for inst in (1, 2):
            if session.expected_login(inst) == login:
                hint = TERMINALS[inst]["dir"] / "MQL5"
                for t in terms:
                    if t["root"] == hint:
                        return t
                break
        return None
    return terms[0] if terms else None

def bridge_age(extra_paths: list[Path] | None = None) -> float:
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        if feed_age(inst) < 5:
            return True
        time.sleep(1)
    return False


def _any_bridge_fresh(timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if any(feed_age(inst) < 5 for inst in (1, 2)):
            return True
        time.sleep(0.25)
    return False

def feed_age(inst: int = 1) -> float:
    """Seconds since THIS terminal's EA last wrote a feed file.

    Scoped to the terminal's own install tree.  The old version scanned
    every terminal's files and returned the global newest, so a dead
    terminal still read LIVE while its sibling was up - the supervisor,
    the health endpoint and the dashboard could not see a one-sided
    feed death.  Falls back to the global scan when the install tree
    holds no feed files at all (e.g. a non-portable setup).
    """
    now = time.time()
    cached = _FEED_AGE_CACHE.get(inst)
    if cached and now - cached[0] < 1.0:
        return cached[1]
    try:
        own = TERMINALS[inst]["dir"] / "MQL5" / "Files"
        newest = 0.0
        for name in ("spots.csv", "trades.csv", "candles.csv"):
            try:
                newest = max(newest, (own / name).stat().st_mtime)
            except OSError:
                pass
        if newest:
            age = now - newest
            _FEED_AGE_CACHE[inst] = (now, age)
            return age
    except (KeyError, TypeError):
        pass
    roots = data_roots()
    files: list[Path] = []
    for root in roots:
        files += [root / "Files" / "spots.csv", root / "Files" / "trades.csv",
                  root / "Files" / "candles.csv"]
    newest = 0.0
    for p in files:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    age = now - newest if newest else 999.0
    _FEED_AGE_CACHE[inst] = (now, age)
    return age

_FEED_AGE_CACHE: dict[int, tuple[float, float]] = {}

HEADER_PROOF_MAX_AGE_S = 10.0

def wine_bin() -> str:
    for cand in ("/usr/bin/wine", "/usr/bin/wine64",
                 shutil.which("wine"), shutil.which("wine64"),
                 "/usr/lib/wine/wine64"):
        if cand and os.path.exists(cand):
            return cand
    return "wine"

def wineserver_bin() -> str:
    for ws in (shutil.which("wineserver"), "/usr/lib/wine/wineserver",
               "/usr/bin/wineserver"):
        if ws and os.path.exists(ws):
            return ws
    return "/usr/bin/wineserver"

def term_running(inst: int = 1, max_age: float = 1.0) -> bool:
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

def check_terminal_running() -> bool:
    return term_running(1)

# Legacy XML chart form.  Unused (MT5 .chr/.tpl files use the key=value text
# format, see MINIMAL_TPL in make_bridge_tpl.py) - kept only so nothing that
# references the name breaks.
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
    charts_roots = [TERMINALS[inst]["dir"] / "Profiles" / "Charts",
                    TERMINALS[inst]["dir"] / "MQL5" / "Profiles" / "Charts"]
    for charts_root in charts_roots:
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
            log.debug(f"terminal {inst}: chart sanitize skipped ({charts_root}): {exc}")

def ensure_minimal_profile(inst: int) -> None:
    """Force the boot profile SpotBridgeN to be ONE clean EURUSD chart.

    This is the actual reason terminal 1 could come up cluttered while
    terminal 2 came up minimal: MT5 silently rebuilds a default (busy)
    profile whenever the profile folder is missing or holds no chart
    files, and that state is sticky per install.  We write exactly one
    chart file (chart01.chr) from the same minimalist text the Bridge
    template uses, so BOTH terminals boot identically - one EURUSD chart
    with the SpotDump EA attached and nothing else.
    """
    try:
        import make_bridge_tpl
        tpl = make_bridge_tpl.minimal_chart_text()
    except Exception as exc:
        log.debug(f"terminal {inst}: minimal profile skipped: {exc}")
        return
    for charts_root in (TERMINALS[inst]["dir"] / "Profiles" / "Charts",
                        TERMINALS[inst]["dir"] / "MQL5" / "Profiles" / "Charts"):
        prof = charts_root / f"SpotBridge{inst}"
        try:
            prof.mkdir(parents=True, exist_ok=True)
            for old in prof.glob("chart*.chr"):
                try:
                    old.unlink()
                except OSError:
                    pass
            (prof / "chart01.chr").write_text(tpl, encoding="utf-16")
        except OSError as exc:
            log.debug(f"terminal {inst}: minimal profile write skipped "
                      f"({prof}): {exc}")

def force_profile(inst: int = 1) -> None:
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
    ini = TERMINALS[inst]["ini"]
    try:
        common = ""
        if with_login:
            a = read_accounts().get(inst, {})
            lg = str(a.get("login", "")).strip()
            if lg in ("", "0", "?", "LOGIN") or not lg.isdigit():
                log.warning(f"terminal {inst}: session login {lg!r} is not a "
                            f"plausible account - booting WITHOUT the login block")
                with_login = False
        if with_login:
            # A server MT5 has never seen must be given as an access point
            # (host:port); booting it by bare name silently never connects
            # and the terminal falls back to whatever account it had last.
            server_name = str(a.get("server", ""))
            ap = config.access_point_for(server_name)
            server_field = ap or server_name
            if ap:
                log.info(f"terminal {inst}: booting {server_name} via access "
                         f"point {ap} (server not in MT5's list)")
            common = ("[Common]\r\n"
                      f"Login={a.get('login', '')}\r\n"
                      f"Password={a.get('password', '')}\r\n"
                      f"Server={server_field}\r\n"
                      f"Profile=SpotBridge{inst}\r\n")
        # NOTE: no Symbol/Period under [StartUp] on purpose.  MT5 opens an
        # ADDITIONAL chart for [StartUp] Symbol, which is exactly why the
        # terminals came up with two charts instead of one.  With no Symbol,
        # MT5 opens no extra chart and starts the EA on the first (and only)
        # chart of the profile - one clean chart, no clutter.
        body = ("[Experts]\r\n"
                "AllowLiveTrading=1\r\n"
                "Enabled=1\r\n"
                "Account=0\r\n"
                f"Profile=SpotBridge{inst}\r\n"
                "[StartUp]\r\nExpert=SpotDump.ex5\r\n"
                f"Profile=SpotBridge{inst}\r\n")
        fd = os.open(str(ini), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (common + body).encode("ascii"))
        finally:
            os.close(fd)
        os.chmod(ini, 0o600)
    except OSError:
        pass

def scrub_start_cfg(inst: int) -> None:
    ini = TERMINALS[inst]["ini"]
    try:
        if not ini.exists():
            return
        lines = [l for l in ini.read_text(encoding="ascii", errors="replace").splitlines()
                 if not l.strip().lower().startswith(("login=", "password=", "server="))]
        ini.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    except OSError as exc:
        log.debug(f"terminal {inst}: start-config scrub skipped: {exc}")

_INI_LOCK = threading.Lock()

def _read_ini(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    # MT5 commonly writes UTF-16LE with a single BOM. Some installs/patches
    # double the BOM bytes, which makes raw.decode("utf-16") silently include
    # an extra U+FEFF inside the file and corrupt key/value matching.
    if raw[:2] == b"\xff\xfe":
        body = raw[2:]
        if body[:2] == b"\xff\xfe":
            body = body[2:]
        return body.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-16", errors="replace")
    except (UnicodeError, ValueError):
        return raw.decode("utf-8", errors="replace")

def _write_ini_atomic(path: Path, text: str) -> None:
    with _INI_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".ini.tmp")
            tmp.write_text(text, encoding="utf-16")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug(f"ini write failed for {path.name}: {exc}")

def _drop_ini_lines(text: str, prefixes: tuple[str, ...]) -> str:
    return "\r\n".join(
        l for l in text.splitlines()
        if not l.strip().lower().startswith(prefixes)) + "\r\n"

def scrub_terminal2_credentials() -> None:
    slot2 = read_accounts().get(2, {})
    if slot2.get("login") and not slot2.get("password"):
        log.debug("terminal 2: wallet-managed login - keeping saved credentials")
        return
    ini = MT5_DIR2 / "Config" / "common.ini"
    if not ini.exists():
        return
    text = _read_ini(ini)
    if not text:
        return
    cleaned = _drop_ini_lines(text, ("login=", "server="))
    if not cleaned.strip():
        return
    cleaned_text = cleaned
    if cleaned_text.startswith("\ufeff"):
        cleaned_text = cleaned_text[len("\ufeff"):]
    cleaned_text = "\ufeff" + cleaned_text
    if cleaned != text:
        _write_ini_atomic(ini, cleaned_text)

def scrub_common_ini_login(inst: int) -> None:
    ini = TERMINALS[inst]["dir"] / "Config" / "common.ini"
    if not ini.exists():
        return
    text = _read_ini(ini)
    if not text:
        return
    cleaned = _drop_ini_lines(text, ("login=", "password=", "server="))
    if not cleaned.strip():
        return
    cleaned_text = cleaned
    if cleaned_text.startswith("\ufeff"):
        cleaned_text = cleaned_text[len("\ufeff"):]
    cleaned_text = "\ufeff" + cleaned_text
    if cleaned != text:
        _write_ini_atomic(ini, cleaned_text)

def repair_common_ini(inst: int) -> None:
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
                continue
            key = s.split("=", 1)[0].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
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
    repaired = body + "\r\n"
    if repaired.strip() + "\r\n" != text.strip() + "\r\n":
        _write_ini_atomic(ini, repaired)
        log.info(f"terminal {inst}: repaired corrupted {ini.name}")

def ensure_autotrading(inst: int = 1) -> bool:
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
    try:
        import make_bridge_tpl
        make_bridge_tpl.install()
    except Exception as exc:
        log.debug(f"terminal {inst}: Bridge template ensure skipped: {exc}")

# Small-window geometry for both terminals (side by side, never fullscreen).
# T1 -> (10,10)-(460,340)  ~= 450x330, T2 -> (480,10)-(930,340).
# Terminal 1 was coming up maximized (Type=3, RSave=1924) while terminal 2
# stayed small (Type=1) - Type/Maximized were never pinned, so the maximized
# state was sticky across restarts.
WIN_GEOM = {1: (10, 10, 460, 340), 2: (480, 10, 930, 340)}

def window_geom(inst: int) -> tuple[int, int, int, int]:
    return WIN_GEOM.get(inst, WIN_GEOM[1])

def pin_window_geometry(inst: int) -> None:
    ini = TERMINALS[inst]["dir"] / "Config" / "terminal.ini"
    L, T, R, B = window_geom(inst)
    W, H = R - L, B - T
    try:
        text = _read_ini(ini)
        if not text:
            return
        lines = text.splitlines()
        out: list[str] = []
        in_win = False
        # Force everything that can maximize/fullscreen the window off,
        # and pin a small geometry.  Type=3 is MT5's maximized marker
        # (observed: T1 Type=3 = fullscreen 1920x1054, T2 Type=1 = small).
        replaced = {"Left": False, "Top": False, "Right": False,
                    "Bottom": False, "Fullscreen": False, "Type": False,
                    "Maximized": False, "Minimized": False,
                    "LSave": False, "TSave": False,
                    "RSave": False, "BSave": False}
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
                if key == "Type":
                    out.append("Type=1")
                    replaced["Type"] = True
                    continue
                if key == "Maximized":
                    out.append("Maximized=0")
                    replaced["Maximized"] = True
                    continue
                if key == "Minimized":
                    out.append("Minimized=0")
                    replaced["Minimized"] = True
                    continue
                if key == "Left":
                    out.append(f"Left={L}")
                    replaced["Left"] = True
                    continue
                if key == "Top":
                    out.append(f"Top={T}")
                    replaced["Top"] = True
                    continue
                if key == "Right":
                    out.append(f"Right={R}")
                    replaced["Right"] = True
                    continue
                if key == "Bottom":
                    out.append(f"Bottom={B}")
                    replaced["Bottom"] = True
                    continue
                if key == "LSave":
                    out.append(f"LSave={L}")
                    replaced["LSave"] = True
                    continue
                if key == "TSave":
                    out.append(f"TSave={T}")
                    replaced["TSave"] = True
                    continue
                if key == "RSave":
                    out.append(f"RSave={R}")
                    replaced["RSave"] = True
                    continue
                if key == "BSave":
                    out.append(f"BSave={B}")
                    replaced["BSave"] = True
                    continue
            out.append(ln)
        if not all(replaced.values()):
            new_lines: list[str] = []
            injected = False
            for ln in out:
                new_lines.append(ln)
                if not injected and ln.strip().lower() == "[window]":
                    if not replaced["Fullscreen"]:
                        new_lines.append("Fullscreen=0")
                    if not replaced["Type"]:
                        new_lines.append("Type=1")
                    if not replaced["Maximized"]:
                        new_lines.append("Maximized=0")
                    if not replaced["Left"]:
                        new_lines.append(f"Left={L}")
                    if not replaced["Top"]:
                        new_lines.append(f"Top={T}")
                    if not replaced["Right"]:
                        new_lines.append(f"Right={R}")
                    if not replaced["Bottom"]:
                        new_lines.append(f"Bottom={B}")
                    if not replaced["LSave"]:
                        new_lines.append(f"LSave={L}")
                    if not replaced["TSave"]:
                        new_lines.append(f"TSave={T}")
                    if not replaced["RSave"]:
                        new_lines.append(f"RSave={R}")
                    if not replaced["BSave"]:
                        new_lines.append(f"BSave={B}")
                    injected = True
            # no [Window] section at all - append one
            if not injected:
                new_lines.append("[Window]")
                new_lines.append("Fullscreen=0")
                new_lines.append("Type=1")
                new_lines.append("Maximized=0")
                new_lines.append(f"Left={L}")
                new_lines.append(f"Top={T}")
                new_lines.append(f"Right={R}")
                new_lines.append(f"Bottom={B}")
            out = new_lines
        _write_ini_atomic(ini, "\r\n".join(out) + "\r\n")
        log.debug(f"terminal {inst}: window pinned to {W}x{H} at ({L},{T}) Type=1")
    except OSError as exc:
        log.debug(f"terminal {inst}: window pin skipped: {exc}")

def purge_exec_channel(inst: int) -> None:
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

def _xdo_env() -> tuple[str | None, dict]:
    xdo = shutil.which("xdotool")
    if not xdo:
        return None, {}
    display = (os.environ.get("MT5_DISPLAY")
               or os.environ.get("DISPLAY") or "").strip()
    if not display:
        display = ":1"
    return xdo, dict(os.environ, DISPLAY=display)


def _xdo(wid: str, *args: str, env: dict, timeout: float = 3.0) -> bool:
    xdo, _ = _xdo_env()
    if not xdo:
        return False
    try:
        subprocess.run([xdo, *args, wid],
                       capture_output=True, timeout=timeout, env=env)
        return True
    except Exception:
        return False


def _list_window_ids(env: dict) -> list[str]:
    xdo, _ = _xdo_env()
    if not xdo:
        return []
    try:
        out = subprocess.run([xdo, "search", "--name", ""],
                             capture_output=True, text=True,
                             timeout=5, env=env).stdout.split()
        return out
    except Exception:
        return []


def _window_name(wid: str, env: dict) -> str:
    xdo, _ = _xdo_env()
    if not xdo:
        return ""
    try:
        return subprocess.run([xdo, "getwindowname", wid],
                              capture_output=True, text=True,
                              timeout=3, env=env).stdout.strip()
    except Exception:
        return ""


def _is_mt5_name(name: str) -> bool:
    if not name:
        return False
    keys = ("MetaTrader", "MetaQuotes", "HFMarkets", "HFM ", " - Hedge",
            "Demo Account", "Live")
    return any(k in name for k in keys)


def shrink_mt5_window(wid: str, L: int, T: int, W: int, H: int,
                      env: dict, do_minimize: bool = True) -> bool:
    """Force one window small: unmaximize -> resize -> move -> minimize.

    Returns True if any step succeeded.  Never raises.
    """
    xdo, _ = _xdo_env()
    if not xdo:
        return False
    ok = False
    try:
        subprocess.run([xdo, "windowunmaximize", wid],
                       capture_output=True, timeout=3, env=env)
        ok = True
    except Exception:
        pass
    for args in (["windowsize", wid, str(W), str(H)],
                 ["windowmove", wid, str(L), str(T)]):
        try:
            subprocess.run([xdo, *args],
                           capture_output=True, timeout=3, env=env)
            ok = True
        except Exception:
            pass
    if do_minimize:
        try:
            subprocess.run([xdo, "windowminimize", wid],
                           capture_output=True, timeout=3, env=env)
            ok = True
        except Exception:
            pass
    return ok


def _enforce_small_windows_once(inst: int, do_minimize: bool = True) -> int:
    """One pass: shrink terminal `inst`'s window(s) to its small geometry.

    Matches by login/server title first so T1 and T2 get their own
    side-by-side slots.  Falls back to any MT5 window when the title does
    not carry the login yet (still booting).  Returns windows handled.
    """
    xdo, env = _xdo_env()
    if not xdo:
        return 0
    L, T, R, B = window_geom(inst)
    W, H = R - L, B - T
    slot = read_accounts().get(inst, {})
    needles = [str(slot.get(k, "")).strip()
               for k in ("login", "server") if str(slot.get(k, "")).strip()]
    handled = 0
    for wid in _list_window_ids(env):
        name = _window_name(wid, env)
        if not name:
            continue
        match = any(n in name for n in needles) if needles else False
        if not match:
            continue
        if shrink_mt5_window(wid, L, T, W, H, env, do_minimize):
            handled += 1
    if handled:
        return handled
    # boot-phase fallback: no login in the title yet - shrink any MT5
    # window so it never sits fullscreen while we wait for login
    for wid in _list_window_ids(env):
        name = _window_name(wid, env)
        if _is_mt5_name(name):
            if shrink_mt5_window(wid, L, T, W, H, env, do_minimize):
                handled += 1
                break  # one per pass; next pass catches the other terminal
    return handled


def _minimize_mt5_windows(inst: int) -> int:
    """Kept for compatibility - now shrinks to small AND minimizes."""
    keep = os.getenv("MT5_KEEP_WINDOWS", "") == "1"
    return _enforce_small_windows_once(inst, do_minimize=not keep)


def shrink_all_mt5_windows(do_minimize: bool = True) -> int:
    """Shrink EVERY MT5 window to small side-by-side slots.

    Used as a safety net after boot: T1 -> slot 1, T2 -> slot 2, so even
    windows we could not attribute by title end up small, never fullscreen.
    """
    xdo, env = _xdo_env()
    if not xdo:
        return 0
    wids = [w for w in _list_window_ids(env) if _is_mt5_name(_window_name(w, env))]
    # stable order so T1/T2 mapping does not flip between passes
    wids.sort(key=int, reverse=False)
    handled = 0
    for i, wid in enumerate(wids[:2]):
        inst = i + 1
        L, T, R, B = window_geom(inst)
        if shrink_mt5_window(wid, L, T, R - L, B - T, env, do_minimize):
            handled += 1
    return handled

def minimize_terminal(inst: int, tries: int = 45,
                      delay: float = 1.0) -> None:
    """Shrink terminal `inst` to its small slot as soon as it appears.

    The MT5 window shows up seconds after launch and can re-maximize on
    login, so this retries for ~45 s and re-pins a few times even after
    the first hit.  Purely cosmetic - never raises, never blocks.
    """
    keep = os.getenv("MT5_KEEP_WINDOWS", "") == "1"

    def _worker() -> None:
        hits = 0
        for i in range(max(1, tries)):
            try:
                if _enforce_small_windows_once(inst, do_minimize=not keep):
                    hits += 1
                    log.debug(f"terminal {inst}: window shrunk small "
                              f"({hits}x)")
                    if hits >= 3:
                        return
                elif hits:
                    # window was small, disappeared (restart?) - keep watching
                    pass
            except Exception:
                pass
            time.sleep(delay)
        # final safety net: make sure BOTH terminals are small, never
        # fullscreen, even if title matching missed one
        try:
            shrink_all_mt5_windows(do_minimize=not keep)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True,
                     name=f"minimize-t{inst}").start()

def launch_terminal(inst: int = 1, jitter: float | None = None,
                    with_login: bool = True) -> None:
    slot = read_accounts().get(inst, {})
    slot_login = str(slot.get("login", "")).strip()
    if slot_login in ("", "0", "?", "LOGIN") or not slot_login.isdigit():
        slot = {}
    wallet_reconnect = bool(with_login and slot.get("login")
                            and not slot.get("password"))
    if wallet_reconnect:
        log.info(f"terminal {inst}: no stored password - booting without "
                 f"login block (terminal wallet reconnects account "
                 f"{slot.get('login')})")
        with_login = False
    if not wallet_reconnect:
        scrub_common_ini_login(inst)
    repair_common_ini(inst)
    _write_start_cfg(inst, with_login=with_login)
    ensure_autotrading(inst)
    scrub_terminal2_credentials()
    sanitize_charts(inst)
    ensure_minimal_profile(inst)
    force_profile(inst)
    ensure_bridge_template(inst)
    pin_window_geometry(inst)
    purge_exec_channel(inst)
    if jitter is None:
        jitter = 0.0 if os.getenv("MT5_NO_LAUNCH_JITTER") else random.uniform(0.05, 0.6)
    if jitter > 0:
        time.sleep(jitter)
    d = TERMINALS[inst]["dir"]
    env = os.environ.copy()
    env["WINEPREFIX"] = str(WINEPREFIX)
    env["WINEDEBUG"] = "-all"
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
    # ALWAYS enforce the small window (both terminals).  MT5_KEEP_WINDOWS=1
    # only skips the minimize step - the resize-to-small still runs so no
    # terminal ever sits fullscreen covering the screen.
    minimize_terminal(inst)

def server_known(inst: int, server: str) -> bool:
    """Has this terminal's MT5 install ever seen trade server `server`?

    MT5 keeps one directory per known trade server under Bases/.  Booting
    an unknown server BY NAME silently never connects - the terminal just
    keeps its last account (seen with FxPro-MT5: 90 s burn, still on the
    old login).  Callers must then use an access point (host:port, which
    resolves without prior knowledge) or onboard the server with one
    manual login inside MT5 followed by ADOPT.
    """
    name = str(server or "").strip()
    if not name:
        return False
    try:
        if session._looks_like_access_point(name):
            return True
    except Exception:
        pass
    try:
        inst_dir = TERMINALS[inst]["dir"]
        bases = inst_dir / "Bases"
        if not bases.is_dir():
            # wine masks case; the second install uses lowercase "bases"
            bases = inst_dir / "bases"
    except (KeyError, TypeError):
        return True                      # cannot tell - do not block
    try:
        if not bases.is_dir():
            return True                  # cannot tell - do not block
        if (bases / name).is_dir():
            return True
        low = name.lower()
        return any(p.name.lower() == low
                   for p in bases.iterdir() if p.is_dir())
    except OSError:
        return True                      # cannot tell - do not block

def _bases_dirs() -> list:
    """All per-install Bases dirs (case differs between installs)."""
    out = []
    for inst in (1, 2):
        try:
            d = TERMINALS[inst]["dir"]
        except (KeyError, TypeError):
            continue
        for cand in (d / "Bases", d / "bases"):
            try:
                if cand.is_dir():
                    out.append(cand)
                    break
            except OSError:
                continue
    return out

def server_known_anywhere(server: str) -> bool:
    """Has ANY terminal install on this box ever seen `server`?

    Used to grade unknown-server logins: known here -> go; known only
    on the sibling install -> attempt anyway with a warning (MT5 may
    still resolve it); known nowhere and no access point -> fail fast.
    """
    name = str(server or "").strip()
    if not name:
        return False
    try:
        if session._looks_like_access_point(name):
            return True
    except Exception:
        pass
    low = name.lower()
    try:
        for bases in _bases_dirs():
            try:
                if (bases / name).is_dir():
                    return True
                if any(p.name.lower() == low
                       for p in bases.iterdir() if p.is_dir()):
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False

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
    pat = TERMINALS[inst]["dir"].name + "/terminal64.exe"
    try:
        subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
    except Exception:
        pass
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
    experts_dir = TERMINALS[inst]["dir"] / "MQL5" / "Experts"
    experts_dir.mkdir(parents=True, exist_ok=True)
    if not SCRIPT_SRC.exists():
        log.error(f"{SCRIPT_SRC} not found next to spot.py")
        return
    dst = experts_dir / "SpotDump.mq5"
    changed = True
    if dst.exists():
        changed = dst.read_text(encoding="cp1252", errors="replace") != SCRIPT_SRC.read_text()
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
    if not os.path.exists(wine_bin()):
        return False
    mt5_dir = TERMINALS[inst]["dir"]
    rel = dst_mq5.relative_to(mt5_dir).as_posix()
    win_path = rel.replace("/", "\\")
    env = os.environ.copy()
    env["WINEPREFIX"] = str(WINEPREFIX)
    env["WINEDEBUG"] = "-all"
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
    global _SPOTS_CACHE
    now = time.monotonic()
    if max_age > 0 and _SPOTS_CACHE and now - _SPOTS_CACHE[0] < max_age:
        return _SPOTS_CACHE[1]
    spots: dict[str, tuple[str, str, str]] = {}
    files: list[Path] = []
    for p in spots_csv_paths():
        try:
            if p.exists():
                p.stat()
                files.append(p)
        except OSError:
            continue
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            raw = p.read_text(encoding="cp1252", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            parts = [q.strip() for q in line.split("\t")]
            if len(parts) < 4:
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
    for s in PAIRS:
        spots.setdefault(s, ("", "", ""))
    _SPOTS_CACHE = (now, spots)
    return spots

def _parse_msc(s: str) -> int:
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return 0

CANDLE_MEMO: dict[str, dict[int, dict]] = {}

def candles(symbol: str = "EURUSD", limit: int = 300) -> list[dict]:
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
            continue
        if len(parts) >= 11:
            rows.append({
                "ticket": parts[0], "symbol": parts[1], "side": parts[2],
                "volume": parts[3], "open": parts[4], "cur": parts[5],
                "pl": parts[6], "swap": parts[7], "magic": parts[8],
                "time": parts[9], "comment": parts[10],
            })
    return rows

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
