#!/usr/bin/env python3.12
"""app.py - the web trading terminal (Flask + production WSGI).

IMPROVEMENTS:
- Production WSGI server (waitress) with configurable workers/threads
- Health check endpoint (/health)
- Graceful shutdown with signal handling
- API rate limiting (in-memory)
- Better JSON error responses
- CORS support for external dashboards
- Request logging middleware
- HFT stats endpoints
- Full API coverage from mt5

Usage:
    python app.py              # http://localhost:8000
    python app.py --production # Use waitress WSGI server
    python app.py --port 9000  # Custom port
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

from flask import Flask, jsonify, request, Response

import config
from config import CONFIG, setup_logging, validate_config

import front
import database as db
from info import snapshot
from spot import read_spots, feed_age, term_running
from executor import start_scheduler, stop_scheduler, sender_for
from monitor import read_positions, account_info
import metrics

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
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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

    code = 200 if health["status"] == "healthy" else 503
    return jsonify(health), code


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _acc_panel(acc_snapshot: dict, inst: int) -> dict:
    """Panel card for one account: account info + positions + schedules."""
    out = dict(acc_snapshot)
    out["positions"] = read_positions(inst)
    scheds = db.list_future_trades(active_only=True)
    out["schedules"] = [s for s in scheds if s["account"] == inst]
    return out


def _system() -> dict:
    sys_info = {
        "scheduler": bool(getattr(sys.modules[__name__], "_scheduler", None) and getattr(sys.modules[__name__], "_scheduler", None).is_alive()),
        "metrics": bool(metrics.is_running()),
        "schedules_active": len(db.list_future_trades(active_only=True)),
        "version": "2.0.0"
    }
    for inst in (1, 2):
        sys_info[f"t{inst}"] = {
            "running": term_running(inst),
            "age": feed_age(inst),
            "stale": feed_age(inst) > CONFIG.bridge_stale_seconds
        }
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
@rate_limit(max_requests=120, window=60)
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
@rate_limit(max_requests=120, window=60)
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
    symbol = str(d.get("symbol", "")).upper()
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
    ok, detail = sender_for(account).open_trade(symbol, side, lot)
    if not ok:
        if "10027" in detail:
            detail = ("terminal has ALGO TRADING OFF (retcode 10027) - "
                      "restart it via bridge.py (launches with algo trading ON)")
        return _fail(detail)
    ticket = detail.split("|")[1] if "|" in detail else ""
    price = detail.split("|")[0] if "|" in detail else ""
    db.log_fired(0, account, symbol, side, lot, "manual", ticket, ok, detail)
    return _ok({"detail": detail, "price": price, "ticket": ticket})


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
    symbol = str(d.get("symbol", "")).upper()
    try:
        cmd = sender_for(account)
        if ticket:
            ok, detail = cmd.close_position(ticket)
        else:
            ok, detail = cmd.close_all(None if symbol in ("", "ALL") else symbol)
        return _ok({"detail": detail}) if ok else _fail(detail)
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
        pair = str(d.get("pair", "")).upper()
        lot = float(d.get("lot", 0))
        n = int(d.get("n", 1))
        ex, cl = d.get("exec") or [0, 0, 0], d.get("close") or [0, 0, 0]
        eh, em, es = (int(x) for x in ex)
        ch, cm, cs = (int(x) for x in cl)
    except (TypeError, ValueError):
        return _fail("bad schedule fields")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
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
    sid = db.add_future_trade(account, pair, "BUY", lot, n, eh, em, es, ch, cm, cs)
    return _ok({"id": sid, "next_fire": db.get_schedule(sid)["next_fire"]})


@app.post("/api/schedule/delete")
@rate_limit(max_requests=20, window=60)
def api_schedule_delete():
    d = request.get_json(silent=True) or {}
    try:
        sid = int(d.get("id", 0))
    except (TypeError, ValueError):
        return _fail("bad id")
    if not db.get_schedule(sid):
        return _fail("no such schedule")
    db.deactivate(sid)
    return _ok()


@app.delete("/api/schedule/<int:sid>")
@rate_limit(max_requests=20, window=60)
def api_delete_schedule(sid: int):
    db.deactivate(sid)
    return _ok()


@app.get("/api/fired")
@rate_limit(max_requests=60, window=60)
def api_fired():
    return jsonify({"ok": True, "fired": db.list_fired(80)})


@app.get("/api/schedules")
@rate_limit(max_requests=60, window=60)
def api_schedules():
    return jsonify({"ok": True, "schedules": db.list_future_trades()})


@app.get("/api/hft/stats")
@rate_limit(max_requests=30, window=60)
def api_hft_stats():
    """Get HFT bot statistics for both accounts."""
    return jsonify({
        "ok": True,
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "accounts": {
            "1": db.get_hft_performance(1),
            "2": db.get_hft_performance(2)
        },
        "recent_trades": {
            "1": db.get_hft_stats(1, limit=20),
            "2": db.get_hft_stats(2, limit=20)
        }
    })


@app.get("/api/hft/trades")
@rate_limit(max_requests=30, window=60)
def api_hft_trades():
    """Get HFT trade history."""
    account = request.args.get("account", type=int)
    symbol = request.args.get("symbol", type=str)
    limit = request.args.get("limit", default=100, type=int)
    return jsonify({
        "ok": True,
        "trades": db.get_hft_stats(account, symbol, limit)
    })


@app.get("/api/metrics/<int:acc>")
@rate_limit(max_requests=60, window=60)
def api_metrics(acc: int):
    return _ok({
        "stats": metrics.stats(acc),
        "equity": metrics.equity_curve(acc),
        "pairs": metrics.pairs_traded(acc),
    })


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
_shutdown_event = threading.Event()


def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name}, shutting down gracefully...")
    _shutdown_event.set()
    stop_scheduler()
    metrics.stop_observer()
    sys.exit(0)


def main() -> int:
    global _scheduler
    ap = argparse.ArgumentParser(description="MT5 web trading terminal (Flask)")
    ap.add_argument("--host", default=CONFIG.web_host)
    ap.add_argument("--port", type=int, default=CONFIG.web_port)
    ap.add_argument("--production", action="store_true",
                    help="serve via waitress (production WSGI) instead of the dev server")
    ap.add_argument("--workers", type=int, default=CONFIG.web_workers,
                    help="waitress worker processes (production mode)")
    ap.add_argument("--threads", type=int, default=CONFIG.web_threads,
                    help="waitress worker threads (production mode)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # Boot-time sanity check (logs warnings; never blocks startup)
    for w in validate_config():
        log.warning(f"config: {w}")

    db.init_db()
    _scheduler = start_scheduler()          # future trades -> to the millisecond
    metrics.start_observer()                # closed trades / equity curve

    # Graceful shutdown: stop the scheduler thread + metrics observer
    def _shutdown(*_):
        log.info("shutting down - stopping scheduler + metrics observer...")
        if _scheduler:
            _scheduler.stop_flag.set()
        metrics.stop_observer()
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
            log.info(f"production WSGI (waitress, {args.workers} workers, {args.threads} threads) "
                     f"-> http://{args.host}:{args.port}")
            serve(app, host=args.host, port=args.port, threads=args.threads,
                  connection_limit=200)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug,
                threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())