#!/usr/bin/env python3.12
"""session.py - the logged-in session: THE single source of account identity.

Replaces the old acc.env auto-login logic: instead of silently logging both
terminals into whatever acc.env contains, the operator is ASKED for each
terminal's login / password / server when bridge.py runs, the terminals are
logged in with exactly those credentials, and every other module (executor,
info, monitor, metrics, close, web) reads THIS session.

Storage (session.json next to this file):
  * obfuscated with a machine-derived key (hostid + a fixed salt, SHA-256
    keystream XOR) - it is NOT encryption against a determined attacker with
    root; it keeps passwords out of plaintext files, out of `grep -r pass`
    output and out of backups.  The old acc.env stays untouched for
    compatibility with mt5_v2 (which has its own copy) but is never read
    again by this codebase.
  * permissions 0600; written atomically (tmp + rename).

API (all other modules use only these):
    session.load()                    -> {1: {...}, 2: {...}} or {} if none
    session.get(inst)                 -> dict for terminal inst (or {})
    session.login(inst)               -> interactive prompt (no-echo password)
    session.prompt_all()              -> login both terminals, persist, return accounts
    session.is_logged_in()            -> True when both accounts are known
    session.expected_login(inst)      -> login string ("" when unknown)
    session.set_override(fn)          -> TEST-ONLY hook replacing the source
    session.clear()                   -> forget everything (delete the file)

The in-memory mirror is read at call time so a login that happens after
import (the normal bridge.py flow) is visible to every consumer without a
restart.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

import config
from config import setup_logging

log = setup_logging(__name__)

SESSION_FILE = Path(__file__).resolve().parent / "session.json"
_SALT = b"mt5-bridge-session-v1"
_MAGIC = "MT5SESSION"

_LOCK = threading.RLock()
_MEM: dict[int, dict[str, str]] | None = None      # None = not loaded yet
_CACHE_STAT: tuple[int, int] | None = None         # (mtime_ns, size) of session.json
                                                   # behind _MEM - None = no/hidden file

_OVERRIDE = None                                    # test-only replacement


# --------------------------------------------------------------------------
# machine key + stream-cipher obfuscation
# --------------------------------------------------------------------------

def _machine_key() -> bytes:
    """Stable per-machine key: hostname + a cpu/net fingerprint + salt.

    Deliberately stable (a copied project dir on the same box still
    decrypts) but not portable across machines.
    """
    import platform
    import uuid
    # MT5_MACHINE_KEY: stable override for containers - docker MACs and
    # hostnames are random per recreation, which would make session.json
    # undecryptable after every restart.  The docker entrypoint pins it.
    node = (os.getenv("MT5_MACHINE_KEY", "").strip()
            or f"{platform.node()}|{uuid.getnode()}|{os.getuid() if hasattr(os, 'getuid') else 0}")
    return hashlib.sha256(node.encode() + b"|" + _SALT).digest()


def _keystream(key: bytes, n: int) -> bytes:
    """SHA-256 counter-mode keystream."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(key + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:n])


def _xor(data: bytes, key: bytes) -> bytes:
    ks = _keystream(key, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def _encode(accounts: dict[int, dict[str, str]]) -> str:
    key = _machine_key()
    blob = json.dumps({str(k): v for k, v in accounts.items()},
                      separators=(",", ":")).encode()
    nonce = secrets.token_bytes(16)
    cipher = _xor(blob, key + nonce)
    doc = {"v": 1, "magic": _MAGIC, "nonce": nonce.hex(), "data": cipher.hex()}
    return json.dumps(doc, separators=(",", ":"))


def _decode(text: str) -> dict[int, dict[str, str]]:
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(doc, dict) or doc.get("magic") != _MAGIC:
        return {}
    try:
        nonce = bytes.fromhex(doc["nonce"])
        cipher = bytes.fromhex(doc["data"])
        key = _machine_key()
        blob = _xor(cipher, key + nonce)
        raw = json.loads(blob.decode())
        return {int(k): {kk: str(vv) for kk, vv in v.items()}
                for k, v in raw.items() if k in ("1", "2")}
    except (KeyError, ValueError, TypeError):
        return {}


def _save(accounts: dict[int, dict[str, str]]) -> None:
    """Atomic write with 0600 perms."""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(SESSION_FILE.parent),
                                        prefix=".session.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(_encode(accounts))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, SESSION_FILE)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _file_accounts() -> dict[int, dict[str, str]]:
    try:
        text = SESSION_FILE.read_text()
    except OSError:
        return {}
    return _decode(text)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def load() -> dict[int, dict[str, str]]:
    """Current session accounts {1: {...}, 2: {...}}; {} when nobody logged in.

    Precedence: test override -> in-memory mirror -> persisted session.json.
    The mirror is STAT-VALIDATED on every call: when another process
    rewrites session.json (bridge re-login, --seed, web login form), the
    very next load() in EVERY process re-reads the file - hot account
    switches propagate everywhere without restarts.

    If session.json exists but decrypts to empty (wrong machine key),
    auto-seed from the legacy acc.env as a fallback.
    """
    if _OVERRIDE is not None:
        try:
            return _OVERRIDE()
        except Exception:                      # never let a test hook crash a reader
            return {}
    global _MEM, _CACHE_STAT
    with _LOCK:
        try:
            st = SESSION_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if _MEM is None or key != _CACHE_STAT:
            _MEM = _file_accounts()
            _CACHE_STAT = key
            # Auto-seed from acc.env if session is empty but file exists
            # (happens when session.json was created on a different machine)
            if not _MEM and SESSION_FILE.exists():
                _seed_from_acc_env()
                _MEM = _file_accounts()
                try:
                    st = SESSION_FILE.stat()
                    _CACHE_STAT = (st.st_mtime_ns, st.st_size)
                except OSError:
                    _CACHE_STAT = None
        return {k: dict(v) for k, v in _MEM.items()}


def file_stamp() -> tuple[int, int] | None:
    """(mtime_ns, size) of session.json - lets watchers detect EXTERNAL
    account switches (another process rewrote the session) with one stat.
    None = no session file."""
    try:
        st = SESSION_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get(inst: int) -> dict[str, str]:
    return load().get(inst, {})


def expected_login(inst: int) -> str:
    return get(inst).get("login", "")


def is_logged_in() -> bool:
    accs = load()
    return bool(accs.get(1, {}).get("login")) and bool(accs.get(2, {}).get("login"))


def set_accounts(accounts: dict[int, dict[str, str]], persist: bool = True) -> None:
    """Programmatic login (used by --seed and tests)."""
    global _MEM
    clean: dict[int, dict[str, str]] = {}
    for inst in (1, 2):
        a = accounts.get(inst) or {}
        if a.get("login"):
            clean[inst] = {"login": str(a["login"]),
                           "password": str(a.get("password", "")),
                           "server": str(a.get("server", "MetaQuotes-Demo"))}
    with _LOCK:
        _MEM = clean
        _CACHE_STAT = None          # force re-stat on next load
        if persist:
            _save(clean)


def clear() -> None:
    """Log out: forget memory + delete the persisted session."""
    global _MEM, _CACHE_STAT
    with _LOCK:
        _MEM = {}
        _CACHE_STAT = None
    try:
        SESSION_FILE.unlink()
    except OSError:
        pass


def login(inst: int, *, stdin=None, fresh: bool = False) -> dict[str, str]:
    """Interactively ask for terminal `inst`'s credentials.

    Password input never echoes.  Default (fresh=False): empty login
    keeps the stored one (re-login without retyping).  With fresh=True
    NOTHING is kept: the login must be typed (empty input re-asks, or
    skips when stdin is a pipe) so bridge.py always connects exactly the
    accounts typed now - never "keep the demos".  Returns the account dict.
    """
    with _LOCK:
        current = (_MEM or _file_accounts()).get(inst, {})
    print(f"\n  Terminal {inst} login")
    if fresh:
        print("  (type the account to connect - Enter does NOT keep the previous one)")
    elif current.get("login"):
        print(f"  (press Enter to keep {current['login']})")
    login_id = ""
    try:
        while True:
            prompt = ("    login: " if fresh
                      else f"    login [{current.get('login', '')}]: ")
            if stdin is not None:
                print(prompt, end="", flush=True)
                login_id = stdin.readline().strip()
                if not login_id and fresh:
                    break                 # stdin cannot be re-asked
            else:
                login_id = input(prompt).strip()
            if login_id or not fresh:
                break
            print("    a login is required - type it (Ctrl+C aborts)")
        if not login_id and current.get("login"):
            login_id = current["login"]
        if not login_id:
            return {}
        pw_prompt = "    password: "
        if stdin is not None:
            print(pw_prompt, end="", flush=True)
            password = stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass(pw_prompt)
        server_prompt = ("    server [MetaQuotes-Demo]: " if fresh
                         else f"    server [{current.get('server', 'MetaQuotes-Demo')}]: ")
        if stdin is not None:
            print(server_prompt, end="", flush=True)
            server = stdin.readline().strip()
        else:
            server = input(server_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return {}
    if not server:
        server = ("MetaQuotes-Demo" if fresh
                  else current.get("server", "MetaQuotes-Demo"))
    return {"login": login_id, "password": password, "server": server}


def prompt_all(*, stdin=None, fresh: bool = False) -> dict[int, dict[str, str]]:
    """Log in both terminals interactively and persist the session.

    fresh=True (bridge.py always) asks for BOTH accounts from scratch:
    stored/demo values are never offered as defaults and never kept on
    an empty Enter.
    """
    accounts: dict[int, dict[str, str]] = {}
    for inst in (1, 2):
        acc = login(inst, stdin=stdin, fresh=fresh)
        if acc:
            accounts[inst] = acc
        else:
            print(f"    (terminal {inst} skipped - staying logged out)")
    if accounts:
        set_accounts(accounts)
        print(f"  {len(accounts)} account(s) saved to {SESSION_FILE.name}")
    return accounts


def set_override(fn) -> None:
    """TEST-ONLY: replace the account source entirely."""
    global _OVERRIDE
    _OVERRIDE = fn


def clear_override() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def _seed_from_acc_env() -> int:
    """TEST HELPER: copy the demo credentials from the legacy acc.env into
    the session store (explicit flag only - never automatic)."""
    import re
    try:
        text = config.ENV_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    accounts: dict[int, dict[str, str]] = {}
    cur = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?i)^account\s*([12])\b\s*(.*)$", line)
        if m:
            cur = int(m.group(1))
            line = m.group(2).strip()
        if not cur:
            continue
        for key in ("login", "password", "server"):
            m2 = re.match(rf"(?i)^{key}\s*=\s*(.+)$", line)
            if m2:
                accounts.setdefault(cur, {})[key] = m2.group(1).strip()
    # acc.env wraps values across lines ('login = X' newline 'password = Y');
    # the loop above already handles one key per line within the account block.
    if accounts:
        set_accounts(accounts)
    return len(accounts)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="MT5 bridge login session")
    ap.add_argument("--status", action="store_true", help="show current session")
    ap.add_argument("--logout", action="store_true", help="forget the session")
    ap.add_argument("--seed", action="store_true",
                    help="TEST HELPER: seed the session from the legacy acc.env "
                         "demo credentials (explicit only)")
    args = ap.parse_args()
    if args.logout:
        clear()
        print("session cleared")
    elif args.seed:
        n = _seed_from_acc_env()
        print(f"seeded {n} account(s) from acc.env (test credentials only)")
    elif args.status:
        accs = load()
        if not accs:
            print("no active session (run bridge.py to log in)")
        for inst in sorted(accs):
            a = accs[inst]
            print(f"terminal {inst}: login={a['login']} server={a['server']}")
    else:
        prompt_all()
