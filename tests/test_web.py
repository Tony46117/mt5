"""Web layer tests: pages render, theme is black/blue/white (no green),
APIs respond and scheduling round-trips through the HTTP interface."""

import datetime as dt

import pytest

import app as appmod
import database as db
import front


@pytest.fixture()
def client():
    return appmod.app.test_client()


# --------------------------------------------------------------------------
# theme
# --------------------------------------------------------------------------

def test_theme_no_green():
    css = front.CSS
    for bad in ("lime", "--green", "#22c55e", "#a3e635", "#84cc16", "#15803d"):
        assert bad not in css, f"green remnant '{bad}' in CSS"
    assert "--blue:" in css and "#4da6ff" in css, "blue accents present"
    assert "--bg:#04060a" in css, "near-black background"


def test_theme_js_has_no_lime_buttons():
    assert "lime" not in front.PANEL_JS, "lime button classes must be gone"


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

def test_dashboard_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "kpiGrid" in html and "acc1" in html
    assert "lime" not in html


def test_panel_page_renders(client):
    r = client.get("/panel")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "sysPill" in html and "modalBg" in html
    assert "lime" not in html


# --------------------------------------------------------------------------
# apis (no terminals running - they must degrade gracefully, not 500)
# --------------------------------------------------------------------------

def test_api_dashboard_ok(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert "accounts" in j and "system" in j


def test_api_panel_ok(client):
    r = client.get("/api/panel")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_api_schedules_and_fired(client):
    for path in ("/api/schedules", "/api/fired"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


def test_health_never_500(client):
    r = client.get("/health")
    assert r.status_code in (200, 503)


# --------------------------------------------------------------------------
# schedule lifecycle through the API
# --------------------------------------------------------------------------

def test_schedule_create_and_delete(client):
    r = client.post("/api/schedule", json={
        "account": 1, "pair": "EURUSD", "lot": 0.01, "n": 1,
        "exec": [23, 59, 55], "close": [23, 59, 58]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["id"] > 0
    sid = j["id"]

    scheds = db.list_future_trades()
    assert any(s["id"] == sid for s in scheds)

    r = client.post("/api/schedule/delete", json={"id": sid})
    assert r.status_code == 200
    assert db.get_schedule(sid)["active"] in (0, False)


def test_schedule_validation(client):
    for bad in (
        {"account": 3},                          # bad account
        {"account": 1, "lot": 0},                # bad lot
        {"account": 1, "lot": 0.01, "n": 99},    # bad n
        {"account": 1, "lot": 0.01, "exec": [25, 0, 0]},   # hour range
        {"account": 1, "lot": 0.01, "exec": [0, 0, 0], "close": [0, 61, 0]},
    ):
        payload = {"account": 1, "pair": "EURUSD", "lot": 0.01, "n": 1,
                   "exec": [10, 0, 0], "close": [11, 0, 0]}
        payload.update(bad)
        r = client.post("/api/schedule", json=payload)
        assert r.status_code == 400, f"{bad} must be rejected"


def test_full_fire_cycle_via_api(client, monkeypatch):
    """schedule -> due -> fired (EA monkeypatched) -> rescheduled."""
    import executor
    from executor import FutureTradeScheduler

    monkeypatch.setattr(FutureTradeScheduler, "_is_trading_day",
                        staticmethod(lambda dt_obj: True))   # date-independent

    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    r = client.post("/api/schedule", json={
        "account": 1, "pair": "EURUSD", "lot": 0.01, "n": 2,
        "exec": [now.hour, now.minute, now.second], "close": [23, 59, 0]})
    sid = r.get_json()["id"]
    # force next_fire just into the past (API rolls a passed time to tomorrow)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    with db._conn() as c:
        cur = c.cursor()
        db._exec(cur, "UPDATE future_trades SET next_fire=? WHERE id=?",
                 (past.isoformat(timespec="seconds"), sid))

    orders = []
    class FakeCmd:
        def open_trade(self, symbol, side, lot, magic=0, comment=""):
            orders.append((symbol, side, lot))
            return True, f"1.10000|T{len(orders)}"
        def close_all(self, symbol=None):
            return True, "closed"
        def send_batch(self, *commands, timeout=None):
            out = []
            for c in commands:               # ("OPEN", sym, side, lot, sl, tp, magic, comment)
                if c[0] == "OPEN":
                    self.open_trade(c[1], c[2], c[3], magic=int(c[6]), comment=c[7])
                    out.append((True, f"1.10000|T{len(orders)}"))
                else:
                    out.append((True, "ok"))
            return out
    monkeypatch.setattr(executor, "sender_for", lambda acc: FakeCmd())

    sch = FutureTradeScheduler()
    for s in db.due_schedules():
        if s["id"] == sid:
            sch._process_due(s)

    assert len(orders) == 2, "n=2 -> two market orders"
    row = db.get_schedule(sid)
    nf = dt.datetime.fromisoformat(row["next_fire"])
    assert nf > dt.datetime.now(dt.timezone.utc), "must be rescheduled to tomorrow"
    fired = db.list_fired(10)
    assert any(f["schedule_id"] == sid and f["ok"] for f in fired)
