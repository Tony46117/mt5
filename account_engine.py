#!/usr/bin/env python3.12
"""Account Integration Engine.

Smooth, concurrent login of any MT5 account (live or demo) into any
terminal, built on top of the existing session / spot / bridge plumbing
(no duplicated state):

  * one worker thread per terminal - logins never fight each other and
    never block the web panel or the executor
  * every operation is validated BEFORE a terminal is touched
  * every operation is verified AFTER it runs (EA-reported login must
    match what was requested)
  * adopts an account already logged in on the terminal (password pulled
    from the known-accounts book when available)
  * demo/real detection from the EA header (ACCOUNT_TRADE_MODE)
  * an operation registry so the web panel and CLI can show live status

Deliberately NOT touched: executor.py, the scheduler, database schema,
and bridge.py's supervisor loops.  This engine cooperates with the
supervisor by writing through session.set_accounts() exactly like the
existing adoption path does.
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
import datetime as dt

from config import setup_logging
import session
import accounts as known_accounts

log = setup_logging(__name__)

WAIT_LOGIN_TIMEOUT_S = 90.0
POLL_S = 0.5
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
        return {"id": self.id, "op": self.op, "terminal": self.terminal,
                "args": self.args, "state": self.state, "detail": self.detail,
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

    def run(self) -> None:
        while True:
            op = self.q.get()
            try:
                op.state = "running"   # busy was already set at enqueue time
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
                self.engine._clear_active(self.inst, op)

    # ---- individual operations ----

    def _execute(self, op: Op) -> str:
        return getattr(self, f"_do_{op.op}")(**op.args)

    def _wait_login(self, want: str, timeout: float = WAIT_LOGIN_TIMEOUT_S) -> dict:
        """Wait until the EA feed is live and reports `want` as its login."""
        br = _bridge()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            h = br.header_for(self.inst)
            got = str(h.get("login", ""))
            if got and got == want and h.get("currency"):
                return h
            time.sleep(POLL_S)
        h = br.header_for(self.inst)
        got = str(h.get("login", ""))
        raise EngineError(f"login not verified within {timeout:.0f}s "
                          f"(terminal reports {got or 'logged out'}, "
                          f"wanted {want})")

    def _do_login(self, login: str, password: str, server: str) -> str:
        login, password, server = validate_credentials(login, password, server)
        sp = _spot()
        exe = sp.TERMINALS[self.inst]["dir"] / "terminal64.exe"
        if not exe.exists():
            raise EngineError(f"terminal {self.inst} is not installed ({exe})")

        # write the slot (the other terminal's slot stays untouched)
        accs = session.load()
        accs[self.inst] = {"login": login, "password": password,
                           "server": server}
        session.set_accounts(accs, persist=True)
        try:
            known_accounts.remember(login, password, server)
        except ValueError:
            pass

        # restart the terminal into the fresh credentials, then verify the
        # EA reports the new login
        from spot import restart_terminal
        if not restart_terminal(self.inst):
            raise EngineError(f"terminal {self.inst} could not be restarted "
                              f"for the login")

        h = self._wait_login(login)
        mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "unknown")
        return (f"logged into {login} @ {server} "
                f"[{mode}] bal {h.get('balance', '-')} "
                f"{h.get('currency', '')}".rstrip())

    def _do_adopt(self) -> str:
        br = _bridge()
        h = br.header_for(self.inst)
        login = str(h.get("login", ""))
        if not login or login in ("0", "?"):
            raise EngineError(f"terminal {self.inst} is not logged into any "
                              f"account in MT5 - nothing to adopt")
        if not h.get("currency"):
            raise EngineError(f"terminal {self.inst} account {login} has not "
                              f"synchronized yet - try again shortly")
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
        if not restart_terminal(self.inst):
            raise EngineError(f"terminal {self.inst} could not be restarted "
                              f"after logout (credentials are scrubbed "
                              f"anyway)")
        return f"logged out{f' (was {login})' if login else ''}"

    def _do_restart(self) -> str:
        from spot import restart_terminal
        if not restart_terminal(self.inst):
            raise EngineError(f"terminal {self.inst} could not be restarted")
        want = session.expected_login(self.inst)
        if want:
            h = self._wait_login(want)
            mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "unknown")
            return f"restarted, logged into {want} [{mode}]"
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
        validate_credentials(login, password, server)
        return self._enqueue(Op(self._next_id(), "login", terminal,
                                {"login": str(login).strip(),
                                 "password": str(password),
                                 "server": str(server).strip()})).public()

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
        # degrade gracefully: no Wine/bridge must not break the status feed
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
            mode = TRADE_MODE_LABELS.get(str(h.get("trade_mode", "")), "") if h else ""
            try:
                age = feed_age_fn(inst)
            except Exception:
                age = None
            with self._lock:
                active = self._active.get(inst)
                running = (active.public() if active
                           and active.state in ("queued", "running")
                           else None)
            slots.append({
                "terminal": inst,
                "want_login": want,
                "login": got,
                "server": h.get("server", "") if h else "",
                "broker": h.get("broker", "") if h else "",
                "mode": mode,                      # demo / contest / live
                "currency": h.get("currency", "") if h else "",
                "balance": h.get("balance", "") if h else "",
                "equity": h.get("equity", "") if h else "",
                "feed_age_s": round(age, 1) if age is not None else None,
                "verified": bool(want and got == want),
                "running": running,
            })
        with self._lock:
            ops = [o.public() for o in self._ops[:15]]
        return {"ok": True, "ts": _now_iso(), "slots": slots, "ops": ops}

    # ---- book helpers ----

    def known(self) -> list[dict]:
        return known_accounts.all_known()

    def forget(self, login: str) -> bool:
        return known_accounts.forget(login)

    def remember(self, login: str, password: str = "", server: str = "",
                 label: str = "") -> dict:
        return known_accounts.remember(login, password, server, label)

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
        time.sleep(0.4)
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
        if s["verified"]:
            state = f"verified in {s['login']}"
        elif s["login"]:
            state = f"in {s['login']} (session wants {s['want_login']})"
        else:
            state = "logged out"
        print(f"terminal {s['terminal']}: {state}"
              f"{'  [' + s['mode'] + ']' if s['mode'] else ''}")
        print(f"  server : {s['server'] or '-'}   broker: {s['broker'] or '-'}")
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
    args = ap.parse_args()

    eng = get_engine()

    try:
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
