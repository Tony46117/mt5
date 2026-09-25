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

def _load() -> dict:
    try:
        doc = json.loads(BOOK_FILE.read_text())
        if isinstance(doc, dict) and isinstance(doc.get("accounts"), list):
            return doc
    except (OSError, ValueError):
        pass
    return {}

def _save(doc: dict) -> None:
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(BOOK_FILE.parent),
                                        prefix=".known.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, BOOK_FILE)
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

def import_from_acc_env(env_file: Path | None = None) -> int:
    import re
    env_file = env_file or Path(__file__).resolve().parent / "acc.env"
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    cur: dict = {}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"(?i)^account\s*([12])\b\s*(.*)$", s)
        if m:
            if cur.get("login"):
                remember(cur["login"], cur.get("password", ""),
                         cur.get("server", ""))
                n += 1
            cur = {}
            s = m.group(2).strip()
        if not s or s.startswith("#"):
            continue
        for key in ("login", "password", "server"):
            m2 = re.match(rf"(?i)^{key}\s*=\s*(.+)$", s)
            if m2:
                cur[key] = m2.group(1).strip()
    if cur.get("login"):
        remember(cur["login"], cur.get("password", ""), cur.get("server", ""))
        n += 1
    return n
