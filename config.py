#!/usr/bin/env python3.12

from __future__ import annotations

import os
import re
import sys
import logging
from pathlib import Path
from dataclasses import dataclass, field

_WINEPREFIX_ENV = os.getenv("MT5_WINEPREFIX", "").strip()
WINEPREFIX = Path(_WINEPREFIX_ENV) if _WINEPREFIX_ENV else Path.home() / ".mt5"
MT5_DIR = WINEPREFIX / "drive_c" / "Program Files" / "MetaTrader 5"
MT5_DIR2 = WINEPREFIX / "drive_c" / "Program Files" / "MetaTrader 5-2"
DB_PATH = Path(__file__).resolve().parent / "trades.db"
ENV_FILE = Path(__file__).resolve().parent / "acc.env"
SESSION_FILE = Path(__file__).resolve().parent / "session.json"
SPOT_DUMP_SRC = Path(__file__).resolve().parent / "SpotDump.mq5"
# Operator-supplied trade-server access points ("<server> = host:port" per
# line, or "<server> host port").  MT5 refuses to connect to a server that
# is not in its own server list unless it is handed host:port, so this file
# is the one place to add a brand-new broker.
ACCESS_POINT_FILE = Path(__file__).resolve().parent / "access_points.env"
# Access points the software PROVED reachable ("learned"): written once a
# login or a TCP probe succeeds, so a name login keeps working after the
# first manual/one-click success without retyping host:port.  Runtime state,
# not config - safe to delete.
ACCESS_POINT_CACHE_FILE = Path(__file__).resolve().parent / "access_points.json"

TERMINALS = {
    1: {
        "dir": MT5_DIR,
        "ini": WINEPREFIX / "drive_c" / "config_bridge1.ini",
    },
    2: {
        "dir": MT5_DIR2,
        "ini": WINEPREFIX / "drive_c" / "config_bridge2.ini",
    },
}

BRIDGE_POLL_MS = int(os.getenv("MT5_BRIDGE_POLL_MS", "50"))
BRIDGE_STALE_SECONDS = int(os.getenv("MT5_BRIDGE_STALE_SECONDS", "20"))
BRIDGE_GRACE_SECONDS = int(os.getenv("MT5_BRIDGE_GRACE_SECONDS", "45"))
BRIDGE_COOLDOWN_SECONDS = int(os.getenv("MT5_BRIDGE_COOLDOWN_SECONDS", "120"))

WEB_HOST = os.getenv("MT5_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("MT5_WEB_PORT", "8000"))
WEB_WORKERS = int(os.getenv("MT5_WEB_WORKERS", "4"))
WEB_THREADS = int(os.getenv("MT5_WEB_THREADS", "8"))

SCHEDULER_POLL_SECONDS = float(os.getenv("MT5_SCHEDULER_POLL", "0.2"))
SCHEDULER_STAGGER_SECONDS = float(os.getenv("MT5_SCHEDULER_STAGGER", "0.5"))

METRICS_POLL_SECONDS = float(os.getenv("MT5_METRICS_POLL", "0.5"))
METRICS_SAMPLE_SECONDS = int(os.getenv("MT5_METRICS_SAMPLE", "10"))
MAX_CLOSED_TRADES = int(os.getenv("MT5_MAX_CLOSED_TRADES", "2000"))
MAX_EQUITY_SAMPLES = int(os.getenv("MT5_MAX_EQUITY_SAMPLES", "4320"))

MS_CANDLE_SECONDS = int(os.getenv("MT5_MS_CANDLE", "15"))
MS_MAX_BARS = int(os.getenv("MT5_MS_BARS", "200"))
DEFAULT_SYMBOL = os.getenv("MT5_DEFAULT_SYMBOL", "EURUSD")
MS_WINDOW = int(os.getenv("MT5_MS_WINDOW", "30"))
SYMBOL_SUFFIX_1 = os.getenv("MT5_SYMBOL_SUFFIX_1", "").strip()
SYMBOL_SUFFIX_2 = os.getenv("MT5_SYMBOL_SUFFIX_2", "").strip()

def _apply_suffix(pairs: tuple[str, ...], suffix: str) -> tuple[str, ...]:
    if not suffix:
        return pairs
    return tuple(f"{p}{suffix}" for p in pairs)

CLASSIC_PAIRS_RAW = (
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
    "AUDJPY", "CHFJPY", "EURCHF", "EURAUD", "GBPCHF",
    "CADJPY", "AUDNZD", "GBPAUD", "EURNZD", "AUDCAD",
    "NZDJPY", "GBPNZD", "EURCAD", "GBPCAD", "AUDCHF",
    "NZDCHF", "CADCHF", "XAUUSD", "XAGUSD",
)

CLASSIC_PAIRS = _apply_suffix(CLASSIC_PAIRS_RAW, SYMBOL_SUFFIX_1) if SYMBOL_SUFFIX_1 else CLASSIC_PAIRS_RAW

EXEC_TIMEOUT_SECONDS = float(os.getenv("MT5_EXEC_TIMEOUT", "8.0"))
EXEC_PING_TIMEOUT_SECONDS = 1.0

def map_symbol(symbol: str, inst: int) -> str:
    """Map a base symbol to the broker-specific symbol for a terminal."""
    suffix = SYMBOL_SUFFIX_1 if inst == 1 else SYMBOL_SUFFIX_2
    if not suffix:
        return symbol
    return f"{symbol}{suffix}"

def classic_pairs_for(inst: int) -> tuple[str, ...]:
    """Get classic pairs with correct suffix for a terminal."""
    suffix = SYMBOL_SUFFIX_1 if inst == 1 else SYMBOL_SUFFIX_2
    if not suffix:
        return CLASSIC_PAIRS_RAW
    return _apply_suffix(CLASSIC_PAIRS_RAW, suffix)

# Some trade servers are missing from a fresh MT5 install's server list, so
# logging in by NAME silently never connects (MT5 just keeps the last
# account).  For those the boot config must carry an ACCESS POINT
# (host:port) instead - MT5 resolves it to the real server name and reports
# that name back through the EA header.
# Source: https://www.hfm.com/ke/en/platforms/mt5-how-to-connect
SERVER_ACCESS_POINTS: dict[str, str] = {
    "HFMarketsKE-Live2": os.getenv("MT5_AP_HFM_LIVE2",
                                   "mt5-europe2.dcglobalfarm.com:1952"),
    "HFMarketsKE-Live10": os.getenv("MT5_AP_HFM_LIVE10",
                                    "mt5-global10.dcglobalfarm.com:21001"),
    "HFMarketsKE-Live11": os.getenv("MT5_AP_HFM_LIVE11",
                                    "mt5-global11.dcglobalfarm.com:21101"),
    "HFMarketsKE-Live15": os.getenv("MT5_AP_HFM_LIVE15",
                                    "mt5-ga-8.dcglobalfarm.com:21501"),
}

_ACCESS_POINT_RE = re.compile(r"^([^\s:/]+):(\d{1,5})$")


def _access_point_env_key(server: str) -> str:
    return "MT5_AP_" + re.sub(r"\W+", "_", str(server or "")).upper().strip("_")


def normalize_access_point(address: str) -> str:
    """'host:port' or 'host port' -> 'host:port' ('' when unusable).

    A bare hostname is deliberately rejected: MT5 needs the port, and
    accepting a portless value is how a login silently never connects."""
    text = str(address or "").strip().strip("\"'")
    if not text:
        return ""
    m = _ACCESS_POINT_RE.match(text)
    if not m:
        host, _, port = text.partition(" ")
        if not port.strip():
            return ""
        m = _ACCESS_POINT_RE.match(f"{host.strip()}:{port.strip()}")
        if not m:
            return ""
    host, port = m.group(1), int(m.group(2))
    if not host or not (1 <= port <= 65535):
        return ""
    return f"{host}:{port}"


_AP_FILE_CACHE: tuple[float, tuple[int, int] | None, dict[str, str]] | None = None
_AP_FILE_TTL_S = 5.0


def _parse_access_point_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        server, sep, address = line.partition("=")
        if not sep:
            # Whitespace form: "<server name> <host> <port>" - the last two
            # tokens are the address, everything before them is the server
            # (so multi-word server names still work without the '=').
            parts = line.split()
            if len(parts) < 3:
                continue
            server, address = " ".join(parts[:-2]), " ".join(parts[-2:])
        server = server.strip()
        address = normalize_access_point(address)
        if server and address:
            out[server] = address
    return out


def _registry_file_points() -> dict[str, str]:
    """Operator registry from access_points.env (mtime-validated)."""
    global _AP_FILE_CACHE
    import time
    now = time.monotonic()
    try:
        st = ACCESS_POINT_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if _AP_FILE_CACHE and now - _AP_FILE_CACHE[0] < _AP_FILE_TTL_S \
            and _AP_FILE_CACHE[1] == key:
        return _AP_FILE_CACHE[2]
    points: dict[str, str] = {}
    if key is not None:
        try:
            points = _parse_access_point_text(
                ACCESS_POINT_FILE.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            points = {}
    _AP_FILE_CACHE = (now, key, points)
    return points


_AP_LEARNED_CACHE: tuple[float, tuple[int, int] | None, dict[str, str]] | None = None


def learned_access_points() -> dict[str, str]:
    """Access points proven to work (written by login/probe successes).

    Memoized on the file's (mtime_ns, size): the supervisor and the account
    engine both ask on every poll, and this is on the panel's status path.
    """
    global _AP_LEARNED_CACHE
    import time
    try:
        st = ACCESS_POINT_CACHE_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    now = time.monotonic()
    if (_AP_LEARNED_CACHE and _AP_LEARNED_CACHE[1] == key
            and now - _AP_LEARNED_CACHE[0] < _AP_FILE_TTL_S):
        return _AP_LEARNED_CACHE[2]
    points: dict[str, str] = {}
    if key is not None:
        try:
            import json
            doc = json.loads(ACCESS_POINT_CACHE_FILE.read_text())
            if isinstance(doc, dict):
                points = {str(k): str(v) for k, v in doc.items()
                          if normalize_access_point(str(v))}
        except (OSError, ValueError):
            points = {}
    _AP_LEARNED_CACHE = (now, key, points)
    return points


def access_point_candidates(server: str) -> list[str]:
    """Every access point worth trying for `server`, best first.

    One place answers "how do I reach this broker": env override, then the
    learned cache, then the operator registry file, then the built-in table.
    Anything that speaks host:port qualifies; bare server names do not (MT5
    cannot resolve those when it does not already list the server).
    """
    name = str(server or "").strip()
    if not name:
        return []
    out: list[str] = []
    env = os.getenv(_access_point_env_key(name), "").strip()
    for cand in (env, learned_access_points().get(name, ""),
                 _registry_file_points().get(name, ""),
                 SERVER_ACCESS_POINTS.get(name, "")):
        norm = normalize_access_point(cand) if cand else ""
        if norm and norm not in out:
            out.append(norm)
    return out


def access_point_for(server: str) -> str:
    """Preferred access point (host:port) for a server NAME MT5 can't
    resolve, or '' when the name is all we have."""
    cands = access_point_candidates(server)
    return cands[0] if cands else ""


def register_access_point(server: str, address: str) -> str:
    """Remember a proven access point.  Returns the normalized address.

    Called when a login or a TCP probe proves host:port reaches `server`,
    so the operator never has to retype it and later name logins resolve.
    """
    name = str(server or "").strip()
    norm = normalize_access_point(address)
    if not name or not norm:
        return ""
    import json
    import tempfile
    points = dict(learned_access_points())     # never mutate the memo
    if points.get(name) == norm:
        return norm
    points[name] = norm
    global _AP_FILE_CACHE, _AP_LEARNED_CACHE
    try:
        fd, tmp = tempfile.mkstemp(dir=str(ACCESS_POINT_CACHE_FILE.parent),
                                  prefix=".ap.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(points, fh, separators=(",", ":"), sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, ACCESS_POINT_CACHE_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        _AP_FILE_CACHE = None
        _AP_LEARNED_CACHE = None
    except OSError:
        return ""
    return norm


def all_access_points() -> dict[str, dict]:
    """Merged registry for the UI/CLI: server -> {address, source}."""
    out: dict[str, dict] = {}
    for server, addr in SERVER_ACCESS_POINTS.items():
        out[server] = {"address": normalize_access_point(addr), "source": "builtin"}
    for server, addr in _registry_file_points().items():
        out[server] = {"address": addr, "source": "registry"}
    for server, addr in learned_access_points().items():
        out[server] = {"address": addr, "source": "learned"}
    for key, value in os.environ.items():
        if key.startswith("MT5_AP_") and value.strip():
            out[f"env:{key}"] = {"address": normalize_access_point(value),
                                 "source": "env"}
    return out

@dataclass(frozen=True, slots=True)
class Config:
    wineprefix: Path = WINEPREFIX
    mt5_dir: Path = MT5_DIR
    mt5_dir2: Path = MT5_DIR2
    db_path: Path = DB_PATH
    env_file: Path = ENV_FILE
    session_file: Path = SESSION_FILE
    spot_dump_src: Path = SPOT_DUMP_SRC
    terminals: dict = field(default_factory=lambda: TERMINALS)

    bridge_poll_ms: int = BRIDGE_POLL_MS
    bridge_stale_seconds: int = BRIDGE_STALE_SECONDS
    bridge_grace_seconds: int = BRIDGE_GRACE_SECONDS
    bridge_cooldown_seconds: int = BRIDGE_COOLDOWN_SECONDS

    web_host: str = WEB_HOST
    web_port: int = WEB_PORT
    web_workers: int = WEB_WORKERS
    web_threads: int = WEB_THREADS

    scheduler_poll_seconds: float = SCHEDULER_POLL_SECONDS
    scheduler_stagger_seconds: float = SCHEDULER_STAGGER_SECONDS

    metrics_poll_seconds: float = METRICS_POLL_SECONDS
    metrics_sample_seconds: int = METRICS_SAMPLE_SECONDS
    max_closed_trades: int = MAX_CLOSED_TRADES
    max_equity_samples: int = MAX_EQUITY_SAMPLES

    ms_candle_seconds: int = MS_CANDLE_SECONDS
    ms_max_bars: int = MS_MAX_BARS
    ms_window: int = MS_WINDOW
    default_symbol: str = DEFAULT_SYMBOL

    classic_pairs: tuple = CLASSIC_PAIRS
    classic_pairs_raw: tuple = CLASSIC_PAIRS_RAW
    symbol_suffix_1: str = SYMBOL_SUFFIX_1
    symbol_suffix_2: str = SYMBOL_SUFFIX_2

    exec_timeout_seconds: float = EXEC_TIMEOUT_SECONDS

CONFIG = Config()

def validate_config() -> list[str]:
    errors = []
    if not CONFIG.wineprefix.exists():
        errors.append(f"WINEPREFIX not found: {CONFIG.wineprefix}")
    if not CONFIG.mt5_dir.exists() and not CONFIG.mt5_dir2.exists():
        errors.append(f"No MT5 installation found at {CONFIG.mt5_dir} or {CONFIG.mt5_dir2}")
    if CONFIG.web_port < 1 or CONFIG.web_port > 65535:
        errors.append(f"Invalid web port: {CONFIG.web_port}")
    return errors

class _ColorFormatter(logging.Formatter):

    COLORS = {
        logging.DEBUG: "\033[2m",
        logging.INFO: "",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[91;1m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:>8}{self.RESET}"
        return super().format(record)

def setup_logging(name: str | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name or "mt5")
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    datefmt = "%H:%M:%S"

    if sys.stdout.isatty():
        handler.setFormatter(_ColorFormatter(fmt, datefmt=datefmt))
    else:
        handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)
    # Without this every record also bubbles to the root logger, so each
    # line appears TWICE in app_run.log (once formatted, once as
    # "WARNING:broker_prober:...") - observed 2026-10-13 10:22:29.
    logger.propagate = False
    return logger

if __name__ == "__main__":
    errs = validate_config()
    if errs:
        print("CONFIG VALIDATION ERRORS:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print("Config OK")
    print(f"  WINEPREFIX: {CONFIG.wineprefix}")
    print(f"  MT5_DIR:    {CONFIG.mt5_dir}")
    print(f"  DB:         {CONFIG.db_path}")
    print(f"  Web:        {CONFIG.web_host}:{CONFIG.web_port}")
    print(f"  Exec:       timeout={CONFIG.exec_timeout_seconds}s")
