#!/usr/bin/env python3.12

from __future__ import annotations

import json
import os
import tempfile
import threading
import datetime as dt
from pathlib import Path

from config import setup_logging

log = setup_logging(__name__)

BOOK_FILE = Path(__file__).resolve().parent / "known_accounts.json"

_LOCK = threading.RLock()

_INVALID_LOGINS = frozenset({"0", "?", "LOGIN"})

def _valid_login(login: str) -> bool:
    return bool(login) and login not in _INVALID_LOGINS and login.isdigit()

_BOOK_CACHE: tuple[float, tuple[int, int] | None, dict] | None = None
_BOOK_TTL_S = 5.0

def _load() -> dict:
    import time
    global _BOOK_CACHE
    now = time.monotonic()
    try:
        st = BOOK_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if _BOOK_CACHE and now - _BOOK_CACHE[0] < _BOOK_TTL_S and _BOOK_CACHE[1] == key:
        return _BOOK_CACHE[2]
    try:
        doc = json.loads(BOOK_FILE.read_text())
        if isinstance(doc, dict) and isinstance(doc.get("accounts"), list):
            _BOOK_CACHE = (now, key, doc)
            return doc
    except (OSError, ValueError):
        pass
    doc = {}
    _BOOK_CACHE = (now, key, doc)
    return doc

def _save(doc: dict) -> None:
    global _BOOK_CACHE
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(BOOK_FILE.parent),
                                        prefix=".known.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, BOOK_FILE)
        _BOOK_CACHE = None
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

def remember(login: str, password: str = "", server: str = "",
             label: str = "") -> dict:
    login = str(login).strip()
    if not _valid_login(login):
        raise ValueError(f"invalid login {login!r} - not a plausible account")
    with _LOCK:
        doc = _load()
        accs = doc.get("accounts", [])
        accs = [a for a in accs
                if not (a.get("login") in _INVALID_LOGINS and login != a.get("login"))]
        for a in accs:
            if a.get("login") == login:
                if password:
                    a["password"] = password
                if server:
                    a["server"] = server
                if label:
                    a["label"] = label
                _save(doc)
                return dict(a)
        entry = {"login": login,
                 "password": str(password or ""),
                 "server": str(server or "MetaQuotes-Demo"),
                 "label": str(label or ""),
                 "added": dt.datetime.now().isoformat(timespec="seconds")}
        accs.append(entry)
        doc["accounts"] = accs
        _save(doc)
        return entry

def forget(login: str) -> bool:
    with _LOCK:
        doc = _load()
        accs = doc.get("accounts", [])
        keep = [a for a in accs if a.get("login") != str(login).strip()]
        if len(keep) == len(accs):
            return False
        doc["accounts"] = keep
        _save(doc)
        return True

def get(login: str) -> dict | None:
    login = str(login).strip()
    with _LOCK:
        for a in _load().get("accounts", []):
            if a.get("login") == login:
                return dict(a)
    return None

def has_credentials(login: str) -> bool:
    a = get(login)
    return bool(a and a.get("password"))

def all_known() -> list[dict]:
    with _LOCK:
        accs = _load().get("accounts", [])
    out = []
    for a in sorted(accs, key=lambda x: x.get("added", "")):
        if not _valid_login(a.get("login", "")):
            continue
        out.append({"login": a.get("login", ""),
                    "server": a.get("server", ""),
                    "label": a.get("label", ""),
                    "has_password": bool(a.get("password"))})
    return out
