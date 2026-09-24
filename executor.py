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

from config import CONFIG, setup_logging

from pathlib import Path

from spot import exec_in_path, exec_out_path, feed_age, BOLD, DIM, RESET, GREEN, RED
import database as db

log = setup_logging(__name__)

# Fast constants
EXEC_POLL_INTERVAL = 0.0005       # 0.5 ms polling for the EA's result
EXEC_POLL_SLEEP = 0.0002          # s sleep between result polls after the spin window
EXEC_REASSERT_INTERVAL = 0.001    # 1 ms pointer re-assert
SCHEDULER_POLL_IDLE = 0.250       # s between DB re-lists when no fire is near
SCHEDULER_POLL_NEAR = 0.005       # s steps through the final 2 s before a fire
SCHEDULER_NEAR_WINDOW = 2.0       # s - switch to fine steps inside this window
FIRE_SPIN_WINDOW = 0.020          # s - busy-spin the final 20 ms for precision
STAGGER_MS = 0.0                  # stagger between multi-position orders: NONE -
                                  # orders are queued back-to-back and the terminal
                                  # pipelines them; a sleep here only delayed fires
MAX_FIRE_LATE = 120.0             # s - missed by more than this = reschedule, NOT fire late
FIRE_DEADLINE = 20.0              # s - max wall time for one schedule's openings
                                  # (5 s was too tight: HFM fills pipeline at
                                  # ~1.4 s each through wine, so a 2-open batch
                                  # legitimately runs past 5 s and the results
                                  # were discarded + a false stall-heal fired)
RETRY_DELAY_S = 0.5               # HARDENING: an open/close that FAILED (bad
                                  # volume, requote, price-off, no-connection)
                                  # is automatically retried after 500 ms -
                                  # 3 attempts max, then it reports for real
RETRY_MAX = 3
# Hard rejects a retry can NEVER fix - re-sending them just burned the
# scheduler for a second (or doubled a later recovery) and tripled every
# row in the fired log (one failure showed up as 3 identical-looking rows,
# which read as 'duplicate trades' in the panel).  Transient rejects that
# DO benefit from a retry: requotes (10004/10006/10008), price-off (10015),
# no-quotes (10020/10021), and 'not responding' timeouts handled elsewhere.
HARD_REJECT_MARKERS = ("10017", "10019", "10027",
                       "unknown symbol", "invalid volume")
# TRANSIENT MARKERS: broker-side conditions that clear on their own, so a
# failed slot must RETRY in-epoch instead of skipping silently to tomorrow.
#   10018 market closed = the broker's DAILY BREAK (gold: ~23:00-01:00 UTC,
#   observed live 2026-09-24: XAUUSD feed froze at 22:59:59.961, opens got
#   'bad volume or no quote' at 23:23 and retcode 10018 at 23:26/23:28 while
#   AUDCAD filled at 23:25) - it ends within the hour, so retry THROUGH it.
#   'bad volume or no quote' = no quote at the fire second (also 10020/10021
#   no-quotes, 10004/10006/10008 requotes, 10015 price-off).  NOTE: this
#   string CONTAINS 'bad volume', so it must never match HARD_REJECT_MARKERS
#   or no-quote failures are written off as permanent (that collision is
#   exactly what left schedules #586/#588/#589 unfired).
TRANSIENT_RETRY_MARKERS = ("10018", "bad volume or no quote", "10020", "10021",
                           "10004", "10006", "10008", "10015", "10031",
                           "requote", "price changed", "no quotes",
                           "market closed", "no connection")
# failed-slot retry cadence: first retries are quick (catch a brief pause),
# then back off toward the epoch deadline (1 h) - gold's break is ~2 h at
# most, and a slot that truly cannot fill must stop before the next epoch
# would make the retry pointless (a retry AT the next exec second just
# doubles that day's trade when the broker accepts it).
RETRY_SCHED_FIRST_DELAY_S = 30.0
RETRY_SCHED_MAX_RETRY_S = 300.0    # cap per-retry delay at 5 min
RETRY_SCHED_EPOCH_S = 3600.0       # give up 1 h after the fire second
# Plain-language hints for broker retcodes, appended to fired-log rows so
# the panel explains WHY an order died instead of a bare 'retcode 10017'.
RETCODE_HINTS = {
    "10004": "requote", "10006": "rejected by broker", "10013": "invalid request",
    "10014": "invalid volume", "10015": "invalid price", "10016": "invalid stops",
    "10017": "trade disabled by broker (symbol/account)",
    "10018": "market closed", "10019": "not enough money",
    "10020": "price changed", "10021": "no quotes",
    "10026": "autotrading disabled by SERVER", "10027": "autotrading disabled by TERMINAL",
    "10028": "position locked", "10030": "unsupported filling mode",
    "10031": "no connection to broker",
}


def _explain(detail: str) -> str:
    """'retcode 10017' -> 'retcode 10017 - trade disabled by broker
    (symbol/account)' (only when a hint exists and isn't already there)."""
    for code, hint in RETCODE_HINTS.items():
        if code in detail and hint not in detail:
            return f"{detail} - {hint}"
    return detail
# 24/7 instruments (Deriv synthetics, HFM's XAUUSD247, ...) trade on
# weekends too - the Mon-Fri guard must never eat their schedules.
ALWAYS_ON_MARKERS = ("247", "BOOM", "CRASH", "JUMP", "RANGE BREAK", "STEP INDEX",
                     "DERIV")


def is_always_on_symbol(pair: str) -> bool:
    """True for 24/7 symbols (XAUUSD247, Boom/Crash, ...) - schedules on
    them fire on any weekday, including Saturday/Sunday."""
    p = (pair or "").upper()
    return any(m in p for m in ALWAYS_ON_MARKERS)
HORIZON_CACHE = 0.1               # s - reuse the nearest-fire DB query this long
EXEC_IN_TTL = 15.0                # s - unconsumed exec_in.<id>.txt older than
                                  # this = EA stalled past the client timeout;
                                  # sweep it so it can never fire as a phantom
HEAL_COOLDOWN_S = 600.0           # s - min spacing between stall auto-restarts
HEAL_BOOT_GRACE_S = 120.0         # s - NEVER stall-heal a terminal this soon
                                  # after the app/terminal booted: leftover exec
                                  # files from BEFORE a boot are purged by
                                  # spot.launch_terminal, so a sweep hit right
                                  # after boot is stale data, not a live stall
                                  # (the old 0 s grace restart-stormed BOTH
                                  # terminals seconds after every app start)
# weekdays (0=Mon..6=Sun) schedules are allowed to fire on.
# DEFAULT = ALL SEVEN DAYS: the old Mon-Fri default silently ate every
# weekend schedule (observed: four Sunday schedules skipped with only a
# log line - 'skipped - not a trading day for AUDCAD' - and nothing in
# the panel).  A schedule the operator armed must FIRE, every day; the
# broker itself rejects orders when a market is closed.  Weekday-only
# behaviour is now OPT-IN: MT5_TRADING_DAYS=0,1,2,3,4.
TRADING_DAYS = tuple(int(x) for x in
                     os.getenv("MT5_TRADING_DAYS", "0,1,2,3,4,5,6").split(",")
                     if x.strip()) or (0, 1, 2, 3, 4, 5, 6)

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

    __slots__ = ('inst', 'lock', 'timeout')

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

    # -- internals ----------------------------------------------------------
    def _write_cmd(self, files_dir, parts: tuple[str, ...]) -> tuple[str, Path]:
        """Write ONE exec_in.<id>.txt command file (caller holds self.lock
        and has ensured files_dir exists) -> (id, cmd_file)."""
        cid = uuid.uuid4().hex[:12]
        cmd_file = files_dir / f"exec_in.{cid}.txt"
        line = ("\t".join((cid, *parts)) + "\n").encode("ascii")
        # Atomic write with O_SYNC - single syscall, no fsync needed
        fd = os.open(str(cmd_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_SYNC, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return cid, cmd_file

    def _await_result(self, cid: str, cmd_file, files_dir, out_path,
                      ptr_file, deadline: float) -> tuple[bool, str]:
        """Assert the exec_next.txt pointer until the EA consumes the command,
        then poll exec_out.csv (mtime/size-gated) for this id's result.
        exec_out.csv is re-read only when its mtime/size changes (the EA
        appends results; one stat per poll, one read per actual result -
        no mmap, so the EA truncating/rewriting the file can never crash us).
        """
        t0 = time.monotonic()
        next_assert = 0.0
        last_mtime = 0.0
        last_size = -1
        ptr_tmp = files_dir / f"exec_next.{cid}.tmp"
        try:
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
                            return res[0] == "OK", res[1]
                except OSError:
                    pass

                # Wait strategy: pure busy-spin for the first ~1.5 ms (a
                # 0.5 ms sleep can cost 1-2 ms of timer slack on Linux),
                # then a 0.2 ms sleep - still sub-ms granular, but unlike the
                # old yield-only loop it does not peg a whole core for the
                # full timeout while the EA is busy.
                if now - t0 < 0.0015:
                    continue                       # spin - poll again immediately
                time.sleep(EXEC_POLL_SLEEP)
        finally:
            ptr_tmp.unlink(missing_ok=True)
        return False, (f"terminal {self.inst} not responding "
                       f"(is the terminal running with the SpotDump EA attached?)")

    # -- public API --------------------------------------------------------
    def send(self, *parts: str, timeout: float | None = None) -> tuple[bool, str]:
        """send("OPEN", "EURUSD", "BUY", "0.10") -> (ok, detail).

        Writes exec_in.<id>.txt with O_SYNC, then asserts the exec_next.txt
        pointer until the EA consumes the command and the result appears in
        exec_out.csv.
        """
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
            try:
                cid, cmd_file = self._write_cmd(files_dir, parts)
            except OSError as exc:
                return False, f"cannot write command file: {exc}"
            return self._await_result(cid, cmd_file, files_dir, out_path,
                                      ptr_file, time.monotonic() + to)

    def send_batch(self, *commands: tuple[str, ...],
                   timeout: float | None = None) -> list[tuple[bool, str]]:
        """Queue ALL commands first, THEN await their results.

        A schedule with n positions used to pay n full round trips back to
        back (each bounded by the terminal's own ~5-10 ms through wine);
        batching queues every command in ~0.1 ms and pays ONE wait whose
        terminal-side work overlaps, so n opens land in about the time of
        one.  The EA consumes the queued files in order via the pointer
        protocol and results are matched by id.
        """
        results: list[tuple[bool, str]] = [(False, "no command")] * len(commands)
        if not commands:
            return results
        to = self.timeout if timeout is None else timeout
        with self.lock:
            files_dir, out_path, ptr_file = self._get_paths()
            try:
                files_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return ([(False, f"terminal {self.inst} files dir unavailable: {exc}")]
                        * len(commands))
            queued: list[tuple[str | None, object, OSError | None]] = []
            for parts in commands:
                try:
                    cid, cmd_file = self._write_cmd(files_dir, tuple(parts))
                    queued.append((cid, cmd_file, None))
                except OSError as exc:
                    queued.append((None, None, exc))

            deadline = time.monotonic() + to
            t0 = time.monotonic()
            next_assert = 0.0
            last_mtime, last_size = 0.0, -1
            got: list[tuple[bool, str] | None] = [None] * len(queued)
            ptr_tmp = files_dir / f"exec_next.batch.{uuid.uuid4().hex[:6]}.tmp"
            try:
                while time.monotonic() < deadline and any(r is None for r in got):
                    now = time.monotonic()
                    # Keep the pointer on the OLDEST still-unconsumed command;
                    # the EA deletes each file as it consumes it, so this walks
                    # the queue forward command by command.
                    pending = [cf for (cid, cf, err), r in zip(queued, got)
                               if r is None and cf is not None and cf.exists()]
                    if pending and now >= next_assert:
                        try:
                            ptr_tmp.write_text(pending[0].name, encoding="ascii")
                            ptr_tmp.rename(ptr_file)
                        except OSError:
                            pass
                        next_assert = now + EXEC_REASSERT_INTERVAL
                    try:
                        st = out_path.stat()
                        if st.st_mtime != last_mtime or st.st_size != last_size:
                            last_mtime, last_size = st.st_mtime, st.st_size
                            raw = out_path.read_bytes()
                            for i, (cid, cf, err) in enumerate(queued):
                                if got[i] is None and cid:
                                    res = self._parse_out(raw, cid)
                                    if res is not None:
                                        got[i] = (res[0] == "OK", res[1])
                    except OSError:
                        pass
                    if now - t0 < 0.0015:
                        continue
                    time.sleep(EXEC_POLL_SLEEP)
            finally:
                ptr_tmp.unlink(missing_ok=True)

            for i, (cid, cf, err) in enumerate(queued):
                if got[i] is not None:
                    results[i] = got[i]
                elif err is not None:
                    results[i] = (False, f"cannot write command file: {err}")
                else:
                    results[i] = (False, f"terminal {self.inst} not responding "
                                           f"(is the terminal running with the SpotDump EA attached?)")
        return results

    # convenience wrappers -------------------------------------------------
    def open_trade(self, symbol: str, side: str, lot: float,
                   magic: int = 777001, comment: str = "py",
                   timeout: float | None = None) -> tuple[bool, str]:
        return self.send("OPEN", symbol.upper(), side.upper(), f"{lot:.2f}",
                         "0", "0", str(magic), comment, timeout=timeout)

    def close_position(self, ticket: str) -> tuple[bool, str]:
        return self.send("CLOSE", str(ticket))

    def close_all(self, symbol: str | None = None,
                  timeout: float | None = None) -> tuple[bool, str]:
        return self.send("CLOSEALL", symbol.upper() if symbol else "ALL",
                         timeout=timeout)

    def ping(self) -> tuple[bool, str]:
        return self.send("PING", timeout=1.0)


# module-level senders: web routes import these
CMD1 = SendCommand(1, timeout=CONFIG.exec_timeout_seconds)
CMD2 = SendCommand(2, timeout=CONFIG.exec_timeout_seconds)


def sender_for(account: int) -> SendCommand:
    return CMD1 if account == 1 else CMD2


def _close_one(acc: int, pair: str, attempts: int = RETRY_MAX) -> tuple[bool, str]:
    """One CLOSEALL with the 500 ms failed-close retry - NO database on the
    hot path (close.py parity: the command round trip is the only latency).
    A failed close (requote/price-off/10027) is retried after 500 ms, up to
    `attempts` times, then reported for real.

    The close gets the FULL fire deadline, not the 3 s order timeout: in the
    5 s burst cycle a close lands while the EA is still pipelining the next
    slot's 4 opens, and its result can legitimately take longer than 3 s -
    the old short timeout turned every such close into a 3.5 s-late retry."""
    ok, detail = False, ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            ok, detail = sender_for(acc).close_all(pair, timeout=FIRE_DEADLINE)
        except Exception as exc:
            detail = f"exception: {exc}"
        if ok:
            if attempt > 1:
                log.info(f"close acc{acc} {pair}: OK on retry {attempt} - {detail}")
            return True, detail
        if attempt < attempts:
            log.warning(f"close acc{acc} {pair} failed ({detail}) - "
                        f"retry {attempt + 1}/{attempts} in {RETRY_DELAY_S * 1000:.0f} ms")
            time.sleep(RETRY_DELAY_S)
    log.error(f"close acc{acc} {pair} FAILED after {attempts} attempts: {detail}")
    return False, detail


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
                 '_hz_ts', '_hz_val', '_janitor_ts', '_heal_ts', '_boot_ts')

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
        self._boot_ts = time.monotonic()   # scheduler start = boot grace anchor

    @staticmethod
    def _is_trading_day(dt_obj: dt.datetime) -> bool:
        """Check if the given datetime is a configured trading day
        (default: EVERY day - schedules must never be silently skipped;
        MT5_TRADING_DAYS=0,1,2,3,4 opts back into weekday-only)."""
        return dt_obj.weekday() in TRADING_DAYS

    def _next_trading_day(self, dt_obj: dt.datetime) -> dt.datetime:
        """Move to the next trading day if on weekend."""
        while not self._is_trading_day(dt_obj):
            dt_obj += dt.timedelta(days=1)
        return dt_obj

    # register close time when a schedule fires.  The close H/M/S typed in
    # the panel are LOCAL wall clock (same semantics as the exec time - see
    # database._local_to_utc); anchoring them as UTC once shifted every
    # auto-close by the machine's tz offset.
    def _register_close(self, sch: dict) -> None:
        ch, cm, cs = int(sch["close_h"]), int(sch["close_m"]), int(sch["close_s"])
        now = dt.datetime.now(dt.timezone.utc)
        # Anchor to the schedule's fire time (not 'now'): registration runs
        # AFTER the opens complete, so a short-lifetime schedule (e.g. close
        # 5s after exec) whose opens finish past the close second must close
        # ASAP - not be pushed a full day ahead.
        fire = self._fire_dt(sch)
        if fire:
            fire_local = fire.astimezone()
            anchored = fire_local.replace(hour=ch, minute=cm, second=cs,
                                          microsecond=0)
            if anchored <= fire_local:
                anchored += dt.timedelta(days=1)
            close = anchored.astimezone(dt.timezone.utc)
        else:
            now_local = dt.datetime.now().astimezone()
            close_local = now_local.replace(hour=ch, minute=cm, second=cs,
                                            microsecond=0)
            if close_local <= now_local:
                close_local += dt.timedelta(days=1)
            close = close_local.astimezone(dt.timezone.utc)
        if close <= now:
            close = now + dt.timedelta(seconds=1)
        # 24/7 symbols (XAUUSD247, Boom/Crash, ...) must close on weekends
        # too - without this exemption every Saturday trade stayed open
        # until Monday (observed: 24 open XAUUSD247 positions, zero closes).
        if not is_always_on_symbol(sch.get("pair", "")):
            close = self._next_trading_day(close)
        key = f"{sch['account']}:{sch['pair']}"
        with self._closes_lock:
            prev = self._closes.get(key)
            # EARLIEST close wins: a CLOSEALL closes every position of the
            # pair anyway, so keeping a later pending close silently DELAYED
            # the earlier schedule's close (its positions stayed open until
            # the other schedule's close time - observed with overlapping
            # same-pair schedules).  Firing the earliest pending close closes
            # the earlier batch exactly when it should and is harmless for
            # the later one (its close fires too, then a no-op).
            if prev is None or close < prev[0]:
                self._closes[key] = (close, sch.get("pair", ""))

    def _due_closes(self) -> list[tuple[int, str]]:
        now = dt.datetime.now(dt.timezone.utc)
        due: list[tuple[int, str]] = []
        with self._closes_lock:
            for key, (when, _pair) in list(self._closes.items()):
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
        logged failure - never a surprise order.

        Files left over from BEFORE a boot are purged by launch_terminal, so
        anything swept here while the terminal is inside its boot grace is
        stale disk content - logged, but NEVER treated as a live stall
        (the old code restart-stormed freshly booted terminals)."""
        now_m = time.monotonic()
        if now_m - self._janitor_ts < 5.0:
            return
        self._janitor_ts = now_m
        # fired-log retention: the scheduled page shows the past 24 h only
        try:
            pruned = db.prune_fired(24)
            if pruned:
                log.info(f"retention: pruned {pruned} fired rows older than 24 h")
            pruned = db.prune_inactive_schedules(1)
            if pruned:
                log.info(f"retention: pruned {pruned} inactive schedules older than 1 d")
        except Exception as exc:
            log.warning(f"retention prune failed (non-fatal): {exc}")
        cutoff = time.time() - EXEC_IN_TTL
        stalled: list[int] = []
        for inst in (1, 2):
            try:
                d = exec_in_path(inst)
                for p in d.glob("exec_in.*.txt"):
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                            if now_m - self._boot_ts < HEAL_BOOT_GRACE_S:
                                log.info(f"terminal {inst}: purged pre-boot exec "
                                         f"command {p.name} (boot grace - "
                                         f"not a stall)")
                            else:
                                log.warning(f"terminal {inst}: swept stale exec "
                                            f"command {p.name}")
                                stalled.append(inst)
                    except OSError:
                        pass          # vanished or busy - next pass
            except OSError:
                pass
        # a swept command = the EA never picked it up = the exec channel is
        # stalled even though the feed may look alive: auto-restart that
        # terminal - but only OUTSIDE the boot grace and with a cooldown, so
        # a broken install can never restart-storm
        for inst in set(stalled):
            if (now_m - self._boot_ts < HEAL_BOOT_GRACE_S
                    or now_m - self._heal_ts.get(inst, 0.0) < HEAL_COOLDOWN_S):
                continue
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

    def _claim(self, sch: dict) -> bool:
        """Validate + atomically claim a due schedule.  True = caller fires it.
        (Split out of _process_due so the run loop can claim EVERYTHING first
        and then fire all claimed schedules concurrently.)"""
        nf = self._fire_dt(sch)
        now = dt.datetime.now(dt.timezone.utc)
        if nf is None:
            log.warning(f"schedule #{sch['id']} has unparsable next_fire "
                        f"{sch.get('next_fire')!r} - skipped (delete + recreate it)")
            return False
        if nf > now:
            return False               # someone else already claimed it
        late = (now - nf).total_seconds()
        if late > MAX_FIRE_LATE:
            log.warning(f"schedule #{sch['id']} missed its slot by {late:.0f}s "
                        f"> {MAX_FIRE_LATE:.0f}s - rescheduled, NOT fired late")
            db.reschedule(sch["id"])
            return False
        return db.claim_schedule(sch["id"], sch["next_fire"])

    def _process_due(self, sch: dict) -> None:
        """Claim one due schedule and fire it (kept for API/tests)."""
        if self._claim(sch):
            self._fire(sch)

    # fire one schedule ----------------------------------------------------
    def _fire(self, sch: dict) -> None:
        # Trading-day guard BEFORE the claim - a schedule for a weekend-
        # closed market must be skipped EARLY (in every racing scheduler),
        # never claimed-and-dropped.  24/7 symbols are exempt: they trade
        # every day, so XAUUSD247/Boom/Crash fire on Saturday too.
        # A skip is VISIBLE: it is logged to the fired table so the panel
        # shows exactly why nothing happened (the old silent skip left the
        # operator staring at a schedule that 'did nothing').
        if (not self._is_trading_day(dt.datetime.now(dt.timezone.utc))
                and not is_always_on_symbol(sch.get("pair", ""))):
            log.info(f"schedule #{sch['id']} skipped - not a trading day "
                     f"for {sch.get('pair')}")
            try:
                db.log_fired(sch["id"], sch["account"], sch.get("pair", ""),
                             sch.get("side", ""), sch["lot"], "skip", "",
                             False, "skipped - not a trading day for this "
                                    "pair (weekend guard; MT5_TRADING_DAYS "
                                    "overrides)")
            except Exception as exc:
                log.warning(f"schedule #{sch['id']}: could not log the "
                            f"trading-day skip: {exc}")
            return
        # CRASH-SAFE: the claim already advanced next_fire (daily slot
        # reservation) - an exception here must NEVER swallow the fire
        # silently (a dead fire thread once made schedules vanish: claimed,
        # never fired, no log anywhere).  Before any open succeeded the
        # slot is retried in 2 s; AFTER opens landed the slot is consumed
        # (retrying would duplicate positions).
        opened = {"ok": 0}
        try:
            results = self._fire_inner(sch, opened)
        except Exception as exc:
            if opened["ok"] > 0:
                log.error(f"schedule #{sch['id']} fire crashed AFTER "
                          f"{opened['ok']} opens ({exc}) - slot consumed, "
                          f"NOT retrying (duplicates)")
                # the opened positions must not dangle without an auto-close
                try:
                    self._register_close(sch)
                except Exception:
                    log.error(f"schedule #{sch['id']}: could not arm the "
                              f"auto-close after the crash")
            else:
                log.error(f"schedule #{sch['id']} fire CRASHED before any "
                          f"open ({exc}) - retrying slot in 2 s")
                try:
                    db.reschedule_at(sch["id"],
                                     dt.datetime.now(dt.timezone.utc)
                                     + dt.timedelta(seconds=2))
                except Exception:
                    log.error(f"schedule #{sch['id']} could not be "
                              f"rescheduled after crash: {exc}")
        else:
            # ZERO-OPEN TRANSIENT FAILURE (market closed / no quote / requote):
            # hand the claimed slot back for an in-epoch retry instead of
            # letting the schedule silently skip to tomorrow - that silent
            # skip is what showed up in the panel as a 'duplicate' scheduled
            # a day ahead (observed: XAUUSD slots failed inside gold's daily
            # break with retcode 10018 and 'bad volume or no quote').
            # Any open that landed, and any 'not responding' timeout (the
            # command may have executed server-side), still consume the slot.
            if (opened["ok"] == 0 and results is not None
                    and self._failed_open_transient(results)):
                self._retry_failed_slot(sch, dt.datetime.now(dt.timezone.utc))

    @staticmethod
    def _failed_open_transient(results: list[tuple[bool, str]]) -> bool:
        """True when a 0-open fire hit a TRANSIENT broker condition (market
        closed / no quote / requote / no connection).  Those clear on their
        own, so the claimed slot is handed back for a retry instead of the
        schedule silently skipping to tomorrow (which the panel shows as a
        'duplicate' suddenly scheduled a day ahead)."""
        return any(not ok and any(m in det for m in TRANSIENT_RETRY_MARKERS)
                   for ok, det in results)

    def _retry_failed_slot(self, sch: dict, now_utc: dt.datetime) -> None:
        """Re-arm a fully-failed slot for an in-epoch retry.  next_fire was
        already advanced to TOMORROW by the claim, so the panel's next-fire
        countdown stays honest - a failed slot's retry runs invisibly at
        exec_h:exec_m:exec_s and only ever writes fired rows; if the retry
        opens a position its auto-close is armed normally.  Retries stop at
        the 1 h epoch deadline (a later success would just double the day's
        trade against the next epoch)."""
        fire = self._fire_dt(sch)
        anchor = fire if (fire and fire <= now_utc) else now_utc
        elapsed = (now_utc - anchor).total_seconds()
        if elapsed > RETRY_SCHED_EPOCH_S:
            log.warning(f"schedule #{sch['id']}: slot given up after "
                        f"{elapsed:.0f}s of transient failures - will fire "
                        f"next epoch")
            return
        delay = min(RETRY_SCHED_MAX_RETRY_S,
                    RETRY_SCHED_FIRST_DELAY_S * (1 + elapsed / 120.0))
        nxt = now_utc + dt.timedelta(seconds=delay)
        try:
            db.reschedule_at(sch["id"], nxt)
            log.warning(f"schedule #{sch['id']}: slot failed transiently "
                        f"({sch.get('pair')}) - retrying at "
                        f"{nxt.astimezone().strftime('%H:%M:%S')}")
        except Exception as exc:
            log.error(f"schedule #{sch['id']}: could not arm slot retry: {exc}")

    def _fire_inner(self, sch: dict, opened: dict | None = None) -> None:
        acc = sch["account"]
        pair, side, lot = sch["pair"], sch["side"], sch["lot"]
        n = max(1, int(sch["n_positions"]))
        cmd = sender_for(acc)
        ok_cnt = 0
        # one blocked terminal (algo trading off -> 10027) must not burn the
        # whole queue: 2+ 10027s in one batch = restart the terminal
        blocked_10027 = 0

        # BATCH fire: queue all n OPEN commands (~0.1 ms), then wait once -
        # the terminal-side order times overlap, so n opens cost about one
        # round trip.  The old serial loop paid n full round trips + stagger.
        commands = [("OPEN", pair, side, f"{lot:.2f}", "0", "0",
                     str(777000 + acc), f"sch#{sch['id']}")
                    for _ in range(n)]
        t0 = time.perf_counter()
        results = list(cmd.send_batch(*commands, timeout=FIRE_DEADLINE))
        fire_ms = (time.perf_counter() - t0) * 1000.0

        def _count_open() -> None:
            nonlocal ok_cnt
            ok_cnt += 1
            if opened is not None:
                opened["ok"] = ok_cnt

        for ok, detail in results:
            ticket = detail.split("|")[1] if ok and "|" in detail else ""
            db.log_fired(sch["id"], acc, pair, side, lot, "open",
                         ticket, ok, _explain(detail), ms=fire_ms)
            if ok:
                _count_open()
            elif "10027" in detail:
                blocked_10027 += 1

        # HARDENING - 500 ms auto-retry on positions that FAILED to open:
        # a transient reject (freshly selected symbol without a quote yet,
        # requote, price-off) is retried after 500 ms instead of leaving
        # the schedule short of its positions.  Hard rejects (trade
        # disabled, market closed, unknown symbol, bad volume) are NOT
        # retried: the broker will answer the same a second later, and the
        # extra attempts only wrote 2 more failed rows per open into the
        # fired log.  'not responding' is NOT retried either: a timed-out
        # command may still have executed server-side, and re-sending it
        # would duplicate the position (a stalled terminal is the janitor's
        # + supervisor's job to restart).
        for attempt in range(1, RETRY_MAX):
            retry_idx = [i for i, (ok, det) in enumerate(results)
                         if not ok and "not responding" not in det
                         and not any(m in det for m in HARD_REJECT_MARKERS)]
            if not retry_idx:
                break
            time.sleep(RETRY_DELAY_S)
            log.warning(f"schedule #{sch['id']}: auto-retry {attempt}/"
                        f"{RETRY_MAX - 1} for {len(retry_idx)} failed open(s) "
                        f"(+{RETRY_DELAY_S * 1000:.0f} ms)")
            for i in retry_idx:
                ok, detail = cmd.send(*commands[i], timeout=FIRE_DEADLINE)
                results[i] = (ok, detail)
                ticket = detail.split("|")[1] if ok and "|" in detail else ""
                db.log_fired(sch["id"], acc, pair, side, lot, "open", ticket,
                             ok, f"retry{attempt}: {_explain(detail)}",
                             ms=(time.perf_counter() - t0) * 1000.0)
                if ok:
                    _count_open()
                elif "10027" in detail:
                    blocked_10027 += 1

        if blocked_10027 >= 2:
            log.error(f"schedule #{sch['id']}: terminal {acc} has "
                      f"ALGO TRADING OFF (10027) - restarting it")
            _restart_terminal_async(acc)

        # Register the auto-close only when a position may actually exist:
        # every open answered with a DEFINITE broker reject (retcode /
        # unknown symbol) means nothing was placed - registering a close
        # here used to fire a CLOSEALL at close time that could close
        # ANOTHER schedule's positions early (earliest close wins).
        # Indeterminate results ('not responding') still register: the
        # order may have executed server-side and must not dangle.
        if ok_cnt > 0 or any(not ok and "not responding" in det
                             for ok, det in results):
            self._register_close(sch)
        else:
            log.info(f"schedule #{sch['id']} acc{acc} {pair}: no position "
                     f"opened - auto-close not armed")
        log.info(f"schedule #{sch['id']} acc{acc} {pair} {side} {lot} x{n}: "
                 f"{ok_cnt}/{n} opened in {fire_ms:.1f} ms")
        return results

    # concurrent fan-out -----------------------------------------------------
    def _fire_concurrently(self, schedules: list[dict]) -> None:
        """Fire every claimed schedule AT THE SAME TIME - one thread per
        schedule (per terminal).  Both terminals' order bursts are queued
        into their exec channels within milliseconds of each other, so
        opens are truly simultaneous, not terminal-1-then-terminal-2."""
        if len(schedules) == 1:
            self._fire(schedules[0])       # common case: no thread overhead
            return
        threads = [threading.Thread(target=self._fire, args=(sch,),
                                    daemon=True, name=f"fire-{sch['id']}")
                   for sch in schedules]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _close_concurrently(self, due: list[tuple[int, str]]) -> None:
        """Close every due (acc, pair) AT THE SAME TIME - one daemon thread
        per pair, each with the 500 ms failed-close retry.

        LIGHTNING-FAST + DB-FREE (matches close.py): the close command goes
        straight down the exec channel - NO database round trip on the hot
        path, and no attempt to hold the GIL against the web app's DB work
        while orders are in flight."""
        for acc, pair in due:
            threading.Thread(target=_close_one, args=(acc, pair), daemon=True,
                             name=f"close-{acc}-{pair}").start()

    def _nearest_fire_seconds(self) -> float | None:
        """Seconds until the next active schedule fires (cached 0.1 s so the
        hot loop never hammers the database)."""
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
                # CONCURRENT closes FIRST: when a batch's close lands on
                # the next batch's open second (e.g. the 5 s burst-cycle),
                # the CLOSEALL must be queued BEFORE the new OPENs - the EA
                # executes commands in order, so firing first would close
                # the fresh positions instantly and let the old ones live
                # twice as long.  DB-free + fully parallel.
                due_closes = self._due_closes()
                if due_closes:
                    self._close_concurrently(due_closes)

                # CONCURRENT fires: claim EVERYTHING due first (claiming is
                # instant DB CAS - no trade latency), then fire all claimed
                # schedules in parallel (one thread per schedule, i.e. per
                # terminal) so both terminals' opens land in the same instant
                # instead of terminal 2 waiting for terminal 1's batch.
                claimed: list[dict] = []
                for sch in db.due_schedules():
                    if self._claim(sch):
                        claimed.append(sch)
                if claimed:
                    self._fire_concurrently(claimed)

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