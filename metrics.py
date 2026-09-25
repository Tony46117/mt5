#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import sys
import threading
import time
import datetime as dt

from config import CONFIG, setup_logging

from spot import (read_accounts, read_header, pick_terminal, BOLD, DIM,
                  RESET, GREEN, RED, YELLOW)
import database as db

log = setup_logging(__name__)

POLL = CONFIG.metrics_poll_seconds
SAMPLE_EVERY = CONFIG.metrics_sample_seconds
MAX_TRADES = CONFIG.max_closed_trades
MAX_SAMPLES = CONFIG.max_equity_samples

def f(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0

def _load(key: str, default):
    v = db.kv_get(key)
    return v if v is not None else default

def _save(key: str, value) -> None:
    db.kv_set(key, value)

class MetricsObserver(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="metrics-observer")
        self.stop_flag = threading.Event()
        self._open: dict[int, dict[str, dict]] = {1: {}, 2: {}}
        self._last_sample = 0.0

    def _read(self, inst: int) -> tuple[dict, list[dict]]:
        login = read_accounts().get(inst, {}).get("login", "")
        term = pick_terminal(login) if login else None
        if not term:
            return {}, []
        head = read_header(term["trades_path"])
        if not head or head.get("login") != login:
            return head, []
        try:
            raw = term["trades_path"].read_text(encoding="cp1252",
                                                errors="replace")
        except OSError:
            return head, []
        rows: list[dict] = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 2 and parts[0] == "NONE":
                continue
            if len(parts) >= 11:
                rows.append({"ticket": parts[0], "symbol": parts[1],
                             "side": parts[2], "volume": parts[3],
                             "pl": parts[6], "swap": parts[7],
                             "time": parts[9]})
        return head, rows

    def _poll_account(self, inst: int) -> None:
        head, rows = self._read(inst)
        key = f"closed_trades_{inst}"
        closed = _load(key, [])

        seen: dict[str, dict] = {}
        for r in rows:
            seen[r["ticket"]] = r
            if r["ticket"] in self._open[inst]:
                self._open[inst][r["ticket"]].update(
                    pl=f(r["pl"]), swap=f(r["swap"]))

        gone = [t for t in self._open[inst] if t not in seen]
        now = dt.datetime.now().isoformat(timespec="seconds")
        for t in gone:
            info = self._open[inst].pop(t)
            net = info.get("pl", 0.0) + info.get("swap", 0.0)
            closed.append({"ticket": t, "symbol": info["symbol"],
                           "side": info["side"], "lot": info.get("lot", 0.0),
                           "net": round(net, 2), "opened": info.get("time", ""),
                           "closed": now})
        if gone:
            _save(key, closed[-MAX_TRADES:])

        for t, r in seen.items():
            if t not in self._open[inst]:
                self._open[inst][t] = {"symbol": r["symbol"], "side": r["side"],
                                       "lot": f(r["volume"]), "pl": f(r["pl"]),
                                       "swap": f(r["swap"]), "time": r["time"]}

        if head and time.monotonic() - self._last_sample >= SAMPLE_EVERY:
            self._last_sample = time.monotonic()
            curve = _load(f"equity_curve_{inst}", [])
            curve.append([int(time.time()),
                          f(head.get("balance", "")),
                          f(head.get("equity", ""))])
            _save(f"equity_curve_{inst}", curve[-MAX_SAMPLES:])

    def run(self) -> None:
        log.info(f"metrics observer started ({POLL} s poll, {SAMPLE_EVERY} s sample)")
        while not self.stop_flag.is_set():
            try:
                for inst in (1, 2):
                    self._poll_account(inst)
            except Exception as exc:
                log.error(f"metrics error: {exc}")
            self.stop_flag.wait(POLL)

def closed_trades(account: int) -> list[dict]:
    return _load(f"closed_trades_{account}", [])

def open_trades(account: int) -> list[dict]:
    obs = _observer
    if obs and obs.is_alive():
        return list(obs._open.get(account, {}).values())
    return []

def pairs_traded(account: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in closed_trades(account):
        counts[t["symbol"]] = counts.get(t["symbol"], 0) + 1
    for t in open_trades(account):
        counts[t["symbol"]] = counts.get(t["symbol"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

def stats(account: int) -> dict:
    closed = closed_trades(account)
    opened = open_trades(account)
    wins = sum(1 for t in closed if t["net"] > 0)
    losses = sum(1 for t in closed if t["net"] < 0)
    net = round(sum(t["net"] for t in closed), 2)
    total = len(closed)
    return {
        "trades_taken": total + len(opened),
        "open": len(opened),
        "closed": total,
        "wins": wins,
        "losses": losses,
        "winrate": round(100.0 * wins / total, 1) if total else 0.0,
        "net_closed": net,
        "pairs": pairs_traded(account),
    }

def equity_curve(account: int, max_points: int = 240) -> list[list]:
    curve = _load(f"equity_curve_{account}", [])
    if len(curve) <= max_points:
        return curve
    step = len(curve) / max_points
    return [curve[int(i * step)] for i in range(max_points)]

_observer: MetricsObserver | None = None

def is_running() -> bool:
    global _observer
    return _observer is not None and _observer.is_alive()

def start_observer() -> MetricsObserver:
    global _observer
    if _observer is None or not _observer.is_alive():
        _observer = MetricsObserver()
        _observer.start()
    return _observer

def stop_observer() -> None:
    global _observer
    if _observer is not None and _observer.is_alive():
        log.info("stopping metrics observer...")
        _observer.stop_flag.set()
        _observer.join(timeout=5.0)
        log.info("metrics observer stopped")
    _observer = None

def main() -> int:
    import signal
    ap = argparse.ArgumentParser(description="Trading metrics (dashboard data)")
    ap.add_argument("--watch", action="store_true", help="refresh every 2 s")
    args = ap.parse_args()

    start_observer()
    
    def _sigterm(*_):
        stop_observer()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    try:
        while True:
            print("\033[2J\033[H", end="")
            print(f"{BOLD}TRADING METRICS{RESET}  {DIM}{dt.datetime.now():%H:%M:%S}{RESET}\n")
            for acc in (1, 2):
                s = stats(acc)
                wr = s["winrate"]
                wr_c = GREEN if wr >= 50 else (RED if s["closed"] and wr < 50 else YELLOW)
                net = s["net_closed"]
                net_c = GREEN if net > 0 else (RED if net < 0 else "")
                print(f"  {BOLD}ACCOUNT {acc}{RESET}")
                print(f"    trades taken  {s['trades_taken']:>6}   "
                      f"(open {s['open']}, closed {s['closed']})")
                print(f"    winrate       {wr_c}{wr:>5.1f} %{RESET}   "
                      f"(W {s['wins']} / L {s['losses']})")
                print(f"    net closed    {net_c}{net:>+10.2f}{RESET}")
                print(f"    pairs         {', '.join(f'{k} x{v}' for k, v in s['pairs'].items()) or '-'}")
                curve = equity_curve(acc)
                if curve:
                    eq = curve[-1][2]
                    print(f"    equity last   {eq:.2f}   ({len(curve)} samples)")
                print()
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_observer()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
