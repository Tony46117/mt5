#!/usr/bin/env python3.12

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

from config import CONFIG, map_symbol, setup_logging, validate_config

import front
import database as db
from info import snapshot
from spot import read_spots, feed_age, term_running, read_header
from executor import start_scheduler, sender_for
from monitor import read_positions, account_info
import metrics
import broker_prober
import account_engine

logging.getLogger('werkzeug').setLevel(logging.WARNING)

log = setup_logging(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False

_rate_limit_store: dict[str, list[float]] = {}
_rate_lock = threading.Lock()

def rate_limit(max_requests: int = 60, window: float = 60.0):
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

@app.before_request
def log_request():
    if request.path.startswith("/api/"):
        log.debug(f"{request.method} {request.path} from {request.remote_addr}")

@app.after_request
def add_security_headers(response):
    origin = os.getenv("MT5_CORS_ORIGIN", "").strip()
    if origin and origin != "*":
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

@app.route("/api/<path:_p>", methods=["OPTIONS"])
def api_preflight(_p):
    origin = os.getenv("MT5_CORS_ORIGIN", "").strip()
    if origin and origin != "*":
        return ("", 204, {"Access-Control-Allow-Origin": origin,
                          "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                          "Access-Control-Allow-Headers": "Content-Type",
                          "Vary": "Origin"})
    return ("", 204)

@app.get("/")
def page_dashboard():
    return front.render_dashboard()

@app.get("/panel")
def page_panel():
    return front.render_panel()

@app.get("/scheduled")
def page_scheduled():
    return front.render_scheduled()

@app.get("/health")
def health():
    health = {
        "status": "healthy",
        "timestamp": dt.datetime.now().isoformat(),
        "version": "2.0.0",
        "components": {}
    }

    try:
        db.list_future_trades(active_only=True)
        health["components"]["database"] = "ok"
    except Exception as e:
        health["components"]["database"] = f"error: {e}"
        health["status"] = "degraded"

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

    sched = getattr(sys.modules[__name__], "_scheduler", None)
    health["components"]["scheduler"] = "running" if sched and sched.is_alive() else "stopped"

    health["components"]["metrics"] = "running" if metrics.is_running() else "stopped"

    health["components"]["broker_prober"] = ("running" if broker_prober.get_prober().is_running()
                                              else "stopped")

    code = 200 if health["status"] == "healthy" else 503
    return jsonify(health), code

def _acc_panel(acc_snapshot: dict, inst: int) -> dict:
    out = dict(acc_snapshot)
    out["positions"] = read_positions(inst)
    return out

_sys_cache: dict = {}
_sys_cache_ts: float = 0.0
_SYS_CACHE_TTL_S = 0.5

def _system() -> dict:
    global _sys_cache, _sys_cache_ts
    now = time.time()
    if _sys_cache and now - _sys_cache_ts < _SYS_CACHE_TTL_S:
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
        try:
            from monitor import read_accounts, pick_terminal
            login = read_accounts().get(inst, {}).get("login", "")
            term = pick_terminal(login) if login else None
            h = read_header(term["trades_path"]) if term else {}
        except Exception:
            h = {}
        logged_in = bool(str(h.get("login", "")) not in ("", "0"))
        sys_info[f"t{inst}"] = {
            "running": term_running(inst),
            "age": age,
            "stale": age > CONFIG.bridge_stale_seconds,
            "logout": (not logged_in and not (age > CONFIG.bridge_stale_seconds)),
            "logged_in": logged_in,
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

# ---------------- account integration engine ----------------

@app.get("/api/engine/status")
@rate_limit(max_requests=240, window=60)
def api_engine_status():
    try:
        return jsonify(account_engine.get_engine().status())
    except Exception as exc:
        return _fail(str(exc), code=500)

@app.get("/api/engine/accounts")
@rate_limit(max_requests=60, window=60)
def api_engine_known():
    try:
        return _ok({"accounts": account_engine.get_engine().known()})
    except Exception as exc:
        return _fail(str(exc), code=500)

@app.post("/api/engine/login")
@rate_limit(max_requests=12, window=60)
def api_engine_login():
    d = request.get_json(silent=True) or {}
    try:
        terminal = int(d.get("terminal", 0))
    except (TypeError, ValueError):
        return _fail("bad terminal")
    try:
        op = account_engine.get_engine().request_login(
            terminal,
            str(d.get("login", "")),
            str(d.get("password", "")),
            str(d.get("server", "")))
    except account_engine.EngineError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"detail": f"terminal {terminal}: logging into "
                          f"{d.get('login')} @ {d.get('server')} ...",
                "op": op})

@app.post("/api/engine/adopt")
@rate_limit(max_requests=12, window=60)
def api_engine_adopt():
    d = request.get_json(silent=True) or {}
    try:
        terminal = int(d.get("terminal", 0))
    except (TypeError, ValueError):
        return _fail("bad terminal")
    try:
        op = account_engine.get_engine().request_adopt(terminal)
    except account_engine.EngineError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"detail": f"terminal {terminal}: adopting the account "
                          f"logged in on it ...", "op": op})

@app.post("/api/engine/logout")
@rate_limit(max_requests=12, window=60)
def api_engine_logout():
    d = request.get_json(silent=True) or {}
    try:
        terminal = int(d.get("terminal", 0))
    except (TypeError, ValueError):
        return _fail("bad terminal")
    try:
        op = account_engine.get_engine().request_logout(terminal)
    except account_engine.EngineError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"detail": f"terminal {terminal}: logging out ...",
                "op": op})

@app.post("/api/engine/restart")
@rate_limit(max_requests=6, window=60)
def api_engine_restart():
    d = request.get_json(silent=True) or {}
    try:
        terminal = int(d.get("terminal", 0))
    except (TypeError, ValueError):
        return _fail("bad terminal")
    try:
        op = account_engine.get_engine().request_restart(terminal)
    except account_engine.EngineError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"detail": f"terminal {terminal}: restarting ...", "op": op})

@app.post("/api/engine/forget")
@rate_limit(max_requests=20, window=60)
def api_engine_forget():
    d = request.get_json(silent=True) or {}
    login_id = str(d.get("login", "")).strip()
    if not login_id:
        return _fail("login is required")
    try:
        removed = account_engine.get_engine().forget(login_id)
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"removed": removed,
                "detail": f"{login_id} "
                          f"{'forgotten' if removed else 'not found'}"})

@app.post("/api/engine/save")
@rate_limit(max_requests=20, window=60)
def api_engine_save():
    """Save (or update) an account's info so it can be logged in later
    with one click - no terminal is touched and no login happens here."""
    d = request.get_json(silent=True) or {}
    login_id = str(d.get("login", "")).strip()
    if not login_id.isdigit():
        return _fail("login must be a plausible MT5 account number")
    try:
        import accounts as _known
        known = _known.get(login_id) or {}
    except Exception:
        known = {}
    server = str(d.get("server", "")).strip() or str(known.get("server", ""))
    if not server:
        return _fail("server is required (e.g. FxPro-MT5 or HFM-Real)")
    password = str(d.get("password", "") or "")
    label = str(d.get("label", "") or "").strip()
    try:
        account_engine.get_engine().remember(login_id, password, server,
                                             label)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    has_pw = bool(password or known.get("password"))
    return _ok({"account": {"login": login_id, "server": server,
                            "label": label or str(known.get("label", "")),
                            "has_password": has_pw},
                "detail": f"{login_id} saved - one click logs it in"})

@app.get("/api/debug")
@rate_limit(max_requests=30, window=60)
def api_debug():
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
    acc1 = account_info(1)
    acc2 = account_info(2)
    return jsonify({"ok": True,
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "accounts": {"1": acc1, "2": acc2}})

@app.post("/api/trade")
@rate_limit(max_requests=300, window=60)
def api_trade():
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
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
    try:
        _raw_n = d.get("n", d.get("count",
                       d.get("n_positions", d.get("orders", 1))))
        if _raw_n is None or (isinstance(_raw_n, str) and not _raw_n.strip()):
            n = 1
        else:
            n = int(_raw_n)
    except (TypeError, ValueError):
        return _fail("bad order count")
    if not 1 <= n <= 50:
        return _fail("orders must be 1..50")
    spots = read_spots()
    if symbol not in spots or not spots[symbol][0]:
        return _fail("no live quote for " + symbol)
    sender = sender_for(account)
    if n == 1:
        t0 = time.perf_counter()
        ok, detail = sender.open_trade(symbol, side, lot, timeout=8.0)
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
                    "n": 1, "ms": round(order_ms, 1)})
    mapped = map_symbol(symbol.strip(), account)
    commands = [("OPEN", mapped, side, f"{lot:.2f}", "0", "0",
                 str(777001), "py") for _ in range(n)]
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*commands, timeout=20.0))
    except Exception as exc:
        return _fail(str(exc))
    order_ms = (time.perf_counter() - t0) * 1000.0
    tickets: list[str] = []
    ok_cnt = 0
    first_price = ""
    first_detail = ""
    for ok, detail in results:
        ticket = detail.split("|")[1] if ok and "|" in detail else ""
        price = detail.split("|")[0] if "|" in detail else ""
        if not first_detail:
            first_detail, first_price = detail, price
        if ok:
            ok_cnt += 1
            if ticket:
                tickets.append(ticket)
        threading.Thread(target=db.log_fired,
                         args=(0, account, symbol, side, lot, "manual", ticket,
                               ok, detail), kwargs={"ms": order_ms},
                         daemon=True, name="manual-trade-log").start()
    if ok_cnt == 0:
        detail = first_detail or "all orders rejected"
        if "10027" in detail:
            detail = ("terminal has ALGO TRADING OFF (retcode 10027) - "
                      "restart it via bridge.py (launches with algo trading ON)")
        return _fail(detail)
    return _ok({"detail": f"{ok_cnt}/{n} filled"
                           f"{' - ' + first_detail if first_detail else ''}",
                "price": first_price, "ticket": tickets[0] if tickets else "",
                "tickets": tickets, "ok": ok_cnt, "n": n,
                "ms": round(order_ms, 1)})

@app.post("/api/order")
@rate_limit(max_requests=300, window=60)
def api_order():
    """Place a PENDING order: BUY LIMIT / BUY STOP / SELL LIMIT / SELL STOP.

    The level is given the way the panel collects it - a whole number plus
    the decimal digits - so `1` + `12165` is 1.12165 on a 5-digit pair.
    """
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    symbol = str(d.get("symbol", "")).strip()
    ptype = (str(d.get("type", "")).upper()
             .replace(" ", "").replace("_", "").replace("-", ""))
    if ptype not in ("BUYLIMIT", "BUYSTOP", "SELLLIMIT", "SELLSTOP"):
        return _fail("type must be BUY LIMIT, BUY STOP, SELL LIMIT or SELL STOP")
    try:
        lot = float(d.get("lot", 0))
    except (TypeError, ValueError):
        return _fail("bad lot")
    if lot <= 0 or lot > 100:
        return _fail("lot out of range")
    try:
        _raw_n = d.get("n", d.get("count",
                       d.get("n_positions", d.get("orders", 1))))
        if _raw_n is None or (isinstance(_raw_n, str) and not _raw_n.strip()):
            n = 1
        else:
            n = int(_raw_n)
    except (TypeError, ValueError):
        return _fail("bad order count")
    if not 1 <= n <= 50:
        return _fail("orders must be 1..50")
    spots = read_spots()
    if symbol not in spots or not spots[symbol][0]:
        return _fail("no live quote for " + symbol)
    try:
        price = float(d.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        # whole + decimal, exactly like the panel/scheduling inputs
        try:
            whole = float(d.get("level_whole", 0) or 0)
        except (TypeError, ValueError):
            return _fail("bad level (whole)")
        frac_s = str(d.get("level_frac", "")).strip().lstrip(".")
        try:
            frac = float("0." + frac_s) if frac_s else 0.0
        except ValueError:
            return _fail("bad level (decimal)")
        price = whole + frac
    bid = float(spots[symbol][0])
    digits = len(f"{bid}".split(".")[1]) if "." in f"{bid}" else 0
    if price <= 0:
        return _fail("level required (whole number + decimal)")
    price = round(price, digits)
    sender = sender_for(account)
    if n == 1:
        t0 = time.perf_counter()
        try:
            ok, detail = sender.open_pending(symbol, ptype, lot,
                                             price, timeout=8.0)
        except Exception as exc:
            return _fail(str(exc))
        order_ms = (time.perf_counter() - t0) * 1000.0
        ticket = detail.split("|")[1] if "|" in detail else ""
        fill_price = detail.split("|")[0] if "|" in detail else ""
        threading.Thread(target=db.log_fired,
                         args=(0, account, symbol, ptype, lot, "pending", ticket,
                               ok, detail), kwargs={"ms": order_ms},
                         daemon=True, name="pending-order-log").start()
        if not ok:
            return _fail(detail)
        return _ok({"detail": detail, "price": fill_price, "ticket": ticket,
                    "type": ptype, "n": 1, "ms": round(order_ms, 1)})
    mapped = map_symbol(symbol.strip(), account)
    p = ptype.upper().replace(" ", "").replace("_", "")
    commands = [("PENDING", mapped, p, f"{lot:.2f}", f"{price:.8f}",
                 "0", "0", str(777002), "pend") for _ in range(n)]
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*commands, timeout=20.0))
    except Exception as exc:
        return _fail(str(exc))
    order_ms = (time.perf_counter() - t0) * 1000.0
    tickets: list[str] = []
    ok_cnt = 0
    first_detail = ""
    first_price = ""
    for ok, detail in results:
        ticket = detail.split("|")[1] if ok and "|" in detail else ""
        fill_price = detail.split("|")[0] if "|" in detail else ""
        if not first_detail:
            first_detail, first_price = detail, fill_price
        if ok:
            ok_cnt += 1
            if ticket:
                tickets.append(ticket)
        threading.Thread(target=db.log_fired,
                         args=(0, account, symbol, ptype, lot, "pending", ticket,
                               ok, detail), kwargs={"ms": order_ms},
                         daemon=True, name="pending-order-log").start()
    if ok_cnt == 0:
        return _fail(first_detail or "all orders rejected")
    return _ok({"detail": f"{ok_cnt}/{n} placed"
                          f"{' - ' + first_detail if first_detail else ''}",
                "price": first_price, "ticket": tickets[0] if tickets else "",
                "tickets": tickets, "ok": ok_cnt, "n": n,
                "type": ptype, "ms": round(order_ms, 1)})

@app.post("/api/close")
@rate_limit(max_requests=300, window=60)
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
            ok, detail = cmd.close_all(None if symbol in ("", "ALL") else symbol,
                                       timeout=8.0)
        close_ms = (time.perf_counter() - t0) * 1000.0
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

@app.post("/api/close-all")
@rate_limit(max_requests=12, window=60)
def api_close_all():
    """Close ALL positions on BOTH terminals concurrently (close.py)."""
    d = request.get_json(silent=True) or {}
    raw = str(d.get("symbol") or d.get("pair") or "ALL").strip() or "ALL"
    pair = None if raw.upper() == "ALL" else raw
    try:
        import close as closer
        res = closer.close_all_accounts_results(pair)
    except Exception as exc:
        return _fail(str(exc), code=500)
    accs = res.get("accounts", {})
    detail = "  |  ".join(
        f"A{n}: {'OK' if v.get('ok') else v.get('detail', 'failed')}"
        for n, v in sorted(accs.items()))
    payload = {"detail": detail, "wall_ms": res.get("wall_ms", 0.0),
               "accounts": accs}
    return _ok(payload) if any(v.get("ok") for v in accs.values()) \
        else _fail(detail or "nothing closed")

@app.post("/api/restart")
@rate_limit(max_requests=6, window=60)
def api_restart():
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
        pair = str(d.get("pair", "")).strip()
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
    try:
        _sym = broker_prober.get_prober().report()["terminals"].get(str(account), {}).get(pair)
        if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
            return _fail(f"{pair} is DISABLED for trading by the broker on "
                         f"account {account} (trade_mode=0, retcode 10017) - "
                         f"pick a tradeable symbol (e.g. XAUUSD)")
    except Exception:
        pass
    for h, m, s in ((eh, em, es), (ch, cm, cs)):
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
            return _fail("time out of range")
    try:
        sid = db.add_future_trade(account, pair, side, lot, n, eh, em, es,
                                  ch, cm, cs)
    except db.DuplicateScheduleError as exc:
        return _fail(str(exc), code=409)
    sch = db.get_schedule(sid)
    nf = sch["next_fire"] if sch else "?"
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
            updates["pair"] = pair
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
    if "pair" in updates and updates["pair"] != cur["pair"]:
        try:
            _sym = broker_prober.get_prober().report()["terminals"] \
                .get(str(cur["account"]), {}).get(updates["pair"])
            if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
                return _fail(f"{updates['pair']} is DISABLED for trading by "
                             f"the broker on account {cur['account']} "
                             f"(trade_mode=0, retcode 10017)")
        except Exception:
            pass
    try:
        changed = db.update_schedule(sid, **updates)
    except db.DuplicateScheduleError as exc:
        return _fail(str(exc), code=409)
    if not changed:
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
    scheds = db.list_future_trades()
    try:
        retries = getattr(sys.modules[__name__], "_scheduler", None)
        snap = retries.retry_snapshot() if retries else {}
    except Exception:
        snap = {}
    out = []
    for s in scheds:
        r = dict(s)
        st = snap.get(s["id"])
        if st:
            r["slot_retry"] = st
        out.append(r)
    return jsonify({"ok": True, "schedules": out})

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
    prober = broker_prober.get_prober()
    if not prober.report()["terminals"]:
        prober.probe_all()
    return jsonify(prober.report())

@app.post("/api/broker-probe")
@rate_limit(max_requests=10, window=60)
def api_broker_probe_refresh():
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

@app.errorhandler(404)
def page_404(_e):
    return jsonify({"ok": False, "error": "not found"}), 404

@app.errorhandler(500)
def server_error(e):
    log.exception("Internal server error")
    return jsonify({"ok": False, "error": "Internal server error"}), 500

_scheduler = None

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

    for w in validate_config():
        log.warning(f"config: {w}")

    db.init_db()
    _scheduler = start_scheduler()
    metrics.start_observer()

    broker_prober.start_prober()

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