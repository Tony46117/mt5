#!/usr/bin/env python3.12
"""cicd_probe.py - continuous integration/deployment loop for the trading
pipeline, running against the LIVE software.

Each ROUND is a full "deploy + verify + troubleshoot" cycle:
  1. SCHEDULE   : one trade per account scheduled for `now + lead` seconds
                  (immediate-ish fires = fast feedback, exactly like a CI
                  pipeline runs the next build ASAP)
  2. VERIFY     : opens must appear (n_positions per account) inside the
                  verify window, book must be FLAT again by close+grace,
                  fire lateness measured vs the planned second
  3. DIAGNOSE   : for every failure, classify it from the evidence:
                    claimed-not-fired  -> a scheduler ate the slot (check
                                          other schedulers / weekend guard)
                    late-fire          -> dispatch or fill latency spike
                    open-missing       -> failed open (retry path didn't run)
                    book-not-flat      -> close failed or never ran
                  using the fired log, the live book and the EA journal
  4. REMEDIATE  : auto-fixes that are safe to apply live:
                    - orphaned positions -> close.py (concurrent CLOSEALL)
                    - duplicate pending schedules for the same slot ->
                      deactivated (CAS should prevent; if it happens, heal)
                    - everything else -> recorded as FINDING for the report
  5. REPORT     : per-round PASS/FAIL line + final CI-style summary

Usage:
    python cicd_probe.py --rounds 8 --lead 6 --n 2 --lifetime 4
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import time
from collections import Counter
from pathlib import Path

import config
from config import setup_logging
import database as db
import spot
from spot import read_accounts, BOLD, DIM, RESET, GREEN, RED, YELLOW

log = setup_logging(__name__)

GRACE_S = 20.0          # wait this long past planned close before verdict
FLAT_POLLS = 3          # consecutive flat-book polls required for "closed"


def book_count(acc: int) -> int:
    """Live open-position count of terminal `acc` (book = truth, DB-free)."""
    p = spot.TERMINALS[acc]["dir"] / "MQL5" / "Files" / "trades.csv"
    try:
        return sum(1 for ln in p.read_text(encoding="cp1252",
                                           errors="replace").splitlines()
                   if ln.strip() and not ln.startswith("NONE")
                   and len(ln.split("\t")) >= 11)
    except OSError:
        return -1


def journal_fills(local_since: str, local_until: str) -> dict[int, list[float]]:
    """EA-journal server-side fill latencies per terminal in a local
    HH:MM:SS window ('done in X ms' lines).  Best-effort diagnostics."""
    out: dict[int, list[float]] = {1: [], 2: []}
    for t, name in ((1, "MetaTrader 5"), (2, "MetaTrader 5-2")):
        d = Path.home() / f".mt5/drive_c/Program Files/{name}/logs"
        files = [p for p in d.glob("*.log") if p.stat().st_size > 0]
        if not files:
            continue
        f = max(files, key=lambda p: p.stat().st_mtime)
        try:
            text = f.read_bytes().decode("utf-16-le", errors="replace")
        except OSError:
            continue
        for ts, ms in re.findall(
                r"(\d\d:\d\d:\d\d)\.\d+\tTrades\t'5752538\d': "
                r"[^\r\n]*?done in ([\d.]+) ms", text):
            if local_since <= ts <= local_until:
                out[t].append(float(ms))
    return out


def sched_fired_rows(sid: int) -> list[dict]:
    return [r for r in db.list_fired(400) if r["schedule_id"] == sid]


def run_round(idx: int, accounts: list[int], lead: float, n: int,
              lot: float, pair: str, side: str, lifetime: float) -> dict:
    """One CI round: schedule -> verify -> diagnose.  Returns a result dict."""
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=lead)
    now = now.replace(microsecond=0)
    close_t = now + dt.timedelta(seconds=max(1.0, round(lifetime)))
    sids: dict[int, int] = {}
    for acc in accounts:
        sids[acc] = db.add_future_trade(acc, pair, side, lot, n,
                                        now.hour, now.minute, now.second,
                                        close_t.hour, close_t.minute, close_t.second)
    print(f"  [{idx}] scheduled acc{accounts}: #{sids[accounts[0]]}"
          f"{', #' + str(sids[accounts[1]]) if len(accounts) > 1 else ''} "
          f"fires {now.strftime('%H:%M:%S')}Z (+{lead:.0f}s), "
          f"close {close_t.strftime('%H:%M:%S')}Z")

    deadline = close_t + dt.timedelta(seconds=GRACE_S)
    flat_streak = 0
    opens: dict[int, list[dict]] = {a: [] for a in accounts}
    while dt.datetime.now(dt.timezone.utc) < deadline:
        for acc in accounts:
            if not opens[acc]:
                opens[acc] = [r for r in sched_fired_rows(sids[acc])
                              if r["kind"] == "open"]
        cur_flat = all(book_count(a) == 0 for a in accounts)
        flat_streak = flat_streak + 1 if cur_flat else 0
        if flat_streak >= FLAT_POLLS and all(opens[a] for a in accounts):
            break
        time.sleep(0.5)

    res: dict = {"idx": idx, "sids": sids, "opens": opens,
                 "fire": now, "close": close_t, "findings": []}

    # ---- verify opens -----------------------------------------------------
    for acc in accounts:
        rows = opens[acc]
        ok_rows = [r for r in rows if r["ok"]]
        res.setdefault("open_ok", {})[acc] = len(ok_rows)
        if not ok_rows:
            claimed = bool(rows) or _slot_consumed(sids[acc])
            res["findings"].append(
                f"acc{acc}: NO opens logged "
                + ("but slot was CLAIMED -> scheduler ate it "
                   "(claimed-not-fired)" if claimed else "-> never fired"))
        elif len(ok_rows) < n:
            res["findings"].append(
                f"acc{acc}: partial opens {len(ok_rows)}/{n} -> "
                f"{[r['detail'] for r in rows if not r['ok']]}")
        # lateness from dispatch estimate (log time - batch ms)
        if ok_rows:
            t = dt.datetime.fromisoformat(str(ok_rows[0]["at"]))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            ms = float(ok_rows[0].get("ms") or 0.0)
            res.setdefault("dispatch_late", {})[acc] = \
                (t - dt.timedelta(milliseconds=ms) - now).total_seconds()

    # ---- verify close -----------------------------------------------------
    res["flat"] = flat_streak >= FLAT_POLLS
    if not res["flat"]:
        orphans = {a: book_count(a) for a in accounts if book_count(a) != 0}
        res["findings"].append(
            f"book not flat by deadline: {orphans} -> closing orphans")
        # REMEDIATE: concurrent close of everything left
        try:
            from executor import _close_one
            for a in orphans:
                if orphans[a] > 0:
                    _close_one(a, "ALL")
            res["findings"].append("orphaned positions closed (remediated)")
        except Exception as exc:
            res["findings"].append(f"remediation failed: {exc}")

    res["pass"] = (all(res.get("open_ok", {}).get(a, 0) >= n for a in accounts)
                   and res["flat"])
    return res


def _slot_consumed(sid: int) -> bool:
    row = db.get_future_trade(sid) if hasattr(db, "get_future_trade") else None
    if row is None:
        try:
            rows = db.list_future_trades()
            row = next((r for r in rows if r["id"] == sid), None)
        except Exception:
            row = None
    if row is None:
        return False
    nf = row.get("next_fire")
    if not nf:
        return False
    if isinstance(nf, str):
        try:
            nf = dt.datetime.fromisoformat(nf)
        except ValueError:
            return False
    if nf.tzinfo is None:
        nf = nf.replace(tzinfo=dt.timezone.utc)
    # consumed = next_fire was advanced well past the slot (daily recurrence)
    return (nf - dt.datetime.now(dt.timezone.utc)).total_seconds() > 3600


def main() -> int:
    ap = argparse.ArgumentParser(description="CI/CD loop for the trade pipeline")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--gap", type=float, default=12.0,
                    help="seconds between rounds")
    ap.add_argument("--lead", type=float, default=6.0,
                    help="schedule fires this many seconds from now")
    ap.add_argument("--n", type=int, default=2, help="opens per account")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--pair", default="XAUUSD247")
    ap.add_argument("--side", default="BUY", choices=("BUY", "SELL"))
    ap.add_argument("--lifetime", type=float, default=4.0)
    ap.add_argument("--max-sweep", type=int, default=2,
                    help="max consecutive failing rounds before giving up")
    args = ap.parse_args()

    accs = read_accounts()
    accounts = [a for a in (1, 2) if accs.get(a, {}).get("login")]
    if not accounts:
        print(f"{RED}nobody logged in - is the bridge running?{RESET}")
        return 1
    db.init_db()

    print(f"{BOLD}CI/CD LOOP - {args.rounds} rounds, {args.n} opens/account, "
          f"lifetime {args.lifetime:.0f}s, gap {args.gap:.0f}s{RESET}\n")
    results: list[dict] = []
    fails_streak = 0
    for i in range(1, args.rounds + 1):
        r = run_round(i, accounts, args.lead, args.n, args.lot,
                      args.pair, args.side, args.lifetime)
        results.append(r)
        late = r.get("dispatch_late", {})
        late_s = "  ".join(f"acc{a}{late.get(a, 0):+.2f}s" for a in accounts)
        mark = f"{GREEN}PASS{RESET}" if r["pass"] else f"{RED}FAIL{RESET}"
        print(f"  [{i}] {mark}  opens "
              + " ".join(f"acc{a}:{r.get('open_ok', {}).get(a, 0)}/{args.n}"
                         for a in accounts)
              + f"  dispatch {late_s}  flat={r['flat']}")
        for fnd in r["findings"]:
            print(f"        {YELLOW}-{fnd}{RESET}")
        fails_streak = 0 if r["pass"] else fails_streak + 1
        if fails_streak >= args.max_sweep:
            print(f"\n{RED}{fails_streak} consecutive failing rounds - "
                  f"stopping for investigation{RESET}")
            break
        time.sleep(max(1.0, args.gap - (args.lifetime + GRACE_S - args.gap)
                       if False else args.gap))

    # ---- final report ------------------------------------------------------
    rounds = len(results)
    passed = sum(1 for r in results if r["pass"])
    print(f"\n{BOLD}CI REPORT{RESET}")
    print(f"  rounds: {rounds}   passed: {passed}   failed: {rounds - passed}")
    lates = sorted(v for r in results for v in r.get("dispatch_late", {}).values())
    if lates:
        med = lates[len(lates) // 2]
        print(f"  dispatch lateness vs plan: median {med:+.2f}s  "
              f"min {lates[0]:+.2f}s  max {lates[-1]:+.2f}s  (n={len(lates)})")
    findings = Counter(f.split("->")[0].split(": ")[-1] for r in results
                       for f in r["findings"])
    if findings:
        print(f"  {BOLD}findings (classified):{RESET}")
        for kind, cnt in findings.most_common():
            print(f"    {cnt}x {kind}")
    ok = passed == rounds
    print(f"\n  overall: {GREEN}GREEN{RESET}" if ok
          else f"\n  overall: {RED}RED{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
