#!/usr/bin/env python3.12
"""database.py - the one store for everything persistent (PostgreSQL + SQLite).

Supports both PostgreSQL (via DATABASE_URL env var) and SQLite (fallback).
Tables:
  * future_trades : trades scheduled from the web panel (pair, lot,
    number of positions, execute time, close time) - executor.py polls
    this table and fires each trade to the second;
  * fired log     : one row per executed schedule slot (id, ticket,
    result) so the panel can show what actually happened;
  * kv            : small JSON blobs (equity-curve history, metrics).

Threading: connections are per-call (context manager) and every
write is wrapped in a transaction, so the Flask threads, the executor
thread and the metrics observer can share the database safely.

Schema is created on first import - no migrations needed for now.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from config import CONFIG, setup_logging

log = setup_logging(__name__)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# PostgreSQL connection pool (if using PostgreSQL)
_pg_pool = None
_pg_lock = threading.Lock()

# SQLite connection (if using SQLite)
# MT5_DB_PATH lets tests (or a second instance) point at a private DB file
_sqlite_path = Path(os.getenv("MT5_DB_PATH", "") or CONFIG.db_path)
_sqlite_lock = threading.Lock()


if USE_POSTGRES:
    try:
        import psycopg2
        from psycopg2 import pool
        from psycopg2.extras import RealDictCursor
        log.info(f"Using PostgreSQL: {DATABASE_URL.split('@')[-1]}")
    except ImportError:
        log.warning("psycopg2 not available, falling back to SQLite")
        USE_POSTGRES = False
        DATABASE_URL = ""

if not USE_POSTGRES:
    log.info(f"Using SQLite: {_sqlite_path}")


# --------------------------------------------------------------------------
# Connection management
# --------------------------------------------------------------------------

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pg_lock:
            if _pg_pool is None:
                _pg_pool = pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    dsn=DATABASE_URL,
                    cursor_factory=RealDictCursor,
                    # fail fast instead of hanging the web thread
                    options="-c statement_timeout=5000 -c lock_timeout=2000",
                )
                log.info("PostgreSQL connection pool created (2-20 conns, 5s stmt timeout)")
    return _pg_pool


@contextmanager
def _pg_conn():
    """Get a PostgreSQL connection from the pool."""
    p = _get_pg_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


_sqlite_conn_obj: sqlite3.Connection | None = None


@contextmanager
def _sqlite_conn():
    """ONE autocommit connection, reused, serialised by _sqlite_lock.

    Every call used to open a fresh connection and re-issue eight pragmas.
    Three of those (journal_mode, page_size, auto_vacuum) persist in the
    file header and were no-ops after the first run; the rest cost a round
    trip each.  More importantly the connect() itself dominated, and the
    scheduler re-lists due schedules up to 200x/s in the two seconds before
    a fire WHILE HOLDING THIS LOCK - so the setup cost sat on the web app's
    critical path as well.

    Reusing one connection is safe here: the lock already serialises every
    caller, and isolation_level=None means each statement is its own
    transaction, so no reader ever pins a WAL snapshot.
    """
    global _sqlite_conn_obj
    with _sqlite_lock:
        conn = _sqlite_conn_obj
        if conn is None:
            conn = sqlite3.connect(_sqlite_path, timeout=10,
                                   isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-32768")     # 32 MB
            _sqlite_conn_obj = conn
        try:
            yield conn
        except Exception:
            # autocommit: only an explicitly opened transaction can be
            # pending, but never leave one behind on a shared connection.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise


@contextmanager
def _conn():
    """Get a database connection (PostgreSQL or SQLite)."""
    if USE_POSTGRES:
        with _pg_conn() as conn:
            yield conn
    else:
        with _sqlite_conn() as conn:
            yield conn


# --------------------------------------------------------------------------
# Schema initialization
# --------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist."""
    if USE_POSTGRES:
        _init_pg()
    else:
        _init_sqlite()
    _migrate()


def _migrate() -> None:
    """Lightweight idempotent migrations (no migration framework yet)."""
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur,
                  "SELECT column_name FROM information_schema.columns "
                  "WHERE table_name='fired' AND column_name='ms'", ())
            if cur.fetchone() is None:
                _exec(cur, "ALTER TABLE fired ADD COLUMN ms REAL", ())
        else:
            cur.execute("PRAGMA table_info(fired)")
            cols = {row[1] for row in cur.fetchall()}
            if "ms" not in cols:
                cur.execute("ALTER TABLE fired ADD COLUMN ms REAL")


def _init_pg() -> None:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS future_trades (
            id           BIGSERIAL PRIMARY KEY,
            account      INTEGER NOT NULL,
            pair         TEXT    NOT NULL,
            side         TEXT    NOT NULL DEFAULT 'BUY',
            lot          REAL    NOT NULL,
            n_positions  INTEGER NOT NULL DEFAULT 1,
            exec_h       INTEGER NOT NULL, exec_m INTEGER NOT NULL,
            exec_s       INTEGER NOT NULL,
            close_h      INTEGER NOT NULL, close_m INTEGER NOT NULL,
            close_s      INTEGER NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active       BOOLEAN NOT NULL DEFAULT TRUE,
            next_fire    TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_future_trades_next_fire
            ON future_trades (next_fire) WHERE active;
        CREATE INDEX IF NOT EXISTS idx_future_trades_account
            ON future_trades (account) WHERE active;
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS fired (
            id           BIGSERIAL PRIMARY KEY,
            schedule_id  BIGINT NOT NULL,
            account      INTEGER NOT NULL,
            pair         TEXT    NOT NULL,
            side         TEXT    NOT NULL,
            lot          REAL    NOT NULL,
            kind         TEXT    NOT NULL,
            ticket       TEXT    DEFAULT '',
            ok           BOOLEAN NOT NULL,
            detail       TEXT    DEFAULT '',
            at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_fired_schedule
            ON fired (schedule_id);
        CREATE INDEX IF NOT EXISTS idx_fired_account
            ON fired (account);
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value JSONB   NOT NULL
        );
        """)


def _init_sqlite() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS future_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account      INTEGER NOT NULL,
            pair         TEXT    NOT NULL,
            side         TEXT    NOT NULL DEFAULT 'BUY',
            lot          REAL    NOT NULL,
            n_positions  INTEGER NOT NULL DEFAULT 1,
            exec_h       INTEGER NOT NULL, exec_m INTEGER NOT NULL,
            exec_s       INTEGER NOT NULL,
            close_h      INTEGER NOT NULL, close_m INTEGER NOT NULL,
            close_s      INTEGER NOT NULL,
            created_at   TEXT    NOT NULL,
            active       INTEGER NOT NULL DEFAULT 1,
            next_fire    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_future_trades_next_fire
            ON future_trades (next_fire) WHERE active=1;
        CREATE INDEX IF NOT EXISTS idx_future_trades_account
            ON future_trades (account) WHERE active=1;

        CREATE TABLE IF NOT EXISTS fired (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id  INTEGER NOT NULL,
            account      INTEGER NOT NULL,
            pair         TEXT    NOT NULL,
            side         TEXT    NOT NULL,
            lot          REAL    NOT NULL,
            kind         TEXT    NOT NULL,
            ticket       TEXT    DEFAULT '',
            ok           INTEGER NOT NULL,
            detail       TEXT    DEFAULT '',
            at           TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fired_schedule ON fired (schedule_id);
        CREATE INDEX IF NOT EXISTS idx_fired_account ON fired (account);

        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)


init_db()


# --------------------------------------------------------------------------
# Helper functions for cross-DB compatibility
# --------------------------------------------------------------------------

def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _exec(cur, query: str, params: tuple) -> Any:
    """Execute query with proper placeholder style.

    (A previous version cached cursors across connections as 'prepared
    statements'; sqlite3 caches statements internally per connection, and
    a cursor from another connection is invalid, so the cache was both
    useless and a latent bug.)"""
    if USE_POSTGRES:
        cur.execute(query, params)
    else:
        q = query.replace("%s", "?")
        cur.execute(q, params)
    return cur


def _fetchone(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def _fetchall(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# future trades
# --------------------------------------------------------------------------

def add_future_trade(account: int, pair: str, side: str, lot: float,
                     n_positions: int, exec_h: int, exec_m: int, exec_s: int,
                     close_h: int, close_m: int, close_s: int) -> int:
    """Insert a schedule; returns its id. next_fire = today (or tomorrow if
    the time already passed) at exec_h:exec_m:exec_s.

    `pair` is stored EXACTLY as given: MT5 symbol names are case-sensitive
    (Deriv's 'Boom 1000 Index' must never become 'BOOM 1000 INDEX' or every
    order dies with 'unknown symbol').  FX pairs arrive uppercase anyway.
    """
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    fire = now.replace(hour=exec_h, minute=exec_m, second=exec_s, microsecond=0)
    if fire <= now:
        fire += dt.timedelta(days=1)
    
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, 
                """INSERT INTO future_trades
                   (account, pair, side, lot, n_positions,
                    exec_h, exec_m, exec_s, close_h, close_m, close_s,
                    created_at, active, next_fire)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
                   RETURNING id""",
                (account, pair, side.upper(), lot, n_positions,
                 exec_h, exec_m, exec_s, close_h, close_m, close_s,
                 now, fire))
            return int(cur.fetchone()["id"])
        else:
            _exec(cur,
                """INSERT INTO future_trades
                   (account, pair, side, lot, n_positions,
                    exec_h, exec_m, exec_s, close_h, close_m, close_s,
                    created_at, active, next_fire)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (account, pair, side.upper(), lot, n_positions,
                 exec_h, exec_m, exec_s, close_h, close_m, close_s,
                 now.isoformat(timespec="seconds"), fire.isoformat(timespec="seconds")))
            return int(cur.lastrowid)


def due_schedules(now: float | None = None) -> list[dict]:
    """Schedules whose next_fire has passed (executor fires these)."""
    import datetime as dt
    now_dt = dt.datetime.fromtimestamp(now, dt.timezone.utc) if now else dt.datetime.now(dt.timezone.utc)
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur,
                "SELECT * FROM future_trades WHERE active AND next_fire <= %s "
                "ORDER BY next_fire",
                (now_dt,))
        else:
            _exec(cur,
                "SELECT * FROM future_trades WHERE active=1 AND next_fire <= ? "
                "ORDER BY next_fire",
                (now_dt.isoformat(timespec="seconds"),))
        return _fetchall(cur)


def reschedule(sid: int) -> None:
    """Move a schedule's next_fire to the next occurrence of its exec time
    STRICTLY AFTER the current next_fire (forward anchor: never same/earlier,
    even when next_fire is already tomorrow-at-exec-time)."""
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT exec_h, exec_m, exec_s, next_fire FROM future_trades WHERE id=%s", (sid,))
        else:
            _exec(cur, "SELECT exec_h, exec_m, exec_s, next_fire FROM future_trades WHERE id=?", (sid,))
        row = _fetchone(cur)
        if not row:
            return
        _update_fire(cur, sid, row["exec_h"], row["exec_m"], row["exec_s"],
                     row["next_fire"])


def reschedule_at(sid: int, when) -> None:
    """Force a schedule's next_fire to an exact UTC moment (used by the
    executor's crash-safe retry: the claimed slot is handed back to 'now'
    so the fire is retried seconds later instead of a day later)."""
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    if getattr(when, "tzinfo", None) is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "UPDATE future_trades SET next_fire=%s WHERE id=%s",
                  (when.isoformat(timespec="seconds"), sid))
        else:
            _exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
                  (when.isoformat(timespec="seconds"), sid))


def claim_schedule(sid: int, expected_fire) -> bool:
    """Atomic compare-and-swap claim: move next_fire to the next exec-time
    occurrence after `expected_fire` IFF the stored next_fire still equals
    `expected_fire`.  Returns True when THIS caller won the claim - only a
    winner may fire, so two scheduler processes can never double-fire."""
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT exec_h, exec_m, exec_s FROM future_trades WHERE id=%s", (sid,))
        else:
            _exec(cur, "SELECT exec_h, exec_m, exec_s FROM future_trades WHERE id=?", (sid,))
        row = _fetchone(cur)
        if not row:
            return False
        nxt = _next_fire_from(row["exec_h"], row["exec_m"], row["exec_s"], expected_fire)
        if USE_POSTGRES:
            _exec(cur,
                "UPDATE future_trades SET next_fire=%s WHERE id=%s AND next_fire=%s",
                (nxt, sid, expected_fire))
        else:
            exp = (expected_fire if isinstance(expected_fire, str)
                   else expected_fire.isoformat(timespec="seconds"))
            _exec(cur,
                "UPDATE future_trades SET next_fire=? WHERE id=? AND next_fire=?",
                (nxt.isoformat(timespec="seconds"), sid, exp))
        return cur.rowcount > 0


def _next_fire_from(exec_h: int, exec_m: int, exec_s: int, anchor) -> dt.datetime:
    """Next occurrence of exec_h:exec_m:exec_s strictly after `anchor`
    (aware UTC datetime or ISO string)."""
    if isinstance(anchor, str):
        try:
            anchor = dt.datetime.fromisoformat(anchor)
        except ValueError:
            anchor = dt.datetime.now(dt.timezone.utc)
    if getattr(anchor, "tzinfo", None) is None:
        anchor = anchor.replace(tzinfo=dt.timezone.utc)
    nxt = anchor.replace(hour=int(exec_h), minute=int(exec_m),
                         second=int(exec_s), microsecond=0)
    if nxt <= anchor:
        nxt += dt.timedelta(days=1)
    return nxt


def _update_fire(cur, sid: int, exec_h: int, exec_m: int, exec_s: int,
                 current_fire) -> None:
    """Unconditional forward-only next_fire update (reschedule helper)."""
    nxt = _next_fire_from(exec_h, exec_m, exec_s, current_fire)
    if USE_POSTGRES:
        _exec(cur, "UPDATE future_trades SET next_fire=%s WHERE id=%s", (nxt, sid))
    else:
        _exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
              (nxt.isoformat(timespec="seconds"), sid))


def deactivate(sid: int) -> None:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "UPDATE future_trades SET active=FALSE WHERE id=%s", (sid,))
        else:
            _exec(cur, "UPDATE future_trades SET active=0 WHERE id=?", (sid,))


def list_future_trades(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM future_trades"
    if active_only:
        if USE_POSTGRES:
            q += " WHERE active"
        else:
            q += " WHERE active=1"
    q += " ORDER BY next_fire"
    with _conn() as c:
        cur = c.cursor()
        _exec(cur, q, ())
        return _fetchall(cur)


def get_schedule(sid: int) -> dict | None:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT * FROM future_trades WHERE id=%s", (sid,))
        else:
            _exec(cur, "SELECT * FROM future_trades WHERE id=?", (sid,))
        return _fetchone(cur)


# --------------------------------------------------------------------------
# fired log
# --------------------------------------------------------------------------

def log_fired(schedule_id: int, account: int, pair: str, side: str,
              lot: float, kind: str, ticket: str, ok: bool, detail: str,
              ms: float | None = None) -> None:
    """Log one fired command.  `ms` = wall milliseconds the whole fire took
    (batch of n opens -> same ms on each of the n rows) - latency auditing"""
    import datetime as dt
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur,
                """INSERT INTO fired (schedule_id, account, pair, side, lot, kind,
                   ticket, ok, detail, ms, at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (schedule_id, account, pair, side, lot, kind, ticket,
                 ok, detail, ms, dt.datetime.now(dt.timezone.utc)))
        else:
            _exec(cur,
                """INSERT INTO fired (schedule_id, account, pair, side, lot, kind,
                   ticket, ok, detail, ms, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (schedule_id, account, pair, side, lot, kind, ticket,
                 1 if ok else 0, detail, ms,
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))


def list_fired(limit: int = 50) -> list[dict]:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT * FROM fired ORDER BY id DESC LIMIT %s", (limit,))
        else:
            _exec(cur, "SELECT * FROM fired ORDER BY id DESC LIMIT ?", (limit,))
        return _fetchall(cur)


# --------------------------------------------------------------------------
# kv blobs (equity curve, metrics)
# --------------------------------------------------------------------------

def kv_set(key: str, value: Any) -> None:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur,
                "INSERT INTO kv (key, value) VALUES (%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, json.dumps(value)))
        else:
            _exec(cur,
                "INSERT INTO kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))


def kv_get(key: str, default=None):
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT value FROM kv WHERE key=%s", (key,))
        else:
            _exec(cur, "SELECT value FROM kv WHERE key=?", (key,))
        r = _fetchone(cur)
        return json.loads(r["value"]) if r else default


if __name__ == "__main__":
    # Quick test
    print("Testing database connection...")
    print(f"Using PostgreSQL: {USE_POSTGRES}")
    print(f"Future trades: {list_future_trades()}")
    print("OK")