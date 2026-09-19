"""session.py tests: obfuscated store round-trip, prompt flow, override hook."""

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch, tmp_path):
    """Point session.json at a tmp file and clear state around each test."""
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "session.json")
    session.clear_override()
    session._MEM = None
    yield
    session.clear_override()
    session._MEM = None


ACC = {1: {"login": "111", "password": "pw1", "server": "S1"},
       2: {"login": "222", "password": "pw2", "server": "S2"}}


def test_roundtrip_obfuscated(tmp_path):
    session.set_accounts(ACC)
    assert session.is_logged_in()
    # file exists, is obfuscated (no plaintext secrets) and locked down
    f = session.SESSION_FILE
    assert f.exists()
    blob = f.read_bytes()
    assert b"pw1" not in blob and b"111" not in blob and b"MT5SESSION" in blob
    assert (os.stat(f).st_mode & 0o777) == 0o600
    # a fresh instance (memory wiped) reads the same accounts back
    session._MEM = None
    assert session.load() == ACC
    assert session.expected_login(2) == "222"


def test_garbage_or_missing_file_yields_empty(tmp_path):
    session.SESSION_FILE.write_text("not a session")
    session._MEM = None
    assert session.load() == {}
    assert not session.is_logged_in()


def test_clear_forgets_everything(tmp_path):
    session.set_accounts(ACC)
    session.clear()
    assert session.load() == {}
    assert not session.SESSION_FILE.exists()


def test_prompt_via_stdin_skips_empty():
    stdin = io.StringIO("777\nsecret\nMyServer\n")       # terminal 1 answers
    stdin2 = io.StringIO("\n\n\n")                        # terminal 2: all defaults -> skipped
    acc1 = session.login(1, stdin=stdin)
    assert acc1 == {"login": "777", "password": "secret", "server": "MyServer"}
    acc2 = session.login(2, stdin=io.StringIO("\n\n\n"))
    assert acc2 == {}                                     # empty login = skip


def test_fresh_prompt_never_keeps_stored(monkeypatch):
    """fresh=True: stored/demo values are never offered and never kept."""
    session.set_accounts(ACC)                            # demo creds stored
    answers = iter(["", "321", "Live-Server"])          # empty login re-asked
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(session.getpass, "getpass", lambda prompt="": "pw3")
    acc = session.login(1, fresh=True)
    assert acc == {"login": "321", "password": "pw3", "server": "Live-Server"}
    assert acc["login"] != "111"                        # demo never kept


def test_prompt_all_persists(monkeypatch, tmp_path):
    answers = iter([io.StringIO("111\npw1\nS1\n"), io.StringIO("222\npw2\nS2\n")])
    monkeypatch.setattr(session, "login",
                        lambda inst, stdin=None: next(answers).readline
                        and session.login.__wrapped__(inst, stdin=stdin)
                        if False else _fake_login(inst))
    # simpler: monkeypatch login directly
    def fake_login(inst, stdin=None, fresh=False):
        return {"login": f"{inst}00", "password": "pw", "server": "S"}
    monkeypatch.setattr(session, "login", fake_login)
    accs = session.prompt_all()
    assert accs[1]["login"] == "100" and accs[2]["login"] == "200"
    session._MEM = None
    assert session.expected_login(1) == "100"             # persisted


def _fake_login(inst):
    return {"login": f"{inst}00", "password": "pw", "server": "S"}


def test_override_hook():
    session.set_override(lambda: {1: dict(ACC[1]), 2: dict(ACC[2])})
    assert session.expected_login(1) == "111"
    assert session.is_logged_in()
    session.clear_override()
    assert session.load() == {}                           # memory untouched by override


def test_set_accounts_drops_empty_entries():
    session.set_accounts({1: ACC[1]})                     # only terminal 1
    assert session.is_logged_in() is False                # needs BOTH
    assert session.expected_login(1) == "111"
    assert session.expected_login(2) == ""


def test_external_session_change_is_picked_up(tmp_path, monkeypatch):
    """Another process rewrites session.json -> next load() in THIS process
    sees the new accounts (stat-validated cache, hot account switching)."""
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / "session.json")
    session.set_accounts({1: dict(ACC[1]), 2: dict(ACC[2])})
    assert session.expected_login(1) == "111"
    # simulate ANOTHER process rewriting the file behind our back
    other = {1: {"login": "999", "password": "pw2", "server": "Other-Demo"},
             2: dict(ACC[2])}
    session.SESSION_FILE.write_text(session._encode(other))
    assert session.expected_login(1) == "999"      # picked up WITHOUT set_accounts
    assert session.expected_login(2) == "222"
