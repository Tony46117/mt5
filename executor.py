#!/usr/bin/env python3.12
"""executor.py - the ONLY way python code trades through the bridge.
Optimized for lightning-fast execution.

SendCommand: writes a TAB-separated command line into the terminal's
    MQL5/Files as exec_in.<id>.txt with a unique id, then polls exec_out.csv
    for the matching result.  The SpotDump EA (v1.40+) consumes the file and
    places/closes orders server-side, so a command round-trip is a few tens
    of milliseconds (bounded below by the EA's 50 ms timer).  One lock per
    terminal serialises writers (flask routes, executor thread).
    Reads of exec_out.csv are mtime/size-gated: a stat per poll, a full read
    only when the EA actually appended.  (A previous mmap-based fast path
    could SIGBUS-kill the whole app when the EA truncated/rewrote the file
    under the mapping - plain gated reads are just as fast for a 4 KB file.)

FutureTradeScheduler: background thread that fires each schedule slot to
    the millisecond.  A due schedule is CLAIMED (next_fire moved forward,
    atomic compare-and-swap) before any order is sent, so a crash or a
    second scheduler can never double-fire, and a schedule missed by more
    than MAX_FIRE_LATE is rescheduled instead of burst-firing stale orders:
      * at exec time  -> n_positions market orders (OPEN), back-to-back
      * at close time -> CLOSEALL for that account+pair
    Polling is adaptive: one indexed DB query per wake-up while far from a
    fire, 5 ms steps through the final 2 s, then a busy-spin over the last
    20 ms so the command file lands on the exact second.  (The EA consumes
    commands on its 50 ms timer, so sub-millisecond python polling buys
    nothing - landing the write within ~1-2 ms of the tick is optimal.)
    Results are logged to the `fired` table and shown in the panel.

Run directly for a CLI heartbeat:  python executor.py --ping
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid
import datetime as dt
import os

import config
from config import CONFIG, setup_logging

from spot import exec_in_path, exec_out_path, feed_age, BOLD, DIM, RESET, GREEN, RED
import database as db

log = setup_logging(__name__)

# Fast constants
EXEC_POLL_INTERVAL = 0.0005       # 0.5 ms polling for the EA's result
EXEC_REASSERT_INTERVAL = 0.001    # 1 ms pointer re-assert
SCHEDULER_POLL_IDLE = 0.250       # s between DB re-lists when no fire is near
SCHEDULER_POLL_NEAR = 0.005       # s steps through the final 2 s before a fire
SCHEDULER_NEAR_WINDOW = 2.0       # s - switch to fine steps inside this window
FIRE_SPIN_WINDOW = 0.020          # s - busy-spin the final 20 ms for precision
STAGGER_MS = 0.0005               # s stagger between multi-position orders
MAX_FIRE_LATE = 120.0             # s - missed by more than this = reschedule, NOT fire late
FIRE_DEADLINE = 5.0               # s - max wall time for one schedule's openings
HORIZON_CACHE = 0.1               # s - reuse the nearest-fire DB query this long
EXEC_IN_TTL = 15.0                # s - unconsumed exec_in.<id>.txt older than
                                  # this = EA stalled past the client timeout;
                                  # sweep it so it can never fire as a phantom
HEAL_COOLDOWN_S = 600.0           # s - min spacing between stall auto-restarts

# EA Protocol constants
EXEC_NEXT_FILE = "exec_next.txt"  # pointer file for EA protocol


def _current_exec_dir(inst: int):
    """Get the current exec directory for a terminal instance (resolves dynamically)."""
    return exec_in_path(inst)


def _current_out_path(inst: int):
    """Get the current exec_out.csv path for a terminal instance."""
    return exec_out_path(inst)


class SendCommand:
    """One atomic command file per order + wait for the result in
    exec_out.csv (per-terminal lock serialises writers).

    Protocol (SpotDump EA v1.40+):
    1. Python writes command to exec_in.<id>.txt (TAB-separated line)
    2. Python writes pointer file exec_next.<id>.tmp with the filename,
       then atomically renames to exec_next.txt
    3. EA reads exec_next.txt, opens the command file, DELETES both files,
       executes the command, appends result to exec_out.csv
    4. Python polls exec_out.csv for the matching result id

    This pointer protocol ensures no commands are lost or duplicated.
    """

    __slots__ = ('inst', 'timeout', 'lock')

    def __init__(self, inst: int, timeout: float = 3.0):
        self.inst = inst
        self.timeout = timeout
        self.lock = threading.Lock()

    def _get_paths(self):
        """Get current (files_dir, out_path, ptr_file) for this terminal."""
        files_dir = _current_exec_dir(self.inst)
        out_path = _current_out_path(self.inst)
        ptr_file = files_dir / EXEC_NEXT_FILE
        return files_dir, out_path, ptr_file

    # -- result parsing -----------------------------------------------------
    @staticmethod
    def _parse_out(raw: bytes, want: str) -> tuple[str, str] | None:
        """Find id==want in exec_out text -> ('OK'|'ERR', detail) (newest wins)."""
        want_bytes = want.encode() + b"\t"
        found = None
        # Search from end for newest match
        for line in reversed(raw.splitlines()):
            if line.startswith(want_bytes):
                parts = line.split(b"\t")
                if len(parts) >= 3:
                    found = (parts[1].decode(), parts[2].decode())
                    break
        return found

    # -- public API --------------------------------------------------------
    def send(self, *parts: str, timeout: float | None = None) -> tuple[bool, str]:
        """send("OPEN", "EURUSD", "BUY", "0.10") -> (ok, detail).

        Writes exec_in.<id>.txt with O_SYNC, then asserts the exec_next.txt
        pointer until the EA consumes the command and the result appears in
        exec_out.csv.  exec_out.csv is re-read only when its mtime/size
        changes (the EA appends results; one stat per poll, one read per
        actual result - no mmap, so the EA truncating/rewriting the file
        can never crash us).
        """
        cid = uuid.uuid4().hex[:12]
        to = self.timeout if timeout is None else timeout
        with self.lock:
            files_dir, out_path, ptr_file = self._get_paths()
            # The order channel used to die with '[Errno 2] No such file or
            # directory' when the terminal had never run yet - create the
            # MQL5/Files dir so orders can always be queued.
            try:
                files_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return False, f"terminal {self.inst} files dir unavailable: {exc}"

            cmd_file = files_dir / f"exec_in.{cid}.txt"
            ptr_tmp = files_dir / f"exec_next.{cid}.tmp"
            line = "\t".join((cid, *parts)) + "\n"
            line_bytes = line.encode("ascii")

            # Atomic write with O_SYNC - single syscall, no fsync needed
            try:
                fd = os.open(str(cmd_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_SYNC, 0o644)
                os.write(fd, line_bytes)
                os.close(fd)
            except OSError as exc:
                return False, f"cannot write command file: {exc}"

            deadline = time.monotonic() + to
            t0 = time.monotonic()
            next_assert = 0.0
            last_mtime = 0.0
            last_size = -1

            while time.monotonic() < deadline:
                now = time.monotonic()
                # Re-assert pointer file (atomic rename) - EA deletes before
                # executing.  Stop asserting once the EA has consumed the
                # command file (re-asserting after consumption only makes the
                # EA churn on failed opens until the result lands).
                if now >= next_assert and cmd_file.exists():
                    try:
                        ptr_tmp.write_text(cmd_file.name, encoding="ascii")
                        ptr_tmp.rename(ptr_file)   # atomic pointer assert
                    except OSError:
                        ptr_tmp.unlink(missing_ok=True)
                    next_assert = now + EXEC_REASSERT_INTERVAL

                # Check for result in exec_out.csv (mtime/size-gated read)
                try:
                    st = out_path.stat()
                    if st.st_mtime != last_mtime or st.st_size != last_size:
                        last_mtime, last_size = st.st_mtime, st.st_size
                        raw = out_path.read_bytes()
                        res = self._parse_out(raw, cid)
                        if res is not None:
                            status, detail = res
                            ptr_tmp.unlink(missing_ok=True)
                            return status == "OK", detail
                except OSError:
                    pass

                # Wait strategy: pure busy-spin for the first ~1.5 ms (a
                # 0.5 ms sleep can cost 1-2 ms of timer slack on Linux),
                # then cheap yield-polls - sub-ms latency without core-hogging
                # on the rare timeout.
                if now - t0 < 0.0015:
                    pass                        # spin - poll again immediately
                else:
                    time.sleep(0)               # yield, stay sub-ms granular

            ptr_tmp.unlink(missing_ok=True)
            return False, (f"terminal {self.inst} not responding "
                           f"(is the terminal running with the SpotDump EA attached?)")

    # convenience wrappers -------------------------------------------------
    def open_trade(self, symbol: str, side: str, lot: float,
                   magic: int = 777001, comment: str = "py") -> tuple[bool, str]:
        return self.send("OPEN", symbol.upper(), side.upper(), f"{lot:.2f}",
                         "0", "0", str(magic), comment)

    def close_position(self, ticket: str) -> tuple[bool, str]:
        return self.send("CLOSE", str(ticket))

    def close_all(self, symbol: str | None = None) -> tuple[bool, str]:
        return self.send("CLOSEALL", symbol.upper() if symbol else "ALL")

    def ping(self) -> tuple[bool, str]:
        return self.send("PING", timeout=1.0)


# module-level senders: web routes import these
CMD1 = SendCommand(1, timeout=CONFIG.exec_timeout_seconds)
CMD2 = SendCommand(2, timeout=CONFIG.exec_timeout_seconds)


def sender_for(account: int) -> SendCommand:
    return CMD1 if account == 1 else CMD2


def _restart_terminal_async(inst: int) -> None:
    """Restart a blocked terminal OFF the scheduler thread (10027 self-heal).
    The bridge supervisor will keep the feed alive while it boots; the EA
    comes back with algo trading ON because launch_terminal() enforces it."""
    def _worker():
        try:
            from spot import restart_terminal
            restart_terminal(inst)
        except Exception as exc:
            log.error(f"terminal {inst} restart failed: {exc}")
    threading.Thread(target=_worker, daemon=True,
                     name=f"restart-terminal-{inst}").start()


# --------------------------------------------------------------------------
# future-trade scheduler (microsecond precision)
# --------------------------------------------------------------------------

class FutureTradeScheduler(threading.Thread):
    """Fires scheduled trades to the millisecond.

    Adaptive polling: one indexed DB re-list per wake-up (250 ms while far
    from a fire, 5 ms through the final 2 s), then a 20 ms busy-spin onto
    the exact second.  Never queries the DB more than ~4x/s in steady state
    (the old 0.5 ms hot loop did 2000/s and starved the web app of the
    SQLite lock)."""

    __slots__ = ('stop_flag', '_closes', '_closes_lock',
                 '_hz_ts', '_hz_val', '_janitor_ts', '_heal_ts')

    def __init__(self):
        super().__init__(daemon=True, name="future-trade-scheduler")
        self.stop_flag = threading.Event()
        self._hz_ts = 0.0          # nearest-fire query cache (monotonic ts)
        self._hz_val: float | None = None
        # close jobs: {account:pair: close_datetime (UTC)}
        self._closes: dict[str, dt.datetime] = {}
        self._closes_lock = threading.Lock()
        self._janitor_ts = 0.0     # last stray exec_in sweep (monotonic)
        self._heal_ts = {1: 0.0, 2: 0.0}   # last stall-heal per terminal

    @staticmethod
    def _is_trading_day(dt_obj: dt.datetime) -> bool:
        """Check if the given datetime is a trading day (Mon-Fri)."""
        return dt_obj.weekday() < 5

    def _next_trading_day(self, dt_obj: dt.datetime) -> dt.datetime:
        """Move to the next trading day if on weekend."""
        while not self._is_trading_day(dt_obj):
            dt_obj += dt.timedelta(days=1)
        return dt_obj

    # register close time when a schedule fires (UTC - must match next_fire,
    # which add_future_trade/reschedule store in UTC; local time here made
    # auto-closes drift by the tz offset)
    def _register_close(self, sch: dict) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        close = now.replace(hour=int(sch["close_h"]), minute=int(sch["close_m"]),
                            second=int(sch["close_s"]), microsecond=0)
        # Anchor to the schedule's fire time (not 'now'): registration runs
        # AFTER the opens complete, so a short-lifetime schedule (e.g. close
        # 5s after exec) whose opens finish past the close second must close
        # ASAP - not be pushed a full day ahead.
        fire = self._fire_dt(sch)
        if fire:
            anchored = fire.replace(hour=int(sch["close_h"]),
                                    minute=int(sch["close_m"]),
                                    second=int(sch["close_s"]), microsecond=0)
            if anchored <= fire:
                anchored += dt.timedelta(days=1)
            close = anchored
        if close <= now:
            close = now + dt.timedelta(seconds=1)
        close = self._next_trading_day(close)
        key = f"{sch['account']}:{sch['pair']}"
        with self._closes_lock:
            prev = self._closes.get(key)
            if prev is None or close > prev:
                self._closes[key] = close

    def _due_closes(self) -> list[tuple[int, str]]:
        now = dt.datetime.now(dt.timezone.utc)
        due: list[tuple[int, str]] = []
        with self._closes_lock:
            for key, when in list(self._closes.items()):
                if now >= when:
                    acc_s, pair = key.split(":", 1)
                    due.append((int(acc_s), pair))
                    del self._closes[key]
        return due

    # stray-command janitor --------------------------------------------------
    def _sweep_stale_exec_in(self) -> None:
        """Delete unconsumed exec_in.<id>.txt command files older than the
        client timeout.  When a terminal's EA stalls (wine pause, etc.) its
        queued command outlives the sender's timeout and the EA can consume
        it minutes later as a phantom trade.  Sweeping turns a stall into a
        logged failure - never a surprise order."""
        now_m = time.monotonic()
        if now_m - self._janitor_ts < 5.0:
            return
        self._janitor_ts = now_m
        cutoff = time.time() - EXEC_IN_TTL
        stalled: list[int] = []
        for inst in (1, 2):
            try:
                d = exec_in_path(inst)
                for p in d.glob("exec_in.*.txt"):
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                            log.warning(f"terminal {inst}: swept stale exec "
                                        f"command {p.name}")
                            stalled.append(inst)
                    except OSError:
                        pass          # vanished or busy - next pass
            except OSError:
                pass
        # a swept command = the EA never picked it up = the exec channel is
        # stalled even though the feed may look alive: auto-restart that
        # terminal (cooldown keeps a broken install from restart-storming)
        for inst in set(stalled):
            if now_m - self._heal_ts.get(inst, 0.0) > HEAL_COOLDOWN_S:
                self._heal_ts[inst] = now_m
                log.error(f"terminal {inst} exec channel stalled - "
                          f"auto-restarting it")
                _restart_terminal_async(inst)

    @staticmethod
    def _fire_dt(sch: dict) -> dt.datetime | None:
        """Schedule next_fire as an aware UTC datetime (None if unparsable)."""
        nf = sch.get("next_fire")
        if not nf:
            return None
        if isinstance(nf, str):
            try:
                nf = dt.datetime.fromisoformat(nf)
            except ValueError:
                return None
        if getattr(nf, "tzinfo", None) is None:
            nf = nf.replace(tzinfo=dt.timezone.utc)
        return nf

    def _process_due(self, sch: dict) -> None:
        """Claim one due schedule and fire it.  Safety rails:
        * not actually due (already claimed by another scheduler) -> skip;
        * missed by more than MAX_FIRE_LATE (app was down / EA dead) ->
          reschedule, never burst-fire stale orders;
        * claim via database.claim_schedule (atomic CAS on next_fire) so
          two app instances can never double-fire the same slot."""
        nf = self._fire_dt(sch)
        now = dt.datetime.now(dt.timezone.utc)
        if nf is None:
            log.warning(f"schedule #{sch['id']} has unparsable next_fire "
                        f"{sch.get('next_fire')!r} - skipped (delete + recreate it)")
            return
        if nf > now:
            return                     # someone else already claimed it
        late = (now - nf).total_seconds()
        if late > MAX_FIRE_LATE:
            log.warning(f"schedule #{sch['id']} missed its slot by {late:.0f}s "
                        f"> {MAX_FIRE_LATE:.0f}s - rescheduled, NOT fired late")
            db.reschedule(sch["id"])
            return
        if not db.claim_schedule(sch["id"], sch["next_fire"]):
            return                     # lost the race - the winner fires it
        self._fire(sch)

    # fire one schedule ----------------------------------------------------
    def _fire(self, sch: dict) -> None:
        # Skip if not a trading day
        if not self._is_trading_day(dt.datetime.now(dt.timezone.utc)):
            log.debug(f"schedule #{sch['id']} skipped - not a trading day")
            return

        acc = sch["account"]
        pair, side, lot = sch["pair"], sch["side"], sch["lot"]
        n = max(1, int(sch["n_positions"]))
        cmd = sender_for(acc)
        ok_cnt = 0
        deadline = time.monotonic() + FIRE_DEADLINE
        # one blocked terminal (algo trading off -> 10027) must not burn the
        # whole queue: after 2 consecutive 10027s stop firing and reschedule
        blocked_10027 = 0

        # Fire all positions rapidly with microsecond stagger
        for i in range(n):
            ok, detail = cmd.open_trade(pair, side, lot,
                                        magic=777000 + acc,
                                        comment=f"sch#{sch['id']}")
            ticket = detail.split("|")[1] if ok and "|" in detail else ""
            db.log_fired(sch["id"], acc, pair, side, lot, "open",
                         ticket, ok, detail)
            if ok:
                ok_cnt += 1
                blocked_10027 = 0
            else:
                if "10027" in detail:
                    blocked_10027 += 1
                    if blocked_10027 >= 2:
                        log.error(f"schedule #{sch['id']}: terminal {acc} has "
                                  f"ALGO TRADING OFF (10027) - restarting it")
                        _restart_terminal_async(acc)
                        break
            if i < n - 1:
                if time.monotonic() >= deadline:   # never block the queue on a dead EA
                    log.warning(f"schedule #{sch['id']}: fire deadline hit - "
                                f"{ok_cnt}/{i + 1} opened, {n - i - 1} skipped")
                    break
                time.sleep(STAGGER_MS)

        self._register_close(sch)
        log.info(f"schedule #{sch['id']} acc{acc} {pair} {side} {lot} x{n}: {ok_cnt}/{n} opened")

    def _nearest_fire_seconds(self) -> float | None:
        """Seconds until the next active schedule fires (cached 0.1 s so the
        hot 0.5 ms loop never hammers the database)."""
        now_m = time.monotonic()
        if now_m - self._hz_ts < HORIZON_CACHE:
            return self._hz_val
        val = self._nearest_fire_seconds_uncached()
        self._hz_ts, self._hz_val = now_m, val
        return val

    def _nearest_fire_seconds_uncached(self) -> float | None:
        """Uncached DB-backed seconds until the next active fire (None if none)."""
        try:
            rows = db.list_future_trades(active_only=True)
        except Exception:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        best: float | None = None
        for r in rows:
            nf = r.get("next_fire")
            if not nf:
                continue
            if isinstance(nf, str):
                try:
                    nf = dt.datetime.fromisoformat(nf)
                except ValueError:
                    continue
            if getattr(nf, "tzinfo", None) is None:
                nf = nf.replace(tzinfo=dt.timezone.utc)
            d = (nf - now).total_seconds()
            if d >= 0 and (best is None or d < best):
                best = d
        return best

    def run(self) -> None:
        log.info(f"future-trade scheduler running "
                 f"(idle {SCHEDULER_POLL_IDLE * 1000:.0f} ms, "
                 f"fine {SCHEDULER_POLL_NEAR * 1000:.0f} ms inside "
                 f"{SCHEDULER_NEAR_WINDOW:.0f} s, spin {FIRE_SPIN_WINDOW * 1000:.0f} ms, "
                 f"{STAGGER_MS * 1000:.1f} ms stagger)")
        while not self.stop_flag.is_set():
            try:
                # Check due schedules (claim-then-fire: never double-fire)
                for sch in db.due_schedules():
                    self._process_due(sch)

                # Check due closes
                for acc, pair in self._due_closes():
                    cmd = sender_for(acc)
                    ok, detail = cmd.close_all(pair)
                    db.log_fired(0, acc, pair, "-", 0.0, "close", "", ok, detail)
                    log.info(f"close acc{acc} {pair}: {detail}")

                # Sweep stray exec_in commands (EA stalled past its timeout)
                self._sweep_stale_exec_in()

                # Adaptive sleep: coarse while far from a fire, fine inside
                # the near window, exact busy-spin over the last 20 ms.
                nearest = self._nearest_fire_seconds()
                if nearest is None:
                    sleep = SCHEDULER_POLL_IDLE
                elif nearest <= SCHEDULER_NEAR_WINDOW:
                    if nearest <= FIRE_SPIN_WINDOW:
                        # fresh (uncached) read, then busy-spin to the exact
                        # fire second - NO further DB reads inside the spin
                        val = self._nearest_fire_seconds_uncached()
                        if val is not None and val <= FIRE_SPIN_WINDOW:
                            target = time.time() + max(0.0, val)
                            while (not self.stop_flag.is_set()
                                   and time.time() < target):
                                pass              # millisecond-precision landing
                        sleep = 0.0               # process due immediately
                    else:
                        sleep = SCHEDULER_POLL_NEAR
                else:
                    sleep = min(SCHEDULER_POLL_IDLE,
                                max(SCHEDULER_POLL_NEAR, nearest / 8))
            except Exception as exc:
                log.error(f"executor error: {exc}")
                sleep = 0.5
            if sleep > 0:
                self.stop_flag.wait(sleep)


def start_scheduler() -> FutureTradeScheduler:
    """Start the singleton scheduler thread (idempotent)."""
    global _scheduler_instance
    if _scheduler_instance and _scheduler_instance.is_alive():
        return _scheduler_instance
    sch = FutureTradeScheduler()
    sch.start()
    _scheduler_instance = sch
    return sch


def stop_scheduler() -> None:
    """Stop the scheduler if running."""
    global _scheduler_instance
    if _scheduler_instance:
        _scheduler_instance.stop_flag.set()
        _scheduler_instance.join(timeout=2.0)
        _scheduler_instance = None


# Module-level scheduler instance for start/stop_scheduler
_scheduler_instance: FutureTradeScheduler | None = None


# --------------------------------------------------------------------------
# CLI: heartbeat / manual commands
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Trade executor via the SpotDump exec channel")
    ap.add_argument("--ping", action="store_true", help="PING both terminals")
    ap.add_argument("--open", nargs=4, metavar=("ACC", "PAIR", "SIDE", "LOT"),
                    help="e.g. --open 1 EURUSD BUY 0.10")
    ap.add_argument("--closeall", nargs=2, metavar=("ACC", "PAIR"),
                    help="close all positions of ACC/PAIR (PAIR=ALL for everything)")
    ap.add_argument("--status", action="store_true", help="show schedule + fired log")
    args = ap.parse_args()

    if args.ping:
        for acc in (1, 2):
            ok, detail = sender_for(acc).ping()
            feed = feed_age(acc)
            print(f"terminal {acc}: {'OK' if ok else 'FAIL'} ({detail})"
                  f"  feed {feed:.1f}s")
        return 0

    if args.open:
        acc, pair, side, lot = args.open
        ok, detail = sender_for(int(acc)).open_trade(pair, side, float(lot))
        print(f"{'OK' if ok else 'FAIL'}: {detail}")
        return 0 if ok else 1

    if args.closeall:
        acc, pair = args.closeall
        ok, detail = sender_for(int(acc)).close_all(None if pair.upper() == "ALL" else pair)
        print(f"{'OK' if ok else 'FAIL'}: {detail}")
        return 0 if ok else 1

    if args.status:
        scheds = db.list_future_trades()
        fired = db.list_fired(20)
        print(f"{BOLD}SCHEDULED TRADES{RESET} ({len(scheds)})")
        for s in scheds:
            active = f"{GREEN}active{RESET}" if s.get("active", 1) else f"{RED}off{RESET}"
            print(f"  #{s['id']:<3} acc{s['account']} {s['pair']:<7} {s['side']:<4} "
                  f"{s['lot']} x{s['n_positions']}  exec {s['exec_h']:02d}:{s['exec_m']:02d}:{s['exec_s']:02d}"
                  f"  close {s['close_h']:02d}:{s['close_m']:02d}:{s['close_s']:02d}  "
                  f"{active}  next {s['next_fire']}")
        print(f"\n{BOLD}FIRED LOG{RESET} (last 20)")
        for r in fired:
            mark = f"{GREEN}OK{RESET}" if r["ok"] else f"{RED}ERR{RESET}"
            print(f"  {r['at']}  acc{r['account']} {r['kind']:<5} {r['pair']:<7} "
                  f"{r['side']:<4} {r['lot']}  {mark} {DIM}{r['detail']}{RESET}")
        if not scheds and not fired:
            print(f"  {DIM}nothing scheduled yet{RESET}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())