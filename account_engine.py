#!/usr/bin/env python3.12
"""Account Integration Engine.

Smooth, concurrent login of any MT5 account (live or demo) into any
terminal, built on top of the existing session / spot / bridge plumbing
(no duplicated state):

  * one worker thread per terminal - logins never fight each other and
    never block the web panel or the executor
  * every operation is validated BEFORE a terminal is touched - including
    whether MT5 can reach the trade server at all (a server this install
    has never seen needs an access point; without one MT5 never dials)
  * every operation is verified AFTER it runs, against the BROKER's own
    authorization (account details, or MT5's `authorized on <server>`
    journal line) - the EA echoing the requested login number is not proof
    of anything, and treating it as one is what made a CXM account look
    logged in while it was never connected
  * adopts an account already logged in on the terminal (password pulled
    from the known-accounts book when available)
  * demo/real detection from the EA header (ACCOUNT_TRADE_MODE)
  * access-point registry so a brand-new broker is a one-line addition
    (`--access-point` / `/api/engine/access-point` / the panel button)
  * an operation registry so the web panel and CLI can show live status

Deliberately NOT touched: executor.py, the scheduler, database schema,
and bridge.py's supervisor loops.  This engine cooperates with the
supervisor by writing through session.set_accounts() exactly like the
existing adoption path does.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import threading
import time
import datetime as dt

import config
from config import setup_logging
import session
import accounts as known_accounts

log = setup_logging(__name__)

WAIT_LOGIN_TIMEOUT_S = 90.0
FAST_POLL_S = 0.15
SLOW_POLL_S = 0.4
FAST_POLL_WINDOW_S = 15.0
# A freshly launched MT5 under wine needs tens of seconds before it even
# ATTEMPTS authorization (EA attach + history sync + network scan observed
# at 20-60 s on cold boots).  Killing or failing the switch inside that
# window murders healthy boots - the "login takes forever" complaint.
# So the stuck-terminal shortcut below is gated on boot age: while the
# process is younger than BOOT_GRACE_S, a live old login (or login 0) just
# means "still starting".  Past grace, STUCK_LOGIN_S of live-wrong-login
# fails fast with an actionable message.
BOOT_GRACE_S = 60.0
# A live feed (EA writing) that keeps reporting a DIFFERENT real login this
# long AFTER boot grace means MT5 has settled and rejected the switch
# (wrong password, or server it cannot resolve) - fail fast with an
# actionable message instead of burning the whole 90 s timeout.  Only
# counts while the feed is live AND shows a real (non-empty) other login;
# a logged-out/empty feed means the terminal is still booting/connecting.
STUCK_LOGIN_S = 30.0
# Phase 2 of verification: the EA already reports the wanted login, but MT5
# has not delivered the trade-account details yet (ACCOUNT_CURRENCY empty,
# balance 0.00 - typical right after logging into a new trade server such
# as FxPro-MT5, which can take a while to synchronize).  Bounded and short:
# the switch itself already happened, so we report success-with-sync-pending
# instead of burning the whole login timeout and failing.
SYNC_TIMEOUT_S = 45.0
SYNC_POLL_S = 0.5
# A header that reports the wanted login is NOT proof of a login.  MT5 fills
# ACCOUNT_LOGIN/ACCOUNT_SERVER straight from the start config, so a terminal
# whose trade-server name this install does not know reports the requested
# login FOREVER while never opening a connection.  Treating that as a match
# is what made a CXM login report "logged in ... broker still syncing" (and
# left terminal 1 wedged) while terminal 2 burned the full 90 s timeout and
# blamed the password.  A login is only real once the broker itself says so:
# account details arrive (currency non-empty) or MT5's journal carries
# `'<login>': authorized on <server>`.  Neither within this window, past the
# boot grace, means MT5 never even attempted it - fail fast and say why.
NO_ATTEMPT_S = 15.0
STATUS_CACHE_TTL_S = 1.0
MAX_OPS_KEPT = 40

TRADE_MODE_LABELS = {"0": "demo", "1": "contest", "2": "live"}


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class EngineError(Exception):
    """Operator-visible failure (bad input, busy terminal, ...)."""


# --------------------------------------------------------------------------
# late-bound helpers (imported lazily so `import account_engine` stays cheap
# and works even when Wine bits are missing)
# --------------------------------------------------------------------------

def _bridge():
    import bridge
    return bridge


def _spot():
    import spot
    return spot


# --------------------------------------------------------------------------
# validation (mirrors session._valid_login - never store implausible logins)
# --------------------------------------------------------------------------

def validate_credentials(login: str, password: str, server: str) -> tuple[str, str, str]:
    login = str(login or "").strip()
    password = str(password or "")
    server = str(server or "").strip()
    if not login.isdigit():
        raise EngineError(f"login {login!r} is not a plausible MT5 account number")
    if not server:
        raise EngineError("server is required (e.g. HFM-Real or MetaQuotes-Demo)")
    if not password:
        raise EngineError("password is required for a direct login")
    return login, password, server


# --------------------------------------------------------------------------
# operation registry (what the panel/CLI display)
# --------------------------------------------------------------------------

class Op:
    __slots__ = ("id", "op", "terminal", "args", "state", "detail", "ts", "done_ts")

    def __init__(self, oid: str, op: str, terminal: int, args: dict):
        self.id = oid
        self.op = op
        self.terminal = terminal
        self.args = dict(args)
        self.state = "queued"      # queued -> running -> done | failed
        self.detail = ""
        self.ts = _now_iso()
        self.done_ts = ""

    def public(self) -> dict:
        args = dict(self.args)
        if args.get("password"):
            args["password"] = "***"   # never leak credentials via status
        return {"id": self.id, "op": self.op, "terminal": self.terminal,
                "args": args, "state": self.state, "detail": self.detail,
                "ts": self.ts, "done_ts": self.done_ts}


# --------------------------------------------------------------------------
# per-terminal worker: serializes operations, owns restart + verification
# --------------------------------------------------------------------------

class TerminalWorker(threading.Thread):

    def __init__(self, engine: "AccountEngine", inst: int):
        super().__init__(daemon=True, name=f"account-engine-t{inst}")
        self.engine = engine
        self.inst = inst
        self.q: queue.Queue[Op] = queue.Queue()
        self._current_op: Op | None = None

    def run(self) -> None:
        while True:
            op = self.q.get()
            self._current_op = op
            try:
                op.state = "running"   # busy was already set at enqueue time
                op.detail = "starting..."
                op.detail = self._execute(op)
                op.state = "done"
                self.engine.log(f"terminal {self.inst}: {op.op} OK - {op.detail}")
            except Exception as exc:
                op.detail = str(exc)
                op.state = "failed"
                self.engine.log(f"terminal {self.inst}: {op.op} FAILED - {exc}")
                log.error(f"account engine terminal {self.inst}: {op.op} "
                          f"failed: {exc}")
            finally:
                op.done_ts = _now_iso()
                self._current_op = None
                self.engine._clear_active(self.inst, op)

    # ---- individual operations ----

    def _execute(self, op: Op) -> str:
        return getattr(self, f"_do_{op.op}")(**op.args)

    def _progress(self, text: str) -> None:
        op = self._current_op
        if op is not None and op.state == "running":
            op.detail = text

    def _authorized(self, want: str, h: dict, started_at: float) -> bool:
        """Has the trade server actually ACCEPTED this login?

        The EA header echoes the login the start config asked for even when
        MT5 never dialled out, so the login number alone proves nothing.
        Proof is either the broker's account details (currency non-empty) or
        MT5's own `'<login>': authorized on <server>` journal line.
        """
        if h.get("currency"):
            return True
        try:
            return bool(_spot().last_auth_success(self.inst, want, started_at))
        except Exception:
            return False

    def _access_point_hint(self, server: str) -> str:
        """The exact, actionable way to make an unknown server loginable."""
        try:
            ap = config.access_point_for(server)
        except Exception:
            ap = ""
        name = server or "SERVER"
        if ap:
            return (f"An access point ({ap}) IS configured for {server or '?'} "
                    f"but no connection came up - check that host:port is "
                    f"still reachable (and that it is the right one).")
        key = "MT5_AP_" + re.sub(r"\W+", "_", str(name)).upper().strip("_")
        return (f"Fix it once and every later login to {server or '?'} works:\n"
                f"    add a line  \"{name} = host:port\"  to "
                f"access_points.env   (or export {key}=host:port), then "
                f"retry - the port is the broker's MT5 access port, e.g.\n"
                f"    {name} = mt5.<broker>.com:443\n"
                f"Alternatively log in once by hand inside MT5 (File -> "
                f"Login to Trade Account) and press ADOPT: after that one "
                f"connect the server is in MT5's own list and plain name "
                f"logins work.")

    def _never_attempted_error(self, want: str, server: str) -> str:
        """MT5 took the credentials into its config but never dialled out.

        This state has the same signature as a slow broker sync (no
        rejection line, no balance) which is exactly why it used to be
        reported as success for the whole timeout.  The cause is almost
        always a trade-server name this MT5 install does not know: MT5 only
        dials a server it already lists, or one handed to it as an ACCESS
        POINT (host:port) in the boot config.
        """
        return (f"terminal {self.inst}: MT5 never attempted login {want} @ "
                f"{server or '?'} - the EA echoed the requested login but no "
                f"connection was made (no authorized / authorization-failed "
                f"line in MT5's journal).  "
                + self._access_point_hint(server))

    def _login_event(self, sp, want: str, started_at: float) -> tuple[str, str]:
        try:
            return sp.last_login_event(self.inst, want, started_at)
        except Exception:
            return "", ""

    def _wait_login(self, want: str, timeout: float = WAIT_LOGIN_TIMEOUT_S,
                    server: str = "", boot_ts: float = 0.0) -> tuple[dict, float, bool]:
        """Two-phase check the terminal is in `want`: the trade server must
        ACCEPT the credentials, then deliver account details.

        Returns (header, matched_in_s, synced).  Matched-but-unsynced is
        success-with-sync-pending, not failure - but only once authorization
        is actually proven, so a login MT5 never even attempted can no longer
        masquerade as success.
        """
        br = _bridge()
        sp = _spot()
        t0 = time.monotonic()
        started_at = time.time()
        deadline = t0 + timeout
        boot_age = (time.monotonic() - boot_ts) if boot_ts else float("inf")
        if boot_ts and boot_age < BOOT_GRACE_S:
            self._progress(f"booting into {want} "
                           f"(terminal just launched, giving it "
                           f"{BOOT_GRACE_S - boot_age:.0f}s to connect...)")
        h: dict = {}
        stuck_since = 0.0
        matched_at = 0.0
        last_progress = 0.0
        self._progress(f"waiting for {want}...")
        while time.monotonic() < deadline:
            h = br.header_for(self.inst)
            got = str(h.get("login", ""))
            if got == want and self._authorized(want, h, started_at):
                break
            rejected = self._broker_rejection(want, started_at, server)
            if rejected:
                raise EngineError(rejected)
            # Boot grace: a young process reporting the old login (or
            # nothing yet) is still starting, NOT stuck.  Only a settled
            # (past-grace) terminal counts toward the shortcuts below.
            past_grace = (not boot_ts
                          or time.monotonic() - boot_ts >= BOOT_GRACE_S)
            try:
                live = sp.feed_age(self.inst) < 5.0
            except Exception:
                live = False
            if got == want:
                # MT5 took the credentials but the trade server has not
                # accepted them yet.  Give it the boot window, then stop
                # pretending: no journal event at all means no dial happened.
                if not matched_at:
                    matched_at = time.monotonic()
                settled = time.monotonic() - matched_at > NO_ATTEMPT_S
                if settled and past_grace and live:
                    kind, _detail = self._login_event(sp, want, started_at)
                    if not kind:
                        raise EngineError(
                            self._never_attempted_error(want, server))
                stuck_since = 0.0
            elif got and got not in ("0", "?"):
                if live and past_grace:
                    if not stuck_since:
                        stuck_since = time.monotonic()
                    elif time.monotonic() - stuck_since > STUCK_LOGIN_S:
                        raise EngineError(
                            f"terminal {self.inst} is live but stuck on "
                            f"{got} - MT5 rejected the switch to {want} "
                            f"(wrong password, or server "
                            f"{server or '?'} unknown to this MT5 install - "
                            f"log in once manually inside MT5, then ADOPT)")
                elif not live or not past_grace:
                    stuck_since = 0.0
            else:
                stuck_since = 0.0
            elapsed = time.monotonic() - t0
            if elapsed - last_progress > 2.0:
                last_progress = elapsed
                if got == want:
                    state = (f"{want} (waiting for the trade server to "
                             f"authorize)")
                elif got and got not in ("0", "?"):
                    state = got
                else:
                    state = "logged out / still starting"
                self._progress(f"waiting for {want}: terminal reports "
                               f"{state} ({elapsed:.0f}s elapsed)")
            time.sleep(FAST_POLL_S if elapsed < FAST_POLL_WINDOW_S else SLOW_POLL_S)
        else:
            h = br.header_for(self.inst)
        matched_in = time.monotonic() - t0
        got = str(h.get("login", ""))
        if got != want:
            raise EngineError(f"terminal did not reach {want} within "
                              f"{timeout:.0f}s (reports {got or 'logged out'}"
                              f"{'' if got and got not in ('0', '?') else ' - MT5 never attempted the login: it may still be booting, or the broker is not answering. Give it one clean attempt, not repeated restarts'}). "
                              f"If it stays logged out, log in once manually "
                              f"inside MT5, then ADOPT")
        if h.get("currency"):
            return h, matched_in, True
        sync_deadline = time.monotonic() + SYNC_TIMEOUT_S
        while time.monotonic() < sync_deadline:
            time.sleep(SYNC_POLL_S)
            h = br.header_for(self.inst)
            if str(h.get("login", "")) != want:
                break              # terminal moved away - re-check below
            if h.get("currency"):
                return h, matched_in, True
            rejected = self._broker_rejection(want, started_at, server)
            if rejected:
                raise EngineError(rejected)
        h = br.header_for(self.inst)
        if str(h.get("login", "")) != want:
            raise EngineError(f"terminal left {want} while waiting for the "
                              f"broker sync (now reports "
                              f"{h.get('login', '') or 'logged out'})")
        if not self._authorized(want, h, started_at):
            # Never authorized, and the sync window is over: this is the
            # phantom-login state, not a slow broker.
            raise EngineError(self._never_attempted_error(want, server))
        return h, matched_in, False

    def _broker_rejection(self, want: str, since: float, server: str) -> str:
        """Did the trade server actually REFUSE this login?

        MT5 journals `'<login>': authorization on <server> failed (...)`
        the moment it is refused.  Without this the failure looks exactly
        like a slow sync, so the operator waits out the whole timeout for
        an account the broker has already rejected - and the terminal is
        restarted into it over and over.  Throttled: the journal is only
        re-read about twice a second, never on every poll.
        """
        if os.getenv("MT5_IGNORE_AUTH_FAILURE", "") == "1":
            return ""
        now = time.monotonic()
        if now - getattr(self, "_reject_ts", 0.0) < 0.5:
            return ""
        self._reject_ts = now
        try:
            line = _spot().last_auth_failure(self.inst, want, since)
        except Exception:
            return ""
        if not line:
            return ""
        log.error(f"terminal {self.inst}: login {want} refused by the trade "
                  f"server: {line}")
        return (f"terminal {self.inst}: the trade server REFUSED login {want} "
                f"@ {server or '?'} - {line}.  The account number, the MT5 "
                f"password or the server name is wrong (some brokers issue "
                f"an MT5-specific password that differs from the platform "
                f"password, and an unused demo can expire).  Fix it in the "
                f"broker's client area, or log in once inside MT5 and press "
                f"ADOPT.")

    def _do_login(self, login: str, password: str, server: str) -> str:
        login, password, server = validate_credentials(login, password, server)
        sp = _spot()
        exe = sp.TERMINALS[self.inst]["dir"] / "terminal64.exe"
        if not exe.exists():
            raise EngineError(f"terminal {self.inst} is not installed ({exe})")
        # Grade the server BEFORE touching anything.  Booting a server name
        # this install has never seen is not a slow login, it is a NO login:
        # the terminal adopts the login into its config (so the EA header
        # echoes it and the engine used to call that "logged in"), never
        # dials anything, and the operator waits out the whole timeout for an
        # account that was never attempted.  Observed live with
        # CXMDirect-Live.  So: known to THIS install -> go; known only to the
        # sibling -> attempt with a warning; known nowhere and with no access
        # point -> stop now with the exact one-line fix.
        # MT5_STRICT_UNKNOWN_SERVER=1 keeps its old meaning (always refuse).
        # MT5_TRY_UNKNOWN_SERVER=1 forces the old attempt-by-name behaviour.
        ap = config.access_point_for(server)
        new_here_note = ""
        if not sp.server_known(self.inst, server):
            if ap:
                new_here_note = (f" (server {server} is new to this MT5 "
                                 f"install - booting via access point {ap})")
                log.info(f"terminal {self.inst}: booting new server {server} "
                         f"via access point {ap}")
            elif sp.server_known_anywhere(server):
                new_here_note = (f" (server {server} is new to terminal "
                                 f"{self.inst} - attempting anyway)")
                log.info(f"terminal {self.inst}: server {server} known to "
                         f"sibling install only - attempting login anyway")
            elif (os.getenv("MT5_STRICT_UNKNOWN_SERVER", "") == "1"
                  or os.getenv("MT5_TRY_UNKNOWN_SERVER", "") != "1"):
                raise EngineError(
                    f"terminal {self.inst} has never seen trade server "
                    f"{server!r} and has no access point for it - MT5 does "
                    f"not dial a server that is missing from its own list, "
                    f"so this login would be attempted forever without ever "
                    f"connecting (the EA header would still echo "
                    f"{login!r}).  " + self._access_point_hint(server))
            else:
                new_here_note = (f" (server {server} is new to this MT5 "
                                 f"install - attempting the login by name)")
                log.warning(
                    f"terminal {self.inst}: server {server} is not in this "
                    f"install's Bases yet - attempting the login anyway "
                    f"(MT5_TRY_UNKNOWN_SERVER=1 is set)")

        # Fast path: already there.  A repeat click (or a manual UI switch
        # the supervisor already adopted) must NOT bounce a healthy terminal
        # through a full wine reboot - verify in place instead.  Only for a
        # login the broker actually accepted: a mere echoed login number is
        # the phantom state, and that one needs the restart below.
        br = _bridge()
        try:
            cur = br.header_for(self.inst)
        except Exception:
            cur = {}
        cur_login = str(cur.get("login", ""))
        if cur_login == login and self._authorized(login, cur, 0.0):
            accs = session.load()
            accs[self.inst] = {"login": login, "password": password,
                               "server": server}
            session.set_accounts(accs, persist=True)
            try:
                known_accounts.remember(login, password, server)
            except ValueError:
                pass
            if ap:
                config.register_access_point(server, ap)
            self._progress(f"already on {login} - verifying sync...")
            h, matched_in, synced = self._wait_login(login, server=server,
                                                     boot_ts=0.0)
            mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")),
                                         "unknown")
            return (f"already on {login} @ {server} [{mode}] - verified "
                    f"without a restart (matched in {matched_in:.0f}s)"
                    + ("" if synced else " - broker sync pending"))

        # write the slot (the other terminal's slot stays untouched)
        prev = session.load()
        accs = dict(prev)
        accs[self.inst] = {"login": login, "password": password,
                           "server": server}
        session.set_accounts(accs, persist=True)
        try:
            known_accounts.remember(login, password, server)
        except ValueError:
            pass

        # restart the terminal into the fresh credentials, then verify the
        # EA reports the new login.  The credentials travel explicitly into
        # the restart (no session re-read inside the launch), so a session
        # revert racing the restart cannot boot the WRONG account and burn
        # the whole login timeout "live but stuck on the old login".
        from spot import restart_terminal, last_launch_ts
        block = {"login": login, "password": password, "server": server}
        try:
            self._progress(f"restarting terminal {self.inst} into {login}...")
            if not restart_terminal(self.inst, login_block=block):
                raise EngineError(f"terminal {self.inst} could not be restarted "
                                  f"for the login")
            h, matched_in, synced = self._wait_login(
                login, server=server,
                boot_ts=last_launch_ts(self.inst))
        except Exception:
            # roll the session back so the supervisor does not chase a
            # doomed account with repeated heal-restarts into it
            try:
                session.set_accounts(prev, persist=True)
            except Exception:
                pass
            raise
        if ap and synced:
            # Prove-and-remember: this access point just delivered a live
            # account, so later logins to this server resolve without the
            # operator keeping the registry line around.
            learned = config.register_access_point(server, ap)
            if learned:
                log.info(f"terminal {self.inst}: learned access point "
                         f"{server} -> {learned}")
        mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "unknown")
        base = (f"logged into {login} @ {server} "
                f"[{mode}] bal {h.get('balance', '-')} "
                f"{h.get('currency', '')}".rstrip()
                + f" (login matched in {matched_in:.0f}s)"
                + new_here_note)
        if synced:
            return base
        return (base + " - authorized, broker still syncing account details "
                "(no currency yet); balances fill in once MT5 finishes "
                "syncing")

    def _do_adopt(self) -> str:
        br = _bridge()
        h = br.header_for(self.inst)
        login = str(h.get("login", ""))
        if not login or login in ("0", "?"):
            raise EngineError(f"terminal {self.inst} is not logged into any "
                              f"account in MT5 - nothing to adopt")
        if not h.get("currency"):
            # The terminal is ALREADY in the account - only the broker's
            # account details are still in flight, and a trade server this
            # install has never used can take tens of seconds to deliver
            # them.  The old 1.8 s probe declared "not synchronized" and
            # refused to adopt an account that was sitting right there.
            self._progress(f"adopting {login} - waiting for the broker "
                           f"sync...")
            deadline = time.monotonic() + SYNC_TIMEOUT_S
            while time.monotonic() < deadline:
                time.sleep(SYNC_POLL_S)
                h = br.header_for(self.inst)
                cur = str(h.get("login", ""))
                if cur in ("", "0", "?"):
                    raise EngineError(
                        f"terminal {self.inst} left account {login} while "
                        f"adopting (now reports logged out)")
                login = cur
                if h.get("currency"):
                    break
            else:
                h = br.header_for(self.inst)
                login = str(h.get("login", "")) or login
                # No account details after the whole sync window: only adopt
                # if the trade server REALLY authorized this login.  Without
                # this check a terminal that merely echoed a configured login
                # (see _never_attempted_error) could be adopted as a live
                # account, and the whole stack would then trade against a
                # phantom.
                if not self._authorized(login, h, 0.0):
                    raise EngineError(
                        f"terminal {self.inst} is not really logged in: MT5 "
                        f"only echoes {login} from its last config and the "
                        f"trade server never authorized it (no account "
                        f"details after {SYNC_TIMEOUT_S:.0f}s).  "
                        + self._access_point_hint(
                            str(h.get("server", "")) or "?"))
                log.warning(f"terminal {self.inst}: adopting account {login} "
                            f"before the broker delivered account details "
                            f"(no currency after {SYNC_TIMEOUT_S:.0f}s)")
        server = str(h.get("server", "")) or "MetaQuotes-Demo"
        known = known_accounts.get(login)
        password = (known or {}).get("password", "")

        accs = session.load()
        accs[self.inst] = {"login": login, "password": password,
                           "server": server}
        session.set_accounts(accs, persist=True)
        try:
            known_accounts.remember(login, password, server)
        except ValueError:
            pass
        mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "unknown")
        note = "" if password else (" (password unknown - it is remembered "
                                    "for this session; auto-relogin needs "
                                    "a direct --login once)")
        return f"adopted {login} @ {server} [{mode}]{note}"

    def _do_logout(self) -> str:
        sp = _spot()
        login = session.get(self.inst).get("login", "")
        accs = session.load()
        accs.pop(self.inst, None)
        session.set_accounts(accs, persist=True)
        # scrub credentials from both start config and common.ini, then boot
        # logged out so the terminal cannot silently reconnect
        sp.scrub_start_cfg(self.inst)
        sp.scrub_common_ini_login(self.inst)
        from spot import restart_terminal
        # Explicit empty block: boot logged out even if the session is
        # rewritten underneath the restart.
        if not restart_terminal(self.inst, login_block={}):
            raise EngineError(f"terminal {self.inst} could not be restarted "
                              f"after logout (credentials are scrubbed "
                              f"anyway)")
        return f"logged out{f' (was {login})' if login else ''}"

    def _do_restart(self) -> str:
        from spot import restart_terminal, last_launch_ts
        want = session.expected_login(self.inst)
        block = dict(session.get(self.inst)) if want else {}
        self._progress(f"restarting terminal {self.inst}...")
        if not restart_terminal(self.inst, login_block=block):
            raise EngineError(f"terminal {self.inst} could not be restarted")
        want = session.expected_login(self.inst)
        if want:
            h, matched_in, synced = self._wait_login(
                want, boot_ts=last_launch_ts(self.inst))
            mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "unknown")
            base = (f"restarted, logged into {want} [{mode}] "
                    f"(matched in {matched_in:.0f}s)")
            if synced:
                return base
            return base + " - broker sync pending (no currency yet)"
        return "restarted (no session account for this slot)"


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

class AccountEngine:

    def __init__(self):
        self._lock = threading.RLock()
        self._ops: list[Op] = []
        self._active: dict[int, Op | None] = {1: None, 2: None}
        self._workers: dict[int, TerminalWorker] = {}
        self._seq = 0

    # ---- worker management ----

    def start(self) -> None:
        with self._lock:
            for inst in (1, 2):
                w = self._workers.get(inst)
                if w is None or not w.is_alive():
                    if w is not None:
                        # worker died mid-op - do not leave the slot stuck busy
                        self._active[inst] = None
                    w = TerminalWorker(self, inst)
                    w.start()
                    self._workers[inst] = w

    def _enqueue(self, op: Op) -> Op:
        with self._lock:
            self.start()
            cur = self._active.get(op.terminal)
            if cur:
                raise EngineError(f"terminal {op.terminal} is busy running "
                                  f"'{cur.op}' ({cur.state}) - try again in "
                                  f"a moment")
            self._active[op.terminal] = op   # busy from the moment it is queued
            self._seq += 1
            self._ops.insert(0, op)
            del self._ops[MAX_OPS_KEPT:]
        self._workers[op.terminal].q.put(op)
        return op

    # ---- public API (used by app.py and the CLI) ----

    def request_login(self, terminal: int, login: str, password: str,
                      server: str) -> dict:
        if terminal not in (1, 2):
            raise EngineError("terminal must be 1 or 2")
        login = str(login or "").strip()
        server = str(server or "").strip()
        password = str(password or "")
        # convenient quick-switch: if the operator picked a saved account
        # without retyping, reuse the remembered password/server.
        if not password or not server:
            known = known_accounts.get(login) or {}
            if not password:
                password = known.get("password", "")
            if not server:
                server = known.get("server", "")
        validate_credentials(login, password, server)
        return self._enqueue(Op(self._next_id(), "login", terminal,
                                {"login": login,
                                 "password": password,
                                 "server": server})).public()

    def request_adopt(self, terminal: int) -> dict:
        if terminal not in (1, 2):
            raise EngineError("terminal must be 1 or 2")
        return self._enqueue(Op(self._next_id(), "adopt", terminal, {})).public()

    def request_logout(self, terminal: int) -> dict:
        if terminal not in (1, 2):
            raise EngineError("terminal must be 1 or 2")
        return self._enqueue(Op(self._next_id(), "logout", terminal, {})).public()

    def request_restart(self, terminal: int) -> dict:
        if terminal not in (1, 2):
            raise EngineError("terminal must be 1 or 2")
        return self._enqueue(Op(self._next_id(), "restart", terminal, {})).public()

    # ---- status ----

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            cached = getattr(self, "_status_cache", None)
            if cached and now - cached[0] < STATUS_CACHE_TTL_S:
                return cached[1]
        try:
            br = _bridge()
            header_for = br.header_for
        except Exception:
            header_for = lambda inst: {}
        try:
            sp = _spot()
            feed_age_fn = sp.feed_age
        except Exception:
            feed_age_fn = lambda inst: None
        accs = session.load()
        try:
            age1 = feed_age_fn(1)
        except Exception:
            age1 = None
        try:
            age2 = feed_age_fn(2)
        except Exception:
            age2 = None
        ages = {1: age1, 2: age2}
        slots = []
        for inst in (1, 2):
            stored = accs.get(inst, {})
            want = stored.get("login", "")
            try:
                h = header_for(inst)
            except Exception:
                h = {}
            got = str(h.get("login", "")) if h else ""
            if got == "0":
                got = ""
            synced_acct = bool(h and h.get("currency"))
            checked_srv = str(stored.get("server", "")) or ""
            echoed = bool(want and got == want)
            # `verified` means the BROKER accepted this login, not merely that
            # the EA echoed the configured number.  MT5 fills ACCOUNT_LOGIN
            # from the start config even when the trade server is unknown and
            # nothing is ever dialled, so "login matches" alone is precisely
            # the phantom state that made a CXM account look logged in.
            recent_auth = False
            if echoed and not synced_acct and checked_srv:
                try:
                    from spot import last_auth_success as _auth_ok
                    recent_auth = bool(_auth_ok(inst, want,
                                                time.time() - 1800.0))
                except Exception:
                    recent_auth = False
            verified = bool(echoed and (synced_acct or recent_auth))
            syncing = bool(verified and not synced_acct)
            phantom = bool(echoed and not verified)
            # Stalled sync: matched login but the broker hasn't delivered
            # account details for 10+ minutes (unreachable server rather
            # than a slow sync).  Tracked in shared kv so every process
            # agrees; touched only while actually syncing.
            stalled = False
            sync_age_s = None
            skey = f"sync_since_{inst}_{want}" if want else ""
            if skey:
                try:
                    import database as _db
                    if syncing:
                        since = _db.kv_get(skey) or 0
                        try:
                            since = float(since)
                        except (TypeError, ValueError):
                            since = 0.0
                        if since <= 0:
                            since = time.time()
                            _db.kv_set(skey, since)
                        sync_age_s = round(time.time() - since, 1)
                        stalled = sync_age_s > 600.0
                    elif _db.kv_get(skey):
                        _db.kv_set(skey, 0.0)
                except Exception:
                    pass
            # Trade mode is only meaningful once the broker delivered account
            # details: a logged-out terminal reports 0, which would show up as
            # a reassuring [demo] next to an account that isn't connected.
            mode = (TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "")
                    if (h and synced_acct) else "")
            try:
                from spot import server_known as _srv_known
                srv_known = _srv_known(inst, stored.get("server", ""))
            except Exception:
                srv_known = True
            want_ap = config.access_point_for(checked_srv)
            age = ages.get(inst)
            with self._lock:
                active = self._active.get(inst)
                running = (active.public() if active
                           and active.state in ("queued", "running")
                           else None)
            slots.append({
                "terminal": inst,
                "want_login": want,
                "want_server": stored.get("server", ""),
                "login": got,
                "server": h.get("server", "") if h else "",
                "broker": h.get("broker", "") if h else "",
                "mode": mode,                      # demo / contest / live
                "currency": h.get("currency", "") if h else "",
                "balance": h.get("balance", "") if h else "",
                "equity": h.get("equity", "") if h else "",
                "feed_age_s": round(age, 1) if age is not None else None,
                "verified": verified,
                "syncing": syncing,
                "phantom": phantom,
                "stalled": stalled,
                "sync_age_s": sync_age_s,
                "server_known": srv_known,
                "access_point": want_ap,
                # The stored account's trade server cannot be dialled at all:
                # no way to log in until an access point or one manual login.
                "want_unreachable": bool(want and not srv_known
                                         and not want_ap),
                "running": running,
            })
        with self._lock:
            ops = [o.public() for o in self._ops[:15]]
        out = {"ok": True, "ts": _now_iso(), "slots": slots, "ops": ops}
        with self._lock:
            self._status_cache = (now, out)
        return out

    # ---- book helpers ----

    def known(self) -> list[dict]:
        return known_accounts.all_known()

    def forget(self, login: str) -> bool:
        return known_accounts.forget(login)

    def remember(self, login: str, password: str = "", server: str = "",
                 label: str = "") -> dict:
        return known_accounts.remember(login, password, server, label)

    def set_access_point(self, server: str, address: str) -> dict:
        """Register an access point (host:port) for a trade server.

        This is the one missing piece when incorporating a broker whose
        server is not in MT5's own list: the terminal then boots through the
        access point, MT5 learns the server, and every later name login
        works - no manual UI step, no 90 s phantom wait.
        """
        server = str(server or "").strip()
        if not server:
            raise EngineError("server is required (e.g. CXMDirect-Live)")
        norm = config.register_access_point(server, address)
        if not norm:
            raise EngineError(
                f"access point {address!r} is not usable - MT5 needs "
                f"host:port (e.g. mt5.example.com:443)")
        log.info(f"account-engine: access point {server} -> {norm}")
        return {"ok": True, "server": server, "access_point": norm}

    def servers(self) -> dict:
        """What this box knows about reaching each broker."""
        try:
            stored = _spot().known_servers()
        except Exception:
            stored = []
        return {"ok": True, "known_servers": stored,
                "access_points": config.all_access_points()}

    # ---- internals ----

    def _next_id(self) -> str:
        return f"{int(time.time() * 1000) % 100000000:x}-{self._seq:x}"

    def _clear_active(self, inst: int, op: Op) -> None:
        with self._lock:
            if self._active.get(inst) is op:
                self._active[inst] = None

    def log(self, text: str) -> None:
        log.info(f"account-engine: {text}")


_engine: AccountEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> AccountEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = AccountEngine()
            _engine.start()
        return _engine


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _await_op(op: dict) -> dict:
    eng = get_engine()
    while op["state"] in ("queued", "running"):
        time.sleep(0.25)
        for o in eng.status()["ops"]:
            if o["id"] == op["id"]:
                op = o
    return op


def _finish(op: dict) -> int:
    op = _await_op(op)
    print(("OK: " if op["state"] == "done" else "FAILED: ") + op["detail"])
    return 0 if op["state"] == "done" else 1


def _print_status() -> None:
    st = get_engine().status()
    for s in st["slots"]:
        if s["verified"] and not s.get("syncing"):
            state = f"verified in {s['login']}"
        elif s["verified"]:
            state = (f"in {s['login']} - authorized, broker SYNCING "
                     f"(no currency yet, balances pending)")
        elif s.get("phantom"):
            state = (f"NOT logged in - MT5 only echoes {s['login']} from its "
                     f"config; the trade server was never reached")
        elif s["login"]:
            state = f"in {s['login']} (session wants {s['want_login']})"
        else:
            state = "logged out"
        print(f"terminal {s['terminal']}: {state}"
              f"{'  [' + s['mode'] + ']' if s['mode'] else ''}")
        print(f"  server : {s['server'] or '-'}   broker: {s['broker'] or '-'}")
        if s.get("want_login") and not s.get("server_known", True):
            ap = s.get("access_point") or ""
            if ap:
                print(f"  !! server {s.get('want_server') or '?'} is new to "
                      f"this MT5 install - booting via access point {ap}")
            else:
                print(f"  !! server {s.get('want_server') or '?'} is not in "
                      f"this MT5 install's list and has NO access point - "
                      f"logins to it cannot connect. Add one to "
                      f"access_points.env (see --access-point)")
        print(f"  balance: {s['balance'] or '-'}   equity: {s['equity'] or '-'}"
              f"   feed: {s['feed_age_s'] if s['feed_age_s'] is not None else '-'}s")
        if s["running"]:
            print(f"  busy   : {s['running']['op']} (started {s['running']['ts']})")
    if st["ops"]:
        print("\nrecent operations:")
        for o in st["ops"][:8]:
            mark = {"queued": "~", "running": ">", "done": "+"}.get(o["state"], "x")
            print(f"  {mark} {o['ts']}  T{o['terminal']} {o['op']:<7} "
                  f"{o['state']:<7} {o['detail']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="account integration engine - smooth login of any "
                    "account (live or demo) into any terminal")
    ap.add_argument("--status", action="store_true",
                    help="show both terminal slots + recent operations")
    ap.add_argument("--login", nargs="+", metavar=("TERM", "LOGIN"),
                    help="log terminal 1|2 into LOGIN [PASSWORD] [SERVER]; "
                         "omitted pieces are prompted for")
    ap.add_argument("--adopt", type=int, metavar="TERM",
                    help="adopt the account already logged in on the "
                         "terminal (no password needed)")
    ap.add_argument("--logout", type=int, metavar="TERM",
                    help="log terminal out and scrub stored credentials")
    ap.add_argument("--restart", type=int, metavar="TERM",
                    help="restart terminal back into its session account")
    ap.add_argument("--list", action="store_true",
                    help="list known accounts (the quick-switch book)")
    ap.add_argument("--forget", metavar="LOGIN",
                    help="remove an account from the known book")
    ap.add_argument("--access-point", nargs="+", dest="access_point",
                    metavar=("SERVER", "HOST:PORT"),
                    help="remember an access point for a trade server so "
                         "logins to it can connect (e.g. --access-point "
                         "CXMDirect-Live mt5.cxmdirect.com:443)")
    ap.add_argument("--servers", action="store_true",
                    help="list the trade servers MT5 can log into plus every "
                         "access point this box knows")
    args = ap.parse_args()

    eng = get_engine()

    try:
        if args.servers:
            info = eng.servers()
            known = info["known_servers"]
            print(f"trade servers MT5 can log into by name ({len(known)}):")
            for name in known:
                print(f"  {name}")
            points = info["access_points"]
            print(f"\naccess points ({len(points)}):")
            for name, meta in sorted(points.items()):
                print(f"  {name:<36} {meta['address']:<34} [{meta['source']}]")
            return 0

        if args.access_point:
            if len(args.access_point) < 2:
                print("usage: account_engine.py --access-point <SERVER> "
                      "<HOST:PORT>")
                return 1
            try:
                res = eng.set_access_point(args.access_point[0],
                                           args.access_point[1])
            except EngineError as exc:
                print(f"rejected: {exc}")
                return 1
            print(f"OK: {res['server']} -> {res['access_point']}")
            return 0

        if args.list:
            for a in eng.known():
                label = f"  {a['label']}" if a["label"] else ""
                pw = "pw ok" if a["has_password"] else "no password"
                print(f"  {a['login']:<12} {a['server'] or '-':<28} {pw}{label}")
            return 0

        if args.forget:
            print("forgotten" if eng.forget(args.forget) else "not found")
            return 0

        if args.login:
            import getpass
            try:
                term = int(args.login[0])
            except ValueError:
                print("usage: account_engine.py --login <1|2> <LOGIN> "
                      "[PASSWORD] [SERVER]")
                return 1
            login_id = args.login[1] if len(args.login) > 1 else \
                input("login: ").strip()
            password = args.login[2] if len(args.login) > 2 else \
                getpass.getpass("password: ")
            server = args.login[3] if len(args.login) > 3 else \
                (input("server [MetaQuotes-Demo]: ").strip()
                 or "MetaQuotes-Demo")
            try:
                op = eng.request_login(term, login_id, password, server)
            except EngineError as exc:
                print(f"rejected: {exc}")
                return 1
            print(f"terminal {term}: logging into {login_id} @ {server} ...")
            return _finish(op)

        if args.adopt:
            return _finish(eng.request_adopt(args.adopt))
        if args.logout:
            return _finish(eng.request_logout(args.logout))
        if args.restart:
            return _finish(eng.request_restart(args.restart))
    except EngineError as exc:
        print(f"rejected: {exc}")
        return 1

    _print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
