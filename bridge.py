#!/usr/bin/env python3.12
"""bridge.py - supervisor that keeps BOTH MT5 terminals and their SpotDump
EA bridges alive. Optimized for low CPU usage and consistent connectivity.

Run this first; then any of the viewers work smoothly against a healthy
bridge:
    python spot.py        # spot prices only
    python monitor.py     # account1 + account2 positions/balance/equity
    python info.py        # full info about the LOGGED-IN accounts + spreads

LOGIN FLOW (no acc.env, NO PROMPT anymore):
  * bridge.py boots BOTH TERMINALS CONCURRENTLY straight into the stored
    session (session.json, obfuscated) - the two stored accounts are
    logged in automatically via the start configs, ALGO ON, EA attached;
  * LOGGING INTO ANY ACCOUNT ON ANY TERMINAL (MT5 UI) IS INCORPORATED
    INTO THE WHOLE SOFTWARE: the supervisor sees the EA's identity change
    and ADOPTS the new account into the session - executor, info,
    monitor, metrics, close and the web follow it live, no restart;
    conversely, when the session is rewritten by another process (web
    login form, --seed), terminals are reconnected into it automatically;
  * each terminal's EA-reported login is verified against the session; a
    wrong account gets exactly one automatic relaunch, then it is reported;
  * ANTIDETECT: each verified boot's start-config login block is scrubbed
    immediately and re-scrubbed after EVERY relaunch/heal (credentials
    never sit in a plaintext ini while running), launches carry a small
    random jitter so the two terminals' logins never look
    machine-simultaneous, and the session file itself is obfuscated + 0600.

What it does:
  * auto-starts terminal 1 (account 1) and terminal 2 (account 2, its own
    MT5 install - created automatically on first run);
  * watches each terminal's bridge feed (spots.csv / trades.csv written
    every 50 ms by the EA) and AUTO-RESTARTS a terminal whose feed goes
    stale (EA detached, terminal hung, ...) - with a cooldown so it can
    never restart-storm;
  * verifies each terminal's EA-reported login against the session and
    shouts if a terminal is in the wrong account;
  * shows ONE STATIC SCREEN with a gentle, slow log: lines appear only
    when something actually CHANGES (terminal up/down, feed stale/live,
    account changed) - never per-poll spam, never scrolling.
  * Uses adaptive polling: fast when issues detected, slow when stable.

Usage:
    python bridge.py              # launch terminals, ask logins, supervise
    python bridge.py --once      # one status frame and exit
    python bridge.py --interval 5  # base poll interval seconds (default 5)
    python bridge.py --logout    # forget the session and exit
"""

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
RELAUNCH_COOLDOWN_S = 10.0   # never spawn a terminal more often than this

# Adaptive polling - state machine (no bouncing multipliers)
POLL_FAST = 1.0     # a terminal is down/stale
POLL_NORMAL = 3.0   # something is laggy
POLL_SLOW = 5.0     # both feeds live and stable (was 15 s: an account
#                     switch then took up to 25 s to be adopted and shown)

now_ts = lambda: dt.datetime.now().strftime("%H:%M:%S")

# how long a terminal has to produce a live feed after boot before the
# login is verified / the start-config scrubbed (the EA must have written
# at least one trades.csv header - it carries the EA-reported login)
LOGIN_VERIFY_TIMEOUT_S = 90.0
SCRUB_AFTER_LIVE_S = 8.0        # feed must be live this long before scrubbing
SYNC_DEAD_S = 45.0              # live feed with EMPTY account data this long = sync-dead, heal


def wait_for_login_feed(inst: int, timeout: float) -> bool:
    """Block until terminal `inst`'s EA feed is fresh (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if term_running(inst) and feed_age(inst) < 5.0:
            return True
        time.sleep(0.5)
    return False


def verify_login(inst: int) -> tuple[bool, str, str]:
    """(ok, ea_reported_login, expected_login) for terminal `inst`.

    Uses header_for() (session-login-aware, ALL data roots) - the old
    install-dir-only read never saw the login of a terminal whose data
    folder lives in AppData, so every boot 'failed' verification after a
    90 s wait and triggered a pointless remedial relaunch."""
    expected = session.expected_login(inst)
    got = header_for(inst).get("login", "")
    return (bool(got) and got == expected, got, expected)


def login_and_boot() -> tuple[bool, list[int]]:
    """Launch terminals FIRST, then ask for fresh logins, then connect.

    Flow (every start):
      1. the stored session (session.json) is the identity source - both
         terminals boot CONCURRENTLY logged into EXACTLY those accounts
         (start config login block + AutoTrading=1 + Bridge template);
      2. each terminal's EA-reported login is verified against the
         session; a wrong account gets one automatic relaunch;
      3. HOT SWITCHING: accounts changed in a terminal's UI are adopted
         into the session (whole software follows); a session rewritten
         externally pulls the terminals into the new accounts.

    Returns (True when both terminals came up with the right accounts,
    list of instances this boot launched) - main() must NEVER re-launch
    a terminal from that list (Popen is async, pgrep lags behind).
    """
    launched: list[int] = []
    st_last_start: dict[int, float] = {}   # boot timestamps for main()'s grace

    # create terminal 2's install if missing (one-time copy)
    if not (MT5_DIR2 / "terminal64.exe").exists():
        print("terminal 2 install missing - creating it (one-time copy)...")
        if not setup_terminal2():
            print(f"{RED}could not create terminal 2 install{RESET}")
            return False, launched

    # 0) IDENTITY SOURCE - the stored session.  No prompting: the bridge
    #    boots straight into these accounts; to trade OTHER accounts just
    #    log into them on a terminal (adopted live) or use session.py --seed.
    if not session.is_logged_in():
        print(f"{RED}no stored session (session.json) - seed one with: "
              f"python session.py --seed{RESET}")
        return False, launched
    accs = session.load()
    for inst in (1, 2):
        a = accs.get(inst, {})
        print(f"  terminal {inst}: login={a.get('login', '-')} "
              f"server={a.get('server', '-')}")

    # 1) BOOT BOTH TERMINALS CONCURRENTLY - WITH their login block (the
    #    stored accounts), ALGO ON, EA auto-attach.  A terminal already
    #    running from before is stopped so MT5 cannot rewrite common.ini
    #    (AutoTrading) on exit behind our back.
    boot_res: dict[int, bool] = {}

    def boot_one(inst: int) -> None:
        """stop -> compile EA -> ALGO ON -> launch with the session's login.

        The EA is installed/compiled BEFORE the launch, while the terminal is
        stopped: compiling after boot (the old order) meant the very first
        boot after any EA change ran WITHOUT a working EA, its feed never
        came up, and the supervisor 'healed' it with a pointless restart a
        minute later - the boot->restart dance the user kept seeing."""
        try:
            if not (TERMINALS[inst]["dir"] / "terminal64.exe").exists():
                print(f"{RED}terminal {inst} executable missing{RESET}")
                boot_res[inst] = False
                return
            if term_running(inst):
                print(f"  terminal {inst} is running - stopping it for a clean boot...")
                stop_terminal(inst)
            install_script(inst)                 # EA present BEFORE first boot
            ensure_autotrading(inst)             # not running anymore - safe to force
            launch_terminal(inst)                # WITH login block = auto-login
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

    # 2) VERIFY - both boots already carry the session's logins; confirm
    #    each EA connected to the right account (concurrently), one
    #    remedial relaunch for a miss, then scrub the credentials
    #    (ANTIDETECT) once the boot consumed them.
    verify_res: dict[int, bool] = {}

    def verify_one(inst: int) -> None:
        """wait feed -> verify login (+ one remedial relaunch) -> scrub."""
        if not wait_for_login_feed(inst, LOGIN_VERIFY_TIMEOUT_S):
            print(f"{RED}terminal {inst} produced no live feed within "
                  f"{LOGIN_VERIFY_TIMEOUT_S:.0f}s{RESET}")
            verify_res[inst] = False
            return
        install_script(inst)
        ok, got, expected = verify_login(inst)
        if not ok:
            # exactly one remedial relaunch with a fresh login config
            print(f"{YELLOW}terminal {inst} logged in as {got or '?'} "
                  f"(want {expected}) - relaunching once{RESET}")
            restart_terminal(inst)
            if wait_for_login_feed(inst, LOGIN_VERIFY_TIMEOUT_S):
                ok, got, expected = verify_login(inst)
        if ok:
            # ANTIDETECT: this boot consumed the login config - scrub
            # it NOW so the credentials never linger in plaintext
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
    """EA header of terminal `inst`'s trades.csv ('' fields when no data).

    Resolves the terminal by its SESSION LOGIN first (pick_terminal scans
    every data root - install dir AND AppData instances), so an account
    whose terminal keeps its data outside the install dir is still seen
    (the old install-dir-only lookup made such accounts invisible to the
    supervisor forever -> 'account refusing to show').  read_header() is
    mtime-cached in spot.py, so this is cheap to call every poll; the
    legacy fallback scan is throttled to once per 30 s.
    """
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
        self.ever_session = False   # EA seen on the session account at least once
        self.header: dict = {}
        self.header_ts = 0.0
        self.launch_fails = 0       # consecutive launches that never came up
        self.heal_fails = 0         # consecutive heals that did not restore the feed
        self.sync_dead_since = 0.0  # feed live but account data empty since (0 = ok)

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
        self._terminal2_ready = (MT5_DIR2 / "terminal64.exe").exists()
        # ANTIDETECT: scrub each terminal's start-config login block once its
        # feed has been live for a few seconds (boot already consumed it).
        # Keyed by the boot's st.last_start so EVERY relaunch/heal is
        # scrubbed again - not just the first one.
        self._live_since: dict[int, float] = {}
        self._scrubbed_for_start: dict[int, float] = {}
        self._sess_stamp = session.file_stamp()   # session.json change watcher
        # INSTANT switch rendering: set on any state/account change -> the
        # supervisor skips its remaining nap and re-renders immediately,
        # so a UI login is adopted (and shown) within one poll (~0.1-0.2 s).
        self._fast_frame = False

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
                    st.launch_fails = 0          # it came up - backoff resets
                    install_script(inst)
                else:
                    self.log(f"terminal {inst} is {RED}DOWN{RESET}")
                    # died right after a launch (or never appeared): count the
                    # failure so the next relaunch backs off exponentially
                    if (st.last_start
                            and 0 < time.monotonic() - st.last_start < 60.0):
                        st.launch_fails += 1
                st.running = running

            if not running:
                # relaunch with an EXPONENTIAL cooldown so a broken install
                # can never spawn-storm wine processes: 10 s -> 20 -> 40 -> 80
                # -> 160 s, reset the moment the terminal is up + live again.
                # A terminal with NO session credentials is launched ONCE
                # (so the operator can log into it via the MT5 UI - the
                # login is then adopted); after that it is never
                # relaunch-spammed: each boot would just pop another
                # logged-out MT5 window every cooldown (the old
                # relaunch-loop that made accounts 'refuse to show').
                credless = expected in ("", "?")
                may_launch = exe_ok and (not credless or st.last_start == 0.0)
                cooldown = min(RELAUNCH_COOLDOWN_S * (2 ** min(st.launch_fails, 5)), 300.0)
                if may_launch and now - st.last_start > cooldown:
                    self.log(f"terminal {inst} not running - {DIM}starting{RESET}")
                    install_script(inst)         # EA ready BEFORE the launch
                    launch_terminal(inst)
                    st.last_start = time.monotonic()
                    st.running = True
                continue

            # bridge health / auto-heal
            b = st.bucket()
            if b != st.age_bucket:
                if b == "live":
                    self.log(f"terminal {inst} bridge is {GREEN}LIVE{RESET}")
                    st.launch_fails = 0                # boot succeeded
                    st.heal_fails = 0                  # feed restored
                elif b == "stale":
                    self.log(f"terminal {inst} bridge {RED}STALE{RESET}")
                elif b == "laggy":
                    self.log(f"terminal {inst} bridge {YELLOW}laggy{RESET}")
                st.age_bucket = b
                self._fast_frame = True   # state changed - render instantly

            # ANTIDETECT: once the feed has been live for a few seconds the
            # boot config was consumed - scrub its login block so plaintext
            # credentials never sit on disk while the terminal runs.  Tied
            # to THIS boot (st.last_start): every relaunch/heal rewrites the
            # config and must be scrubbed again.
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
                # heal backoff: a terminal whose EA never comes back is
                # restarted after 120 s, then 240 s, then 480 s ... capped at
                # 15 min - it reports on screen instead of restart-looping
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

            # SYNCHRONIZATION-DEAD detection: the feed is live but the EA
            # header has NO account data (currency/balance empty) - the
            # terminal never synchronized with the broker (rejected or
            # absent credentials, server unreachable).  Feed-age-only
            # healing called this state 'healthy' forever and the account
            # card showed all zeros.  Heal exactly like a stale feed.
            if b == "live" and st.header and not st.header.get("currency"):
                if st.sync_dead_since == 0.0:
                    st.sync_dead_since = time.monotonic()
                if (time.monotonic() - st.sync_dead_since > SYNC_DEAD_S
                        and time.monotonic() - st.last_start > GRACE_S
                        and time.monotonic() - st.last_heal > COOLDOWN_S):
                    self.log(f"{YELLOW}terminal {inst} feed live but account never "
                             f"synchronized - restarting into session credentials{RESET}")
                    st.last_heal = time.monotonic()
                    if restart_terminal(inst):
                        st.last_start = time.monotonic()
                        st.running = True
                    st.heal_fails += 1
                    st.sync_dead_since = 0.0
            else:
                st.sync_dead_since = 0.0

            # account identity check - ADAPTIVE cadence: ~1 s while the
            # header login is unknown or mismatches the session (fresh boot,
            # UI switch, external session rewrite) so a new account is
            # adopted/shown within a second or two; 5 s once verified.
            if time.monotonic() - st.last_check > (1.0 if (not st.login or st.login != expected) else 5.0):
                st.last_check = time.monotonic()
                h = header_for(inst)
                st.header = h
                st.header_ts = time.monotonic()
                login = h.get("login", "")
                # "0" is MT5's LOGGED-OUT marker, not an account - treating
                # it as a login once poisoned the session with login="0"
                # (and every later header parse with a bogus adoption).
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

                # HOT ACCOUNT SWITCHING - two paths, both instant:
                #  A) ADOPT (UI-side switch): someone logged the TERMINAL
                #     into a different account (MT5 UI or a fresh manual
                #     run).  The feed is live, so the terminal knows the
                #     new account - ADOPT it: update the session to the
                #     new login (password/server preserved) so the whole
                #     software (executor routes, web panel, monitor,
                #     metrics, close) follows CONCURRENTLY.
                #  B) RECONNECT (external switch-in): the session file was
                #     rewritten by ANOTHER process (web login form, --seed,
                #     second bridge).  session.load() stat-validates its
                #     cache every poll, so `expected` is already the new
                #     account; relaunch the terminal INTO it.
                if login == expected:
                    st.ever_session = True      # EA reached the session account
                elif not expected and login:
                    # C) session has NO credentials for this slot but the
                    # terminal IS live in some account: adopt it so the
                    # operator's UI login is incorporated everywhere
                    # (previously this state ran the B-branch below with
                    # expected '?' and 'reconnected' forever - the
                    # account kept flip-flopping and never showed).
                    accs = read_accounts()
                    a = accs.get(inst, {})
                    a["login"] = login
                    if h.get("server"):
                        a["server"] = h["server"]
                    known = known_accounts.get(login)
                    if known:
                        # a KNOWN account (used before): restore its real
                        # credentials so auto-relogin keeps working
                        a["password"] = known["password"]
                        if known.get("server"):
                            a["server"] = known["server"]
                    accs[inst] = a
                    session.set_accounts(accs, persist=True)
                    known_accounts.remember(login, a.get("password", ""),
                                            a.get("server", ""))
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
                    # Only a terminal that WAS on the session account and
                    # then moved is a real user switch (adopt).  A boot that
                    # never reached the session account (MT5 fell back to a
                    # saved login) is a FAILED boot: reconnect, never adopt
                    # - adopting that would clobber the session with the
                    # stale account (observed: MetaQuotes demo adopted over
                    # the HFM account).
                    fresh_session = session.file_stamp() != self._sess_stamp
                    if st.ever_session and not fresh_session:
                        # A) user switch: adopt the new account
                        accs = read_accounts()
                        a = accs.get(inst, {})
                        if a.get("login") and a["login"] != login:
                            a["login"] = login
                            # the EA header knows the new broker server too -
                            # adopt it so a future auto-relogin targets the
                            # right server (the PASSWORD can never be captured
                            # from a UI login; reseed with session.py --seed
                            # if auto-relogin into this account is needed)
                            if h.get("server"):
                                a["server"] = h["server"]
                            known = known_accounts.get(login)
                            if known:
                                # KNOWN account: restore its real credentials
                                a["password"] = known["password"]
                                if known.get("server"):
                                    a["server"] = known["server"]
                            session.set_accounts(accs, persist=True)
                            known_accounts.remember(login, a.get("password", ""),
                                                    a.get("server", ""))
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
                        # B) external switch-in (session rewritten by another
                        # process: web login form, --seed, PANEL SWITCH):
                        # reconnect the terminal INTO the session account.
                        # Only when the session account HAS stored
                        # credentials - a passwordless slot can never be
                        # reconnected into, and trying forever fought the
                        # operator's own switch ("WRONG (want ...)" forever).
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

    # ------------------------------------------------------------------
    def frame(self) -> str:
        accs = read_accounts()
        out = [f"{BOLD}MT5 BRIDGE SUPERVISOR{RESET}  {DIM}2 terminals, "
               f"SpotDump EA feeds, auto-heal{RESET}  "
               f"{DIM}{now_ts()}{RESET}  poll={self._poll_interval:.1f}s{RESET}", ""]
        
        for inst, st in self.terms.items():
            a = accs.get(inst, {})
            exp = a.get("login", "?")
            h = st.header if st.header else header_for(inst)
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
    ok_boot, launched_boot = login_and_boot()
    # give the boot's launches their grace + down-state (Popen is async:
    # pgrep will not see the wine process for a few seconds)
    for inst, st in sup.terms.items():
        if inst in launched_boot:
            st.last_start = time.monotonic()
            st.running = True
    if not ok_boot:
        print(f"{YELLOW}continuing to supervise anyway - the screen shows "
              f"what is wrong{RESET}")

    # fire any terminal the boot phase could not start (e.g. exe appeared
    # late) - but never one the boot itself launched (double-launch!)
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
            # INSTANT rendering: any state/account change inside poll()
            # shortens the very nap that would delay the redraw - a UI
            # account switch shows up in ~1 s instead of up to 15 s.
            fast = sup._fast_frame
            sup._fast_frame = False
            if frame != last_frame:     # static screen, only real changes
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last_frame = frame
                time.sleep(max(sup._poll_interval, 0.5))
            elif fast:
                time.sleep(1.0)         # settle, then re-render the new state
            else:
                time.sleep(max(sup._poll_interval, 0.5))
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\nbye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())