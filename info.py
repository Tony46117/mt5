#!/usr/bin/env python3.12
"""info.py - full information about the accounts logged into the two MT5
terminals (identities come from the runtime login session - session.py -
set up by bridge.py; data comes from the SpotDump EA bridge).

Shows, per account: login, holder, broker, server, currency, trade/margin
mode, leverage, balance, equity, floating P/L, margin state, position
count, live SPREADS (pts) for every symbol the terminal quotes and the
probed best order FILLING method (broker_prober.py) so you can see exactly
how orders will be filled.

Two consumers:
  * CLI:      python info.py            # one snapshot and exit
              python info.py --watch    # static screen, redraws on change
  * web:      snapshot() -> JSON-friendly dict for the web dashboard
              (see app.py / front.py); spread() is the single place that
              turns a bid/ask into a spread in points.
"""

from __future__ import annotations

import argparse
import sys
import time
import datetime as dt

import config
from config import CONFIG, setup_logging

from spot import (
    BOLD, DIM, RESET, GREEN, RED, YELLOW,
    read_accounts, read_header, read_spots, term_running, feed_age,
    pick_terminal, TERMINALS,
)

log = setup_logging(__name__)

MARGIN_MODES = {0: "netting", 1: "exchange", 2: "hedging"}
TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}


def _probed_filling(inst: int, sym: str) -> str:
    """Best order filling for (terminal, symbol) from the broker prober;
    '' when the prober has not probed it (never blocks rendering)."""
    try:
        import broker_prober
        return broker_prober.best_filling(inst, sym) or ""
    except Exception:
        return ""


_PROBE_TS = 0.0


def _ensure_probed() -> None:
    """CLI convenience: when this process has no probe results yet (app.py
    normally owns them), run one quick probe so the FILLING column shows
    real answers.  Throttled to once a minute (--watch)."""
    global _PROBE_TS
    if time.time() - _PROBE_TS < 60:
        return
    _PROBE_TS = time.time()
    try:
        import broker_prober
        p = broker_prober.get_prober()
        if not p.report()["terminals"]:
            accs = read_accounts()
            for inst in (1, 2):
                if accs.get(inst, {}).get("login"):
                    p.probe_terminal(inst)
    except Exception:
        pass


def f(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return 0.0


def spread_pts(sym: str, spots: dict) -> float | None:
    """Spread of `sym` in points from the (bid, ask, ts) map; None if no quote."""
    bid, ask, _ = spots.get(sym, ("", "", ""))
    if not bid or not ask:
        return None
    digits = 2 if sym.upper() == "XAUUSD" else (3 if sym.upper() == "XAGUSD" else 5)
    try:
        return (float(ask) - float(bid)) * (10 ** digits)
    except ValueError:
        return None


def spread(sym: str, spots: dict) -> str:
    """Spread of `sym` formatted for the terminal ('-' when no quote)."""
    s = spread_pts(sym, spots)
    return f"{s:.0f} pts" if s is not None else f"{DIM}-{RESET}"


def acc_line(label: str, value: str, color: str = "") -> str:
    return f"  {DIM}{label:<15}{RESET} {color}{value}{RESET}"


# --------------------------------------------------------------------------
# structured snapshot (web dashboard consumes this)
# --------------------------------------------------------------------------

def snapshot() -> dict:
    """JSON-friendly live state of both accounts for the web dashboard.

    {'ts': iso, 'feeds': {'age_1': s, 'age_2': s, 'running_1': b, ...},
     'accounts': {'1': {...account fields..., 'spreads': {...}}, '2': {...}}}
    """
    accs = read_accounts()
    spots = read_spots()
    out: dict = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                 "accounts": {}, "feeds": {}}
    for inst in (1, 2):
        login = accs.get(inst, {}).get("login", "")
        term = pick_terminal(login) if login else None
        if term:
            head = read_header(term["trades_path"])
        else:
            head = {}
        expected = accs.get(inst, {}).get("login", "?")
        age = feed_age(inst)
        out["feeds"][f"age_{inst}"] = round(age, 2)
        out["feeds"][f"running_{inst}"] = term_running(inst)
        cur = head.get("currency", "") or "USD"
        login_got = head.get("login", "") or ""
        lev = f(head.get("leverage", "")) or 0.0
        bal, eq = f(head.get("balance", "")), f(head.get("equity", ""))
        profit = f(head.get("profit", ""))
        try:
            tmode = TRADE_MODES.get(int(head.get("trade_mode", "")), "demo")
        except ValueError:
            tmode = "demo"
        try:
            mmode = MARGIN_MODES.get(int(head.get("margin_mode", "")), "hedging")
        except ValueError:
            mmode = "hedging"
        out["accounts"][str(inst)] = {
            "terminal": inst,
            "login": login_got or expected,
            "holder": head.get("holder", "") or "?",
            "broker": head.get("broker", "") or "?",
            "server": head.get("server", "") or accs.get(inst, {}).get("server", "?"),
            "currency": cur,
            "leverage": f"1:{head.get('leverage', '') or '?'}",
            "trade_mode": tmode,
            "margin_mode": mmode,
            "balance": round(bal, 2),
            "equity": round(eq, 2),
            "profit": round(profit, 2),
            "margin": round(f(head.get("margin", "")), 2),
            "margin_free": round(f(head.get("margin_free", "")), 2),
            "margin_level": round(f(head.get("margin_level", "")), 2),
            "positions": head.get("positions", 0),
            "algo_allowed": head.get("trade_allowed", ""),   # EA v1.50+: 1=algo on
            "mql_allowed": head.get("mql_allowed", ""),
            "identity_ok": bool(login_got) and login_got == expected,
            "live": age < 5,
            "age_s": round(age, 2),
            "spreads": {sym: spread_pts(sym, spots) for sym in spots},
            # broker-probe results per symbol: best filling + spread pts
            "fillings": {sym: _probed_filling(inst, sym) for sym in spots},
        }
    return out


# --------------------------------------------------------------------------
# terminal rendering
# --------------------------------------------------------------------------

def render_account(inst: int, head: dict, spots: dict, expected: str) -> str:
    age = feed_age(inst)
    if age < 5:
        link = f"{GREEN}live ({age*1000:.0f} ms){RESET}"
    elif age < 60:
        link = f"{YELLOW}laggy ({age:.0f} s){RESET}"
    else:
        link = f"{RED}stale ({age:.0f} s){RESET}"

    login = head.get("login", "") or "-"
    name = head.get("holder", "") or "?"
    broker = head.get("broker", "") or "?"
    server = head.get("server", "") or "?"
    cur = head.get("currency", "") or "?"
    lev = head.get("leverage", "") or "?"
    bal, eq = head.get("balance", ""), head.get("equity", "")
    marg, mfree = head.get("margin", ""), head.get("margin_free", "")
    mlevel = head.get("margin_level", "")
    profit = head.get("profit", "")
    try:
        tmode = TRADE_MODES.get(int(head.get("trade_mode", "")), "?")
    except ValueError:
        tmode = "?"
    try:
        mmode = MARGIN_MODES.get(int(head.get("margin_mode", "")), "?")
    except ValueError:
        mmode = "?"
    npos = head.get("positions", 0)

    eq_c = GREEN if f(eq) > f(bal) else (RED if f(eq) < f(bal) else "")
    pf_c = GREEN if f(profit) > 0 else (RED if f(profit) < 0 else "")
    id_ok = (login == expected)
    id_s = (f"{GREEN}OK{RESET}" if id_ok else
            f"{RED}WRONG (want {expected}){RESET}" if login != "-" else
            f"{RED}no data{RESET}")

    out = [f"{BOLD}TERMINAL {inst}  -  ACCOUNT {login}{RESET}  {link}  "
           f"{DIM}{dt.datetime.now():%H:%M:%S}{RESET}"]
    out.append(acc_line("Identity", f"{id_s}"))
    out.append(acc_line("Holder", name))
    out.append(acc_line("Broker", broker))
    out.append(acc_line("Server", server))
    out.append(acc_line("Trade mode", tmode,
                        YELLOW if tmode == "real" else ""))
    out.append(acc_line("Margin mode", mmode))
    out.append(acc_line("Leverage", f"1:{lev}"))
    out.append("")
    out.append(acc_line("Balance", f"{bal} {cur}"))
    out.append(acc_line("Equity", f"{eq_c}{eq} {cur}{RESET}"))
    out.append(acc_line("Floating P/L", f"{pf_c}{profit} {cur}{RESET}"))
    out.append(acc_line("Margin used", f"{marg} {cur}"))
    out.append(acc_line("Margin free", f"{mfree} {cur}"))
    if mlevel and f(mlevel) > 0:
        ml_c = RED if f(mlevel) < 100 else (YELLOW if f(mlevel) < 300 else GREEN)
        out.append(acc_line("Margin level", f"{ml_c}{mlevel} %{RESET}"))
    else:
        out.append(acc_line("Margin level", f"{DIM}n/a (no open positions){RESET}"))
    out.append(acc_line("Open positions", str(npos)))
    out.append("")
    quoted = [s for s in spots if s in ("EURUSD", "GBPUSD", "XAUUSD")]
    quoted += sorted(s for s in spots if s not in quoted)
    out.append(f"  {DIM}{'SYMBOL':<8}{'BID':>11}{'ASK':>11}{'SPREAD':>10}{'FILLING':>9}{RESET}")
    for sym in quoted:
        bid, ask, _ = spots.get(sym, ("", "", ""))
        digits = 2 if sym == "XAUUSD" else 5
        try:
            b = f"{float(bid):.{digits}f}" if bid else "-"
            a = f"{float(ask):.{digits}f}" if ask else "-"
        except ValueError:
            b = a = "-"
        fill = _probed_filling(inst, sym)
        fill_s = f"{GREEN}{fill}{RESET}" if fill else f"{DIM}probe...{RESET}"
        out.append(f"  {sym:<8}{b:>11}{a:>11}  {spread(sym, spots):>10}  {fill_s}")
    return "\n".join(out)


def build_frame() -> str:
    accs = read_accounts()
    spots = read_spots()
    _ensure_probed()
    logged = [n for n in (1, 2) if accs.get(n, {}).get("login")]
    out = [f"{BOLD}MT5 ACCOUNT INFO{RESET}  {DIM}logged-in session: "
           f"{', '.join(accs[n]['login'] for n in logged) or 'nobody'}  -  "
           f"SpotDump EA bridge  -  {dt.datetime.now():%Y-%m-%d %H:%M:%S}{RESET}", ""]

    for inst in (1, 2):
        d = TERMINALS[inst]["dir"]
        tp = d / "MQL5" / "Files" / "trades.csv"
        head = read_header(tp)
        expected = accs.get(inst, {}).get("login", "?")
        out.append(render_account(inst, head, spots, expected))
        if inst == 1:
            out.append("")
            out.append("-" * 62)
            out.append("")

    if not term_running(1) or not term_running(2):
        out.append(f"{RED}note: a terminal is not running - start bridge.py{RESET}")
    if not logged:
        out.append(f"{RED}note: nobody logged in - run bridge.py to log in{RESET}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Full account info for both MT5 terminals")
    ap.add_argument("--watch", action="store_true",
                    help="keep refreshing (static screen, redraws on change)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="refresh seconds for --watch (default 5)")
    args = ap.parse_args()

    if not args.watch:
        print(build_frame())
        return 0

    log.info(f"info watch mode started (interval: {args.interval}s)")
    last = ""
    first = True
    try:
        while True:
            frame = build_frame()
            if frame != last:                    # redraw only on change
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last = frame
            time.sleep(max(args.interval, 1))
    except KeyboardInterrupt:
        log.info("info watch stopped")
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
