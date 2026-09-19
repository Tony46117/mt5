#!/usr/bin/env python3.12
"""probe_schedules.py - continuously schedule immediate trades until we are
SURE scheduled trades actually execute, and fast.

Per cycle it:
  1. schedules n=4 BUY 0.01 EURUSD jobs per terminal with exec time a few
     seconds out and close time seconds later (auto-close keeps the demo
     accounts clean);
  2. waits for the fire, measuring when the fired-log rows appear;
  3. reports per-cycle open latency (schedule->fired) and print stats;
  4. loops until --cycles reached or Ctrl+C, then CLOSEALLs everything.

Latency target: the executor batches all 4 opens into ONE exec-channel
round trip, so a cycle's open burst should land in the tens of ms after
the exec second.

Usage:
    python probe_schedules.py                    # 5 cycles, then cleanup
    python probe_schedules.py --cycles 20 --lot 0.02
    python probe_schedules.py --account 1        # probe one terminal only
"""

from __future__ import annotations

import argparse
import datetime as dt
import time

import config
from config import setup_logging
import database as db
from executor import sender_for, FutureTradeScheduler, start_scheduler
from spot import BOLD, DIM, RESET, GREEN, RED, YELLOW, read_accounts
import session

log = setup_logging(__name__)

LEAD_S = 6          # schedule fires this many seconds from now
LIFETIME_S = 3      # position auto-closes this many seconds after open
                    # (3 s: Deriv Boom/Crash speed test - see --lifetime)
GRACE_S = 15.0      # keep polling for the fire this long past the planned
                    # second before calling it a miss (the old one-shot check
                    # ran 1.5 s after the fire second and reported legitimate
                    # 1-2 s-late fills as 0/2 misses - a probe-timing bug)


def schedule_probe(acc: int, pair: str, side: str, lot: float, n: int,
                   lifetime: float = LIFETIME_S) -> int:
    """One schedule due LEAD_S seconds from now, closing `lifetime` later."""
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=LEAD_S)
    close_t = now + dt.timedelta(seconds=max(1, round(lifetime)))
    sid = db.add_future_trade(acc, pair, side, lot, n,
                              now.hour, now.minute, now.second,
                              close_t.hour, close_t.minute, close_t.second)
    return sid


def fired_rows_for(sid: int):
    return [r for r in db.list_fired(100) if r["schedule_id"] == sid]


def run_cycle(cycle: int, accounts: list[int], pair: str, side: str,
              lot: float, n: int, lifetime: float = LIFETIME_S) -> dict:
    """Schedule -> wait for the fire -> report latency for this cycle."""
    sids: dict[int, int] = {}
    t_sched = time.perf_counter()
    for acc in accounts:
        sids[acc] = schedule_probe(acc, pair, side, lot, n, lifetime=lifetime)
    sched_ms = (time.perf_counter() - t_sched) * 1000.0

    # planned fire second = the schedule's exec time (now + LEAD_S, whole s)
    planned = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) \
        + dt.timedelta(seconds=LEAD_S)
    deadline = planned + dt.timedelta(seconds=GRACE_S)

    # POLL for the fire rows (the old code slept to planned+1.5 s and checked
    # ONCE - any fill landing >1.5 s late was reported as a 0/2 miss even
    # though it executed fine two seconds later).
    latencies: dict[int, list[float]] = {}
    oks: dict[int, int] = {}
    late_s: dict[int, float] = {}
    while dt.datetime.now(dt.timezone.utc) < deadline:
        done = True
        for acc, sid in sids.items():
            rows = fired_rows_for(sid)
            oks[acc] = sum(1 for r in rows if r["ok"] and r["kind"] == "open")
            latencies[acc] = [r["ms"] for r in rows if r.get("ms") is not None]
            if oks[acc] < n:
                done = False
            if oks[acc] and acc not in late_s:
                first_at = min((r["at"] for r in rows
                                if r["ok"] and r["kind"] == "open" and r.get("at")),
                               default=None)
                if first_at:
                    try:
                        fa = dt.datetime.fromisoformat(str(first_at))
                        if fa.tzinfo is None:
                            fa = fa.replace(tzinfo=dt.timezone.utc)
                        late_s[acc] = max(0.0, (fa - planned).total_seconds())
                    except ValueError:
                        pass
        if done:
            break
        time.sleep(0.25)

    print(f"\n{BOLD}cycle {cycle}{RESET}  {DIM}schedules {sids} "
          f"(db writes {sched_ms:.1f} ms, planned fire "
          f"{planned.strftime('%H:%M:%S')} UTC){RESET}")
    for acc in accounts:
        ok, total = oks.get(acc, 0), n
        col = GREEN if ok == total else (YELLOW if ok else RED)
        lat = ",".join(f"{x:.0f}" for x in latencies.get(acc, []))
        tardy = f"  +{late_s[acc]:.1f}s vs plan" if acc in late_s else ""
        print(f"  terminal {acc}: {col}{ok}/{total} opened{RESET}"
              + (f"  fired-log latency ms: {lat}" if lat else "") + tardy)

    return {"cycle": cycle, "oks": oks, "latencies": latencies,
            "late": late_s, "planned": planned}


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous scheduled-trade probe")
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--n", type=int, default=4, help="positions per schedule")
    ap.add_argument("--pair", type=str, default="Boom 1000 Index",
                    help="MT5 symbol EXACTLY as in Market Watch (Deriv: "
                         "'Boom 1000 Index', 'Crash 1000 Index', 'EURUSD'...) "
                         "- case matters")
    ap.add_argument("--lifetime", type=float, default=LIFETIME_S,
                    help="seconds a position stays open before auto-close "
                         "(default 3 - speed test)")
    ap.add_argument("--side", type=str, default="BUY", choices=("BUY", "SELL"))
    ap.add_argument("--account", type=int, choices=(1, 2), default=0,
                    help="probe one terminal only (default: both)")
    args = ap.parse_args()

    accs = read_accounts()
    accounts = [args.account] if args.account else \
        [n for n in (1, 2) if accs.get(n, {}).get("login")]
    if not accounts:
        print(f"{RED}nobody logged in - run bridge.py first{RESET}")
        return 1

    db.init_db()
    sched = start_scheduler()
    print(f"{BOLD}SCHEDULED-TRADE PROBE{RESET} {DIM}"
          f"{args.cycles} cycles x ({args.n} x {args.lot} {args.side} {args.pair}) "
          f"on terminals {accounts}, exec +{LEAD_S}s, close +{LIFETIME_S}s "
          f"(scheduler alive: {sched.is_alive()}){RESET}")

    results = []
    try:
        for c in range(1, args.cycles + 1):
            results.append(run_cycle(c, accounts, args.pair,
                                     args.side, args.lot, args.n,
                                     lifetime=args.lifetime))
    except KeyboardInterrupt:
        print(f"\n{YELLOW}interrupted - cleaning up{RESET}")

    # stats
    all_lat = [x for r in results for v in r["latencies"].values() for x in v]
    all_ok = sum(sum(r["oks"].values()) for r in results)
    all_total = sum(len(r["oks"]) * args.n for r in results)
    print(f"\n{BOLD}STATS{RESET}  {all_ok}/{all_total} opens fired")
    if all_lat:
        all_lat.sort()
        n = len(all_lat)
        print(f"  fired-log latency ms  min {all_lat[0]:.0f}  "
              f"median {all_lat[n // 2]:.0f}  p90 {all_lat[int(n * 0.9)]:.0f}  "
              f"max {all_lat[-1]:.0f}")
    all_late = [v for r in results for v in r.get("late", {}).values()]
    if all_late:
        all_late.sort()
        m = len(all_late)
        print(f"  timeliness vs planned second: median +{all_late[m // 2]:.1f}s  "
              f"max +{all_late[-1]:.1f}s")

    # cleanup: close everything the probe may have left open
    print(f"\n{BOLD}cleanup{RESET} - CLOSEALL on probed terminals...")
    for acc in accounts:
        ok, detail = sender_for(acc).close_all()
        print(f"  terminal {acc}: {'OK' if ok else 'FAILED'} - {detail}")
    return 0 if all_ok == all_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
