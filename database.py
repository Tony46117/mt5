#!/usr/bin/env python3.12

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

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

_pg_pool = None
_pg_lock = threading.Lock()

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
                    options="-c statement_timeout=5000 -c lock_timeout=2000",
                )
                log.info("PostgreSQL connection pool created (2-20 conns, 5s stmt timeout)")
    return _pg_pool

@contextmanager
def _pg_conn():
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
            conn.execute("PRAGMA cache_size=-32768")
            _sqlite_conn_obj = conn
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

@contextmanager
def _conn():
    if USE_POSTGRES:
        with _pg_conn() as conn:
            yield conn
    else:
        with _sqlite_conn() as conn:
            yield conn

def init_db() -> None:
    if USE_POSTGRES:
        _init_pg()
    else:
        _init_sqlite()
    _migrate()

def _migrate() -> None:
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

        ph = "%s" if USE_POSTGRES else "?"
        act = "TRUE" if USE_POSTGRES else "1"
        grp = ("account, pair, side, lot, n_positions, "
               "exec_h, exec_m, exec_s, close_h, close_m, close_s")
        cur.execute(f"""UPDATE future_trades SET active={act if USE_POSTGRES else '0'}
                     WHERE active={act} AND id NOT IN
                     (SELECT MIN(id) FROM future_trades WHERE active={act}
                      GROUP BY {grp})""")
        deduped = cur.rowcount
        where = "WHERE active" if USE_POSTGRES else "WHERE active=1"
        cur.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS uq_future_trades_active
                     ON future_trades ({grp}) {where}""")
        if deduped and deduped > 0:
            log.warning(f"schedule dedupe: demoted {deduped} clone row(s) - "
                        f"identical active schedules are no longer allowed")

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

def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def _exec(cur, query: str, params: tuple) -> Any:
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

class DuplicateScheduleError(Exception):
    pass

def _local_to_utc(dt_obj: dt.datetime) -> dt.datetime:
    return dt_obj.astimezone(dt.timezone.utc)

def add_future_trade(account: int, pair: str, side: str, lot: float,
                     n_positions: int, exec_h: int, exec_m: int, exec_s: int,
                     close_h: int, close_m: int, close_s: int) -> int:
    import datetime as dt
    now_local = dt.datetime.now().astimezone()
    fire = _local_to_utc(now_local.replace(hour=exec_h, minute=exec_m,
                                           second=exec_s, microsecond=0))
    now = dt.datetime.now(dt.timezone.utc)
    if fire <= now:
        fire += dt.timedelta(days=1)

    dup = find_duplicate(account, pair, side, lot, n_positions,
                         exec_h, exec_m, exec_s, close_h, close_m, close_s)
    if dup:
        raise DuplicateScheduleError(
            f"schedule #{dup['id']} already does this - acc{account} {pair} "
            f"{side.upper()} {lot} x{n_positions} at {exec_h:02d}:{exec_m:02d}:"
            f"{exec_s:02d} (edit or PAUSE that one instead of creating a clone)")

    try:
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
                     now.isoformat(timespec="seconds"),
                     fire.isoformat(timespec="seconds")))
                return int(cur.lastrowid)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateScheduleError(
                "an identical active schedule already exists - none created") from exc
        raise

def _is_unique_violation(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return "duplicate key" in str(exc) or "UNIQUE constraint" in str(exc)

def find_duplicate(account: int, pair: str, side: str, lot: float,
                   n_positions: int, exec_h: int, exec_m: int, exec_s: int,
                   close_h: int, close_m: int, close_s: int,
                   exclude_id: int | None = None) -> dict | None:
    ph = "%s" if USE_POSTGRES else "?"
    act = "TRUE" if USE_POSTGRES else "1"
    q = (f"SELECT * FROM future_trades WHERE active={act} "
         f"AND account={ph} AND pair={ph} AND side={ph} AND lot={ph} "
         f"AND n_positions={ph} AND exec_h={ph} AND exec_m={ph} "
         f"AND exec_s={ph} AND close_h={ph} AND close_m={ph} AND close_s={ph}")
    params: list = [account, pair, side.upper(), lot, n_positions,
                    exec_h, exec_m, exec_s, close_h, close_m, close_s]
    if exclude_id is not None:
        q += f" AND id<>{ph}"
        params.append(exclude_id)
    q += " ORDER BY id LIMIT 1"
    with _conn() as c:
        cur = c.cursor()
        _exec(cur, q, tuple(params))
        return _fetchone(cur)

def due_schedules(now: float | None = None) -> list[dict]:
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
    if isinstance(anchor, str):
        try:
            anchor = dt.datetime.fromisoformat(anchor)
        except ValueError:
            anchor = dt.datetime.now(dt.timezone.utc)
    if getattr(anchor, "tzinfo", None) is None:
        anchor = anchor.replace(tzinfo=dt.timezone.utc)
    anchor_local = anchor.astimezone()
    nxt_local = anchor_local.replace(hour=int(exec_h), minute=int(exec_m),
                                     second=int(exec_s), microsecond=0)
    if nxt_local <= anchor_local:
        nxt_local += dt.timedelta(days=1)
    return _local_to_utc(nxt_local)

def _update_fire(cur, sid: int, exec_h: int, exec_m: int, exec_s: int,
                 current_fire) -> None:
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

def update_schedule(sid: int, *, pair: str | None = None, side: str | None = None,
                    lot: float | None = None, n_positions: int | None = None,
                    exec_h: int | None = None, exec_m: int | None = None,
                    exec_s: int | None = None, close_h: int | None = None,
                    close_m: int | None = None, close_s: int | None = None,
                    active: bool | None = None) -> bool:
    cur_sch = get_schedule(sid)
    if not cur_sch:
        return False
    p = pair if pair is not None else cur_sch["pair"]
    s = (side if side is not None else cur_sch["side"]).upper()
    l = lot if lot is not None else cur_sch["lot"]
    n = n_positions if n_positions is not None else cur_sch["n_positions"]
    eh = exec_h if exec_h is not None else cur_sch["exec_h"]
    em = exec_m if exec_m is not None else cur_sch["exec_m"]
    es = exec_s if exec_s is not None else cur_sch["exec_s"]
    ch = close_h if close_h is not None else cur_sch["close_h"]
    cm = close_m if close_m is not None else cur_sch["close_m"]
    cs = close_s if close_s is not None else cur_sch["close_s"]
    act = (1 if active else 0) if active is not None else cur_sch["active"]
    act = 1 if act in (1, True) else 0
    now = dt.datetime.now(dt.timezone.utc)

    if act:
        dup = find_duplicate(account=cur_sch["account"], pair=p, side=s,
                             lot=l, n_positions=n, exec_h=eh, exec_m=em,
                             exec_s=es, close_h=ch, close_m=cm, close_s=cs,
                             exclude_id=sid)
        if dup:
            raise DuplicateScheduleError(
                f"schedule #{dup['id']} already does this - acc"
                f"{cur_sch['account']} {p} {s} {l} x{n} at "
                f"{eh:02d}:{em:02d}:{es:02d} (edit or PAUSE that one instead)")

    same_exec = (int(eh) == int(cur_sch["exec_h"])
                 and int(em) == int(cur_sch["exec_m"])
                 and int(es) == int(cur_sch["exec_s"]))
    cur_fire = cur_sch.get("next_fire")
    if isinstance(cur_fire, str):
        try:
            cur_fire = dt.datetime.fromisoformat(cur_fire)
        except ValueError:
            cur_fire = None
    if cur_fire is not None and getattr(cur_fire, "tzinfo", None) is None:
        cur_fire = cur_fire.replace(tzinfo=dt.timezone.utc)
    if same_exec and cur_fire is not None and cur_fire > now:
        fire = cur_fire
    else:
        now_local = dt.datetime.now().astimezone()
        fire = _local_to_utc(now_local.replace(hour=int(eh), minute=int(em),
                                               second=int(es), microsecond=0))
        if fire <= now:
            fire += dt.timedelta(days=1)
    try:
        with _conn() as c:
            cur = c.cursor()
            if USE_POSTGRES:
                _exec(cur, """UPDATE future_trades SET pair=%s, side=%s, lot=%s,
                            n_positions=%s, exec_h=%s, exec_m=%s, exec_s=%s,
                            close_h=%s, close_m=%s, close_s=%s, active=%s,
                            next_fire=%s WHERE id=%s""",
                      (p, s, l, n, eh, em, es, ch, cm, cs, bool(act), fire, sid))
            else:
                _exec(cur, """UPDATE future_trades SET pair=?, side=?, lot=?,
                            n_positions=?, exec_h=?, exec_m=?, exec_s=?,
                            close_h=?, close_m=?, close_s=?, active=?,
                            next_fire=? WHERE id=?""",
                      (p, s, l, n, eh, em, es, ch, cm, cs, act,
                       fire.isoformat(timespec="seconds"), sid))
            return cur.rowcount > 0
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateScheduleError(
                "another active schedule already does this - none changed") from exc
        raise

def delete_schedule(sid: int) -> bool:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "DELETE FROM future_trades WHERE id=%s", (sid,))
        else:
            _exec(cur, "DELETE FROM future_trades WHERE id=?", (sid,))
        return cur.rowcount > 0

def clear_history() -> int:
    deleted = 0
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "DELETE FROM fired", ())
            deleted += cur.rowcount
            _exec(cur, "DELETE FROM future_trades WHERE active = FALSE", ())
            deleted += max(0, cur.rowcount)
        else:
            _exec(cur, "DELETE FROM fired", ())
            deleted += max(0, cur.rowcount)
            _exec(cur, "DELETE FROM future_trades WHERE active = 0", ())
            deleted += max(0, cur.rowcount)
    return deleted

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

def log_fired(schedule_id: int, account: int, pair: str, side: str,
              lot: float, kind: str, ticket: str, ok: bool, detail: str,
              ms: float | None = None) -> None:
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

def fired_since(iso_utc: str) -> list[dict]:
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "SELECT * FROM fired WHERE at >= %s ORDER BY id ASC",
                  (iso_utc,))
        else:
            _exec(cur, "SELECT * FROM fired WHERE at >= ? ORDER BY id ASC",
                  (iso_utc,))
        return _fetchall(cur)

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

def prune_fired(hours: int = 24) -> int:
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur, "DELETE FROM fired WHERE at < %s", (cutoff,))
        else:
            _exec(cur, "DELETE FROM fired WHERE at < ?", (cutoff,))
        return cur.rowcount

def prune_inactive_schedules(days: int = 1) -> int:
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.cursor()
        if USE_POSTGRES:
            _exec(cur,
                  "DELETE FROM future_trades WHERE active = FALSE AND created_at < %s",
                  (cutoff,))
        else:
            _exec(cur,
                  "DELETE FROM future_trades WHERE active = 0 AND created_at < ?",
                  (cutoff,))
        return cur.rowcount

if __name__ == "__main__":
    print("Testing database connection...")
    print(f"Using PostgreSQL: {USE_POSTGRES}")
    print(f"Future trades: {list_future_trades()}")
    print("OK")