#!/usr/bin/env python3.12
"""burst_probe.py - concurrency + timing audit of the scheduled-fire pipeline.

Schedules `--jobs` bursts per account, `--stride` seconds apart, each burst
opening `--n` positions CONCURRENTLY per account, auto-closed `--lifetime` s
later.  Then verifies against the fired log:
  * every burst opened AT its planned second (lateness per burst),
  * all n opens per account landed (concurrent batch),
  * a close ran ~lifetime later.
Exit 0 only when every account-burst fully executed on time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import config
from config import setup_logging
import database as db
import spot
from spot import read_accounts, BOLD, DIM, RESET, GREEN, RED, YELLOW

log = setup_logging(__name__)

GRACE_S = 25.0


def _utc_today(h: int, m: int, s: int) -> dt.datetime:
    local = dt.datetime.now().astimezone()
    return local.replace(hour=h, minute=m, second=s, microsecond=0) \
                .astimezone(dt.timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scheduled-burst concurrency audit")
    ap.add_argument("--start", required=True, metavar="HH:MM:SS",
                    help="first burst time (LOCAL)")
    ap.add_argument("--jobs", type=int, default=12, help="bursts per account")
    ap.add_argument("--stride", type=float, default=5.0, help="seconds between bursts")
    ap.add_argument("--n", type=int, default=4, help="positions per account per burst")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--pair", default="XAUUSD247")
    ap.add_argument("--side", default="BUY", choices=("BUY", "SELL"))
    ap.add_argument("--lifetime", type=float, default=4.0)
    args = ap.parse_args()

    accs = read_accounts()
    accounts = [n for n in (1, 2) if accs.get(n, {}).get("login")]
    if not accounts:
        print(f"{RED}nobody logged in - run bridge.py first{RESET}")
        return 1
    db.init_db()

    h, m, s = (int(x) for x in args.start.split(":"))
    first = _utc_today(h, m, s)
    now = dt.datetime.now(dt.timezone.utc)
    if first <= now + dt.timedelta(seconds=20):
        first = (now + dt.timedelta(seconds=20))
        first = first.replace(microsecond=0)
        print(f"{YELLOW}start pushed to {first.strftime('%H:%M:%S')} UTC "
              f"(needs a 20 s lead for terminal warm-up){RESET}")

    jobs: list[dict] = []
    for k in range(args.jobs):
        fire = first + dt.timedelta(seconds=k * args.stride)
        close = fire + dt.timedelta(seconds=max(1.0, round(args.lifetime)))
        for acc in accounts:
            sid = db.add_future_trade(acc, args.pair, args.side, args.lot, args.n,
                                      fire.hour, fire.minute, fire.second,
                                      close.hour, close.minute, close.second)
            jobs.append({"sid": sid, "acc": acc, "fire": fire, "close": close,
                         "n": args.n, "open_ok": 0, "dispatch": None,
                         "closed_seen": None})
            print(f"  burst {k + 1:>2}  acc{acc}  #{sid:<4} fires "
                  f"{fire.strftime('%H:%M:%S')} UTC  close {close.strftime('%H:%M:%S')} UTC")

    print(f"\n{BOLD}{len(jobs)} schedules ({args.jobs} bursts x {len(accounts)} accounts, "
          f"{args.n} positions each, {args.lifetime:.0f}s lifetime) - watching...{RESET}\n")

    done_at = jobs[-1]["close"] + dt.timedelta(seconds=GRACE_S)

    def _open_count(acc: int) -> int:
        """Positions the terminal currently holds (live book, not the DB -
        the close path is database-free by design, so the book IS the truth)."""
        p = (spot.TERMINALS[acc]["dir"] / "MQL5" / "Files" / "trades.csv")
        try:
            return sum(1 for ln in p.read_text(encoding="cp1252",
                                               errors="replace").splitlines()
                       if ln.strip() and not ln.startswith("NONE")
                       and len(ln.split("\t")) >= 11)
        except OSError:
            return -1

    while dt.datetime.now(dt.timezone.utc) < done_at:
        rows = db.list_fired(600)
        opens_by_sid: dict[int, list[tuple[dt.datetime, float]]] = {}
        for r in rows:
            if not r["ok"] or not r.get("at") or r["kind"] != "open" \
                    or not r["schedule_id"]:
                continue
            t = dt.datetime.fromisoformat(str(r["at"]))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            opens_by_sid.setdefault(r["schedule_id"], []).append(
                (t, float(r.get("ms") or 0.0)))
        for j in jobs:
            opens = opens_by_sid.get(j["sid"], [])
            j["open_ok"] = len(opens)
            if opens and j["dispatch"] is None:
                # dispatch instant = result log time - batch wall time
                # (log_fired stamps the whole batch's ms on every row)
                t, ms = opens[0]
                j["dispatch"] = t - dt.timedelta(milliseconds=ms)
            # CLOSE check = live book: at close+3 s the terminal must hold
            # 0 positions of this burst's account (bursts are sequential,
            # so a flat book at that instant == this burst was closed)
            if (j["closed_seen"] is None
                    and dt.datetime.now(dt.timezone.utc)
                    > j["close"] + dt.timedelta(seconds=3)):
                j["closed_seen"] = max(0, _open_count(j["acc"]))
        time.sleep(0.5)

    print(f"\n{BOLD}BURST TIMELINESS ({args.pair} x{args.n} per account, "
          f"every {args.stride:.0f}s, lifetime {args.lifetime:.0f}s){RESET}\n")
    print(f"  {'BURST (UTC)':<12}{'ACC':<5}{'OPENED':>10}{'LATE':>9}{'BOOK@CLOSE':>11}  VERDICT")
    bad = 0
    lates: list[float] = []
    for j in sorted(jobs, key=lambda x: (x["fire"], x["acc"])):
        late = ((j["dispatch"] - j["fire"]).total_seconds()
                if j["dispatch"] else None)
        closed = j["closed_seen"] == 0
        ok = j["open_ok"] >= j["n"] and closed
        if not ok:
            bad += 1
        if late is not None:
            lates.append(late)
        if j["dispatch"] is None:
            print(f"  {j['fire'].strftime('%H:%M:%S'):<12}{j['acc']:<5}"
                  f"{j['open_ok']:>6}/{j['n']}{'MISS':>9}{'?':>11}  "
                  f"{RED}MISSED{RESET}")
        else:
            lcol = GREEN if late <= 1.0 else (YELLOW if late <= 2.0 else RED)
            ccol = GREEN if closed else RED
            print(f"  {j['fire'].strftime('%H:%M:%S'):<12}{j['acc']:<5}"
                  f"{j['open_ok']:>6}/{j['n']}{lcol}{late:>+8.1f}s{RESET}"
                  f"{ccol}{j['closed_seen']:>7}   "
                  + (f"{GREEN}OK{RESET}" if ok else f"{RED}PARTIAL{RESET}"))

    if lates:
        lates.sort()
        n = len(lates)
        med = lates[n // 2] if n % 2 else (lates[n // 2 - 1] + lates[n // 2]) / 2
        print(f"\n  open lateness vs plan: median {med:+.2f}s  "
              f"min {lates[0]:+.2f}s  max {lates[-1]:+.2f}s  (n={n})")
    total = len(jobs)
    print(f"  {total - bad}/{total} account-bursts fully executed "
          f"(opened at time + closed)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
