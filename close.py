#!/usr/bin/env python3.12

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
    t0 = time.perf_counter()
    ok, detail = _close_one(acc_num, "ALL")
    results[acc_num] = (ok, detail, (time.perf_counter() - t0) * 1000.0)

def close_account_pair(acc_num: int, pair: str, results: dict) -> None:
    t0 = time.perf_counter()
    ok, detail = _close_one(acc_num, pair)
    results[acc_num] = (ok, detail, (time.perf_counter() - t0) * 1000.0)

def close_all_accounts_results(pair: str | None = None) -> dict:
    """Close every position on every logged-in terminal CONCURRENTLY.

    Returns a JSON-friendly result the web panel can use directly:
      {"accounts": {1: {"ok", "detail", "ms"}, 2: {...}}, "wall_ms": ...}
    One thread per terminal, joined - so wall time is the slowest terminal,
    not the sum.
    """
    accounts = read_accounts()
    active = [n for n in (1, 2) if accounts.get(n, {}).get("login")]
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

    out: dict[int, dict] = {}
    for n in (1, 2):
        if n not in active:
            out[n] = {"ok": False, "detail": "not logged in", "ms": 0.0}
            continue
        ok, detail, ms = results.get(n, (False, "no result", 0.0))
        out[n] = {"ok": ok, "detail": detail, "ms": round(ms, 1)}
    return {"accounts": out, "wall_ms": round(wall_ms, 1)}

def _fan_out(active: list[int], pair: str | None) -> float:
    data = close_all_accounts_results(pair)
    accounts = read_accounts()
    for n in active:
        r = data["accounts"].get(n, {"ok": False, "detail": "no result", "ms": 0.0})
        mark = "OK" if r["ok"] else "FAILED"
        print(f"Account {n} (login: {accounts[n]['login']}): {mark} - "
              f"{r['detail']}  [command {r['ms']:.1f} ms]")
    return data["wall_ms"]

def close_all_accounts(pair: str | None = None) -> float:
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
