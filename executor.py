#!/usr/bin/env python3.12

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

EXEC_POLL_INTERVAL = 0.0005
EXEC_POLL_SLEEP = 0.0002
EXEC_REASSERT_INTERVAL = 0.001
SCHEDULER_POLL_IDLE = 0.250
SCHEDULER_POLL_NEAR = 0.005
SCHEDULER_NEAR_WINDOW = 2.0
FIRE_SPIN_WINDOW = 0.020
STAGGER_MS = 0.0
MAX_FIRE_LATE = 120.0
FIRE_DEADLINE = 20.0
RETRY_DELAY_S = 0.5
RETRY_MAX = 3
HARD_REJECT_MARKERS = ("10017", "10019", "10027",
                       "unknown symbol", "invalid volume")
TRANSIENT_RETRY_MARKERS = ("10018", "bad volume or no quote", "10020", "10021",
                           "10004", "10006", "10008", "10015", "10031",
                           "requote", "price changed", "no quotes",
                           "market closed", "no connection")
RETRY_SCHED_FIRST_DELAY_S = 45.0
RETRY_SCHED_MAX_RETRY_S = 300.0
RETRY_SCHED_EPOCH_S = 7200.0
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
    for code, hint in RETCODE_HINTS.items():
        if code in detail and hint not in detail:
            return f"{detail} - {hint}"
    return detail
ALWAYS_ON_MARKERS = ("247", "BOOM", "CRASH", "JUMP", "RANGE BREAK", "STEP INDEX",
                     "DERIV")

def is_always_on_symbol(pair: str) -> bool:
    p = (pair or "").upper()
    return any(m in p for m in ALWAYS_ON_MARKERS)
HORIZON_CACHE = 0.1
EXEC_IN_TTL = 15.0
HEAL_COOLDOWN_S = 600.0
HEAL_BOOT_GRACE_S = 120.0
TRADING_DAYS = tuple(int(x) for x in
                     os.getenv("MT5_TRADING_DAYS", "0,1,2,3,4,5,6").split(",")
                     if x.strip()) or (0, 1, 2, 3, 4, 5, 6)

EXEC_NEXT_FILE = "exec_next.txt"

def _current_exec_dir(inst: int):
    return exec_in_path(inst)

def _current_out_path(inst: int):
    return exec_out_path(inst)

class SendCommand:

    __slots__ = ('inst', 'lock', 'timeout')

    def __init__(self, inst: int, timeout: float = 3.0):
        self.inst = inst
        self.timeout = timeout
        self.lock = threading.Lock()

    def _get_paths(self):
        files_dir = _current_exec_dir(self.inst)
        out_path = _current_out_path(self.inst)
        ptr_file = files_dir / EXEC_NEXT_FILE
        return files_dir, out_path, ptr_file

    @staticmethod
    def _parse_out(raw: bytes, want: str) -> tuple[str, str] | None:
        want_bytes = want.encode() + b"\t"
        found = None
        for line in reversed(raw.splitlines()):
            if line.startswith(want_bytes):
                parts = line.split(b"\t")
                if len(parts) >= 3:
                    found = (parts[1].decode(), parts[2].decode())
                    break
        return found

    def _write_cmd(self, files_dir, parts: tuple[str, ...]) -> tuple[str, Path]:
        cid = uuid.uuid4().hex[:12]
        cmd_file = files_dir / f"exec_in.{cid}.txt"
        line = ("\t".join((cid, *parts)) + "\n").encode("ascii")
        fd = os.open(str(cmd_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_SYNC, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return cid, cmd_file

    def _await_result(self, cid: str, cmd_file, files_dir, out_path,
                      ptr_file, deadline: float) -> tuple[bool, str]:
        t0 = time.monotonic()
        next_assert = 0.0
        last_mtime = 0.0
        last_size = -1
        ptr_tmp = files_dir / f"exec_next.{cid}.tmp"
        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_assert and cmd_file.exists():
                    try:
                        ptr_tmp.write_text(cmd_file.name, encoding="ascii")
                        ptr_tmp.rename(ptr_file)
                    except OSError:
                        ptr_tmp.unlink(missing_ok=True)
                    next_assert = now + EXEC_REASSERT_INTERVAL

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

                if now - t0 < 0.0015:
                    continue
                time.sleep(EXEC_POLL_SLEEP)
        finally:
            ptr_tmp.unlink(missing_ok=True)
        return False, (f"terminal {self.inst} not responding "
                       f"(is the terminal running with the SpotDump EA attached?)")

    def send(self, *parts: str, timeout: float | None = None) -> tuple[bool, str]:
        to = self.timeout if timeout is None else timeout
        with self.lock:
            files_dir, out_path, ptr_file = self._get_paths()
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

CMD1 = SendCommand(1, timeout=CONFIG.exec_timeout_seconds)
CMD2 = SendCommand(2, timeout=CONFIG.exec_timeout_seconds)

def sender_for(account: int) -> SendCommand:
    return CMD1 if account == 1 else CMD2

def _close_one(acc: int, pair: str, attempts: int = RETRY_MAX) -> tuple[bool, str]:
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
    def _worker():
        try:
            from spot import restart_terminal
            restart_terminal(inst)
        except Exception as exc:
            log.error(f"terminal {inst} restart failed: {exc}")
    threading.Thread(target=_worker, daemon=True,
                     name=f"restart-terminal-{inst}").start()

class FutureTradeScheduler(threading.Thread):

    __slots__ = ('stop_flag', '_closes', '_closes_lock',
                 '_hz_ts', '_hz_val', '_janitor_ts', '_heal_ts', '_boot_ts',
                 '_slot_attempts', '_slot_next_try', '_catchup_done')

    def __init__(self):
        super().__init__(daemon=True, name="future-trade-scheduler")
        self.stop_flag = threading.Event()
        self._hz_ts = 0.0
        self._hz_val: float | None = None
        self._closes: dict[str, dt.datetime] = {}
        self._closes_lock = threading.Lock()
        self._janitor_ts = 0.0
        self._heal_ts = {1: 0.0, 2: 0.0}
        self._boot_ts = time.monotonic()
        self._slot_attempts: dict[int, list] = {}
        self._slot_next_try: dict[int, float] = {}
        self._catchup_done = False

    @staticmethod
    def _is_trading_day(dt_obj: dt.datetime) -> bool:
        return dt_obj.weekday() in TRADING_DAYS

    def _next_trading_day(self, dt_obj: dt.datetime) -> dt.datetime:
        while not self._is_trading_day(dt_obj):
            dt_obj += dt.timedelta(days=1)
        return dt_obj

    def _register_close(self, sch: dict) -> None:
        ch, cm, cs = int(sch["close_h"]), int(sch["close_m"]), int(sch["close_s"])
        now = dt.datetime.now(dt.timezone.utc)
        fire = self._fire_dt(sch)
        if fire and fire <= now:
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
        if not is_always_on_symbol(sch.get("pair", "")):
            close = self._next_trading_day(close)
        key = f"{sch['account']}:{sch['pair']}"
        with self._closes_lock:
            prev = self._closes.get(key)
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

    def _sweep_stale_exec_in(self) -> None:
        now_m = time.monotonic()
        if now_m - self._janitor_ts < 5.0:
            return
        self._janitor_ts = now_m
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
                        pass
            except OSError:
                pass
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
        nf = self._fire_dt(sch)
        now = dt.datetime.now(dt.timezone.utc)
        if nf is None:
            log.warning(f"schedule #{sch['id']} has unparsable next_fire "
                        f"{sch.get('next_fire')!r} - skipped (delete + recreate it)")
            return False
        if nf > now:
            return False
        late = (now - nf).total_seconds()
        if late > MAX_FIRE_LATE:
            log.warning(f"schedule #{sch['id']} missed its slot by {late:.0f}s "
                        f"> {MAX_FIRE_LATE:.0f}s - one-shot schedule done, "
                        f"NOT fired late")
            db.deactivate(sch["id"])
            self._slot_attempts.pop(sch["id"], None)
            self._slot_next_try.pop(sch["id"], None)
            return False
        return db.claim_schedule(sch["id"], sch["next_fire"])

    def _process_due(self, sch: dict) -> None:
        if self._claim(sch):
            self._fire(sch)

    def _fire(self, sch: dict) -> None:
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
        opened = {"ok": 0}
        try:
            results = self._fire_inner(sch, opened)
        except Exception as exc:
            if opened["ok"] > 0:
                log.error(f"schedule #{sch['id']} fire crashed AFTER "
                          f"{opened['ok']} opens ({exc}) - slot consumed, "
                          f"NOT retrying (duplicates)")
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
            if (opened["ok"] == 0 and results is not None
                    and self._failed_open_transient(results)):
                self._retry_failed_slot(sch, dt.datetime.now(dt.timezone.utc))
            else:
                db.deactivate(sch["id"])
                self._slot_attempts.pop(sch["id"], None)
                self._slot_next_try.pop(sch["id"], None)

    @staticmethod
    def _failed_open_transient(results: list[tuple[bool, str]]) -> bool:
        return any(not ok and any(m in det for m in TRANSIENT_RETRY_MARKERS)
                   for ok, det in results)

    def _retry_failed_slot(self, sch: dict, now_utc: dt.datetime) -> None:
        sid = sch["id"]
        st = self._slot_attempts.get(sid)
        if st is None:
            st = [time.monotonic(), 0]
            self._slot_attempts[sid] = st
        st[1] += 1
        elapsed = time.monotonic() - st[0]
        if elapsed > RETRY_SCHED_EPOCH_S:
            self._slot_attempts.pop(sid, None)
            self._slot_next_try.pop(sid, None)
            try:
                db.log_fired(sid, sch["account"], sch.get("pair", ""),
                             sch.get("side", ""), sch["lot"], "giveup", "",
                             False,
                             f"slot abandoned after {st[1]} attempts in "
                             f"{elapsed / 60:.0f} min - broker kept rejecting "
                             f"(market closed / no quote)")
            except Exception:
                pass
            log.warning(f"schedule #{sid}: slot given up after {st[1]} "
                        f"attempts / {elapsed / 60:.0f} min - one-shot done")
            db.deactivate(sid)
            return
        delay = min(RETRY_SCHED_MAX_RETRY_S,
                    max(RETRY_SCHED_FIRST_DELAY_S, 45.0 * st[1]))
        self._slot_next_try[sid] = time.monotonic() + delay
        log.warning(f"schedule #{sid} {sch.get('pair')}: broker rejected the "
                    f"opens (market closed / no quote) - retry #{st[1]} in "
                    f"{delay:.0f} s")

    def retry_snapshot(self) -> dict[int, dict]:
        now_m = time.monotonic()
        out: dict[int, dict] = {}
        for sid, (_t0, tries) in self._slot_attempts.items():
            nxt = self._slot_next_try.get(sid)
            out[sid] = {"tries": tries,
                        "next_in": (max(0.0, round(nxt - now_m))
                                    if nxt is not None else None)}
        return out

    def _recover_pending_slots(self) -> None:
        try:
            boot_iso = (dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(seconds=time.monotonic()))
            since = (boot_iso - dt.timedelta(minutes=10)).isoformat(
                timespec="seconds")
            rows = db.fired_since(since)
        except Exception as exc:
            log.warning(f"boot catch-up scan failed (non-fatal): {exc}")
            return
        if not rows:
            return
        last_by_sched: dict[int, dict] = {}
        for r in rows:
            last_by_sched[r["schedule_id"]] = r
        now = dt.datetime.now(dt.timezone.utc)
        for sid, r in last_by_sched.items():
            if r["ok"] or r["kind"] not in ("open", "retry-open"):
                continue
            if not any(m in (r["detail"] or "")
                       for m in TRANSIENT_RETRY_MARKERS):
                continue
            sch = db.get_schedule(sid)
            if not sch or not sch.get("active"):
                continue
            nf = self._fire_dt(sch)
            if nf is None or nf <= now:
                continue
            log.info(f"boot catch-up: schedule #{sid} had a transiently "
                     f"failed slot before the restart - re-arming retries")
            self._retry_failed_slot(sch, now)

    def _fire_inner(self, sch: dict, opened: dict | None = None) -> None:
        acc = sch["account"]
        pair, side, lot = sch["pair"], sch["side"], sch["lot"]
        n = max(1, int(sch["n_positions"]))
        cmd = sender_for(acc)
        ok_cnt = 0
        blocked_10027 = 0

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

        summary_mode = (ok_cnt == 0 and bool(results)
                        and self._failed_open_transient(results))
        if summary_mode:
            try:
                db.log_fired(sch["id"], acc, pair, side, lot, "retry-open",
                             "", False,
                             f"x{n} rejected - {_explain(results[0][1])} "
                             f"(auto-retrying until the market reopens)")
            except Exception:
                pass
            log.info(f"schedule #{sch['id']} acc{acc} {pair} {side} {lot} x{n}: "
                     f"0/{n} opened in {fire_ms:.1f} ms (transient - slot "
                     f"retry scheduled)")
            return results

        for ok, detail in results:
            ticket = detail.split("|")[1] if ok and "|" in detail else ""
            db.log_fired(sch["id"], acc, pair, side, lot, "open",
                         ticket, ok, _explain(detail), ms=fire_ms)
            if ok:
                _count_open()
            elif "10027" in detail:
                blocked_10027 += 1

        first_attempt_all_transient = (
            ok_cnt == 0 and bool(results) and self._failed_open_transient(results))
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

        if ok_cnt > 0 or any(not ok and "not responding" in det
                             for ok, det in results):
            self._register_close(sch)
            self._slot_attempts.pop(sch["id"], None)
            self._slot_next_try.pop(sch["id"], None)
        else:
            log.info(f"schedule #{sch['id']} acc{acc} {pair}: no position "
                     f"opened - auto-close not armed")
            self._slot_attempts.pop(sch["id"], None)
            self._slot_next_try.pop(sch["id"], None)
        log.info(f"schedule #{sch['id']} acc{acc} {pair} {side} {lot} x{n}: "
                 f"{ok_cnt}/{n} opened in {fire_ms:.1f} ms")
        return results

    def _fire_concurrently(self, schedules: list[dict]) -> None:
        if len(schedules) == 1:
            self._fire(schedules[0])
            return
        threads = [threading.Thread(target=self._fire, args=(sch,),
                                    daemon=True, name=f"fire-{sch['id']}")
                   for sch in schedules]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _close_concurrently(self, due: list[tuple[int, str]]) -> None:
        for acc, pair in due:
            threading.Thread(target=_close_one, args=(acc, pair), daemon=True,
                             name=f"close-{acc}-{pair}").start()

    def _nearest_fire_seconds(self) -> float | None:
        now_m = time.monotonic()
        if now_m - self._hz_ts < HORIZON_CACHE:
            return self._hz_val
        val = self._nearest_fire_seconds_uncached()
        self._hz_ts, self._hz_val = now_m, val
        return val

    def _nearest_fire_seconds_uncached(self) -> float | None:
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
                due_closes = self._due_closes()
                if due_closes:
                    self._close_concurrently(due_closes)

                claimed: list[dict] = []
                for sch in db.due_schedules():
                    if self._claim(sch):
                        claimed.append(sch)
                if claimed:
                    self._fire_concurrently(claimed)

                if self._slot_next_try:
                    now_m = time.monotonic()
                    for sid in [s for s, t in self._slot_next_try.items()
                                if now_m >= t]:
                        self._slot_next_try.pop(sid, None)
                        st = self._slot_attempts.get(sid)
                        if st is None:
                            continue
                        sch = next((r for r in db.list_future_trades()
                                    if r["id"] == sid and r["active"]), None)
                        if sch is None:
                            self._slot_attempts.pop(sid, None)
                            continue
                        fired = {"ok": 0}
                        try:
                            results = self._fire_inner(sch, fired)
                        except Exception as exc:
                            log.error(f"schedule #{sid} slot retry crashed "
                                      f"({exc}) - retry continues")
                            self._slot_next_try[sid] = (now_m +
                                                        RETRY_SCHED_FIRST_DELAY_S)
                            continue
                        if fired["ok"] > 0:
                            log.info(f"schedule #{sid}: slot retry FILLED "
                                     f"{fired['ok']} position(s)")
                            self._slot_attempts.pop(sid, None)
                            self._slot_next_try.pop(sid, None)
                            db.deactivate(sid)
                        elif self._failed_open_transient(results):
                            self._retry_failed_slot(sch,
                                                    dt.datetime.now(dt.timezone.utc))
                        else:
                            log.warning(f"schedule #{sid}: slot retry hit a "
                                        f"hard reject - abandoning retries")
                            self._slot_attempts.pop(sid, None)
                            self._slot_next_try.pop(sid, None)
                            db.deactivate(sid)

                if not self._catchup_done and (time.monotonic()
                                               - self._boot_ts) > 5.0:
                    self._catchup_done = True
                    self._recover_pending_slots()

                self._sweep_stale_exec_in()

                nearest = self._nearest_fire_seconds()
                if nearest is None:
                    sleep = SCHEDULER_POLL_IDLE
                elif nearest <= SCHEDULER_NEAR_WINDOW:
                    if nearest <= FIRE_SPIN_WINDOW:
                        val = self._nearest_fire_seconds_uncached()
                        if val is not None and val <= FIRE_SPIN_WINDOW:
                            target = time.time() + max(0.0, val)
                            while (not self.stop_flag.is_set()
                                   and time.time() < target):
                                pass
                        sleep = 0.0
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
    global _scheduler_instance
    if _scheduler_instance and _scheduler_instance.is_alive():
        return _scheduler_instance
    sch = FutureTradeScheduler()
    sch.start()
    _scheduler_instance = sch
    return sch

def stop_scheduler() -> None:
    global _scheduler_instance
    if _scheduler_instance:
        _scheduler_instance.stop_flag.set()
        _scheduler_instance.join(timeout=2.0)
        _scheduler_instance = None

_scheduler_instance: FutureTradeScheduler | None = None

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