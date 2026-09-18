"""Executor scheduler logic tests (no orders are sent - the EA channel is
monkeypatched out; we verify claim/reschedule/skip semantics only)."""

import datetime as dt

import database as db
import executor
from executor import FutureTradeScheduler


def _mk_row(sid_time: dt.datetime, acc: int = 1, pair: str = "EURUSD") -> dict:
    """Create a real schedule row whose next_fire is in the past; return it."""
    sid = db.add_future_trade(acc, pair, "BUY", 0.01, 1,
                              sid_time.hour, sid_time.minute, sid_time.second,
                              23, 59, 0)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    with db._conn() as c:
        cur = c.cursor()
        db._exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
                 (sid_time.isoformat(timespec="seconds"), sid))
    return db.get_schedule(sid)


def test_fresh_schedule_fires_and_claims(monkeypatch):
    """A due schedule is claimed (next_fire -> tomorrow) and fired once."""
    sch = FutureTradeScheduler()
    fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    s = _mk_row(fire)

    fired = []
    monkeypatch.setattr(FutureTradeScheduler, "_fire",
                        lambda self, s: fired.append(s["id"]))

    sch._process_due(s)
    assert fired == [s["id"]], "a fresh (1s late) schedule must fire"
    # after claim the row is no longer due
    due = [x["id"] for x in db.due_schedules()]
    assert s["id"] not in due
    row = db.get_schedule(s["id"])
    nf = dt.datetime.fromisoformat(row["next_fire"])
    assert nf > dt.datetime.now(dt.timezone.utc), "claim moved next_fire forward"


def test_stale_schedule_reschedules_without_firing(monkeypatch):
    """Missed by > MAX_FIRE_LATE (app was down): reschedule, never fire late."""
    sch = FutureTradeScheduler()
    fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=executor.MAX_FIRE_LATE + 30)
    s = _mk_row(fire)

    fired = []
    monkeypatch.setattr(FutureTradeScheduler, "_fire",
                        lambda self, s: fired.append(s["id"]))

    sch._process_due(s)
    assert fired == [], "a stale schedule must NOT burst-fire late"
    due = [x["id"] for x in db.due_schedules()]
    assert s["id"] not in due, "stale schedule must be rescheduled to tomorrow"


def test_double_due_only_fires_once(monkeypatch):
    """The CAS claim happens before firing, so a second pass sees nothing due
    - even if handed the same (now stale) row dict twice."""
    sch = FutureTradeScheduler()
    fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    s = _mk_row(fire)
    sid = s["id"]

    fired = []
    monkeypatch.setattr(FutureTradeScheduler, "_fire",
                        lambda self, s: fired.append(sid))

    sch._process_due(s)                      # claims + fires
    sch._process_due(s)                      # same stale dict: must NOT fire again
    assert fired.count(sid) == 1, "claim-then-fire must prevent double fires"
    # and a second scheduler holding the fresh row must not fire either
    sch._process_due(db.get_schedule(sid))
    assert fired.count(sid) == 1


def test_claim_race_only_one_winner(monkeypatch):
    """Two schedulers processing the SAME row: exactly one claim wins."""
    sch = FutureTradeScheduler()
    fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    s = _mk_row(fire)

    fired = []
    monkeypatch.setattr(FutureTradeScheduler, "_fire",
                        lambda self, s: fired.append(s["id"]))

    sch._process_due(s)
    # second scheduler re-reads the row (already claimed -> future next_fire)
    row2 = db.get_schedule(s["id"])
    sch._process_due(row2)
    assert fired.count(s["id"]) == 1


def test_nearest_fire_cache(monkeypatch):
    """_nearest_fire_seconds caches for HORIZON_CACHE seconds."""
    sch = FutureTradeScheduler()
    calls = []

    def fake_uncached():
        calls.append(1)
        return 3.0

    monkeypatch.setattr(sch, "_nearest_fire_seconds_uncached", fake_uncached)
    a = sch._nearest_fire_seconds()
    b = sch._nearest_fire_seconds()
    assert a == b == 3.0
    assert len(calls) == 1, "second call within cache window must reuse value"


def test_due_closes():
    sch = FutureTradeScheduler()
    now = dt.datetime.now(dt.timezone.utc)
    sch._closes = {"1:EURUSD": now - dt.timedelta(seconds=2),
                   "2:GBPUSD": now + dt.timedelta(hours=1)}
    due = sch._due_closes()
    assert due == [(1, "EURUSD")]
    assert "1:EURUSD" not in sch._closes
    assert "2:GBPUSD" in sch._closes


def test_register_close_tomorrow_when_past():
    sch = FutureTradeScheduler()
    sch._register_close({"account": 1, "pair": "EURUSD",
                         "close_h": 0, "close_m": 0, "close_s": 1})
    when = sch._closes["1:EURUSD"]
    now = dt.datetime.now(dt.timezone.utc)
    if now.hour == 0 and now.minute == 0 and now.second == 0:
        return  # flaky edge: exactly midnight
    assert when > now, "close time already passed today must roll to tomorrow"


def test_fire_dt_parsing():
    fire = dt.datetime.now(dt.timezone.utc)
    assert FutureTradeScheduler._fire_dt(
        {"next_fire": fire.isoformat(timespec="seconds")}) is not None
    assert FutureTradeScheduler._fire_dt({"next_fire": ""}) is None
    assert FutureTradeScheduler._fire_dt({"next_fire": "garbage"}) is None
