#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import threading
import time

from config import setup_logging
from spot import read_accounts, BOLD, DIM, RESET, GREEN, RED

log = setup_logging(__name__)

FLAG_FOK = 1
FLAG_IOC = 2

FILLING_NAMES = {1: "FOK", 2: "IOC", 3: "IOC/FOK"}
MODE_NAMES = {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}
EXEC_NAMES = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE", 4: "SYNCHRONIZED"}

PROBE_TTL_S = 600.0
PROBE_TIMEOUT_S = 3.0
PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "XAUUSD247",
                 "GBPJPY", "AUDUSD")

ACCESS_POINTS: dict[str, list[str]] = {
    "HFMarketsKE-Demo2": [
        "mt5-europe3.dcglobalfarm.com:1953",
        "mt5-europe2.dcglobalfarm.com:1953",
        "mt5-europe4.dcglobalfarm.com:1953",
        "mt5-asia1.dcglobalfarm.com:1953",
        "mt5-asia6.dcglobalfarm.com:1953",
        "mt5-asia7.dcglobalfarm.com:1953",
        "mt5-samerica.dcglobalfarm.com:1953",
    ],
}
ACCESS_POINTS["MetaQuotes-Demo"] = []

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
    raw = logs[-1].read_bytes()
    txt = (raw.decode("utf-16-le", errors="replace")
           if raw[:2] == b"\xff\xfe" else raw.decode(errors="replace"))
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
    cands = ACCESS_POINTS.get(server) or ([server] if server else [])
    out: list[tuple[str, float]] = []
    for a in cands:
        ms = tcp_ms(a)
        if ms is not None:
            out.append((a, ms))
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pass: dict[int, float] = {}
        self._ok: dict[int, bool] = {}
        self._opt: dict[int, dict] = {}
        self._opt_ts = 0.0

    @staticmethod
    def _sender(inst: int):
        from executor import sender_for
        return sender_for(inst)

    def probe_terminal(self, inst: int, symbols=PROBE_SYMBOLS) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cmd = self._sender(inst)
        ok, detail = cmd.ping()
        if not ok:
            log.warning(f"prober: terminal {inst} does not answer PING ({detail})")
            return out
        commands = [("PROBE", s) for s in symbols]
        results = cmd.send_batch(*commands, timeout=PROBE_TIMEOUT_S)
        for sym, (ok2, detail2) in zip(symbols, results):
            if not ok2:
                log.debug(f"prober: terminal {inst} PROBE {sym} failed: {detail2}")
                continue
            fields = detail2.split("|")
            if len(fields) < 12:
                continue
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
                    "spread_pts": int(fields[11]),
                }
            except ValueError:
                continue
            info["filling_allowed"] = FILLING_NAMES.get(info["filling_flags"], "RETURN")
            info["best"] = best_filling_from_flags(info["filling_flags"])
            info["trade_mode_name"] = MODE_NAMES.get(info["trade_mode"], str(info["trade_mode"]))
            info["exec_mode_name"] = EXEC_NAMES.get(info["trade_exemode"], str(info["trade_exemode"]))
            out[sym] = info
        with self._lock:
            self._data.setdefault(inst, {}).update(out)
            self._last_pass[inst] = time.time()
            self._ok[inst] = bool(out)
        if out:
            log.info(f"prober: terminal {inst} probed {len(out)} symbols - "
                     f"best filling decided per symbol")
        return out

    def probe_all(self) -> None:
        accs = read_accounts()
        for inst in (1, 2):
            if accs.get(inst, {}).get("login"):
                try:
                    self.probe_terminal(inst)
                except Exception as exc:
                    log.warning(f"prober: terminal {inst} probe failed: {exc}")
        try:
            self.optimize_servers()
        except Exception as exc:
            log.warning(f"prober: server optimization failed: {exc}")

    def optimize_servers(self, accs: dict | None = None) -> dict[int, dict]:
        import session as session_mod
        from spot import restart_terminal

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
                if ranked and ranked[0][0] != server:
                    new_addr = ranked[0][0]
                    try:
                        accs[key]["server"] = new_addr
                        session_mod.set_accounts(accs, persist=True)
                        rep["rotated_to"] = new_addr
                        log.warning(f"prober: terminal {inst} fills slow "
                                    f"(median {my_med:.0f} ms) - rotating "
                                    f"access point {server} -> {new_addr}")
                        restart_terminal(inst)
                        rep["restarted"] = True
                    except Exception as exc:
                        rep["rotate_error"] = str(exc)
                        log.warning(f"prober: rotation failed for terminal "
                                    f"{inst}: {exc}")
            reports[inst] = rep

        with self._lock:
            self._opt = reports
            self._opt_ts = now
        return reports

    def best_filling(self, inst: int, symbol: str) -> str | None:
        with self._lock:
            info = self._data.get(inst, {}).get(symbol.upper())
        if not info or time.time() - info.get("_t", 0) > PROBE_TTL_S * 4:
            return info.get("best") if info else None
        return info["best"]

    def report(self) -> dict:
        with self._lock:
            snap = {inst: {s: dict(i) for s, i in syms.items()}
                    for inst, syms in self._data.items()}
            opt = {str(k): dict(v) for k, v in self._opt.items()}
        return {"ok": True, "terminals": {str(k): v for k, v in snap.items()},
                "probed_at": {str(k): v for k, v in self._last_pass.items()},
                "server_optimization": opt}

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
    out = [f"{BOLD}TERMINAL {inst}{RESET}  {DIM}{len(data)} symbols probed{RESET}",
           f"  {DIM}{'SYMBOL':<8}{'BEST':<7}{'ALLOWED':<9}{'EXEC':<10}{'TRADE':<9}"
           f"{'SPREAD':>7}{'STOPS':>7}{'VOL STEP':>10}{RESET}"]
    for sym, i in sorted(data.items()):
        out.append(f"  {sym:<8}{GREEN}{i['best']:<7}{RESET}{i['filling_allowed']:<9}"
                   f"{i['exec_mode_name']:<10}{i['trade_mode_name']:<9}"
                   f"{i['spread_pts']:>5}pt{i['stops_level']:>7}"
                   f"{i['volume_step']:>10.2f}")
    return "\n".join(out)

def main() -> int:
    ap = argparse.ArgumentParser(description="Probe broker capabilities + best filling")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SEC",
                    help="keep re-probing every SEC seconds")
    ap.add_argument("--symbols", type=str, default=",".join(PROBE_SYMBOLS),
                    help="comma-separated symbols to probe")
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    accs = read_accounts()
    if not any(accs.get(n, {}).get("login") for n in (1, 2)):
        print(f"{RED}nobody logged in - run bridge.py first{RESET}")
        return 1

    prober = BrokerProber()
    while True:
        print(f"\n{BOLD}BROKER PROBE{RESET} {DIM}{time.strftime('%H:%M:%S')}{RESET}")
        for inst in (1, 2):
            if accs.get(inst, {}).get("login"):
                data = prober.probe_terminal(inst, symbols)
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
