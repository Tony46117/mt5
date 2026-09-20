#!/usr/bin/env python3.12
"""app.py - the web trading terminal (Flask) and the scheduler's host process.

Serves the dashboard ("/") and the trading panel ("/panel") rendered by
front.py, exposes the JSON API those pages poll, and owns the three
background workers: the future-trade scheduler (executor.py), the closed-
trade/equity observer (metrics.py) and the broker capability prober.

SECURITY: there is NO authentication on this API and every /api/trade,
/api/close and /api/schedule call moves real money.  Bind it to localhost
only (the default) and do not expose the port.  Cross-origin access is
refused unless MT5_CORS_ORIGIN names an exact origin.

Usage:
    python app.py              # http://127.0.0.1:8000
    python app.py --production # serve via waitress instead of the dev server
    python app.py --port 9000  # custom port
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import threading
import time
from functools import wraps
from typing import Callable

from flask import Flask, jsonify, request

from config import CONFIG, setup_logging, validate_config

import front
import database as db
from info import snapshot
from spot import read_spots, feed_age, term_running
from executor import start_scheduler, sender_for
from monitor import read_positions, account_info
import metrics
import broker_prober

# Configure logging - reduce Flask/Werkzeug noise
logging.getLogger('werkzeug').setLevel(logging.WARNING)

log = setup_logging(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False

# --------------------------------------------------------------------------
# Rate limiting (simple in-memory)
# --------------------------------------------------------------------------
_rate_limit_store: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def rate_limit(max_requests: int = 60, window: float = 60.0):
    """Decorator to rate limit endpoints."""
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = f"{request.remote_addr}:{f.__name__}"
            now = time.time()
            with _rate_lock:
                reqs = _rate_limit_store.get(key, [])
                reqs = [t for t in reqs if now - t < window]
                if len(reqs) >= max_requests:
                    return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429
                reqs.append(now)
                _rate_limit_store[key] = reqs
            return f(*args, **kwargs)
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Request logging middleware
# --------------------------------------------------------------------------

@app.before_request
def log_request():
    if request.path.startswith("/api/"):
        log.debug(f"{request.method} {request.path} from {request.remote_addr}")


# --------------------------------------------------------------------------
# CORS support
# --------------------------------------------------------------------------

@app.after_request
def add_security_headers(response):
    """This API places and closes REAL orders and has no authentication.
    It used to answer with Access-Control-Allow-Origin: * , which let any
    web page the operator happened to visit read account state and fire
    trades on their behalf.  No cross-origin access is granted; set
    MT5_CORS_ORIGIN to an exact origin if an external dashboard needs it."""
    origin = os.getenv("MT5_CORS_ORIGIN", "").strip()
    if origin and origin != "*":
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/")
def page_dashboard():
    return front.render_dashboard()


@app.get("/panel")
def page_panel():
    return front.render_panel()


@app.get("/scheduled")
def page_scheduled():
    return front.render_scheduled()


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    """Machine-readable health for monitoring / uptime checks."""
    health = {
        "status": "healthy",
        "timestamp": dt.datetime.now().isoformat(),
        "version": "2.0.0",
        "components": {}
    }

    # Check database
    try:
        db.list_future_trades(active_only=True)
        health["components"]["database"] = "ok"
    except Exception as e:
        health["components"]["database"] = f"error: {e}"
        health["status"] = "degraded"

    # Check terminals
    for inst in (1, 2):
        running = term_running(inst)
        age = feed_age(inst)
        stale = age > CONFIG.bridge_stale_seconds
        health["components"][f"terminal_{inst}"] = {
            "running": running,
            "feed_age_s": round(age, 2),
            "stale": stale
        }
        if not running or stale:
            health["status"] = "degraded"

    # Check scheduler
    sched = getattr(sys.modules[__name__], "_scheduler", None)
    health["components"]["scheduler"] = "running" if sched and sched.is_alive() else "stopped"

    # Check metrics observer
    health["components"]["metrics"] = "running" if metrics.is_running() else "stopped"

    # Check broker prober
    health["components"]["broker_prober"] = ("running" if broker_prober.get_prober().is_running()
                                              else "stopped")

    code = 200 if health["status"] == "healthy" else 503
    return jsonify(health), code


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _acc_panel(acc_snapshot: dict, inst: int) -> dict:
    """Panel card for one account: account info + positions."""
    out = dict(acc_snapshot)
    out["positions"] = read_positions(inst)
    return out


_sys_cache: dict = {}
_sys_cache_ts: float = 0.0

def _system() -> dict:
    global _sys_cache, _sys_cache_ts
    now = time.time()
    # Cache for 1 s - avoids a DB query + two pgrep forks per panel poll (0.6 s)
    if _sys_cache and now - _sys_cache_ts < 1.0:
        return _sys_cache
    sched_alive = bool(getattr(sys.modules[__name__], "_scheduler", None)
                       and getattr(sys.modules[__name__], "_scheduler", None).is_alive())
    sys_info = {
        "scheduler": sched_alive,
        "metrics": bool(metrics.is_running()),
        "schedules_active": len(db.list_future_trades(active_only=True)),
        "version": "2.0.0"
    }
    for inst in (1, 2):
        age = feed_age(inst)
        sys_info[f"t{inst}"] = {
            "running": term_running(inst),
            "age": age,
            "stale": age > CONFIG.bridge_stale_seconds
        }
    _sys_cache = sys_info
    _sys_cache_ts = now
    return sys_info


def _ok(payload: dict | None = None):
    j = {"ok": True}
    if payload:
        j.update(payload)
    return jsonify(j)


def _fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


# --------------------------------------------------------------------------
# apis
# --------------------------------------------------------------------------

@app.get("/api/debug")
@rate_limit(max_requests=30, window=60)
def api_debug():
    """Debug endpoint to verify API connectivity and data structure."""
    try:
        snap = snapshot()
        sys_info = _system()
        return jsonify({
            "ok": True,
            "ts": snap["ts"],
            "accounts_keys": list(snap["accounts"].keys()),
            "account1_keys": list(snap["accounts"].get("1", {}).keys()) if "1" in snap["accounts"] else [],
            "account2_keys": list(snap["accounts"].get("2", {}).keys()) if "2" in snap["accounts"] else [],
            "system": sys_info,
            "python_version": "3.12",
        })
    except Exception as exc:
        log.error(f"/api/debug error: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/dashboard")
@rate_limit(max_requests=600, window=60)
def api_dashboard():
    try:
        snap = snapshot()
        for n in ("1", "2"):
            if n in snap["accounts"]:
                a = snap["accounts"][n]
                try:
                    st = metrics.stats(int(n))
                    a["stats"] = st
                    a["equity_curve"] = metrics.equity_curve(int(n))
                    a["pairs"] = st.get("pairs", {})
                except Exception as exc:
                    log.warning(f"metrics error for account {n}: {exc}")
                    a["stats"] = {"trades_taken": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0, "winrate": 0.0, "net_closed": 0.0, "pairs": {}}
                    a["equity_curve"] = []
                    a["pairs"] = {}
        return jsonify({"ok": True, "ts": snap["ts"], "accounts": snap["accounts"], "system": _system()})
    except Exception as exc:
        log.error(f"/api/dashboard error: {exc}")
        return jsonify({"ok": False, "error": str(exc), "accounts": {"1": {}, "2": {}}, "system": _system()}), 500


@app.get("/api/panel")
@rate_limit(max_requests=600, window=60)
def api_panel():
    try:
        snap = snapshot()
        for n in ("1", "2"):
            if n in snap["accounts"]:
                snap["accounts"][n] = _acc_panel(snap["accounts"][n], int(n))
        snap["system"] = _system()
        snap["spots"] = read_spots()
        return jsonify({"ok": True, "ts": snap["ts"], "accounts": snap["accounts"], "system": _system()})
    except Exception as exc:
        log.error(f"/api/panel error: {exc}")
        return jsonify({"ok": False, "error": str(exc), "accounts": {"1": {}, "2": {}}, "system": _system()}), 500


@app.get("/api/spots")
@rate_limit(max_requests=300, window=60)
def api_spots():
    spots = read_spots()
    return jsonify({"ok": True,
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "spots": {k: {"bid": b, "ask": a, "ts": t}
                              for k, (b, a, t) in spots.items()}})


@app.get("/api/accounts")
@rate_limit(max_requests=60, window=60)
def api_accounts():
    """Get detailed account info from monitor.py"""
    acc1 = account_info(1)
    acc2 = account_info(2)
    return jsonify({"ok": True,
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "accounts": {"1": acc1, "2": acc2}})


@app.post("/api/trade")
@rate_limit(max_requests=30, window=60)
def api_trade():
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    # MT5 symbol names are CASE-SENSITIVE ("Boom 1000 Index"): pass the
    # symbol through exactly as the feed reported it.
    symbol = str(d.get("symbol", "")).strip()
    side = str(d.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        return _fail("side must be BUY or SELL")
    try:
        lot = float(d.get("lot", 0))
    except (TypeError, ValueError):
        return _fail("bad lot")
    if lot <= 0 or lot > 100:
        return _fail("lot out of range")
    spots = read_spots()
    if symbol not in spots or not spots[symbol][0]:
        return _fail("no live quote for " + symbol)
    # DIRECT execution: the order round trip is the ONLY thing on the hot
    # path - the audit write happens in a background thread AFTER the
    # response, so the button's latency is pure terminal round trip.
    t0 = time.perf_counter()
    # 8 s deadline (not the 3 s channel default): in thin Sunday liquidity
    # the broker itself took 1.9-4.8 s to fill two async SELLs, and the
    # default turned that into a false 'REJECTED ... not responding' while
    # the order HAD executed (the UI would then invite a duplicate).
    ok, detail = sender_for(account).open_trade(symbol, side, lot, timeout=8.0)
    order_ms = (time.perf_counter() - t0) * 1000.0
    ticket = detail.split("|")[1] if "|" in detail else ""
    price = detail.split("|")[0] if "|" in detail else ""
    threading.Thread(target=db.log_fired,
                     args=(0, account, symbol, side, lot, "manual", ticket,
                           ok, detail), kwargs={"ms": order_ms},
                     daemon=True, name="manual-trade-log").start()
    if not ok:
        if "10027" in detail:
            detail = ("terminal has ALGO TRADING OFF (retcode 10027) - "
                      "restart it via bridge.py (launches with algo trading ON)")
        return _fail(detail)
    return _ok({"detail": detail, "price": price, "ticket": ticket,
                "ms": round(order_ms, 1)})


@app.post("/api/close")
@rate_limit(max_requests=30, window=60)
def api_close():
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    ticket = str(d.get("ticket", ""))
    symbol = str(d.get("symbol", "")).strip()
    try:
        cmd = sender_for(account)
        t0 = time.perf_counter()
        if ticket:
            ok, detail = cmd.close_position(ticket)
        else:
            ok, detail = cmd.close_all(None if symbol in ("", "ALL") else symbol)
        close_ms = (time.perf_counter() - t0) * 1000.0
        # audit log OFF the hot path (same as /api/trade)
        if ticket:
            threading.Thread(target=db.log_fired,
                             args=(0, account, symbol or "-", "-", 0.0,
                                   "manual-close", ticket, ok, detail),
                             kwargs={"ms": close_ms}, daemon=True,
                             name="manual-close-log").start()
        resp = {"detail": detail, "ms": round(close_ms, 1)}
        return _ok(resp) if ok else _fail(detail)
    except Exception as exc:
        return _fail(str(exc))


@app.post("/api/restart")
@rate_limit(max_requests=6, window=60)
def api_restart():
    """Manual heal: restart one terminal (or both with account=0)."""
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (0, 1, 2):
        return _fail("account must be 0 (both), 1 or 2")
    from spot import restart_terminal

    def _worker():
        for inst in ((1, 2) if account == 0 else (account,)):
            try:
                restart_terminal(inst)
            except Exception as exc:
                log.error(f"manual restart of terminal {inst} failed: {exc}")
    threading.Thread(target=_worker, daemon=True,
                     name="manual-restart").start()
    return _ok({"detail": f"restarting terminal "
                          f"{account if account else '1+2'}"})


@app.post("/api/schedule")
@rate_limit(max_requests=20, window=60)
def api_schedule():
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
        pair = str(d.get("pair", "")).strip()   # case-sensitive symbol name
        side = str(d.get("side", "BUY")).upper()
        lot = float(d.get("lot", 0))
        n = int(d.get("n", 1))
        ex, cl = d.get("exec") or [0, 0, 0], d.get("close") or [0, 0, 0]
        eh, em, es = (int(x) for x in ex)
        ch, cm, cs = (int(x) for x in cl)
    except (TypeError, ValueError):
        return _fail("bad schedule fields")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    if side not in ("BUY", "SELL"):
        return _fail("side must be BUY or SELL")
    if not pair:
        return _fail("pair is required")
    if lot <= 0 or lot > 100:
        return _fail("lot out of range")
    if not 1 <= n <= 50:
        return _fail("positions must be 1..50")
    # NOTE: no live-quote requirement here - the EA only quotes symbols in
    # its Market Watch, and demanding a quote made scheduling (and thus the
    # whole future-trades feature) fail whenever the pair was dormant or a
    # terminal was still booting.  A bad pair is caught at fire time and
    # logged to the fired table instead.
    for h, m, s in ((eh, em, es), (ch, cm, cs)):
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
            return _fail("time out of range")
    # H/M/S are LOCAL WALL CLOCK (the operator reads the panel clock on the
    # same wall) - database.add_future_trade converts to UTC for storage.
    sid = db.add_future_trade(account, pair, side, lot, n, eh, em, es, ch, cm, cs)
    sch = db.get_schedule(sid)
    nf = sch["next_fire"] if sch else "?"
    # show the operator their OWN wall-clock fire time, not the UTC storage
    try:
        nf_local = (dt.datetime.fromisoformat(str(nf)).astimezone()
                    .strftime("%H:%M:%S"))
    except (ValueError, TypeError):
        nf_local = str(nf)
    return _ok({"id": sid, "next_fire": str(nf), "next_fire_local": nf_local})


@app.post("/api/schedule/delete")
@rate_limit(max_requests=30, window=60)
def api_schedule_delete():
    d = request.get_json(silent=True) or {}
    try:
        sid = int(d.get("id", 0))
    except (TypeError, ValueError):
        return _fail("bad id")
    if not db.get_schedule(sid):
        return _fail("no such schedule")
    if d.get("hard"):
        db.delete_schedule(sid)
        return _ok({"detail": f"schedule #{sid} deleted"})
    db.deactivate(sid)
    return _ok()


@app.delete("/api/schedule/<int:sid>")
@rate_limit(max_requests=20, window=60)
def api_delete_schedule(sid: int):
    if not db.get_schedule(sid):
        return _fail("no such schedule")
    db.delete_schedule(sid)
    return _ok({"detail": f"schedule #{sid} deleted"})


@app.post("/api/schedule/update")
@rate_limit(max_requests=40, window=60)
def api_schedule_update():
    """EDIT one schedule in place (scheduled page -> ADJUST).
    All fields optional; missing ones keep their current value."""
    d = request.get_json(silent=True) or {}
    try:
        sid = int(d.get("id", 0))
    except (TypeError, ValueError):
        return _fail("bad id")
    cur = db.get_schedule(sid)
    if not cur:
        return _fail("no such schedule")
    updates: dict = {}
    try:
        if "pair" in d:
            pair = str(d["pair"]).strip()
            if not pair:
                return _fail("pair is required")
            updates["pair"] = pair            # case-sensitive symbol
        if "side" in d:
            side = str(d["side"]).upper()
            if side not in ("BUY", "SELL"):
                return _fail("side must be BUY or SELL")
            updates["side"] = side
        if "lot" in d:
            lot = float(d["lot"])
            if lot <= 0 or lot > 100:
                return _fail("lot out of range")
            updates["lot"] = lot
        if "n" in d:
            n = int(d["n"])
            if not 1 <= n <= 50:
                return _fail("positions must be 1..50")
            updates["n_positions"] = n
        for key in ("exec", "close"):
            if key in d and d[key] is not None:
                h, m, s = (int(x) for x in d[key])
                if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
                    return _fail("time out of range")
                if key == "exec":
                    updates.update(exec_h=h, exec_m=m, exec_s=s)
                else:
                    updates.update(close_h=h, close_m=m, close_s=s)
        if "active" in d:
            updates["active"] = bool(d["active"])
    except (TypeError, ValueError):
        return _fail("bad update fields")
    if not updates:
        return _fail("nothing to update")
    if not db.update_schedule(sid, **updates):
        return _fail("update failed")
    sch = db.get_schedule(sid)
    nf = sch["next_fire"] if sch else "?"
    try:
        nf_local = (dt.datetime.fromisoformat(str(nf)).astimezone()
                    .strftime("%H:%M:%S"))
    except (ValueError, TypeError):
        nf_local = str(nf)
    return _ok({"id": sid, "next_fire": str(nf), "next_fire_local": nf_local})


@app.post("/api/history/clear")
@rate_limit(max_requests=6, window=60)
def api_history_clear():
    """START AFRESH: wipe the fired log and drop inactive schedules.
    ACTIVE schedules are kept - they are armed trades, not history."""
    deleted = db.clear_history()
    return _ok({"deleted": deleted,
                "detail": f"cleared {deleted} rows - history starts afresh"})


@app.get("/api/fired")
@rate_limit(max_requests=600, window=60)
def api_fired():
    return jsonify({"ok": True, "fired": db.list_fired(80)})


@app.get("/api/clock")
@rate_limit(max_requests=120, window=60)
def api_clock():
    """Which clock schedule times are interpreted in.

    Schedules are stored and fired in UTC (database.add_future_trade), but
    the panel used to render a bare "14:30:00" with no zone, so an operator
    in UTC+3 scheduled 14:30 and the order fired at 17:30 their time.  The
    panel now labels the zone and shows the local equivalent.
    """
    now_local = dt.datetime.now().astimezone()
    return _ok({
        "schedule_tz": "UTC",
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "local": now_local.isoformat(timespec="seconds"),
        "local_name": now_local.tzname() or "local",
        "utc_offset_minutes": int(now_local.utcoffset().total_seconds() // 60),
    })


@app.get("/api/schedules")
@rate_limit(max_requests=600, window=60)
def api_schedules():
    return jsonify({"ok": True, "schedules": db.list_future_trades()})


@app.get("/api/metrics/<int:acc>")
@rate_limit(max_requests=60, window=60)
def api_metrics(acc: int):
    return _ok({
        "stats": metrics.stats(acc),
        "equity": metrics.equity_curve(acc),
        "pairs": metrics.pairs_traded(acc),
    })


@app.get("/api/broker-probe")
@rate_limit(max_requests=30, window=60)
def api_broker_probe():
    """Live broker capabilities + best order filling per terminal/symbol."""
    prober = broker_prober.get_prober()
    if not prober.report()["terminals"]:
        # nothing cached yet: probe synchronously once (first call after boot)
        prober.probe_all()
    return jsonify(prober.report())


@app.post("/api/broker-probe")
@rate_limit(max_requests=10, window=60)
def api_broker_probe_refresh():
    """Force a fresh probe now (body may pin one terminal)."""
    d = request.get_json(silent=True) or {}
    prober = broker_prober.get_prober()
    try:
        acc = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if acc == 0:
        prober.probe_all()
    elif acc in (1, 2):
        prober.probe_terminal(acc)
    else:
        return _fail("account must be 0 (both), 1 or 2")
    return jsonify(prober.report())


# --------------------------------------------------------------------------
# Error handlers
# --------------------------------------------------------------------------

@app.errorhandler(404)
def page_404(_e):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(500)
def server_error(e):
    log.exception("Internal server error")
    return jsonify({"ok": False, "error": "Internal server error"}), 500


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

_scheduler = None


def _warmup_first_orders() -> None:
    """CI-style deploy warm-up: fire one tiny open->close per terminal right
    after boot, in background threads.

    WHY: a terminal's very FIRST order on a session-fresh symbol (e.g.
    XAUUSD247 right after login) can stall ~25 s inside the broker's
    server-side symbol-sync before the EA's OrderSend returns - observed
    live: the first scheduled trade after a fresh boot timed out and its
    close timed out twice, while every later order ran at ~550 ms.  A
    warm-up trade absorbs that cold path at deploy time, so the first
    REAL scheduled order is never the one that pays for it.

    This places and closes a REAL order on both accounts at every start,
    so it is opt-OUT (MT5_WARMUP=0) and the symbol/lot are configurable
    instead of a hardcoded XAUUSD247 0.01 that only suits one broker.
    """
    if os.getenv("MT5_WARMUP", "1") != "1":
        log.info("deploy warm-up disabled (MT5_WARMUP=0)")
        return
    symbol = os.getenv("MT5_WARMUP_SYMBOL", "XAUUSD247")
    try:
        lot = float(os.getenv("MT5_WARMUP_LOT", "0.01"))
    except ValueError:
        lot = 0.01
    log.info(f"deploy warm-up armed: one {symbol} {lot} open+close per "
             f"terminal in 20 s (MT5_WARMUP=0 disables)")

    def _warm(inst: int) -> None:
        try:
            time.sleep(20.0)               # let feeds/login settle first
            from executor import _close_one
            ok, det = sender_for(inst).open_trade(symbol, "BUY", lot,
                                                  magic=777099,
                                                  comment="warmup")
            if ok:
                time.sleep(0.4)
                _close_one(inst, symbol)
                log.info(f"terminal {inst}: deploy warm-up trade done")
            else:
                log.warning(f"terminal {inst}: warm-up open failed ({det}) - "
                            f"first scheduled trade may still hit the "
                            f"cold-symbol path")
        except Exception as exc:
            log.warning(f"terminal {inst}: warm-up skipped ({exc})")

    for inst in (1, 2):
        threading.Thread(target=_warm, args=(inst,), daemon=True,
                         name=f"warmup-{inst}").start()


def main() -> int:
    global _scheduler
    ap = argparse.ArgumentParser(description="MT5 web trading terminal (Flask)")
    ap.add_argument("--host", default=CONFIG.web_host)
    ap.add_argument("--port", type=int, default=CONFIG.web_port)
    ap.add_argument("--production", action="store_true",
                    help="serve via waitress (production WSGI) instead of the dev server")
    ap.add_argument("--threads", type=int, default=CONFIG.web_threads,
                    help="waitress worker threads (production mode)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # Boot-time sanity check (logs warnings; never blocks startup)
    for w in validate_config():
        log.warning(f"config: {w}")

    db.init_db()
    _scheduler = start_scheduler()          # future trades -> to the millisecond
    _warmup_first_orders()                  # CI-style deploy warm-up
    metrics.start_observer()                # closed trades / equity curve

    # BROKER PROBER: probes the logged-in brokers and decides the best
    # order filling method so trades never get rejected (retcode 10030)
    broker_prober.start_prober()

    # Graceful shutdown: stop the scheduler thread + metrics observer
    def _shutdown(*_):
        log.info("shutting down - stopping scheduler + metrics observer...")
        if _scheduler:
            _scheduler.stop_flag.set()
        metrics.stop_observer()
        broker_prober.stop_prober()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(f"MT5 web terminal v2.0 -> http://{args.host}:{args.port} (dashboard) /panel (trading panel)")
    log.info(f"  Health:     http://{args.host}:{args.port}/health")

    if args.production:
        try:
            from waitress import serve
        except ImportError:
            log.error("waitress not installed - falling back to the dev server "
                      "(pip install waitress)")
            app.run(host=args.host, port=args.port, debug=False,
                    threaded=True, use_reloader=False)
        else:
            log.info(f"production WSGI (waitress, {args.threads} threads) "
                     f"-> http://{args.host}:{args.port}")
            serve(app, host=args.host, port=args.port, threads=args.threads,
                  connection_limit=200)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug,
                threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())