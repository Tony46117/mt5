#!/usr/bin/env python3.12
"""close.py - Close ALL open positions across ALL accounts - in <10 ms.

Speed model: the per-command round-trip through the wine/EA channel is
bounded below by the terminal itself (~3-8 ms), so the only overhead this
script can remove is its own: it fires every terminal's CLOSEALL
CONCURRENTLY (one thread per terminal, 0 ms stagger), skips the legacy
per-position CLOSE loop entirely (CLOSEALL is one atomic command that
closes everything server-side in a single pass) and parses results as
they arrive.

DATABASE-FREE: the close path touches NO database - no reads to decide
what to close (CLOSEALL is atomic), no audit writes before the response.
Failed closes auto-retry after 500 ms (executor._close_one), 3 attempts.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from executor import _close_one
from spot import read_accounts
from config import setup_logging

log = setup_logging(__name__)


def close_account(acc_num: int, results: dict) -> None:
    """CLOSEALL one terminal - one atomic server-side command, retry x3."""
    t0 = time.perf_counter()
    ok, detail = _close_one(acc_num, "ALL")
    results[acc_num] = (ok, detail, (time.perf_counter() - t0) * 1000.0)


def close_account_pair(acc_num: int, pair: str, results: dict) -> None:
    """CLOSEALL one terminal, one symbol - still one atomic command."""
    t0 = time.perf_counter()
    ok, detail = _close_one(acc_num, pair)
    results[acc_num] = (ok, detail, (time.perf_counter() - t0) * 1000.0)


def _fan_out(active: list[int], pair: str | None) -> float:
    """Fire every terminal's close AT THE SAME TIME (one thread per
    terminal) and return the wall time in ms.  The per-command round trip
    is bounded by the terminal itself - concurrency removes OUR overhead,
    so n terminals close in the wall time of one."""
    results: dict[int, tuple[bool, str, float]] = {}
    target = close_account if pair in (None, "ALL") else close_account_pair
    args_by = (lambda n: (n, results)) if pair in (None, "ALL") \
        else (lambda n: (n, pair, results))
    threads = [threading.Thread(target=target, args=args_by(n),
                                daemon=True, name=f"close-all-{n}")
               for n in active]
    wall0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_ms = (time.perf_counter() - wall0) * 1000.0

    accounts = read_accounts()
    for n in active:
        ok, detail, ms = results.get(n, (False, "no result", 0.0))
        mark = "OK" if ok else "FAILED"
        print(f"Account {n} (login: {accounts[n]['login']}): {mark} - {detail}  "
              f"[command {ms:.1f} ms]")
    return wall_ms


def close_all_accounts(pair: str | None = None) -> float:
    """Close everything on all terminals concurrently.

    CLOSEALL closes every position in one command - no per-position round
    trips - so the wall time is one command latency (a few ms), not
    positions x latency.  Optional `pair` targets one symbol per terminal
    (still one atomic command each, still all terminals in parallel).
    Returns the wall time in ms."""
    accounts = read_accounts()
    active = [n for n in (1, 2) if accounts.get(n, {}).get("login")]
    for n in (1, 2):
        if n not in active:
            print(f"Account {n}: not logged in, skipping")
    return _fan_out(active, pair)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Close positions concurrently across accounts")
    ap.add_argument("--pair", type=str, default="ALL",
                    help="close only this symbol (e.g. XAUUSD247); default ALL")
    args = ap.parse_args()
    print(f"Closing {'ALL positions' if args.pair.upper() == 'ALL' else args.pair.upper()} "
          f"across ALL accounts - CONCURRENTLY...")
    wall_ms = close_all_accounts(args.pair.upper())
    print(f"\nDone in {wall_ms:.1f} ms wall time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
