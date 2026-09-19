"""Database round-trip tests (run on the isolated MT5_DB_PATH SQLite db)."""

import datetime as dt

import database as db


def test_add_and_get_schedule():
    sid = db.add_future_trade(1, "EURUSD", "BUY", 0.01, 2, 10, 30, 0, 23, 59, 0)
    s = db.get_schedule(sid)
    assert s is not None
    assert s["account"] == 1
    assert s["pair"] == "EURUSD"
    assert s["side"] == "BUY"
    assert s["lot"] == 0.01
    assert s["n_positions"] == 2
    assert (s["exec_h"], s["exec_m"], s["exec_s"]) == (10, 30, 0)
    assert (s["close_h"], s["close_m"], s["close_s"]) == (23, 59, 0)
    assert s["active"] in (1, True)
    assert s["next_fire"]


def test_next_fire_is_utc_and_future():
    now = dt.datetime.now(dt.timezone.utc)
    sid = db.add_future_trade(1, "GBPUSD", "SELL", 0.02, 1, 23, 59, 59, 23, 59, 59)
    s = db.get_schedule(sid)
    nf = dt.datetime.fromisoformat(s["next_fire"])
    if nf.tzinfo is None:
        nf = nf.replace(tzinfo=dt.timezone.utc)
    assert nf.tzinfo is not None, "next_fire must be timezone-aware UTC"
    assert nf > now, "a just-created schedule must fire in the future"


def test_reschedule_moves_fire_one_day_ahead():
    sid = db.add_future_trade(2, "EURUSD", "BUY", 0.01, 1, 0, 0, 1, 0, 5, 0)
    before = dt.datetime.fromisoformat(db.get_schedule(sid)["next_fire"])
    db.reschedule(sid)
    after = dt.datetime.fromisoformat(db.get_schedule(sid)["next_fire"])
    if after.tzinfo is None:
        after = after.replace(tzinfo=dt.timezone.utc)
    if before.tzinfo is None:
        before = before.replace(tzinfo=dt.timezone.utc)
    delta = after - before
    assert 0.9 * 86400 <= delta.total_seconds() <= 1.1 * 86400, \
        f"reschedule should move ~1 day ahead, got {delta}"


def test_reschedule_missing_schedule_is_noop():
    db.reschedule(999999)  # must not raise


def test_due_schedules_ordered_and_filtered():
    now = dt.datetime.now(dt.timezone.utc)
    past = (now - dt.timedelta(seconds=5))
    # add one already-past and one future schedule
    sid_past = db.add_future_trade(1, "EURUSD", "BUY", 0.01, 1,
                                   past.hour, past.minute, past.second,
                                   23, 59, 0)
    # force next_fire into the past (add FutureTrade pushes to tomorrow if passed)
    with db._conn() as c:
        cur = c.cursor()
        db._exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
                 (past.isoformat(timespec="seconds"), sid_past))
    sid_future = db.add_future_trade(1, "EURUSD", "BUY", 0.01, 1, 23, 59, 58,
                                     23, 59, 59)
    due = db.due_schedules()
    ids = [s["id"] for s in due]
    assert sid_past in ids
    assert sid_future not in ids
    # ordering: ascending next_fire
    fires = [s["next_fire"] for s in due]
    assert fires == sorted(fires)


def test_deactivate_hides_from_due():
    now = dt.datetime.now(dt.timezone.utc)
    past = now - dt.timedelta(seconds=5)
    sid = db.add_future_trade(1, "XAUUSD", "BUY", 0.01, 1, 1, 1, 1, 1, 2, 0)
    with db._conn() as c:
        cur = c.cursor()
        db._exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
                 (past.isoformat(timespec="seconds"), sid))
    assert sid in [s["id"] for s in db.due_schedules()]
    db.deactivate(sid)
    assert sid not in [s["id"] for s in db.due_schedules()]
    assert db.get_schedule(sid)["active"] in (0, False)


def test_fired_log_roundtrip():
    db.log_fired(42, 1, "EURUSD", "BUY", 0.05, "open", "123456", True, "OK|1.1")
    rows = db.list_fired(5)
    r = rows[0]
    assert r["schedule_id"] == 42
    assert r["kind"] == "open"
    assert r["ok"] in (1, True)
    assert r["ticket"] == "123456"
