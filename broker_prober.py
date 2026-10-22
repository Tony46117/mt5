#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import os
import threading
import time

from config import setup_logging, classic_pairs_for
from spot import read_accounts, BOLD, DIM, RESET, GREEN, RED

log = setup_logging(__name__)

FLAG_FOK = 1
FLAG_IOC = 2

FILLING_NAMES = {1: "FOK", 2: "IOC", 3: "IOC/FOK"}
MODE_NAMES = {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}
EXEC_NAMES = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE", 4: "SYNCHRONIZED"}

PROBE_TTL_S = 600.0
PROBE_TIMEOUT_S = 3.0
# Probe the whole board, not a sample: every symbol the account exposes
# (live and demo alike), so scheduling validation and the panel see each
# account's full tradeable universe.  The cap only exists so a pathological
# catalogue (thousands of expiry CFDs) cannot flood the log.
MAX_PROBE_SYMBOLS = int(os.getenv("MT5_MAX_PROBE_SYMBOLS", "2000"))
PROBE_CHUNK = 30

def _field(fields: list[str], i: int) -> str:
    return fields[i] if len(fields) > i else ""

def _cat_quoted(fields: list[str]) -> bool:
    """Does this catalogue row carry a live quote right now?"""
    try:
        return float(_field(fields, 15)) > 0.0
    except ValueError:
        return False

# Brokers name spot metals differently: FxPro lists GOLD/GOLDoz, not
# XAUUSD.  The catalogue knows the name but a symbol the terminal never
# selected reports bid/ask 0, so the prober has to PROBE it once to make
# the EA subscribe it; the next catalogue/spots dump then carries the quote.
QUOTE_ALIASES = ("XAU", "GOLD", "XAG", "SILVER")


def _unquoted_quote_aliases(cat: dict[str, dict]) -> list[str]:
    """Catalogue metals whose quote is still zero (not yet subscribed)."""
    out: list[str] = []
    for sym, info in cat.items():
        up = str(sym).upper()
        if not any(up.startswith(alias) for alias in QUOTE_ALIASES):
            continue
        try:
            if float((info or {}).get("bid") or 0.0) > 0.0:
                continue
        except (TypeError, ValueError):
            pass
        out.append(sym)
    return out

def catalogue_symbols_for(inst: int) -> list[str]:
    """Every symbol this account can see, broker-quoted ones first.

    Comes from the EA's full catalogue (symbols.csv) - the account's
    complete quote universe, including Deriv's Boom/Crash/Jump/Volatility
    indices, cent-account variants and every broker-specific suffix - and
    falls back to the live watchlist (spots.csv) and then to the classic
    pairs.  No exec round trips: the terminal writes this file itself.
    """
    try:
        from spot import read_symbol_catalogue
        cat = read_symbol_catalogue(inst)
    except Exception:
        cat = {}
    if cat:
        quoted = [s for s, f in cat.items() if _cat_quoted(f)]
        quoted_set = set(quoted)
        rest = [s for s in cat if s not in quoted_set]
        return (quoted + rest)[:MAX_PROBE_SYMBOLS]
    return list(probe_symbols_for(inst))

def _quote_ts_recent(ts: str, max_age_s: float = 120.0) -> bool:
    """Is this spots quote timestamp fresh?  Unparseable -> keep (conservative).

    The feed keeps dead symbols with live-looking bids (e.g. XAUUSD.FIX from
    2024, XAUUSD.F from August) - probing them is pure exec noise, and right
    after a broker switch the file briefly carries the PREVIOUS broker's
    symbols (the c.Z / SP500m 'unknown symbol' storm in 2026-10-13 10:22).
    """
    import datetime as _dt
    s = str(ts or "").strip()[:19]
    if not s:
        return True
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            stamp = _dt.datetime.strptime(s, fmt).timestamp()
            return (time.time() - stamp) <= max_age_s
        except ValueError:
            continue
    return True


def probe_symbols_for(inst: int) -> tuple[str, ...]:
    """Every FRESHLY quoted symbol on this terminal (classic pairs first),
    capped for one fast batch.  Falls back to classic pairs with no feed yet."""
    try:
        from spot import read_spots
        spots = read_spots(max_age=5.0)
        classic = set(classic_pairs_for(inst))
        available = [sym for sym, (bid, ask, ts) in spots.items()
                     if bid and float(bid) > 0.0 and _quote_ts_recent(ts)]
        if available:
            result = [s for s in available if s in classic]
            result.extend(s for s in available if s not in classic)
            return tuple(result[:MAX_PROBE_SYMBOLS])
    except Exception:
        pass
    # Fallback: use classic pairs for this terminal
    return tuple(classic_pairs_for(inst)[:MAX_PROBE_SYMBOLS])


def is_disabled(info: dict | None) -> bool:
    """Did the broker disable this symbol (trade_mode=0 -> retcode 10017)?"""
    if not info:
        return False
    try:
        return int(info.get("trade_mode", 4)) == 0
    except (TypeError, ValueError):
        return False

def parse_probe_fields(fields: list[str]) -> dict | None:
    """PROBE reply / catalogue row -> one capability dict.

    Both sources describe a symbol with the same field layout, so the
    prober's view of the broker is identical whether the data arrived
    from a PROBE round trip or from the terminal's own catalogue.
    """
    if len(fields) < 12:
        return None
    try:
        info = {
            "symbol": fields[0],
            "digits": int(fields[1]),
            "filling_flags": int(fields[2]),
            "trade_exemode": int(fields[3]),
            "trade_mode": int(fields[4]),
            "order_mode": int(fields[5]),
            "stops_level": int(fields[6]),
            "freeze_level": int(fields[7]),
            "volume_min": float(fields[8]),
            "volume_max": float(fields[9]),
            "volume_step": float(fields[10]),
            "spread_pts": int(float(fields[11])),
        }
    except ValueError:
        return None
    # EA v1.73+ appends contract/tick economics; older EAs send 12 fields
    # and simply omit these (scheduling validation degrades gracefully to
    # volume limits).
    if len(fields) >= 15:
        try:
            info["contract_size"] = float(fields[12])
            info["tick_value"] = float(fields[13])
            info["tick_size"] = float(fields[14])
        except ValueError:
            pass
    # catalogue rows (EA v1.75+) also carry the live quote
    if len(fields) >= 17:
        try:
            info["bid"] = float(fields[15])
            info["ask"] = float(fields[16])
        except ValueError:
            pass
    info["filling_allowed"] = FILLING_NAMES.get(info["filling_flags"], "RETURN")
    info["best"] = best_filling_from_flags(info["filling_flags"])
    info["trade_mode_name"] = MODE_NAMES.get(info["trade_mode"], str(info["trade_mode"]))
    info["exec_mode_name"] = EXEC_NAMES.get(info["trade_exemode"], str(info["trade_exemode"]))
    return info

OPT_TTL_S = 1800.0
FILL_SLOW_MS = 1500.0
FILL_RATIO = 2.0

def _journal_fill_ms(inst: int, limit: int = 12) -> list[float]:
    import re
    from pathlib import Path
    try:
        from spot import TERMINALS
        logs_dir = Path(TERMINALS[inst]["dir"]) / "logs"
    except Exception:
        return []
    if not logs_dir.is_dir():
        return []
    logs = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return []
    try:
        with open(logs[-1], "rb") as fh:
            # the encoding BOM sits at the file START, so read it there -
            # checking raw[:2] at the tail offset never sees it and silently
            # decoded UTF-16 into noise (fill times always came back empty)
            head = fh.read(2)
            try:
                fh.seek(0, 2)
                size = fh.tell()
                start = max(0, size - 65536)
                if head == b"\xff\xfe" and start % 2:
                    start += 1
                fh.seek(start)
            except OSError:
                pass
            raw = fh.read()
    except OSError:
        return []
    txt = raw.decode("utf-16-le" if head == b"\xff\xfe" else "utf-8",
                     errors="replace")
    vals = [float(m.group(1)) for m in re.finditer(
        r"done in ([0-9.]+) ms", txt)]
    return vals[-limit:]

def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

def tcp_ms(address: str, timeout: float = 3.0) -> float | None:
    import socket
    host, _, port = address.partition(":")
    try:
        t0 = time.perf_counter()
        with socket.create_connection((host, int(port or 443)), timeout=timeout):
            return (time.perf_counter() - t0) * 1000.0
    except OSError:
        return None

def rank_access_points(server: str) -> list[tuple[str, float]]:
    """Access points for `server`, fastest reachable first.

    Candidates come from the ONE shared registry
    (config.access_point_candidates: env override, learned cache,
    access_points.env, built-ins) instead of a second hardcoded table - so a
    broker added in one place is rankable everywhere, which is what makes
    `--access-point` useful for probing too.
    """
    from config import access_point_candidates
    cands = list(access_point_candidates(server))
    if not cands and server and ":" in str(server):
        cands = [str(server)]
    out: list[tuple[str, float]] = []
    if not cands:
        return out
    import threading
    lock = threading.Lock()

    def _one(a: str) -> None:
        ms = tcp_ms(a)
        if ms is not None:
            with lock:
                out.append((a, ms))

    threads = [threading.Thread(target=_one, args=(a,), daemon=True,
                                name=f"tcp-probe-{a}")
               for a in cands]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4.0)
    return sorted(out, key=lambda x: x[1])

def best_filling_from_flags(flags: int) -> str:
    if flags & FLAG_IOC:
        return "IOC"
    if flags & FLAG_FOK:
        return "FOK"
    return "RETURN"

class BrokerProber:

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[int, dict[str, dict]] = {}
        # case-insensitive index: brokers such as Deriv name symbols with
        # spaces and mixed case ("Boom 1000 Index"), so an uppercase lookup
        # against the broker-exact key would never hit
        self._upper: dict[int, dict[str, dict]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pass: dict[int, float] = {}
        self._ok: dict[int, bool] = {}
        self._opt: dict[int, dict] = {}
        self._opt_ts = 0.0
        self._accounts: dict[int, dict] = {}
        self._accounts_ts = 0.0

    def _store(self, inst: int, data: dict[str, dict]) -> None:
        if not data:
            return
        with self._lock:
            syms = self._data.setdefault(inst, {})
            up = self._upper.setdefault(inst, {})
            for sym, info in data.items():
                syms[sym] = info
                up[str(sym).upper()] = info
            self._last_pass[inst] = time.time()
            self._ok[inst] = True

    def load_catalogue(self, inst: int) -> dict[str, dict]:
        """Prime the prober from the terminal's own full symbol catalogue.

        The EA writes EVERY symbol the account can see (with the same
        fields a PROBE reply carries), so the account's whole quote
        universe - Boom/Crash/Jump/Volatility indices, cent variants,
        suffixed ECN pairs - is known after one file read instead of N
        exec round trips.  This is what makes probing all quotes cheap.
        """
        from spot import read_symbol_catalogue
        rows = read_symbol_catalogue(inst)
        out: dict[str, dict] = {}
        for _sym, fields in rows.items():
            info = parse_probe_fields(fields)
            if info:
                out[info["symbol"]] = info
        self._store(inst, out)
        return out

    @staticmethod
    def _sender(inst: int):
        from executor import sender_for
        return sender_for(inst)

    def probe_terminal(self, inst: int, symbols=None) -> dict[str, dict]:
        if symbols is None:
            symbols = probe_symbols_for(inst)
        symbols = tuple(symbols)[:MAX_PROBE_SYMBOLS]
        if not symbols:
            return {}
        try:
            from spot import term_running
            if not term_running(inst):
                log.warning(f"prober: terminal {inst} process not running - skipping probe")
                return {}
        except Exception:
            pass
        out: dict[str, dict] = {}
        cmd = self._sender(inst)
        # PING is the round-trip health check before a PROBE batch.  The 1 s
        # default is too tight while the EA is busy (login switch, heavy
        # fill) - 2026-10-13 10:22:29 logged both terminals as dead mid-login
        # with a confusing tail of OLD probe errors.  Retry 3x at 2 s, keep
        # the full diagnosis at debug and the warning to one line.
        ok, detail = False, ""
        for attempt in range(1, 4):
            try:
                ok, detail = cmd.send("PING", timeout=2.0)
            except Exception as exc:
                ok, detail = False, str(exc)
            if ok:
                break
            time.sleep(0.3)
        if not ok:
            log.warning(f"prober: terminal {inst} does not answer PING "
                        f"(3 tries) - skipping probe")
            log.debug(f"prober: terminal {inst} PING diagnosis: {detail[:500]}")
            return out
        for off in range(0, len(symbols), PROBE_CHUNK):
            chunk = symbols[off:off + PROBE_CHUNK]
            commands = [("PROBE", s) for s in chunk]
            results = cmd.send_batch(*commands, timeout=PROBE_TIMEOUT_S)
            for sym, (ok2, detail2) in zip(chunk, results):
                if not ok2:
                    log.debug(f"prober: terminal {inst} PROBE {sym} failed: {detail2}")
                    continue
                info = parse_probe_fields(detail2.split("|"))
                if info is None:
                    log.warning(f"prober: terminal {inst} returned short response for {sym}: {detail2[:120]}")
                    continue
                out[sym] = info
        self._store(inst, out)
        if out:
            log.info(f"prober: terminal {inst} probed {len(out)} symbols - "
                     f"best filling decided per symbol")
        return out

    def probe_all(self) -> None:
        accs = read_accounts()
        targets = [inst for inst in (1, 2) if accs.get(inst, {}).get("login")]
        if not targets:
            return
        import threading
        # one thread per terminal: both accounts probe CONCURRENTLY, so the
        # pass costs the slowest terminal, never the sum of both.
        threads = [threading.Thread(target=self._probe_one,
                                    args=(inst,), daemon=True,
                                    name=f"probe-t{inst}")
                   for inst in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        try:
            self.optimize_servers(accs)
        except Exception as exc:
            log.warning(f"prober: server optimization failed: {exc}")

    def _probe_one(self, inst: int) -> None:
        # Fast path: the terminal's own catalogue already lists every quote
        # the account can see, so take it as the probe result and only
        # PROBE what the catalogue does not cover (older EA, or a symbol
        # that appeared since the last refresh).
        try:
            cat = self.load_catalogue(inst)
        except Exception as exc:
            cat = {}
            log.debug(f"prober: terminal {inst} catalogue unavailable: {exc}")
        if cat:
            log.info(f"prober: terminal {inst} catalogue carries {len(cat)} "
                     f"symbols - full quote universe (no round trips)")
            try:
                missing = [s for s in probe_symbols_for(inst) if s not in cat]
                unquoted = [s for s in _unquoted_quote_aliases(cat)
                            if s not in missing]
                if missing or unquoted:
                    self.probe_terminal(inst, missing + unquoted)
            except Exception as exc:
                log.debug(f"prober: terminal {inst} gap probe skipped: {exc}")
            return
        try:
            self.probe_terminal(inst)
        except Exception as exc:
            log.warning(f"prober: terminal {inst} probe failed: {exc}")

    def optimize_servers(self, accs: dict | None = None) -> dict[int, dict]:
        if accs is None:
            accs = read_accounts()
        now = time.time()
        if now - self._opt_ts < OPT_TTL_S and self._opt:
            return self._opt

        fills = {i: _journal_fill_ms(i) for i in (1, 2)}
        med = {i: _median(v) for i, v in fills.items()}
        known = [m for m in med.values() if m is not None]
        best = min(known) if known else None

        reports: dict[int, dict] = {}
        for inst in (1, 2):
            if not accs.get(inst, {}).get("login"):
                continue
            key = inst if inst in accs else str(inst)
            a = accs[key]
            server = a.get("server", "")
            my_med = med.get(inst)
            slow = (my_med is not None and best is not None
                    and (my_med > FILL_SLOW_MS
                         or my_med > best * FILL_RATIO))
            rep: dict = {
                "login": a.get("login"),
                "server": server,
                "fills_sampled": len(fills.get(inst, [])),
                "median_fill_ms": round(my_med, 1) if my_med is not None else None,
                "fastest_terminal_median_ms": round(best, 1) if best is not None else None,
                "verdict": "slow" if slow else "ok",
            }
            if slow:
                ranked = rank_access_points(server)
                rep["candidates"] = [
                    {"address": addr, "tcp_ms": round(ms, 1)}
                    for addr, ms in ranked[:5]
                ]
                # MT5 logs in by SERVER NAME (e.g. HFMarketsKE-Live2), not by
                # an access point (host:port).  This code used to overwrite
                # session["server"] with the fastest access point and restart
                # the terminal - which silently broke the login (MT5 cannot
                # resolve a host:port as a server) and was why a live account
                # stopped being read by the bridge.  Only REPORT the
                # recommendation; never mutate the login server or restart.
                rep["recommended_access_point"] = (ranked[0][0]
                                                   if ranked else None)
            reports[inst] = rep

        with self._lock:
            self._opt = reports
            self._opt_ts = now
        return reports

    def best_filling(self, inst: int, symbol: str) -> str | None:
        sym = str(symbol or "").strip()
        with self._lock:
            info = (self._data.get(inst, {}).get(sym)
                    or self._upper.get(inst, {}).get(sym.upper()))
        return info.get("best") if info else None

    def report(self) -> dict:
        with self._lock:
            snap = {inst: {s: dict(i) for s, i in syms.items()}
                    for inst, syms in self._data.items()}
            opt = {str(k): dict(v) for k, v in self._opt.items()}
        try:
            from spot import detect_suffix
            suffix = {str(i): detect_suffix(i) for i in (1, 2)}
        except Exception:
            suffix = {}
        return {"ok": True, "terminals": {str(k): v for k, v in snap.items()},
                "probed_at": {str(k): v for k, v in self._last_pass.items()},
                "server_optimization": opt,
                "accounts": self.account_snapshot(),
                "suffix": suffix}

    def account_snapshot(self) -> dict:
        """Per-terminal account economics for scheduling: leverage,
        currency, margin mode/trade mode, balance.  Refreshed at most
        every 30 s; reads the EA header (no trading)."""
        now = time.time()
        with self._lock:
            if self._accounts and now - self._accounts_ts < 30.0:
                return {str(k): dict(v) for k, v in self._accounts.items()}
        snap: dict[int, dict] = {}
        try:
            from monitor import account_info
            from spot import read_accounts, pick_terminal, read_header
            for inst in (1, 2):
                try:
                    a = account_info(inst)
                except Exception:
                    continue
                lev = str(a.get("leverage", "") or "").strip()
                if lev.isdigit():
                    lev = f"1:{lev}"
                cur = ""
                try:
                    login = read_accounts().get(inst, {}).get("login", "")
                    term = pick_terminal(login) if login else None
                    if term:
                        cur = read_header(term["trades_path"]).get("currency", "") or ""
                except Exception:
                    pass
                snap[inst] = {
                    "login": a.get("login", ""),
                    "server": a.get("server", ""),
                    "currency": cur,
                    "leverage": lev,
                    "balance": a.get("balance", 0.0),
                    "equity": a.get("equity", 0.0),
                    "margin": a.get("margin", 0.0),
                    "margin_free": a.get("margin_free", 0.0),
                    "margin_level": a.get("margin_level", 0.0),
                    "positions": a.get("positions", 0),
                    "trade_allowed": a.get("trade_allowed", "1"),
                    "mql_allowed": a.get("mql_allowed", "1"),
                }
        except Exception:
            pass
        with self._lock:
            self._accounts = snap
            self._accounts_ts = now
        return {str(k): dict(v) for k, v in snap.items()}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="broker-prober")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        self._stop.wait(5.0)
        while not self._stop.is_set():
            self.probe_all()
            self._stop.wait(PROBE_TTL_S)

_prober: BrokerProber | None = None
_prober_lock = threading.Lock()

def get_prober() -> BrokerProber:
    global _prober
    with _prober_lock:
        if _prober is None:
            _prober = BrokerProber()
    return _prober

def start_prober() -> BrokerProber:
    p = get_prober()
    p.start()
    return p

def stop_prober() -> None:
    get_prober().stop()

def best_filling(inst: int, symbol: str) -> str | None:
    return get_prober().best_filling(inst, symbol)

def _render(inst: int, data: dict[str, dict]) -> str:
    # Broker names are not always short upper-case pairs - Deriv's synthetics
    # are "Boom 1000 Index" and cent/ECN feeds add letters - so size the
    # symbol column to the widest name in this account's list.
    width = max([8] + [len(s) for s in data]) + 2
    header = [f"{BOLD}TERMINAL {inst}{RESET}  "
              f"{DIM}{len(data)} symbols probed{RESET}",
              f"  {DIM}{'SYMBOL':<{width}}{'BEST':<7}{'ALLOWED':<9}{'EXEC':<10}"
              f"{'TRADE':<9}{'SPREAD':>7}{'STOPS':>7}{'VOL STEP':>10}{RESET}"]
    for sym, i in sorted(data.items()):
        header.append(
            f"  {sym:<{width}}{GREEN}{i['best']:<7}{RESET}{i['filling_allowed']:<9}"
            f"{i['exec_mode_name']:<10}{i['trade_mode_name']:<9}"
            f"{i['spread_pts']:>5}pt{i['stops_level']:>7}"
            f"{i['volume_step']:>10.2f}")
    return "\n".join(header)

def main() -> int:
    ap = argparse.ArgumentParser(description="Probe broker capabilities + best filling")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SEC",
                    help="keep re-probing every SEC seconds")
    ap.add_argument("--symbols", type=str, default="",
                    help="comma-separated symbols to probe (default: auto per terminal)")
    args = ap.parse_args()
    cli_symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()) if args.symbols else None

    accs = read_accounts()
    if not any(accs.get(n, {}).get("login") for n in (1, 2)):
        print(f"{RED}nobody logged in - run bridge.py first{RESET}")
        return 1

    prober = BrokerProber()
    while True:
        print(f"\n{BOLD}BROKER PROBE{RESET} {DIM}{time.strftime('%H:%M:%S')}{RESET}")
        for inst in (1, 2):
            if accs.get(inst, {}).get("login"):
                # explicit --symbols always goes through a live PROBE round
                # trip; otherwise report the account's whole catalogue
                # (every quote it can see), falling back to PROBE only when
                # the catalogue is unavailable (older EA / dead terminal).
                data = (prober.probe_terminal(inst, cli_symbols) if cli_symbols
                        else prober.load_catalogue(inst))
                if not data and not cli_symbols:
                    data = prober.probe_terminal(inst)
                print(_render(inst, data) if data else
                      f"{RED}terminal {inst}: probe failed (terminal not running?){RESET}")
        if args.watch <= 0:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0

if __name__ == "__main__":
    raise SystemExit(main())
