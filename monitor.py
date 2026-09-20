#!/usr/bin/env python3.12
"""monitor.py - ONE monitor for BOTH accounts, side by side.

The old monitor1.py / monitor2.py pair merged: both accounts render on a
single static screen separated by a straight line, redrawn only when
something actually changes.  LIVE TRADES ONLY: the screen shows the
positions that are open RIGHT NOW (plus balance/equity) - there is
deliberately NO event log of opened/closed trades; history lives in the
database/web panel, the monitor is a pure live book.

Identity stays strict: each half only shows data when a terminal's EA
header proves it is logged into that acc.env account - it can never show
account1's trades mislabelled as account2's.

Also exposes the data helpers the web terminal uses:
    read_positions(inst)  -> open positions of terminal `inst`
    account_info(inst)    -> {login, server, balance, equity, ...}

Usage:
    python monitor.py            # live dashboard (auto-starts terminals)
    python monitor.py --once     # single frame
    python monitor.py --check    # which terminals are visible & their accounts
    python monitor.py --restart 1      # bounce terminal 1
    python monitor.py --restart 2      # bounce terminal 2
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import datetime as dt

from config import setup_logging

from spot import (
    BOLD, DIM, RESET, GREEN, RED, read_accounts, pick_terminal, read_header, scan_terminals,
    ensure_terminal, restart_terminal, install_script, setup_terminal2,
    compiled_paths, wait_for_bridge, term_running, feed_age, WINEPREFIX, MT5_DIR2,
)

log = setup_logging(__name__)

HEADER = (f"{'TICKET':<11} {'SYMBOL':<8} {'SIDE':<5} {'VOL':>6} "
          f"{'OPEN':>10} {'CUR':>10} {'P/L':>10} {'SWAP':>8} {'NET':>10} "
          f"{'OPENED':>20}  MAGIC      COMMENT")

SEP = "=" * 100


def f(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# shared data helpers (web app imports these)
# --------------------------------------------------------------------------

def read_positions(inst: int) -> list[dict]:
    """Open positions of terminal `inst` (identity-checked against acc.env)."""
    login = read_accounts().get(inst, {}).get("login", "")
    term = pick_terminal(login) if login else None
    if not term:
        return []
    try:
        raw = term["trades_path"].read_text(encoding="cp1252", errors="replace")
    except OSError:
        return []
    head = read_header(term["trades_path"])
    if not head or head.get("login") != login:
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 2 and parts[0] == "NONE":
            continue
        if len(parts) >= 11:
            rows.append({
                "ticket": parts[0], "symbol": parts[1], "side": parts[2],
                "volume": parts[3], "open": parts[4], "cur": parts[5],
                "pl": parts[6], "swap": parts[7], "magic": parts[8],
                "time": parts[9], "comment": parts[10],
            })
    return rows


def account_info(inst: int) -> dict:
    """Account snapshot of terminal `inst` from its EA header."""
    login = read_accounts().get(inst, {}).get("login", "")
    term = pick_terminal(login) if login else None
    if not term:
        return {"login": "", "server": "", "balance": 0.0, "equity": 0.0,
                "profit": 0.0, "margin": 0.0, "margin_free": 0.0,
                "margin_level": 0.0, "positions": 0}
    head = read_header(term["trades_path"])
    return {"login": head.get("login", ""), "server": head.get("server", ""),
            "balance": f(head.get("balance", "")),
            "equity": f(head.get("equity", "")),
            "profit": f(head.get("profit", "")),
            "margin": f(head.get("margin", "")),
            "margin_free": f(head.get("margin_free", "")),
            "margin_level": f(head.get("margin_level", "")),
            "positions": head.get("positions", 0)}


def TERMINAL_DIR(inst: int):
    from spot import TERMINALS
    return TERMINALS[inst]["dir"]


# --------------------------------------------------------------------------
# per-account monitor (adapted from monitor_core.Monitor)
# --------------------------------------------------------------------------

class Monitor:
    """One instance per account number (1 or 2); inst == account_no."""

    def __init__(self, account_no: int, strict_identity: bool = True):
        self.account_no = account_no
        self.inst = account_no
        self.strict_identity = strict_identity
        self.prev_tickets: set[str] | None = None
        self.prev_pl: float | None = None
        self.prev_bal = self.prev_eq = None
        self.prev_mtime = 0.0
        self.last_head: dict = {}

    # ------------------------------------------------------------------
    def acc_env(self) -> dict:
        return read_accounts().get(self.account_no, {})

    def identity_status(self, head: dict) -> str:
        expected = self.acc_env().get("login", "")
        got = head.get("login", "")
        if got and expected:
            return "verified" if got == expected else "mismatch"
        return "unverified"

    def identity_error_frame(self, status: str) -> str:
        a = self.acc_env()
        expected = a.get("login", "?")
        lines = [f"{BOLD}ACCOUNT {expected}  ({a.get('server', '?')}){RESET}  "
                 f"{RED}identity NOT confirmed{RESET}  "
                 f"{DIM}{dt.datetime.now():%H:%M:%S}{RESET}", ""]
        if status == "mismatch":
            lines.append(f"{RED}[!] the visible terminal is logged into a "
                         f"different account - showing nothing.{RESET}")
        else:
            lines.append(f"{RED}[!] no terminal reports "
                         f"login {expected} yet.{RESET}")
            lines.append("    (EA header is pre-v1.20 or terminal2 is not set up)")
        lines.append("")
        lines.append(f"{BOLD}visible terminals:{RESET}")
        terms = scan_terminals()
        if not terms:
            lines.append(f"  {DIM}(none - is any MT5 terminal running with "
                         f"the SpotDump EA?){RESET}")
        for t in terms:
            h = t["header"]
            try:
                rel = t["root"].relative_to(WINEPREFIX)
            except ValueError:
                rel = t["root"]
            lines.append(f"  {rel}  login={h.get('login', '? (EA < v1.20)')}  "
                         f"server={h.get('server', '?')}")
        lines.append("")
        lines.append(f"{DIM}set up terminal2 for account2 (run: "
                     f"python monitor.py --setup2), or:  python monitor.py --restart 2{RESET}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def load(self) -> tuple[dict, list[dict], float, str]:
        login = self.acc_env().get("login", "")
        term = pick_terminal(login) if login else None
        if not term:
            return {}, [], 0.0, "unverified"
        try:
            mtime = term["trades_path"].stat().st_mtime
            raw = term["trades_path"].read_text(encoding="cp1252", errors="replace")
        except OSError:
            return {}, [], 0.0, "unverified"
        head = read_header(term["trades_path"])
        if not head and not raw.strip():
            head = self.last_head
        self.last_head = head or self.last_head
        status = self.identity_status(head or self.last_head)
        if self.strict_identity and status != "verified":
            return head, [], mtime, status
        rows: list[dict] = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 2 and parts[0] == "NONE":
                continue
            if len(parts) >= 11:
                rows.append({
                    "ticket": parts[0], "symbol": parts[1], "side": parts[2],
                    "volume": parts[3], "open": parts[4], "cur": parts[5],
                    "pl": parts[6], "swap": parts[7], "magic": parts[8],
                    "time": parts[9], "comment": parts[10],
                })
        return head, rows, mtime, status

    # ------------------------------------------------------------------
    def detect_changes(self, head: dict, trades: list[dict], mtime: float) -> bool:
        """Detect ANY change so the static screen redraws at the exact
        moment the live book moves - deliberately no OPENED/CLOSED event
        log (monitor = live ongoing trades only)."""
        changed = False
        tickets = {t["ticket"] for t in trades}
        if self.prev_tickets is None:
            self.prev_tickets = tickets
        elif tickets != self.prev_tickets:
            changed = True                 # book changed - redraw only
            self.prev_tickets = tickets

        pl = sum(f(t["pl"]) for t in trades)
        if self.prev_pl is None or abs(pl - self.prev_pl) >= 0.005:
            changed = changed or self.prev_pl is not None
            self.prev_pl = pl

        bal = f(head.get("balance", ""))
        eq = f(head.get("equity", ""))
        if head and (self.prev_bal is None or self.prev_eq is None
                     or bal != self.prev_bal or eq != self.prev_eq):
            changed = changed or self.prev_bal is not None
            self.prev_bal, self.prev_eq = bal, eq

        if mtime != self.prev_mtime:
            self.prev_mtime = mtime
            changed = True
        return changed

    # ------------------------------------------------------------------
    def render(self, head: dict, trades: list[dict], mtime: float) -> str:
        a = self.acc_env()
        login = head.get("login") or a.get("login", "?")
        server = head.get("server") or a.get("server", "?")

        age = time.time() - mtime if mtime else 999
        stale = age > 2.0
        link = (f"{RED}STALE ({age:.0f}s - EA not writing, try --restart {self.inst}){RESET}"
                if stale else f"{DIM}live ({age*1000:.0f} ms old){RESET}")

        bal, eq = self.prev_bal or 0.0, self.prev_eq or 0.0
        pl = self.prev_pl or 0.0
        swap_sum = sum(f(t["swap"]) for t in trades)
        net = pl + swap_sum
        pl_c = GREEN if pl > 0 else (RED if pl < 0 else "")
        net_c = GREEN if net > 0 else (RED if net < 0 else "")
        eq_c = GREEN if eq > bal else (RED if eq < bal else "")

        out = [f"{BOLD}ACCOUNT {login}  ({server}){RESET}  {link}  "
               f"{DIM}{dt.datetime.now():%H:%M:%S}{RESET}"]
        if bal or eq:
            out.append(f"{BOLD}BAL {bal:>10.2f}{RESET}   "
                       f"{BOLD}EQUITY {eq_c}{eq:>10.2f}{RESET}   "
                       f"{BOLD}FLOAT {pl_c}{pl:>+10.2f}{RESET}   "
                       f"{BOLD}NET {net_c}{net:>+10.2f}{RESET}")
        else:
            out.append(f"{DIM}balance/equity need SpotDump v1.20 "
                       f"(python monitor.py --restart {self.inst} recompiles it){RESET}")
        out.append("")

        if not trades:
            out.append(f"{DIM}no open positions{RESET}")
            return "\n".join(out)

        out.append(HEADER)
        for t in trades:
            p, swap = f(t["pl"]), f(t["swap"])
            n = p + swap
            p_c = GREEN if p > 0 else (RED if p < 0 else "")
            n_c = GREEN if n > 0 else (RED if n < 0 else "")
            out.append(f"{t['ticket']:<11} {t['symbol']:<8} {t['side']:<5} "
                       f"{t['volume']:>6} {t['open']:>10} {t['cur']:>10} "
                       f"{p_c}{p:>+10.2f}{RESET} {swap:>+8.2f} {n_c}{n:>+10.2f}{RESET} "
                       f"{t['time']:>20}  {t['magic']:<10} {DIM}{t['comment']}{RESET}")

        out.append("-" * len(HEADER))
        n = len(trades)
        out.append(f"{BOLD}{n} position{'s' if n != 1 else ''}{RESET}   "
                   f"floating {pl:>+10.2f}   net {net_c}{net:>+10.2f}{RESET}")
        return "\n".join(out)


# --------------------------------------------------------------------------
# merged screen
# --------------------------------------------------------------------------

def build_frame(monitors: dict[int, Monitor], once: bool = False) -> str:
    frames: list[str] = []
    for inst in (1, 2):
        m = monitors[inst]
        head, trades, mtime, status = m.load()
        if status != "verified":
            frames.append(m.identity_error_frame(status))
            m.prev_tickets, m.prev_pl = None, None
            m.prev_bal = m.prev_eq = None
            m.prev_mtime = 0.0
        else:
            m.detect_changes(head, trades, mtime)
            frames.append(m.render(head, trades, mtime))
    return f"\n{SEP}\n".join(frames)


def check() -> int:
    print(f"{BOLD}monitor --check{RESET}\n")
    terms = scan_terminals()
    if not terms:
        print(f"  {DIM}no trades.csv found - no terminal with the SpotDump "
              f"EA is running{RESET}")
        return 1
    accs = read_accounts()
    for n in (1, 2):
        login = accs.get(n, {}).get("login", "?")
        found = False
        for t in terms:
            h = t["header"]
            if h.get("login") == login:
                found = True
                print(f"  account{n} ({login}) -> terminal at {t['root']}"
                      f"  server={h.get('server', '?')}")
        if not found:
            print(f"  account{n} ({login}) -> {RED}no terminal reports this login{RESET}")
    return 0


def setup2() -> int:
    if not setup_terminal2():
        return 1
    print(f"{BOLD}terminal2 ready:{RESET} {MT5_DIR2}")
    print(f"{DIM}It will auto-login account2 (acc.env) and auto-attach the "
          f"SpotDump EA when launched.{RESET}")
    return 0


def _wait_bridges(insts: tuple[int, ...], timeout: float) -> dict[int, bool]:
    """Wait for SEVERAL terminals' EA feeds CONCURRENTLY (the old code waited
    serially - one dead terminal stalled startup for the full timeout).
    Prints a progress line every 15 s so a slow boot never looks hung."""
    res: dict[int, bool] = {i: False for i in insts}

    def w(inst: int) -> None:
        deadline = time.time() + timeout
        next_progress = time.time() + 15.0
        while time.time() < deadline:
            if feed_age(inst) < 5:
                res[inst] = True
                return
            if time.time() >= next_progress:
                log.info(f"  still waiting for terminal {inst} feed... "
                         f"{int(deadline - time.time())} s left")
                next_progress = time.time() + 15.0
            time.sleep(1)

    threads = [threading.Thread(target=w, args=(i,), daemon=True,
                                name=f"wait-bridge-{i}") for i in insts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return res


def run(once: bool, interval: float, restart: int | None) -> int:
    log.info("MT5 DUAL ACCOUNT MONITOR starting - account1 + account2, one screen, strict identity")

    # --once mode: skip terminal launch / compile / long waits entirely -
    # just read whatever data the EA has already written and print one frame.
    if not once:
        for inst in (1, 2):
            if inst == 2 and not (MT5_DIR2 / "terminal64.exe").exists():
                if not setup_terminal2():
                    return 1
            if restart == inst or restart == 0:
                install_script(inst)       # compile BEFORE the restart boot
                if not restart_terminal(inst):
                    return 1
            elif not term_running(inst):
                install_script(inst)       # EA ready BEFORE the first boot
                if not ensure_terminal(inst):
                    return 1
            else:
                install_script(inst)       # no-op when unchanged
            if not any(p.exists() for p in compiled_paths(inst)):
                log.error(f"SpotDump.ex5 missing for terminal {inst} - compilation failed.")
                return 1

        # Parallel 45 s wait, then ONE automatic self-heal restart for a dead
        # bridge (the old code sat on a serial 180 s wait and then told the
        # user to run --restart by hand - three minutes of nothing).
        log.info("waiting for the EA bridges (both terminals, 45 s max)...")
        ok = _wait_bridges((1, 2), timeout=45.0)
        dead = [i for i in (1, 2) if not ok[i]]
        if dead:
            for inst in dead:
                log.warning(f"terminal {inst} has no EA feed after 45 s - "
                            f"restarting it once (self-heal)")
                restart_terminal(inst)
            ok.update(_wait_bridges(tuple(dead), timeout=45.0))
        for inst in (1, 2):
            if not ok[inst]:
                log.error(f"bridge of terminal {inst} is not producing data - SpotDump EA is not attached.")
                log.error(f"Run:  python monitor.py --restart {inst}")
                return 1

    monitors = {1: Monitor(1), 2: Monitor(2)}
    if once:
        print(build_frame(monitors))
        return 0
    last_frame = ""
    first = True
    try:
        while True:
            frame = build_frame(monitors)
            if frame != last_frame:            # redraw ONLY on real change
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last_frame = frame
            time.sleep(max(interval, 0.02))
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Merged monitor for both MT5 accounts (one screen)")
    ap.add_argument("--once", action="store_true", help="single frame and exit")
    ap.add_argument("--check", action="store_true",
                    help="show which terminal reports which account")
    ap.add_argument("--setup2", action="store_true",
                    help="one-time: create the second MT5 install")
    ap.add_argument("--restart", type=int, choices=(0, 1, 2), metavar="N",
                    help="restart terminal N (0 = both) to re-attach the EA")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="poll seconds (default 0.1)")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        sys.exit("python 3.10+ required")
    if args.check:
        sys.exit(check())
    if args.setup2:
        sys.exit(setup2())
    sys.exit(run(args.once, args.interval, args.restart))


if __name__ == "__main__":
    raise SystemExit(main())
