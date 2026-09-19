#!/usr/bin/env python3.12
"""minute_probe.py - schedule a trade for EACH minute in a range and verify
every one executes AT its set time (opens at exec second, closes ~3 s later).

Times are LOCAL wall-clock (converted to UTC, matching next_fire storage).
Creates all schedules up front, then polls the fired log and prints a
per-minute timeliness table - the definitive answer to "do scheduled trades
execute exactly when set".

Usage:
    python minute_probe.py --from 15:25 --to 15:34 --pair XAUUSD247
    python minute_probe.py --from 15:25 --to 15:34 --sec 5 --lot 0.01 --n 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import config
from config import setup_logging
import database as db
from spot import read_accounts, BOLD, DIM, RESET, GREEN, RED, YELLOW

log = setup_logging(__name__)

LIFETIME_S = 3          # position closes this many seconds after its open
GRACE_S = 25.0          # keep waiting this long past the close before "MISS"


def _local_to_utc_today(hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    local = dt.datetime.now().astimezone()
    return local.replace(hour=h, minute=m, second=0, microsecond=0) \
                .astimezone(dt.timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-minute scheduled-fire audit")
    ap.add_argument("--from", dest="t_from", required=True, metavar="HH:MM",
                    help="first minute to fire (LOCAL time)")
    ap.add_argument("--to", dest="t_to", required=True, metavar="HH:MM",
                    help="last minute to fire (LOCAL time, inclusive)")
    ap.add_argument("--sec", type=int, default=5,
                    help="second within each minute to fire (default 5)")
    ap.add_argument("--stride", type=int, default=1, metavar="MIN",
                    help="fire every Nth minute in the range (default 1 = "
                         "every minute; 3 = :00, :03, :06 ...)")
    ap.add_argument("--pair", type=str, default="XAUUSD247")
    ap.add_argument("--side", type=str, default="BUY", choices=("BUY", "SELL"))
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--n", type=int, default=2, help="opens per account per minute")
    ap.add_argument("--lifetime", type=float, default=LIFETIME_S)
    args = ap.parse_args()

    accs = read_accounts()
    accounts = [n for n in (1, 2) if accs.get(n, {}).get("login")]
    if not accounts:
        print(f"{RED}nobody logged in - run bridge.py first{RESET}")
        return 1

    db.init_db()

    t0 = _local_to_utc_today(args.t_from)
    t1 = _local_to_utc_today(args.t_to)
    if t1 < t0:
        t1 += dt.timedelta(days=1)
    now_utc = dt.datetime.now(dt.timezone.utc)
    if t0 <= now_utc:                      # first minute already gone -> tomorrow
        t0 += dt.timedelta(days=1)
        t1 += dt.timedelta(days=1)

    # build the minute grid
    minutes: list[dt.datetime] = []
    cur = t0
    while cur <= t1:
        minutes.append(cur)
        cur += dt.timedelta(minutes=max(1, args.stride))

    # create ALL schedules up front (exec at minute + --sec, close lifetime later)
    jobs: list[dict] = []
    for fire in minutes:
        exec_t = fire.replace(second=args.sec)
        close_t = exec_t + dt.timedelta(seconds=max(1, round(args.lifetime)))
        for acc in accounts:
            sid = db.add_future_trade(acc, args.pair, args.side, args.lot, args.n,
                                      exec_t.hour, exec_t.minute, exec_t.second,
                                      close_t.hour, close_t.minute, close_t.second)
            jobs.append({"sid": sid, "acc": acc, "planned": exec_t,
                         "planned_close": close_t, "n": args.n,
                         "fired_at": None, "open_ok": 0, "close_ok": 0})
            print(f"  scheduled #{sid}  acc{acc}  {args.pair} {args.side} "
                  f"{args.lot} x{args.n}  fires {exec_t.strftime('%H:%M:%S')} "
                  f"({fire.strftime('%H:%M')} local +{args.sec}s)")

    print(f"\n{BOLD}{len(jobs)} schedules created - watching until "
          f"{(t1 + dt.timedelta(seconds=args.sec + GRACE_S)).strftime('%H:%M:%S')} UTC{RESET}\n")

    # watch: poll until every job opened (or grace expired)
    done_at = t1 + dt.timedelta(seconds=args.sec + GRACE_S)
    next_report = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    reported: set[int] = set()
    while dt.datetime.now(dt.timezone.utc) < done_at:
        for j in jobs:
            if j["sid"] in reported:
                continue
            rows = [r for r in db.list_fired(400) if r["schedule_id"] == j["sid"]]
            j["open_ok"] = sum(1 for r in rows if r["ok"] and r["kind"] == "open")
            # close rows are logged with schedule_id=0 (one CLOSEALL per
            # account/pair) - count them by TIME WINDOW around this job's
            # planned close instead of by schedule id.
            if dt.datetime.now(dt.timezone.utc) > j["planned_close"]:
                j["close_ok"] = sum(
                    1 for r in db.list_fired(400)
                    if r["ok"] and r["kind"] == "close" and r.get("at")
                    and int(r.get("account", -1)) == j["acc"]
                    and j["planned_close"] - dt.timedelta(seconds=2)
                    <= (dt.datetime.fromisoformat(str(r["at"]).replace("+00:00", "+00:00"))
                        .replace(tzinfo=dt.timezone.utc)
                        if dt.datetime.fromisoformat(str(r["at"])).tzinfo is None
                        else dt.datetime.fromisoformat(str(r["at"])))
                    <= j["planned_close"] + dt.timedelta(seconds=GRACE_S))
            opens = [r for r in rows if r["ok"] and r["kind"] == "open" and r.get("at")]
            if opens:
                first = min(dt.datetime.fromisoformat(str(r["at"])).replace(tzinfo=dt.timezone.utc)
                            if dt.datetime.fromisoformat(str(r["at"])).tzinfo is None
                            else dt.datetime.fromisoformat(str(r["at"]))
                            for r in opens)
                j["fired_at"] = first
            if j["open_ok"] >= j["n"] and dt.datetime.now(dt.timezone.utc) > j["planned_close"] + dt.timedelta(seconds=GRACE_S / 2):
                reported.add(j["sid"])
        # live per-minute line as each minute completes
        stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        for j in jobs:
            if j["sid"] in reported or not j["fired_at"]:
                continue
            if j["planned"] < stamp and j["sid"] not in reported:
                reported.add(j["sid"])
        time.sleep(1.0)

    # final table
    print(f"\n{BOLD}PER-MINUTE TIMELINESS ({args.pair}, fires at :{args.sec:02d} "
          f"of each minute, local {args.t_from}-{args.t_to}){RESET}\n")
    print(f"  {'PLANNED (UTC)':<16}{'ACC':<5}{'FIRED (UTC)':<16}"
          f"{'DELTA':>8}{'OPENS':>8}{'CLOSES':>8}  VERDICT")
    misses = 0
    deltas: list[float] = []
    for j in sorted(jobs, key=lambda x: (x["planned"], x["acc"])):
        if j["fired_at"]:
            delta = (j["fired_at"] - j["planned"]).total_seconds()
            deltas.append(delta)
            dcol = GREEN if delta <= 2.0 else (YELLOW if delta <= 5.0 else RED)
            verdict = f"{GREEN}OK{RESET}" if j["open_ok"] >= j["n"] else f"{RED}PARTIAL{RESET}"
            if j["open_ok"] < j["n"]:
                misses += 1
            print(f"  {j['planned'].strftime('%H:%M:%S'):<16}{j['acc']:<5}"
                  f"{j['fired_at'].strftime('%H:%M:%S'):<16}"
                  f"{dcol}{delta:>+7.1f}s{RESET}{j['open_ok']:>5}/{j['n']}"
                  f"{j['close_ok']:>6}   {verdict}")
        else:
            misses += 1
            print(f"  {j['planned'].strftime('%H:%M:%S'):<16}{j['acc']:<5}"
                  f"{'-':<16}{'MISS':>8}{j['open_ok']:>5}/{j['n']}"
                  f"{j['close_ok']:>6}   {RED}MISSED{RESET}")

    print()
    if deltas:
        deltas.sort()
        m = len(deltas)
        med = deltas[m // 2] if m % 2 else (deltas[m // 2 - 1] + deltas[m // 2]) / 2
        print(f"  fire delta vs plan:  median {med:+.1f}s   "
              f"min {deltas[0]:+.1f}s   max {deltas[-1]:+.1f}s   "
              f"(n={m})")
    total = len(jobs)
    print(f"  {total - misses}/{total} minute-jobs executed on their set minute")
    return 0 if misses == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
