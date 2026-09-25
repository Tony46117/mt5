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

def data_roots() -> list[Path]:
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
    return [root / "Files" / "spots.csv" for root in data_roots()]

def trades_csv_paths() -> list[Path]:
    return [root / "Files" / "trades.csv" for root in data_roots()]

def candles_csv_paths() -> list[Path]:
    return [root / "Files" / "candles.csv" for root in data_roots()]

def exec_in_path(inst: int = 1) -> Path:
    return _exec_dir(inst)

def exec_out_path(inst: int = 1) -> Path:
    return _exec_dir(inst) / "exec_out.csv"

def _exec_dir(inst: int) -> Path:
    login = read_accounts().get(inst, {}).get("login", "")
    if login:
        term = pick_terminal(login)
        if term:
            return term["root"] / "Files"
    return TERMINALS[inst]["dir"] / "MQL5" / "Files"

def compiled_paths(inst: int | None = None) -> list[Path]:
    if inst:
        return [TERMINALS[inst]["dir"] / "MQL5" / "Experts" / "SpotDump.ex5"]
    return [root / "Experts" / "SpotDump.ex5" for root in data_roots()]

_SPOTS_CACHE: tuple[float, dict] | None = None
_HEADER_CACHE: dict[Path, tuple[float, int, int, dict]] = {}
_TERM_RUNNING_CACHE: dict[int, tuple[float, bool]] = {}

def read_header(path: Path) -> dict:
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
                if len(parts) > 15:
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
                if len(parts) > 18:
                    h["trade_allowed"] = parts[16]
                    h["mql_allowed"] = parts[17]
                    h["account_trade_allowed"] = parts[18]
                return h
        time.sleep(0.05)
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

def feed_age(inst: int = 1) -> float:
    now = time.time()
    cached = _FEED_AGE_CACHE.get(inst)
    if cached and now - cached[0] < 1.0:
        return cached[1]
    other_root = (TERMINALS[2] if inst == 1 else TERMINALS[1])["dir"] / "MQL5"
    roots = [r for r in data_roots() if r != other_root]
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
        return path.read_text(encoding="utf-16", errors="replace")
    except (OSError, UnicodeError):
        return ""

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
    if cleaned != text:
        _write_ini_atomic(ini, cleaned)

def scrub_common_ini_login(inst: int) -> None:
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
    repaired = "\ufeff" + body + "\r\n"
    if repaired != text:
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

def pin_window_geometry(inst: int) -> None:
    ini = TERMINALS[inst]["dir"] / "Config" / "terminal.ini"
    L, T, R, B = 10 + (inst - 1) * 30, 10 + (inst - 1) * 24, 460, 330
    try:
        text = _read_ini(ini)
        if not text:
            return
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
                    out.append(ln)
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
