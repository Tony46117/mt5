"""conftest.py - isolate tests from the live trading database.

MT5_DB_PATH is read by database.py at import time, so it MUST be set
before any project module is imported (hence at conftest top level).
"""

import os
import sys
import tempfile
from pathlib import Path

# private DB per test session - never touch trades.db
_TEST_DB = os.path.join(tempfile.gettempdir(), "mt5_test_trades.db")
if os.path.exists(_TEST_DB):
    os.unlink(_TEST_DB)
os.environ["MT5_DB_PATH"] = _TEST_DB

# project root on sys.path (tests/ lives beside the modules)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
