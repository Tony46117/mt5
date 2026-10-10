#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import threading
import time
import uuid
import datetime as dt
import os

from config import CONFIG, setup_logging, map_symbol, classic_pairs_for

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
# Must exceed the longest command round-trip (FIRE_DEADLINE 20 s + retries):
# sweeping earlier deleted in-flight order files mid-fill and triggered a
# spurious terminal restart on slow live fills.
EXEC_IN_TTL = 45.0
HEAL_COOLDOWN_S = 600.0
HEAL_BOOT_GRACE_S = 120.0
TRADING_DAYS = tuple(int(x) for x in
                     os.getenv("MT5_TRADING_DAYS", "0,1,2,3,4,5,6").split(",")
                     if x.strip()) or (0, 1, 2, 3, 4, 5, 6)

EXEC_NEXT_FILE = "exec_next.txt"

class _OutReader:
    """Incremental reader for the EA's exec_out.csv.

    The file grows all day and the old code slurped the WHOLE thing on
    every poll inside a sub-millisecond loop - cost proportional to file
    size, forever.  This reads only the bytes appended since the previous
    poll and stitches a partial line across reads.
    """

    __slots__ = ("path", "offset", "carry")

    def __init__(self, path, offset: int = 0):
        self.path = path
        self.offset = max(0, int(offset))
        self.carry = b""

    def poll(self) -> list[bytes]:
        """Return the complete lines appended since the last poll."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:            # truncated / rotated by the EA
            self.offset, self.carry = 0, b""
        if size == self.offset:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return []
        self.offset = size
        data = self.carry + chunk
        cut = data.rfind(b"\n")
        if cut == -1:                     # partial line - hold it over
            self.carry = data
            return []
        self.carry = data[cut + 1:]
        return data[:cut].split(b"\n")

def _poll_sleep(elapsed: float) -> None:
    """Adaptive backoff for the exec poll loop.

    A flat 0.2 ms sleep is effectively a busy-wait: it burns a whole CPU
    core for the entire order round-trip and steals cycles from wine/MT5
    running on the same box.  Broker fills land in tens of milliseconds,
    so a tight first ~2 ms catches essentially everything, then we back
    off so the rest of the software stays responsive.
    """
    if elapsed < 0.002:
        time.sleep(0)
    elif elapsed < 0.05:
        time.sleep(0.0005)
    elif elapsed < 0.5:
        time.sleep(0.002)
    else:
        time.sleep(0.01)

def _current_exec_dir(inst: int):
    return exec_in_path(inst)

def _current_out_path(inst: int):
    return exec_out_path(inst)

_MKDIR_DONE: set = set()
_MKDIR_LOCK = threading.Lock()

def _ensure_files_dir(files_dir) -> None:
    key = str(files_dir)
    with _MKDIR_LOCK:
        if key in _MKDIR_DONE:
            return
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise
    with _MKDIR_LOCK:
        _MKDIR_DONE.add(key)

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
        # NOTE: no O_SYNC - forcing an fsync on every order cost real
        # milliseconds of latency per trade.  The file name carries a
        # unique uuid, so there is no torn-write risk; a plain close is
        # enough and the EA picks the file up from the directory listing.
        fd = os.open(str(cmd_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return cid, cmd_file

    def _await_result(self, cid: str, cmd_file, files_dir, out_path,
                      ptr_file, deadline: float,
                      start_offset: int = 0) -> tuple[bool, str]:
        t0 = time.monotonic()
        next_assert = 0.0
        ptr_tmp = files_dir / f"exec_next.{cid}.tmp"
        reader = _OutReader(out_path, start_offset)
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

                lines = reader.poll()
                if lines:
                    res = self._parse_out(b"\n".join(lines), cid)
                    if res is not None:
                        return res[0] == "OK", res[1]

                _poll_sleep(now - t0)
        finally:
            ptr_tmp.unlink(missing_ok=True)
        return self._not_responding(cid)

    def _not_responding(self, cid: str) -> tuple[bool, str]:
        return False, self._diagnose_unresponsive(cid)

    def _diagnose_unresponsive(self, cid: str) -> str:
        out_path = self._current_out_path()
        msg = [f"terminal {self.inst} not responding "
               f"(is the terminal running with the SpotDump EA attached?)"]
        if out_path.exists():
            try:
                with open(out_path, "rb") as fh:
                    try:
                        fh.seek(0, 2)
                        size = fh.tell()
                        fh.seek(max(0, size - 8192))
                    except OSError:
                        pass
                    raw = fh.read()
                if raw:
                    nuls = sum(1 for b in raw if b == 0)
                    ratio = nuls / len(raw)
                    if ratio > 0.5:
                        msg.append(f" -- exec_out.csv is corrupted "
                                   f"({nuls}/{len(raw)} bytes are NUL, "
                                   f"ratio {ratio:.0%}); "
                                   f"the EA may have crashed or written binary data")
                    ascii_lines = [l for l in raw.splitlines()
                                   if l and not l.startswith(b'\x00')]
                    if ascii_lines:
                        tail = b'\n'.join(ascii_lines[-3:]).decode('ascii', errors='replace')
                        msg.append(f" -- last readable lines in exec_out.csv:\n"
                                   f"    {tail.replace(chr(10), chr(10) + '    ')}")
            except OSError:
                pass
        try:
            from spot import read_accounts
            accs = read_accounts()
            login = accs.get(self.inst, {}).get('login', '?')
            if login and login != '?':
                from spot import pick_terminal, read_header, read_spots
                term = pick_terminal(login)
                if term:
                    head = read_header(term['trades_path'])
                    trade_allowed = head.get('account_trade_allowed', '1')
                    mql_allowed = head.get('mql_allowed', '1')
                    if trade_allowed == '0':
                        msg.append(f" -- account_trade_allowed=0 "
                                   f"(server blocked trading on this account)")
                    if mql_allowed == '0':
                        msg.append(f" -- mql_allowed=0 "
                                   f"(EA/automated trading blocked on this account)")
                    # If the error was "unknown symbol", list available symbols
                    spots = read_spots()
                    known = sorted(s for s, v in spots.items() if v[0])
                    if known:
                        msg.append(f" -- broker feed has {len(known)} symbols; "
                                   f"first 12: {', '.join(known[:12])}")
        except Exception:
            pass
        return ' '.join(msg)

    def _current_out_path(self) -> Path:
        return _current_out_path(self.inst)

    def send(self, *parts: str, timeout: float | None = None) -> tuple[bool, str]:
        to = self.timeout if timeout is None else timeout
        with self.lock:
            files_dir, out_path, ptr_file = self._get_paths()
            try:
                _ensure_files_dir(files_dir)
            except OSError as exc:
                return False, f"terminal {self.inst} files dir unavailable: {exc}"
            try:
                # remember where the result file ended BEFORE we place the
                # command: our answer can only ever be appended after this,
                # so we never have to scan old output
                start_offset = out_path.stat().st_size
            except OSError:
                start_offset = 0
            try:
                cid, cmd_file = self._write_cmd(files_dir, parts)
            except OSError as exc:
                return False, f"cannot write command file: {exc}"
            return self._await_result(cid, cmd_file, files_dir, out_path,
                                      ptr_file, time.monotonic() + to,
                                      start_offset)

    def send_batch(self, *commands: tuple[str, ...],
                   timeout: float | None = None) -> list[tuple[bool, str]]:
        results: list[tuple[bool, str]] = [(False, "no command")] * len(commands)
        if not commands:
            return results
        to = self.timeout if timeout is None else timeout
        with self.lock:
            files_dir, out_path, ptr_file = self._get_paths()
            try:
                _ensure_files_dir(files_dir)
            except OSError as exc:
                return ([(False, f"terminal {self.inst} files dir unavailable: {exc}")]
                        * len(commands))
            try:
                start_offset = out_path.stat().st_size
            except OSError:
                start_offset = 0
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
            got: list[tuple[bool, str] | None] = [None] * len(queued)
            ptr_tmp = files_dir / f"exec_next.batch.{uuid.uuid4().hex[:6]}.tmp"
            reader = _OutReader(out_path, start_offset)
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

                    for line in reader.poll():
                        parts = line.split(b"\t")
                        if len(parts) < 3:
                            continue
                        done_cid = parts[0].decode(errors="replace")
                        for i, (cid, cf, err) in enumerate(queued):
                            if got[i] is None and cid == done_cid:
                                got[i] = (parts[1].decode() == "OK",
                                          parts[2].decode(errors="replace"))
                                break
                    if not any(r is None for r in got):
                        break
                    _poll_sleep(time.monotonic() - t0)
            finally:
                ptr_tmp.unlink(missing_ok=True)

            for i, (cid, cf, err) in enumerate(queued):
                if got[i] is not None:
                    results[i] = got[i]
                elif err is not None:
                    results[i] = (False, f"cannot write command file: {err}")
                else:
                    results[i] = (False, self._diagnose_unresponsive(cid))
        return results

    def open_trade(self, symbol: str, side: str, lot: float,
                   magic: int = 777001, comment: str = "py",
                   timeout: float | None = None) -> tuple[bool, str]:
        # Symbol case is significant (e.g. "Boom 1000 Index") - pass through
        # exactly as given, only stripping whitespace.
        mapped = map_symbol(symbol.strip(), self.inst)
        return self.send("OPEN", mapped, side.upper(), f"{lot:.2f}",
                         "0", "0", str(magic), comment, timeout=timeout)

    def open_pending(self, symbol: str, ptype: str, lot: float, price: float,
                     magic: int = 777002, comment: str = "pend",
                     timeout: float | None = None) -> tuple[bool, str]:
        """Place a pending order: type is BUYLIMIT / BUYSTOP / SELLLIMIT /
        SELLSTOP and `price` is the trigger level.  The result detail is
        `price|order_ticket|volume`, same shape as a market fill."""
        mapped = map_symbol(symbol.strip(), self.inst)
        p = ptype.upper().replace(" ", "").replace("_", "")
        return self.send("PENDING", mapped, p, f"{lot:.2f}", f"{price:.8f}",
                         "0", "0", str(magic), comment, timeout=timeout)

    def close_position(self, ticket: str) -> tuple[bool, str]:
        return self.send("CLOSE", str(ticket))

    def close_all(self, symbol: str | None = None,
                  timeout: float | None = None) -> tuple[bool, str]:
        mapped = map_symbol(symbol.strip(), self.inst) if symbol else "ALL"
        return self.send("CLOSEALL", mapped, timeout=timeout)

    def ping(self) -> tuple[bool, str]:
        return self.send("PING", timeout=1.0)

CMD1 = SendCommand(1, timeout=CONFIG.exec_timeout_seconds)
CMD2 = SendCommand(2, timeout=CONFIG.exec_timeout_seconds)

def sender_for(account: int) -> SendCommand:
    return CMD1 if account == 1 else CMD2


def diagnose_terminal(inst: int) -> dict:
    """Return a structured diagnostic of why a terminal may not be responding."""
    from spot import (read_accounts, pick_terminal, read_header, read_spots,
                     term_running, feed_age, exec_out_path)
    accs = read_accounts()
    login = accs.get(inst, {}).get('login', '')
    result: dict = {
        'inst': inst,
        'login': login,
        'running': term_running(inst),
        'feed_age_s': feed_age(inst),
        'issues': [],
    }
    if not login or login in ('0', '?', ''):
        result['issues'].append('no login configured for this terminal')
        return result
    if not result['running']:
        result['issues'].append('terminal process is not running')
    if result['feed_age_s'] > 5:
        result['issues'].append(f"feed is stale ({result['feed_age_s']:.1f}s old)")
    term = pick_terminal(login)
    if term:
        head = read_header(term['trades_path'])
        trade_allowed = head.get('account_trade_allowed', '1')
        mql_allowed = head.get('mql_allowed', '1')
        if trade_allowed == '0':
            result['issues'].append(
                'account_trade_allowed=0: the MT5 server has blocked trading '
                'on this account (contact your broker)')
        if mql_allowed == '0':
            result['issues'].append(
                'mql_allowed=0: EA/automated trading is blocked on this account')
        if head.get('login') != login:
            result['issues'].append(
                f"EA reports login {head.get('login')!r} but session expects "
                f"{login!r} - account mismatch")
    fout = exec_out_path(inst)
    if fout.exists():
        try:
            raw = fout.read_bytes()
            if raw:
                nuls = sum(1 for b in raw if b == 0)
                if nuls / len(raw) > 0.5:
                    result['issues'].append(
                        f"exec_out.csv is corrupted "
                        f"({nuls}/{len(raw)} bytes are NUL, "
                        f"{nuls/len(raw):.0%})")
        except OSError:
            pass
    spots = read_spots()
    known = sorted(s for s, v in spots.items() if v[0])
    if known:
        result['known_symbols'] = known
    return result


def print_diagnosis(inst: int) -> None:
    d = diagnose_terminal(inst)
    print(f"\n{BOLD}TERMINAL {inst} DIAGNOSTIC{RESET}")
    print(f"  login: {d['login'] or '-'}")
    print(f"  running: {d['running']}")
    print(f"  feed_age: {d['feed_age_s']:.2f}s")
    if d['issues']:
        print(f"  ISSUES ({len(d['issues'])}):")
        for iss in d['issues']:
            print(f"    - {iss}")
    if 'known_symbols' in d:
        syms = d['known_symbols']
        print(f"  broker feed has {len(syms)} symbols; first 12: "
              f"{', '.join(syms[:12])}")
    else:
        print(f"  broker feed: no symbols available")
    print()

def _open_count(acc: int, pair: str | None) -> int | None:
    """Open positions for (acc, pair) right now, or None when unknown.

    None (identity mismatch / no feed) must NEVER read as "empty" - a
    close verified against a veil would declare victory while positions
    stand.  Callers keep waiting/retrying on None.
    """
    from spot import read_accounts, pick_terminal, read_header
    from monitor import read_positions
    login = read_accounts().get(acc, {}).get("login", "")
    term = pick_terminal(login) if login else None
    if not term:
        return None
    try:
        head = read_header(term["trades_path"])
    except Exception:
        return None
    if not head or head.get("login") != login:
        return None
    if pair in (None, "ALL"):
        try:
            return int(head.get("positions", 0))
        except (TypeError, ValueError):
            return None
    try:
        rows = read_positions(acc)
    except Exception:
        return None
    return sum(1 for r in rows if r.get("symbol") == pair)

def _positions_empty(acc: int, pair: str | None, timeout: float = 5.0) -> bool:
    """Poll until the broker reports zero open positions (or timeout).

    The EA fans CLOSEALL out asynchronously, so the command ack only means
    "closes flighted" - this poll is the actual truth, and it costs one
    50 ms tick past the fills instead of serializing N blocking closes.
    """
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        n = _open_count(acc, pair)
        if n == 0:
            return True
        time.sleep(0.05)
    return _open_count(acc, pair) == 0

def _close_one(acc: int, pair: str, attempts: int = RETRY_MAX) -> tuple[bool, str]:
    ok, detail = False, ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            ok, detail = sender_for(acc).close_all(pair, timeout=8.0)
        except Exception as exc:
            detail = f"exception: {exc}"
        # The ack only proves the closes were flighted (async fan-out);
        # verify against the position book before claiming success.
        if _positions_empty(acc, pair, timeout=5.0):
            if attempt > 1:
                log.info(f"close acc{acc} {pair}: verified empty on retry "
                         f"{attempt} - {detail}")
            return True, detail
        if attempt < attempts:
            log.warning(f"close acc{acc} {pair} not empty after attempt "
                        f"{attempt} ({detail}) - "
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
                 '_hz_ts', '_hz_val', '_janitor_ts', '_heal_ts', '_boot_ts')

    def __init__(self):
        super().__init__(daemon=True, name="future-trade-scheduler")
        self.stop_flag = threading.Event()
        self._hz_ts = 0.0
        self._hz_val: float | None = None
        # Keyed by schedule id: two schedules on the same account+symbol with
        # different close times must each arm their own close.  The old
        # account:pair key silently dropped every close but the earliest.
        self._closes: dict[str, tuple[dt.datetime, int, str]] = {}
        self._closes_lock = threading.Lock()
        self._janitor_ts = 0.0
        self._heal_ts = {1: 0.0, 2: 0.0}
        self._boot_ts = time.monotonic()

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
        key = f"sch#{sch['id']}"
        with self._closes_lock:
            self._closes[key] = (close, int(sch["account"]),
                                 sch.get("pair", ""))

    def _due_closes(self) -> list[tuple[int, str]]:
        now = dt.datetime.now(dt.timezone.utc)
        due: list[tuple[int, str]] = []
        with self._closes_lock:
            for key, (when, acc, pair) in list(self._closes.items()):
                if now >= when:
                    due.append((int(acc), pair))
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
            # Check exec_out.csv for NUL corruption.  Only the recent tail
            # matters (same 8 KB window _diagnose_unresponsive uses): the
            # file grows all day, so a whole-file read gets slower every
            # hour, and ancient bytes say nothing about channel health now.
            try:
                out_path = exec_out_path(inst)
                if out_path.exists():
                    with open(out_path, "rb") as fh:
                        try:
                            fh.seek(0, 2)
                            size = fh.tell()
                            fh.seek(max(0, size - 8192))
                        except OSError:
                            pass
                        raw = fh.read()
                    if raw:
                        nuls = sum(1 for b in raw if b == 0)
                        if nuls / len(raw) > 0.5:
                            log.error(f"terminal {inst}: exec_out.csv is "
                                      f"corrupted ({nuls}/{len(raw)} bytes NUL) "
                                      f"- purging and restarting")
                            try:
                                out_path.unlink()
                            except OSError:
                                pass
                            for p in d.glob("exec_in.*.txt"):
                                try:
                                    p.unlink()
                                except OSError:
                                    pass
                            stalled.append(inst)
            except OSError:
                pass
        for inst in set(stalled):
            if (now_m - self._boot_ts < HEAL_BOOT_GRACE_S
                    or now_m - self._heal_ts.get(inst, 0.0) < HEAL_COOLDOWN_S):
                continue
            self._heal_ts[inst] = now_m
            log.error(f"terminal {inst} exec channel stalled/corrupted - "
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
            return False
        return db.claim_schedule(sch["id"], sch["next_fire"])

    def _process_due(self, sch: dict) -> None:
        if self._claim(sch):
            self._fire(sch)

    @staticmethod
    def _terminal_ready(acc: int) -> tuple[bool, str]:
        """Is terminal `acc` up and fed enough to take a scheduled fire?

        Checked BEFORE claiming a slot so a reboot/boot window does not eat
        one-shot schedules: unready terminals leave the slot for the next
        loop iteration (still bounded by MAX_FIRE_LATE).
        """
        try:
            from spot import term_running, feed_age
            if not term_running(acc):
                return False, "terminal not running"
            if feed_age(acc) > 30.0:
                return False, "feed stale"
        except Exception as exc:
            return False, f"readiness check failed: {exc}"
        return True, ""

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
            self._fire_inner(sch, opened)
        except Exception as exc:
            if opened["ok"] > 0:
                log.error(f"schedule #{sch['id']} fire crashed AFTER "
                          f"{opened['ok']} opens ({exc}) - slot consumed, "
                          f"NOT re-fired (duplicates)")
                try:
                    self._register_close(sch)
                except Exception:
                    log.error(f"schedule #{sch['id']}: could not arm the "
                              f"auto-close after the crash")
            else:
                log.error(f"schedule #{sch['id']} fire crashed before any "
                          f"open ({exc}) - slot consumed (no retries)")
        db.deactivate(sch["id"])

    def _fire_inner(self, sch: dict, opened: dict | None = None) -> None:
        acc = sch["account"]
        pair, side, lot = sch["pair"], sch["side"], sch["lot"]
        n = max(1, int(sch["n_positions"]))
        cmd = sender_for(acc)
        # same broker-symbol mapping as every other order path
        # (open_trade / open_pending / close_all all map_symbol).
        # Case is significant ("Boom 1000 Index") - never uppercase.
        mapped = map_symbol(pair.strip(), acc)
        try:
            from spot import read_spots
            spots = read_spots()
            if mapped not in spots or not spots[mapped][0]:
                log.warning(f"schedule #{sch['id']} acc{acc} {pair}: no live "
                            f"quote for {mapped} - firing anyway, the broker "
                            f"decides (manual orders would reject here)")
        except Exception as exc:
            log.warning(f"schedule #{sch['id']}: quote pre-check failed: {exc}")

        # Pre-flight: check account trading permission from the EA header
        try:
            from spot import read_accounts, pick_terminal, read_header
            accs = read_accounts()
            login = accs.get(acc, {}).get('login', '')
            if login:
                term = pick_terminal(login)
                if term:
                    head = read_header(term['trades_path'])
                    trade_allowed = head.get('account_trade_allowed', '1')
                    mql_allowed = head.get('mql_allowed', '1')
                    if trade_allowed == '0' or mql_allowed == '0':
                        reason = ("account_trade_allowed=0 (server blocked)"
                                  if trade_allowed == '0'
                                  else "mql_allowed=0 (EA trading blocked)")
                        log.error(f"schedule #{sch['id']} acc{acc} {pair}: "
                                  f"NOT firing - {reason}")
                        db.log_fired(sch["id"], acc, pair, side, lot, "open",
                                     "", False, f"blocked: {reason}", ms=0.0)
                        return
        except Exception as exc:
            log.warning(f"schedule #{sch['id']}: pre-flight permission check failed: {exc}")

        commands = [("OPEN", mapped, side, f"{lot:.2f}", "0", "0",
                     str(777000 + acc), f"sch#{sch['id']}")
                    for _ in range(n)]
        # Retry loop: transient broker rejects (requote/price changed/no
        # quotes) get a 500 ms retry; partial fills are never retried (that
        # would duplicate positions).  Total loss still deactivates the slot.
        results: list[tuple[bool, str]] = []
        fire_ms = 0.0
        for attempt in range(1, max(1, RETRY_MAX) + 1):
            t0 = time.perf_counter()
            results = list(cmd.send_batch(*commands, timeout=FIRE_DEADLINE))
            fire_ms = (time.perf_counter() - t0) * 1000.0
            ok_cnt = sum(1 for ok, _ in results if ok)
            if ok_cnt > 0 or attempt >= max(1, RETRY_MAX):
                break
            first = results[0][1] if results else ""
            log.warning(f"schedule #{sch['id']} acc{acc} {pair}: open attempt "
                        f"{attempt}/{RETRY_MAX} failed ({first}) - retry in "
                        f"{RETRY_DELAY_S * 1000:.0f} ms")
            time.sleep(RETRY_DELAY_S)

        ok_cnt = 0
        blocked_10027 = 0
        for ok, detail in results:
            ticket = detail.split("|")[1] if ok and "|" in detail else ""
            db.log_fired(sch["id"], acc, pair, side, lot, "open",
                         ticket, ok, _explain(detail), ms=fire_ms)
            if ok:
                ok_cnt += 1
                if opened is not None:
                    opened["ok"] = ok_cnt
            elif "10027" in detail:
                blocked_10027 += 1

        if blocked_10027 >= 2:
            log.error(f"schedule #{sch['id']}: terminal {acc} has "
                      f"ALGO TRADING OFF (10027) - restarting it")
            _restart_terminal_async(acc)

        if ok_cnt > 0:
            self._register_close(sch)
        else:
            log.info(f"schedule #{sch['id']} acc{acc} {pair}: no position "
                     f"opened - auto-close not armed")
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
                    ready, why = self._terminal_ready(int(sch.get("account", 0)))
                    if not ready:
                        log.warning(f"schedule #{sch['id']} due but {why} - "
                                    f"slot held for retry (bounded by "
                                    f"{MAX_FIRE_LATE:.0f}s lateness guard)")
                        continue
                    if self._claim(sch):
                        claimed.append(sch)
                if claimed:
                    self._fire_concurrently(claimed)

                self._sweep_stale_exec_in()

                nearest = self._nearest_fire_seconds()
                if nearest is None:
                    sleep = SCHEDULER_POLL_IDLE
                elif nearest <= SCHEDULER_NEAR_WINDOW:
                    if nearest <= FIRE_SPIN_WINDOW:
                        val = self._nearest_fire_seconds_uncached()
                        if val is not None and val <= FIRE_SPIN_WINDOW:
                            target = time.time() + max(0.0, val)
                            # SLEEP until the last ~2 ms, then spin.  A full
                            # busy-wait for the whole spin window pegged a
                            # CPU core right before every fire - exactly when
                            # the broker round-trip needs that CPU.
                            while not self.stop_flag.is_set():
                                left = target - time.time()
                                if left <= 0.002:
                                    break
                                time.sleep(min(max(left - 0.002, 0.0), 0.005))
                            while (not self.stop_flag.is_set()
                                   and time.time() < target):
                                time.sleep(0)
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
    ap = argparse.ArgumentParser(description="manual / debug CLI for the MT5 bridge")
    ap.add_argument("--ping", action="store_true",
                    help="ping both terminals and show feed age")
    ap.add_argument("--open", nargs=4, metavar=("ACC", "PAIR", "SIDE", "LOT"),
                    help="open a trade: acc pair side lot")
    ap.add_argument("--closeall", nargs=2, metavar=("ACC", "PAIR"),
                    help="close all positions for acc (pair or ALL)")
    ap.add_argument("--status", action="store_true", help="show schedule + fired log")
    args = ap.parse_args()

    if args.ping:
        for acc in (1, 2):
            ok, detail = sender_for(acc).ping()
            feed = feed_age(acc)
            print(f"terminal {acc}: {'OK' if ok else 'FAIL'} ({detail})"
                  f"  feed {feed:.1f}s")
        print()
        for acc in (1, 2):
            print_diagnosis(acc)
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