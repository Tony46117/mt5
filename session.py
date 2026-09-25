#!/usr/bin/env python3.12

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

_env_session = os.environ.get("MT5_SESSION_FILE")
if _env_session:
    SESSION_FILE = Path(_env_session)
_SALT = b"mt5-bridge-session-v1"
_MAGIC = "MT5SESSION"

_LOCK = threading.RLock()
_MEM: dict[int, dict[str, str]] | None = None
_CACHE_STAT: tuple[int, int] | None = None

_OVERRIDE = None

_INVALID_LOGINS = frozenset({"0", "?", "LOGIN"})

def _valid_login(login: str) -> bool:
    return bool(login) and login not in _INVALID_LOGINS and login.isdigit()

def _machine_key() -> bytes:
    import platform
    import uuid
    node = (os.getenv("MT5_MACHINE_KEY", "").strip()
            or f"{platform.node()}|{uuid.getnode()}|{os.getuid() if hasattr(os, 'getuid') else 0}")
    return hashlib.sha256(node.encode() + b"|" + _SALT).digest()

def _keystream(key: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(key + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:n])

def _xor(data: bytes, key: bytes) -> bytes:
    ks = _keystream(key, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))

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

def load() -> dict[int, dict[str, str]]:
    if _OVERRIDE is not None:
        try:
            return _OVERRIDE()
        except Exception:
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
    global _MEM, _CACHE_STAT
    clean: dict[int, dict[str, str]] = {}
    for inst in (1, 2):
        a = accounts.get(inst) or {}
        lg = str(a.get("login", "")).strip()
        if not _valid_login(lg):
            if a.get("login"):
                log.warning(f"session: refusing to store invalid login "
                            f"{a.get('login')!r} for terminal {inst}")
            continue
        clean[inst] = {"login": lg,
                       "password": str(a.get("password", "")),
                       "server": str(a.get("server", "MetaQuotes-Demo"))}
    with _LOCK:
        _MEM = clean
        _CACHE_STAT = None
        if persist:
            _save(clean)

def clear() -> None:
    global _MEM, _CACHE_STAT
    with _LOCK:
        _MEM = {}
        _CACHE_STAT = None
    try:
        SESSION_FILE.unlink()
    except OSError:
        pass

def login(inst: int, *, stdin=None, fresh: bool = False) -> dict[str, str]:
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
                    break
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
    global _OVERRIDE
    _OVERRIDE = fn

def clear_override() -> None:
    global _OVERRIDE
    _OVERRIDE = None

def _seed_from_acc_env() -> int:
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
