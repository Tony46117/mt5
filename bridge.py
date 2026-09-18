#!/usr/bin/env python3.12
"""bridge.py - supervisor that keeps BOTH MT5 terminals and their SpotDump
EA bridges alive. Optimized for low CPU usage and consistent connectivity.

Run this first; then any of the viewers work smoothly against a healthy
bridge:
    python spot.py        # spot prices only
    python monitor.py     # account1 + account2 positions/balance/equity

What it does:
  * auto-starts terminal 1 (account1) and terminal 2 (account2, its own
    MT5 install - created automatically on first run);
  * watches each terminal's bridge feed (spots.csv / trades.csv written
    every 50 ms by the EA) and AUTO-RESTARTS a terminal whose feed goes
    stale (EA detached, terminal hung, ...) - with a cooldown so it can
    never restart-storm;
  * verifies each terminal's EA-reported login against acc.env and
    shouts if a terminal is in the wrong account;
  * shows ONE STATIC SCREEN with a gentle, slow log: lines appear only
    when something actually CHANGES (terminal up/down, feed stale/live,
    account changed) - never per-poll spam, never scrolling.
  * Uses adaptive polling: fast when issues detected, slow when stable.

Usage:
    python bridge.py              # supervise (Ctrl+C to stop)
    python bridge.py --once      # one status frame and exit
    python bridge.py --interval 5  # base poll interval seconds (default 5)
"""

from __future__ import annotations

import argparse
import sys
import time
import datetime as dt
import threading

import config
from config import CONFIG, setup_logging

from spot import (
    BOLD, DIM, RESET, GREEN, RED, YELLOW,
    TERMINALS, MT5_DIR2, read_accounts, read_header, scan_terminals,
    term_running, launch_terminal, restart_terminal, setup_terminal2,
    install_script, feed_age, compiled_paths,
)

log = setup_logging(__name__)

GRACE_S = CONFIG.bridge_grace_seconds
STALE_S = CONFIG.bridge_stale_seconds
COOLDOWN_S = CONFIG.bridge_cooldown_seconds
MAX_EVENTS = 10
RELAUNCH_COOLDOWN_S = 10.0   # never spawn a terminal more often than this

# Adaptive polling - state machine (no bouncing multipliers)
POLL_FAST = 1.0     # a terminal is down/stale
POLL_NORMAL = 5.0   # something is laggy (default)
POLL_SLOW = 15.0    # both feeds live and stable

now_ts = lambda: dt.datetime.now().strftime("%H:%M:%S")


def header_for(inst: int) -> dict:
    """EA header of terminal `inst`'s trades.csv ('' fields when no data).

    read_header() is mtime-cached in spot.py, so this is cheap to call
    every poll; the legacy fallback scan is throttled to once per 30 s.
    """
    if inst == 1:
        path = TERMINALS[1]["dir"] / "MQL5" / "Files" / "trades.csv"
        h = read_header(path)
        if h:
            return h
        terms = _cached_scan()
        terms = [t for t in terms if t["root"] != MT5_DIR2 / "MQL5"]
        terms.sort(key=lambda t: t["mtime"], reverse=True)
        return terms[0]["header"] if terms else {}
    return read_header(TERMINALS[2]["dir"] / "MQL5" / "Files" / "trades.csv")


class TermState:
    __slots__ = ('inst', 'running', 'age_bucket', 'login', 'last_start', 'last_heal', 'last_check')

    def __init__(self, inst: int):
        self.inst = inst
        self.running = False
        self.age_bucket = ""
        self.login = ""
        self.last_start = 0.0
        self.last_heal = 0.0
        self.last_check = 0.0

    def bucket(self) -> str:
        if not self.running:
            return "down"
        a = feed_age(self.inst)
        if a < 2:
            return "live"
        if a < STALE_S:
            return "laggy"
        return "stale"

    def color(self) -> str:
        return {"live": GREEN, "laggy": YELLOW, "stale": RED,
                "down": RED}.get(self.bucket(), DIM)


_scan_cache: list[dict] = []
_scan_cache_ts = 0.0


def _cached_scan() -> list[dict]:
    """scan_terminals() with a 30 s TTL (it stats + parses every trades.csv)."""
    global _scan_cache, _scan_cache_ts
    now = time.monotonic()
    if now - _scan_cache_ts > 30.0:
        _scan_cache = scan_terminals()
        _scan_cache_ts = now
    return _scan_cache


class Supervisor:
    def __init__(self):
        self.terms = {1: TermState(1), 2: TermState(2)}
        self.events: list[str] = []
        self._poll_interval = POLL_NORMAL
        self._terminal2_ready = False

    def log(self, text: str) -> None:
        self.events.insert(0, f"{DIM}{now_ts()}{RESET}  {text}")
        self.events = self.events[:MAX_EVENTS]
        # Also log to system logger for debugging
        clean = text.replace(GREEN, "").replace(RED, "").replace(YELLOW, "").replace(DIM, "").replace(RESET, "").replace(BOLD, "")
        log.info(clean)

    def _adjust_poll_interval(self) -> None:
        """State machine: down/stale -> fast, laggy -> normal, all live -> slow."""
        buckets = {st.bucket() for st in self.terms.values()}
        if "down" in buckets or "stale" in buckets:
            self._poll_interval = POLL_FAST
        elif "laggy" in buckets:
            self._poll_interval = POLL_NORMAL
        else:
            self._poll_interval = POLL_SLOW

    # ------------------------------------------------------------------
    def poll(self) -> None:
        try:
            self._poll()
        except Exception as exc:
            # one bad terminal must never kill the supervisor loop
            log.error(f"supervisor poll error: {exc}")

    def _poll(self) -> None:
        accs = read_accounts()
        
        # Check terminal 2 setup once
        if not self._terminal2_ready and not (MT5_DIR2 / "terminal64.exe").exists():
            self.log(f"{YELLOW}terminal 2 install missing - creating it{RESET}")
            if setup_terminal2():
                self._terminal2_ready = True
                self.log(f"{GREEN}terminal 2 created successfully{RESET}")
            else:
                self.log(f"{RED}could not create terminal 2 install{RESET}")

        now = time.monotonic()
        for inst, st in self.terms.items():
            expected = accs.get(inst, {}).get("login", "?")
            exe_ok = (TERMINALS[inst]["dir"] / "terminal64.exe").exists()
            
            running = term_running(inst)
            if running != st.running:
                if running:
                    self.log(f"terminal {inst} is {GREEN}UP{RESET}")
                    st.last_start = time.monotonic()
                    install_script(inst)
                else:
                    self.log(f"terminal {inst} is {RED}DOWN{RESET}")
                st.running = running

            if not running:
                # relaunch with a cooldown so a broken install can never
                # spawn-storm wine processes
                if exe_ok and now - st.last_start > RELAUNCH_COOLDOWN_S:
                    self.log(f"terminal {inst} not running - {DIM}starting{RESET}")
                    launch_terminal(inst)
                    st.last_start = time.monotonic()
                    st.running = True
                continue

            # bridge health / auto-heal
            b = st.bucket()
            if b != st.age_bucket:
                if b == "live":
                    self.log(f"terminal {inst} bridge is {GREEN}LIVE{RESET}")
                elif b == "stale":
                    self.log(f"terminal {inst} bridge {RED}STALE{RESET}")
                elif b == "laggy":
                    self.log(f"terminal {inst} bridge {YELLOW}laggy{RESET}")
                st.age_bucket = b

            if b == "stale":
                in_grace = time.monotonic() - st.last_start < GRACE_S
                cooled = time.monotonic() - st.last_heal > COOLDOWN_S
                if not in_grace and cooled:
                    self.log(f"{YELLOW}healing terminal {inst} "
                             f"(restart to re-attach EA){RESET}")
                    st.last_heal = time.monotonic()
                    if restart_terminal(inst):
                        st.last_start = time.monotonic()
                        st.running = True
                    else:
                        self.log(f"{RED}heal of terminal {inst} failed{RESET}")

            # account identity check (less frequent)
            if time.monotonic() - st.last_check > 10:
                st.last_check = time.monotonic()
                h = header_for(inst)
                login = h.get("login", "")
                if login and login != st.login:
                    if st.login:
                        col = GREEN if login == expected else RED
                        self.log(f"terminal {inst} account: {st.login} -> "
                                 f"{col}{login}{RESET}"
                                 + ("" if login == expected else
                                    f"  {RED}(expected {expected}!){RESET}"))
                    elif login != expected:
                        self.log(f"{RED}terminal {inst} is in account {login}, "
                                 f"expected {expected}{RESET}")
                    st.login = login

        self._adjust_poll_interval()

    # ------------------------------------------------------------------
    def frame(self) -> str:
        accs = read_accounts()
        out = [f"{BOLD}MT5 BRIDGE SUPERVISOR{RESET}  {DIM}2 terminals, "
               f"SpotDump EA feeds, auto-heal{RESET}  "
               f"{DIM}{now_ts()}{RESET}  poll={self._poll_interval:.1f}s{RESET}", ""]
        
        for inst, st in self.terms.items():
            a = accs.get(inst, {})
            exp = a.get("login", "?")
            h = header_for(inst)
            login = h.get("login", "") or "-"
            ok = (login == exp)
            age = feed_age(inst)
            age_s = f"{age*1000:.0f} ms" if age < 5 else f"{age:.0f} s"
            bal = h.get("balance", "-")
            eq = h.get("equity", "-")
            algo = h.get("trade_allowed", "")   # EA v1.50+ header diagnostic
            if algo == "1":
                algo_s = f"{GREEN}ALGO ON {RESET}"
            elif algo == "0":
                algo_s = f"{RED}ALGO OFF{RESET}"
            else:
                algo_s = f"{DIM}ALGO ?  {RESET}"
            out.append(
                f"  T{inst}  {st.color()}{st.bucket().upper():<5}{RESET}  "
                f"feed {age_s:>8}  {algo_s}  "
                f"login {login:<11}"
                f"{'(ok)' if ok else f'{RED}(want {exp}){RESET}'}  "
                f"{DIM}bal {bal}  eq {eq}{RESET}")
        
        out.append("")
        out.append(f"{BOLD}EVENTS{RESET}  {DIM}(only changes are logged){RESET}")
        if self.events:
            out.extend(f"  {e}" for e in self.events)
        else:
            out.append(f"  {DIM}quiet - everything nominal{RESET}")
        return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep both MT5 terminals and "
                                              "their EA bridges alive")
    ap.add_argument("--once", action="store_true", help="one frame and exit")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="base poll interval seconds (default 5 - gentle)")
    args = ap.parse_args()

    print(f"{BOLD}MT5 Bridge Supervisor{RESET}  {DIM}"
          f"starting terminals 1+2 and maintaining the EA bridges...{RESET}")

    sup = Supervisor()

    # FAST BOOT: fire both terminal launches back-to-back (Popen is
    # non-blocking) so wine boots them in parallel instead of one-by-one.
    launched: list[int] = []
    for inst, st in sup.terms.items():
        if (TERMINALS[inst]["dir"] / "terminal64.exe").exists() and not term_running(inst):
            launch_terminal(inst)
            st.last_start = time.monotonic()
            st.running = True
            launched.append(inst)
    if launched:
        log.info(f"launched terminals {launched} in parallel")
    sup.poll()                      # initial sweep (installs EA, heals what's missing)

    last_frame = ""
    first = True
    try:
        while True:
            sup.poll()
            frame = sup.frame()
            if args.once:
                print(frame)
                break
            if frame != last_frame:     # static screen, only real changes
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last_frame = frame
            time.sleep(max(sup._poll_interval, 0.5))
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())