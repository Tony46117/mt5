#!/usr/bin/env python3.12
"""config.py - every tunable of the MT5 trading system in one place.

Each setting is a module constant with an MT5_* environment override, and
CONFIG is a frozen dataclass mirror of them for typed access.  Run this
module directly to validate paths and print the effective settings.
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# MT5_WINEPREFIX lets Docker (or any sandbox) relocate the wine prefix;
# default stays the historical ~/.mt5 for native Linux installs.
_WINEPREFIX_ENV = os.getenv("MT5_WINEPREFIX", "").strip()
WINEPREFIX = Path(_WINEPREFIX_ENV) if _WINEPREFIX_ENV else Path.home() / ".mt5"
MT5_DIR = WINEPREFIX / "drive_c" / "Program Files" / "MetaTrader 5"
MT5_DIR2 = WINEPREFIX / "drive_c" / "Program Files" / "MetaTrader 5-2"
DB_PATH = Path(__file__).resolve().parent / "trades.db"
ENV_FILE = Path(__file__).resolve().parent / "acc.env"      # legacy only (mt5_v2 compat) - unused at runtime
SESSION_FILE = Path(__file__).resolve().parent / "session.json"
SPOT_DUMP_SRC = Path(__file__).resolve().parent / "SpotDump.mq5"

# --------------------------------------------------------------------------
# Terminal configuration
# NOTE: launch configs are config_bridge*.ini - NOT mt5_v2's config_spot*.ini,
# so the two projects never overwrite each other's terminal start configs
# (they share one wineprefix).
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# Bridge / feed settings
# --------------------------------------------------------------------------
BRIDGE_POLL_MS = int(os.getenv("MT5_BRIDGE_POLL_MS", "50"))
BRIDGE_STALE_SECONDS = int(os.getenv("MT5_BRIDGE_STALE_SECONDS", "20"))
BRIDGE_GRACE_SECONDS = int(os.getenv("MT5_BRIDGE_GRACE_SECONDS", "45"))
BRIDGE_COOLDOWN_SECONDS = int(os.getenv("MT5_BRIDGE_COOLDOWN_SECONDS", "120"))

# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
WEB_HOST = os.getenv("MT5_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("MT5_WEB_PORT", "8000"))
WEB_WORKERS = int(os.getenv("MT5_WEB_WORKERS", "4"))
WEB_THREADS = int(os.getenv("MT5_WEB_THREADS", "8"))

# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
SCHEDULER_POLL_SECONDS = float(os.getenv("MT5_SCHEDULER_POLL", "0.2"))
SCHEDULER_STAGGER_SECONDS = float(os.getenv("MT5_SCHEDULER_STAGGER", "0.5"))

# --------------------------------------------------------------------------
# Metrics observer
# --------------------------------------------------------------------------
METRICS_POLL_SECONDS = float(os.getenv("MT5_METRICS_POLL", "0.5"))
METRICS_SAMPLE_SECONDS = int(os.getenv("MT5_METRICS_SAMPLE", "10"))
MAX_CLOSED_TRADES = int(os.getenv("MT5_MAX_CLOSED_TRADES", "2000"))
MAX_EQUITY_SAMPLES = int(os.getenv("MT5_MAX_EQUITY_SAMPLES", "4320"))

# --------------------------------------------------------------------------
# Chart / display
# --------------------------------------------------------------------------
MS_CANDLE_SECONDS = int(os.getenv("MT5_MS_CANDLE", "15"))
MS_MAX_BARS = int(os.getenv("MT5_MS_BARS", "200"))
DEFAULT_SYMBOL = os.getenv("MT5_DEFAULT_SYMBOL", "EURUSD")
MS_WINDOW = int(os.getenv("MT5_MS_WINDOW", "5"))

# --------------------------------------------------------------------------
# Pairs
# --------------------------------------------------------------------------
CLASSIC_PAIRS = (
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
    "AUDJPY", "CHFJPY", "EURCHF", "EURAUD", "GBPCHF",
    "CADJPY", "AUDNZD", "GBPAUD", "EURNZD", "AUDCAD",
    "NZDJPY", "GBPNZD", "EURCAD", "GBPCAD", "AUDCHF",
    "NZDCHF", "CADCHF", "XAUUSD", "XAGUSD",
)

# --------------------------------------------------------------------------
# Order channel
# --------------------------------------------------------------------------
EXEC_TIMEOUT_SECONDS = float(os.getenv("MT5_EXEC_TIMEOUT", "3.0"))
EXEC_PING_TIMEOUT_SECONDS = 1.0

# --------------------------------------------------------------------------
# Dataclass config (runtime)
# --------------------------------------------------------------------------

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

    exec_timeout_seconds: float = EXEC_TIMEOUT_SECONDS


CONFIG = Config()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_config() -> list[str]:
    """Return list of validation errors (empty = OK)."""
    errors = []
    if not CONFIG.wineprefix.exists():
        errors.append(f"WINEPREFIX not found: {CONFIG.wineprefix}")
    if not CONFIG.mt5_dir.exists() and not CONFIG.mt5_dir2.exists():
        errors.append(f"No MT5 installation found at {CONFIG.mt5_dir} or {CONFIG.mt5_dir2}")
    if CONFIG.web_port < 1 or CONFIG.web_port > 65535:
        errors.append(f"Invalid web port: {CONFIG.web_port}")
    return errors


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class _ColorFormatter(logging.Formatter):
    """Colored terminal log formatter."""

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
    """Configure structured logging with colors."""
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
