#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import sys
import time
import datetime as dt
import threading

from config import CONFIG, setup_logging

from spot import (
    BOLD, DIM, RESET, GREEN, RED, YELLOW,
    TERMINALS, MT5_DIR2, read_accounts, read_header, scan_terminals,
    term_running, launch_terminal, restart_terminal, stop_terminal,
    setup_terminal2, install_script, feed_age, scrub_start_cfg,
    ensure_autotrading, pick_terminal,
)
import session
import accounts as known_accounts

log = setup_logging(__name__)

GRACE_S = CONFIG.bridge_grace_seconds
STALE_S = CONFIG.bridge_stale_seconds
COOLDOWN_S = CONFIG.bridge_cooldown_seconds
MAX_EVENTS = 10
RELAUNCH_COOLDOWN_S = 10.0

POLL_FAST = 1.0
POLL_NORMAL = 3.0
POLL_SLOW = 5.0

now_ts = lambda: dt.datetime.now().strftime("%H:%M:%S")

def _session_accounts() -> dict[int, dict[str, str]]:
    accs = read_accounts()
    for inst, a in list(accs.items()):
        lg = str(a.get("login", "")).strip()
        if lg in ("", "0", "?", "LOGIN"):
            accs[inst] = {k: v for k, v in a.items() if k != "login"}
    return accs

LOGIN_VERIFY_TIMEOUT_S = 90.0
SCRUB_AFTER_LIVE_S = 8.0
SYNC_DEAD_S = 45.0

def wait_for_login_feed(inst: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if term_running(inst) and feed_age(inst) < 5.0:
            return True
        time.sleep(0.5)
    return False

def verify_login(inst: int) -> tuple[bool, str, str]:
    expected = session.expected_login(inst)
    got = header_for(inst).get("login", "")
    return (bool(got) and got == expected, got, expected)

def login_and_boot() -> tuple[bool, list[int]]:
    launched: list[int] = []
    st_last_start: dict[int, float] = {}

    if not (MT5_DIR2 / "terminal64.exe").exists():
        print("terminal 2 install missing - creating it (one-time copy)...")
        if not setup_terminal2():
            print(f"{RED}could not create terminal 2 install{RESET}")
            return False, launched

    if not session.is_logged_in():
        print(f"{RED}no stored session (session.json) - seed one with: "
              f"python session.py --seed{RESET}")
        return False, launched
    accs = session.load()
    for inst in (1, 2):
        a = accs.get(inst, {})
        print(f"  terminal {inst}: login={a.get('login', '-')} "
              f"server={a.get('server', '-')}")

    boot_res: dict[int, bool] = {}

    def boot_one(inst: int) -> None:
        try:
            if not (TERMINALS[inst]["dir"] / "terminal64.exe").exists():
                print(f"{RED}terminal {inst} executable missing{RESET}")
                boot_res[inst] = False
                return
            if term_running(inst):
                print(f"  terminal {inst} is running - stopping it for a clean boot...")
                stop_terminal(inst)
            install_script(inst)
            ensure_autotrading(inst)
            launch_terminal(inst)
            st_last_start[inst] = time.monotonic()
            launched.append(inst)
            boot_res[inst] = True
        except Exception as exc:
            print(f"{RED}terminal {inst} boot failed: {exc}{RESET}")
            boot_res[inst] = False

    print("booting both terminals CONCURRENTLY into the stored accounts (ALGO ON)...")
    threads = [threading.Thread(target=boot_one, args=(i,), daemon=True,
                                name=f"boot-{i}") for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if not all(boot_res.get(i, False) for i in (1, 2)):
        return False, launched

    accs = session.load()
    for inst in (1, 2):
        a = accs.get(inst, {})
        print(f"  terminal {inst}: login={a.get('login', '-')} server={a.get('server', '-')}")

    verify_res: dict[int, bool] = {}

    def verify_one(inst: int) -> None:
        if not wait_for_login_feed(inst, LOGIN_VERIFY_TIMEOUT_S):
            print(f"{RED}terminal {inst} produced no live feed within "
                  f"{LOGIN_VERIFY_TIMEOUT_S:.0f}s{RESET}")
            verify_res[inst] = False
            return
        install_script(inst)
        ok, got, expected = verify_login(inst)
        if not ok:
            print(f"{YELLOW}terminal {inst} logged in as {got or '?'} "
                  f"(want {expected}) - relaunching once{RESET}")
            restart_terminal(inst)
            if wait_for_login_feed(inst, LOGIN_VERIFY_TIMEOUT_S):
                ok, got, expected = verify_login(inst)
        if ok:
            scrub_start_cfg(inst)
            print(f"{GREEN}terminal {inst} logged in as {got} - "
                  f"ALGO ON, login config scrubbed{RESET}")
        else:
            print(f"{RED}terminal {inst} identity NOT confirmed "
                  f"(got {got or '?'}, want {expected}){RESET}")
        verify_res[inst] = ok

    threads = [threading.Thread(target=verify_one, args=(i,), daemon=True,
                                name=f"verify-{i}") for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return all(verify_res.get(i, False) for i in (1, 2)), launched

def header_for(inst: int) -> dict:
    login = session.expected_login(inst)
    if login:
        term = pick_terminal(login)
        if term:
            return read_header(term["trades_path"])
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
    __slots__ = ('inst', 'running', 'age_bucket', 'login', 'last_start',
                 'last_heal', 'last_check', 'ever_session', 'header',
                 'header_ts', 'launch_fails', 'heal_fails',
                 'sync_dead_since')

    def __init__(self, inst: int):
        self.inst = inst
        self.running = False
        self.age_bucket = ""
        self.login = ""
        self.last_start = 0.0
        self.last_heal = 0.0
        self.last_check = 0.0
        self.ever_session = False
        self.header: dict = {}
        self.header_ts = 0.0
        self.launch_fails = 0
        self.heal_fails = 0
        self.sync_dead_since = 0.0

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
        self._terminal2_ready = (MT5_DIR2 / "terminal64.exe").exists()
        self._live_since: dict[int, float] = {}
        self._scrubbed_for_start: dict[int, float] = {}
        self._sess_stamp = session.file_stamp()
        self._fast_frame = False

    def log(self, text: str) -> None:
        self.events.insert(0, f"{DIM}{now_ts()}{RESET}  {text}")
        self.events = self.events[:MAX_EVENTS]
        clean = text.replace(GREEN, "").replace(RED, "").replace(YELLOW, "").replace(DIM, "").replace(RESET, "").replace(BOLD, "")
        log.info(clean)

    def _adjust_poll_interval(self) -> None:
        buckets = {st.bucket() for st in self.terms.values()}
        if "down" in buckets or "stale" in buckets:
            self._poll_interval = POLL_FAST
        elif "laggy" in buckets:
            self._poll_interval = POLL_NORMAL
        else:
            self._poll_interval = POLL_SLOW

    def poll(self) -> None:
        try:
            self._poll()
        except Exception as exc:
            log.error(f"supervisor poll error: {exc}")

    def _poll(self) -> None:
        accs = _session_accounts()
        
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
                    st.launch_fails = 0
                    install_script(inst)
                else:
                    self.log(f"terminal {inst} is {RED}DOWN{RESET}")
                    if (st.last_start
                            and 0 < time.monotonic() - st.last_start < 60.0):
                        st.launch_fails += 1
                st.running = running

            if not running:
                credless = expected in ("", "?")
                may_launch = exe_ok and (not credless or st.last_start == 0.0)
                cooldown = min(RELAUNCH_COOLDOWN_S * (2 ** min(st.launch_fails, 5)), 300.0)
                if may_launch and now - st.last_start > cooldown:
                    self.log(f"terminal {inst} not running - {DIM}starting{RESET}")
                    install_script(inst)
                    launch_terminal(inst)
                    st.last_start = time.monotonic()
                    st.running = True
                continue

            b = st.bucket()
            if b != st.age_bucket:
                if b == "live":
                    self.log(f"terminal {inst} bridge is {GREEN}LIVE{RESET}")
                    st.launch_fails = 0
                    st.heal_fails = 0
                elif b == "stale":
                    self.log(f"terminal {inst} bridge {RED}STALE{RESET}")
                elif b == "laggy":
                    self.log(f"terminal {inst} bridge {YELLOW}laggy{RESET}")
                st.age_bucket = b
                self._fast_frame = True

            if b == "live":
                since = self._live_since.setdefault(inst, time.monotonic())
                if (time.monotonic() - since > SCRUB_AFTER_LIVE_S
                        and self._scrubbed_for_start.get(inst) != st.last_start):
                    scrub_start_cfg(inst)
                    self._scrubbed_for_start[inst] = st.last_start
                    log.info(f"terminal {inst}: start-config login scrubbed "
                             f"(antidetect)")
            else:
                self._live_since.pop(inst, None)

            if b == "stale":
                in_grace = time.monotonic() - st.last_start < GRACE_S
                heal_gap = min(COOLDOWN_S * (2 ** min(st.heal_fails, 4)), 900.0)
                cooled = time.monotonic() - st.last_heal > heal_gap
                if not in_grace and cooled:
                    self.log(f"{YELLOW}healing terminal {inst} "
                             f"(restart to re-attach EA){RESET}")
                    st.last_heal = time.monotonic()
                    if restart_terminal(inst):
                        st.last_start = time.monotonic()
                        st.running = True
                    else:
                        self.log(f"{RED}heal of terminal {inst} failed{RESET}")
                    st.heal_fails += 1

            h_now = header_for(inst)
            if h_now:
                st.header = h_now
            h_login = (st.header or {}).get("login", "")
            no_identity = ((not (st.header or {}).get("currency"))
                           or h_login in ("", "0"))
            if (b == "live" and st.header and no_identity
                    and expected not in ("", "?")):
                if st.sync_dead_since == 0.0:
                    st.sync_dead_since = time.monotonic()
                if (time.monotonic() - st.sync_dead_since > SYNC_DEAD_S
                        and time.monotonic() - st.last_start > GRACE_S
                        and time.monotonic() - st.last_heal > COOLDOWN_S):
                    self.log(f"{YELLOW}terminal {inst} feed live but LOGGED OUT "
                             f"(login 0) / never synchronized - restarting into "
                             f"session account {expected}{RESET}")
                    st.last_heal = time.monotonic()
                    if restart_terminal(inst):
                        st.last_start = time.monotonic()
                        st.running = True
                    st.heal_fails += 1
                    st.sync_dead_since = 0.0
            else:
                st.sync_dead_since = 0.0

            if time.monotonic() - st.last_check > (1.0 if (not st.login or st.login != expected) else 5.0):
                st.last_check = time.monotonic()
                h = header_for(inst)
                st.header = h
                st.header_ts = time.monotonic()
                login = h.get("login", "")
                if login == "0":
                    login = ""
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

                if login == expected:
                    st.ever_session = True
                elif not expected and login:
                    accs = _session_accounts()
                    a = accs.get(inst, {})
                    a["login"] = login
                    if h.get("server"):
                        a["server"] = h["server"]
                    known = known_accounts.get(login)
                    if known:
                        a["password"] = known["password"]
                        if known.get("server"):
                            a["server"] = known["server"]
                    else:
                        try:
                            known_accounts.remember(login, "", a.get("server", ""))
                        except ValueError:
                            pass
                    accs[inst] = a
                    session.set_accounts(accs, persist=True)
                    try:
                        known_accounts.remember(login, a.get("password", ""),
                                                a.get("server", ""))
                    except ValueError:
                        pass
                    self.log(f"{YELLOW}terminal {inst} live in account {login} - "
                             f"session adopted (was unset), whole software follows{RESET}")
                    log.warning(f"terminal {inst}: adopted account {login} "
                                f"(server {a.get('server')}) - password unknown; "
                                f"run 'python session.py --seed' if auto-relogin "
                                f"into it is ever needed")
                    st.login = login
                    st.ever_session = True
                    self._fast_frame = True
                elif expected and login:
                    fresh_session = session.file_stamp() != self._sess_stamp
                    if st.ever_session and not fresh_session:
                        accs = _session_accounts()
                        a = accs.get(inst, {})
                        if a.get("login") and a["login"] != login:
                            a["login"] = login
                            if h.get("server"):
                                a["server"] = h["server"]
                            known = known_accounts.get(login)
                            if known:
                                a["password"] = known["password"]
                                if known.get("server"):
                                    a["server"] = known["server"]
                            session.set_accounts(accs, persist=True)
                            try:
                                known_accounts.remember(login, a.get("password", ""),
                                                        a.get("server", ""))
                            except ValueError:
                                pass
                            self.log(f"{YELLOW}terminal {inst} switched to "
                                     f"account {login} - session adopted, "
                                     f"whole software follows{RESET}")
                            log.warning(f"terminal {inst}: adopted account "
                                        f"{login} (server {a.get('server')}) - "
                                        f"password unknown; run 'python session.py "
                                        f"--seed' if auto-relogin is needed")
                            st.login = login
                            st.ever_session = False
                            self._fast_frame = True
                    elif (expected not in ("", "?")
                          and known_accounts.has_credentials(expected)
                          and time.monotonic() - st.last_start > GRACE_S
                          and time.monotonic() - st.last_heal > COOLDOWN_S):
                        self.log(f"{YELLOW}terminal {inst} is in {login} - "
                                 f"reconnecting into session account "
                                 f"{expected}{RESET}")
                        st.last_heal = time.monotonic()
                        if restart_terminal(inst):
                            st.last_start = time.monotonic()
                            st.running = True
                        self._fast_frame = True
                self._sess_stamp = session.file_stamp()

        self._adjust_poll_interval()

    def frame(self) -> str:
        accs = _session_accounts()
        out = [f"{BOLD}MT5 BRIDGE SUPERVISOR{RESET}  {DIM}2 terminals, "
               f"SpotDump EA feeds, auto-heal{RESET}  "
               f"{DIM}{now_ts()}{RESET}  poll={self._poll_interval:.1f}s{RESET}", ""]
        
        for inst, st in self.terms.items():
            a = accs.get(inst, {})
            exp = a.get("login", "?")
            h = st.header if st.header else header_for(inst)
            raw_login = h.get("login", "")
            logged_out = raw_login in ("", "0")
            login = "-" if logged_out else raw_login
            ok = (not logged_out and login == exp)
            age = feed_age(inst)
            age_s = f"{age*1000:.0f} ms" if age < 5 else f"{age:.0f} s"
            bal = "-" if logged_out else h.get("balance", "-")
            eq = "-" if logged_out else h.get("equity", "-")
            algo = h.get("trade_allowed", "")
            if algo == "1":
                algo_s = f"{GREEN}ALGO ON {RESET}"
            elif algo == "0":
                algo_s = f"{RED}ALGO OFF{RESET}"
            else:
                algo_s = f"{DIM}ALGO ?  {RESET}"
            b = st.bucket()
            btxt = "LOGOUT" if (logged_out and b == "live" and exp not in ("", "?")) \
                else b.upper()
            bcol = RED if btxt == "LOGOUT" else st.color()
            out.append(
                f"  T{inst}  {bcol}{btxt:<6}{RESET}  "
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
    ap.add_argument("--logout", action="store_true",
                    help="forget the stored session and exit")
    args = ap.parse_args()

    if args.logout:
        session.clear()
        print("session cleared - bridge.py will ask for logins again")
        return 0

    print(f"{BOLD}MT5 Bridge Supervisor{RESET}  {DIM}"
          f"login -> launch both terminals ALGO ON + antidetect -> "
          f"maintain the EA bridges...{RESET}")

    sup = Supervisor()
    for ghost in ("LOGIN", "0", "?"):
        try:
            if known_accounts.forget(ghost):
                log.warning(f"removed invalid known-account entry {ghost!r} "
                            f"(placeholder garbage)")
        except Exception:
            pass
    ok_boot, launched_boot = login_and_boot()
    for inst, st in sup.terms.items():
        if inst in launched_boot:
            st.last_start = time.monotonic()
            st.running = True
    if not ok_boot:
        print(f"{YELLOW}continuing to supervise anyway - the screen shows "
              f"what is wrong{RESET}")

    for inst, st in sup.terms.items():
        if inst in launched_boot:
            continue
        if (TERMINALS[inst]["dir"] / "terminal64.exe").exists() and not term_running(inst):
            launch_terminal(inst)
            st.last_start = time.monotonic()
            st.running = True
            launched_boot.append(inst)
    if launched_boot:
        log.info(f"launched terminals {launched_boot} in parallel")
    sup.poll()

    last_frame = ""
    first = True
    try:
        while True:
            sup.poll()
            frame = sup.frame()
            if args.once:
                print(frame)
                break
            fast = sup._fast_frame
            sup._fast_frame = False
            if frame != last_frame:
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last_frame = frame
                time.sleep(max(sup._poll_interval, 0.5))
            elif fast:
                time.sleep(1.0)
            else:
                time.sleep(max(sup._poll_interval, 0.5))
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())