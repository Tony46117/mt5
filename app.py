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

from config import CONFIG, setup_logging, validate_config

import front
import database as db
from info import snapshot
from spot import read_spots, feed_age, term_running, read_header, resolve_symbol
from executor import start_scheduler, sender_for
from monitor import read_positions, account_info
import stopexe as stopexe_mod
import metrics
import broker_prober
import account_engine
import news as news_mod

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

@app.get("/factory")
def page_factory():
    return front.render_factory()

@app.get("/api/factory/map")
@rate_limit(max_requests=60, window=60)
def api_factory_map():
    try:
        payload = _factory_map_payload()
    except Exception as exc:
        log.error(f"factory map unavailable: {exc}")
        return _fail(f"factory map unavailable: {exc}", code=503)
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp

@app.get("/api/factory/news")
@rate_limit(max_requests=240, window=60)
def api_factory_news():
    snap = _factory_news_snapshot(force=request.args.get("force") == "1")
    return jsonify({"ok": True, "now_ms": int(time.time() * 1000), **snap})

@app.get("/health")
def health():
    health = {
        "status": "healthy",
        "timestamp": dt.datetime.now().isoformat(),
        "version": "2.0.0",
        # which process answers here - CI's smoke check uses this to prove it
        # is talking to the app it started and not a leftover server
        "pid": os.getpid(),
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
    try:
        from spot import read_pending_orders
        out["pendings"] = read_pending_orders(inst)
    except Exception:
        out["pendings"] = []
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

def _lot_check(account: int, broker_symbol: str, lot: float) -> str | None:
    """Reject lots the broker will refuse, using probed volume limits.

    Returns an error string, or None when the lot is fine (or when no
    probe data exists yet - never block trading on missing metadata).
    DISABLED symbols (broker trade_mode=0, retcode 10017 - e.g. HFM's
    unsuffixed base pairs while only the .Z variants trade) are rejected
    here too, so manual /api/trade and /api/order get the same guard the
    scheduler already had.  Observed 2026-10-13 as repeated 10017s.
    """
    try:
        syms = broker_prober.get_prober().report()["terminals"].get(str(account), {})
        info = syms.get(broker_symbol)
        if info is None:
            # case-insensitive fallback (Deriv "Boom 1000 Index" etc.)
            for k, v in syms.items():
                if str(k).upper() == str(broker_symbol).upper():
                    info = v
                    break
    except Exception:
        return None
    if not info:
        return None
    try:
        if int(info.get("trade_mode", 4)) == 0:
            return (f"{broker_symbol} is DISABLED for trading by the broker "
                    f"on account {account} (trade_mode=0, retcode 10017) - "
                    f"pick the tradeable variant (e.g. {broker_symbol}.Z)")
    except (TypeError, ValueError):
        pass
    try:
        vmin = float(info.get("volume_min", 0) or 0)
        vmax = float(info.get("volume_max", 0) or 0)
        step = float(info.get("volume_step", 0) or 0)
    except (TypeError, ValueError):
        return None
    if vmin > 0 and lot < vmin - 1e-9:
        return (f"lot {lot:g} is below the broker minimum {vmin:g} for "
                f"{broker_symbol} on account {account}")
    if vmax > 0 and lot > vmax + 1e-9:
        return (f"lot {lot:g} is above the broker maximum {vmax:g} for "
                f"{broker_symbol} on account {account}")
    if step > 0:
        steps = round(lot / step)
        if abs(steps * step - lot) > 1e-6:
            near = steps * step
            return (f"lot {lot:g} is not a multiple of the broker step "
                    f"{step:g} for {broker_symbol} - nearest: {near:g}")
    return None

def _fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code

# ---------------- factory HUD (scheduled-trades sub-section) ----------------
# factory.py draws its HUD with tkinter/PIL, which cannot run inside a
# browser (and needs a display).  The web page renders the same HUD
# client-side instead: the static map geometry is exported once here and
# every frame (clock + globe rotation + hover) is drawn on a canvas with
# requestAnimationFrame, so there is no per-frame server round-trip - that
# is the low-latency path.  Only the slowly-changing news feed is polled.
_factory_map_cache: dict | None = None
_factory_map_lock = threading.Lock()
_factory_state_lock = threading.Lock()
_factory_news: dict = {"events": [], "source": "loading", "ts": 0.0,
                       "last_try": 0.0, "busy": False, "error": ""}


def _factory_b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


# The factory page asks for a much denser lattice than factory.py's default
# 0.63deg grid: ~200,000 dots.  Dot count scales as 1/step^2, so 5.3x
# density is step/sqrt(5.3) (~0.274deg, ~200k dots measured).  The dense
# lattice is expensive to rebuild with shapely (~2s), so it is cached as
# raw little-endian arrays.
FACTORY_DOT_MULT = 5.3


def _factory_i16(vals) -> bytes:
    """Scale degrees x100 into little-endian int16 (no numpy)."""
    from array import array
    a = array("h")
    for v in vals:
        a.append(max(-32768, min(32767, int(round(float(v) * 100.0)))))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _factory_dense_dots(fx) -> tuple[bytes, bytes, bytes, int, str]:
    """Build/load the 5.3x dot lattice (~200k dots) -> (lat, lon, ccy, n, source)."""
    import math
    import struct
    base = float(getattr(fx, "DOT_STEP", 0.63))
    step = base / math.sqrt(FACTORY_DOT_MULT)
    cache = os.path.expanduser(
        f"~/.cache/forex_globe_maps/dots_{FACTORY_DOT_MULT:g}x_{step:.4f}.bin")
    try:
        with open(cache, "rb") as f:
            raw = f.read()
        if raw[:8] == b"MT5FD6X1":
            n = struct.unpack_from("<I", raw, 8)[0]
            off = 12
            lat = raw[off:off + 2 * n]
            off += 2 * n
            lon = raw[off:off + 2 * n]
            off += 2 * n
            ccy = raw[off:off + n]
            off += n
            if n > 0 and off == len(raw):
                return lat, lon, ccy, n, \
                    f"NaturalEarth 50m (cached {FACTORY_DOT_MULT:g}x)"
    except OSError:
        pass

    dots, labels, source = fx.build_accurate_dots(step=step)
    lat = _factory_i16(d[0] for d in dots)
    lon = _factory_i16(d[1] for d in dots)
    from array import array
    ccy_arr = array("b")
    for c in labels:
        ccy_arr.append(fx.CCY_LIST.index(c) if c in fx.CCY_LIST else -1)
    ccy = ccy_arr.tobytes()
    n = len(dots)
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"MT5FD6X1" + struct.pack("<I", n) + lat + lon + ccy)
        os.replace(tmp, cache)
    except OSError:
        pass
    return lat, lon, ccy, n, f"{source} \u00b7 {FACTORY_DOT_MULT:g}x dots"


def _factory_map_payload() -> dict:
    """Export factory.py's Natural Earth map as compact typed arrays.

    Int16 arrays are scaled x100 (0.01 deg resolution) and base64'd so
    the 5.3x land lattice (~200k dots) + borders ship as one ~1.4 MB response
    that the browser caches.  Everything is derived from factory's own
    cached map files; no downloads happen here after the first import.
    """
    global _factory_map_cache
    if _factory_map_cache is not None:
        return _factory_map_cache
    with _factory_map_lock:
        if _factory_map_cache is not None:
            return _factory_map_cache
        from array import array
        import factory as fx

        fx._ensure_borders()
        ccy_list = list(fx.CCY_LIST)
        lat, lon, ccy, dot_count, dot_source = _factory_dense_dots(fx)

        borders = []
        for entry in getattr(fx, "BORDERS", []):
            try:
                iso, bla, blo = entry[0], entry[1], entry[2]
            except Exception:
                continue
            if len(bla) < 2 or len(bla) != len(blo):
                continue
            pts = array("h")
            for la_v, lo_v in zip(bla, blo):
                pts.append(max(-32768, min(32767, int(round(float(la_v) * 100.0)))))
                pts.append(max(-32768, min(32767, int(round(float(lo_v) * 100.0)))))
            if sys.byteorder == "big":
                pts.byteswap()
            borders.append({"i": str(iso),
                            "p": _factory_b64(pts.tobytes())})

        countries = []
        for iso, name, geom, bounds in getattr(fx, "FULL_GEO", []):
            try:
                p = geom.representative_point()
                clon, clat = float(p.x), float(p.y)
            except Exception:
                clon = (float(bounds[0]) + float(bounds[2])) / 2.0
                clat = (float(bounds[1]) + float(bounds[3])) / 2.0
            countries.append({
                "i": str(iso), "n": str(name),
                "b": [round(float(bounds[0]), 3), round(float(bounds[1]), 3),
                      round(float(bounds[2]), 3), round(float(bounds[3]), 3)],
                "c": [round(clon, 3), round(clat, 3)],
            })

        centers = {}
        for cur, v in getattr(fx, "COUNTRIES", {}).items():
            try:
                centers[str(cur)] = [float(v[0]), float(v[1]), float(v[2]),
                                     str(v[3])]
            except Exception:
                continue
        extra = {}
        for cur, pts in getattr(fx, "EXTRA_CENTERS", {}).items():
            extra[str(cur)] = [[float(a), float(b), float(r)]
                               for a, b, r in pts]

        _factory_map_cache = {
            "ok": True,
            "ccy": ccy_list,
            "iso2ccy": {str(k): str(v)
                        for k, v in getattr(fx, "ISO_TO_CCY", {}).items()},
            "centers": centers,
            "extra": extra,
            "dots": {"lat": _factory_b64(lat),
                     "lon": _factory_b64(lon),
                     "ccy": _factory_b64(ccy)},
            "dot_count": int(dot_count),
            "borders": borders,
            "countries": countries,
            "source": dot_source,
            "generated": dt.datetime.now(dt.timezone.utc)
                            .isoformat(timespec="seconds"),
        }
        return _factory_map_cache


def _factory_news_worker() -> None:
    rows: list[dict] = []
    source = "unavailable"
    err = ""
    try:
        import factory as fx
        events, source = fx.fetch_ff_events(timeout=6.0)
        for e in events[:800]:
            d = e["dt"]
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            rows.append({
                "dt": d.astimezone(dt.timezone.utc).isoformat(),
                "currency": str(e.get("currency", "?")),
                "impact": str(e.get("impact", "")),
                "title": str(e.get("title", "")),
                "forecast": str(e.get("forecast", "-")),
                "previous": str(e.get("previous", "-")),
            })
    except Exception as exc:
        err = str(exc)
    with _factory_state_lock:
        if rows:
            _factory_news["events"] = rows
            _factory_news["source"] = source
            _factory_news["error"] = ""
        else:
            _factory_news["error"] = err or "feed returned no events"
        _factory_news["ts"] = time.time()
        _factory_news["busy"] = False


def _factory_news_snapshot(force: bool = False) -> dict:
    """Return the cached feed immediately; refresh in the background.

    A ForexFactory fetch can take >10 s on a bad network, so it must never
    block a request (the page polls this every few seconds).
    """
    now = time.time()
    with _factory_state_lock:
        c = _factory_news
        stale = (not c["events"]) or (now - c["ts"] > 300.0)
        if not c["busy"] and (force or stale) and (now - c["last_try"] > 5.0):
            c["busy"] = True
            c["last_try"] = now
            threading.Thread(target=_factory_news_worker, daemon=True,
                             name="factory-news").start()
        return {"events": list(c["events"]), "source": c["source"],
                "age_s": (round(max(0.0, now - c["ts"]), 1)
                          if c["ts"] else None),
                "loading": c["busy"], "error": c["error"]}

# ---------------- news module (release bracket + closer) ----------------
# The panel's NEWS button slides through the week's calendar (the same
# factory feed the Factory page polls) and arms a StopExe straddle 5 s
# before the release.  All timing lives in news.py; these routes only
# validate what the desk picked and hand it over.

@app.get("/api/news/events")
@rate_limit(max_requests=300, window=60)
def api_news_events():
    snap = _factory_news_snapshot(force=request.args.get("force") == "1")
    high_only = str(request.args.get("impact", "high")).lower() != "all"
    try:
        payload = news_mod.week_events(snap["events"], high_only)
    except Exception as exc:
        log.error(f"news week failed: {exc}")
        return _fail(f"news feed unavailable: {exc}", code=503)
    return jsonify({"ok": True, "now_ms": int(time.time() * 1000),
                    "source": snap["source"], "loading": snap["loading"],
                    "error": snap["error"], **payload})


@app.get("/api/news/map")
@rate_limit(max_requests=120, window=60)
def api_news_map():
    """Mini map of the country behind a news currency (SVG path data)."""
    ccy = str(request.args.get("ccy", "")).strip().upper()
    if not ccy or len(ccy) > 8 or not ccy.isalpha():
        return _fail("ccy is required (e.g. USD)")
    try:
        payload = news_mod.country_map(ccy)
    except Exception as exc:
        return _fail(f"map unavailable: {exc}", code=503)
    resp = jsonify({"ok": True, **payload})
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/api/news/quotes")
@rate_limit(max_requests=120, window=60)
def api_news_quotes():
    """Symbols the broker probe says are tradeable AND quoting right now."""
    try:
        account = int(request.args.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    return jsonify({"ok": True, **news_mod.quotes(account)})


@app.get("/api/news/plans")
@rate_limit(max_requests=300, window=60)
def api_news_plans():
    return jsonify({"ok": True, "now_ms": int(time.time() * 1000),
                    "plans": news_mod.plans()})


@app.post("/api/news/plan")
@rate_limit(max_requests=30, window=60)
def api_news_plan():
    """Arm an event: buy stops + sell stops at T-5 s, flat at T+5 s."""
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    symbol = str(d.get("symbol", "")).strip()
    if not symbol:
        return _fail("symbol is required")
    try:
        lot = float(d.get("lot", 0))
        orders = int(d.get("orders", d.get("n", 1)))
        buy = int(d.get("buy_orders", orders))
        sell = int(d.get("sell_orders", orders))
        dpips = float(d.get("distance_pips", 1.0))
    except (TypeError, ValueError):
        return _fail("bad lot / orders / distance")
    if not 0 < lot <= 100:
        return _fail("lot out of range")
    if not 1 <= orders <= 50:
        return _fail("orders must be 1..50")
    if not 0 <= buy <= 50 or not 0 <= sell <= 50:
        return _fail("buy/sell orders must be 0..50 each")
    if buy + sell <= 0:
        return _fail("at least one buy or sell stop is required")
    if not 0.5 <= dpips <= 100:
        return _fail("distance must be 0.5..100 pips")
    event = d.get("event") or {}
    if not isinstance(event, dict) or not event:
        return _fail("event is required (pick one from the calendar)")
    broker_sym = resolve_symbol(symbol, account)
    spots = read_spots()
    if broker_sym not in spots or not spots[broker_sym][0]:
        return _fail("no live quote for " + symbol)
    try:
        _sym = broker_prober.get_prober().report()["terminals"] \
            .get(str(account), {}).get(broker_sym)
        if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
            return _fail(f"{symbol} is DISABLED for trading by the broker "
                         f"on account {account} (trade_mode=0, retcode 10017)")
    except Exception:
        pass
    lot_err = _lot_check(account, broker_sym, lot)
    if lot_err:
        return _fail(lot_err)
    try:
        plan = news_mod.plan_event(account, symbol, lot, orders, event,
                                   bool(d.get("closer")), dpips,
                                   buy_orders=buy, sell_orders=sell)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    at = dt.datetime.fromtimestamp(plan["event_ms"] / 1000.0,
                                   dt.timezone.utc)
    return _ok({"plan": plan,
                "detail": f"news plan #{plan['id']} armed for {plan['symbol']} "
                          f"{plan['orders']}x{plan['lot']:g} at "
                          f"{at:%H:%M:%S} UTC (T-5 s)"
                          + (" - closer ON at T+5 s" if plan["closer"] else "")})


@app.post("/api/news/cancel")
@rate_limit(max_requests=60, window=60)
def api_news_cancel():
    d = request.get_json(silent=True) or {}
    pid = str(d.get("id", "")).strip()
    if not pid:
        return _fail("id is required")
    try:
        plan = news_mod.cancel(pid)
    except ValueError as exc:
        return _fail(str(exc))
    return _ok({"plan": plan, "detail": f"news plan #{pid} cancelled"})


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

@app.get("/api/engine/servers")
@rate_limit(max_requests=60, window=60)
def api_engine_servers():
    """Trade servers MT5 can log into by name, plus every access point."""
    try:
        return jsonify(account_engine.get_engine().servers())
    except Exception as exc:
        return _fail(str(exc), code=500)

@app.post("/api/engine/access-point")
@rate_limit(max_requests=20, window=60)
def api_engine_access_point():
    """Remember an access point (host:port) for a trade server.

    The one-step fix for a broker MT5 does not list yet: MT5 only dials a
    server it already knows, so a name it has never seen needs host:port
    once - after that the terminal learns the server and name logins work.
    """
    d = request.get_json(silent=True) or {}
    server = str(d.get("server", "")).strip()
    address = str(d.get("access_point", "") or d.get("address", "")).strip()
    if not server or not address:
        return _fail("server and access_point (host:port) are required")
    try:
        res = account_engine.get_engine().set_access_point(server, address)
    except account_engine.EngineError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok({"server": res["server"],
                "access_point": res["access_point"],
                "detail": f"access point saved for {res['server']} - log in "
                          f"again and it will be used"})

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
        return jsonify({"ok": True, "ts": snap["ts"], "accounts": snap["accounts"],
                        "system": snap["system"],
                        "spots": {k: [b, a] for k, (b, a, _t) in snap["spots"].items() if b}})
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
    broker_sym = resolve_symbol(symbol, account)
    if broker_sym not in spots or not spots[broker_sym][0]:
        return _fail("no live quote for " + symbol)
    lot_err = _lot_check(account, broker_sym, lot)
    if lot_err:
        return _fail(lot_err)
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
    commands = [("OPEN", broker_sym, side, f"{lot:.2f}", "0", "0",
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
    broker_sym = resolve_symbol(symbol, account)
    if broker_sym not in spots or not spots[broker_sym][0]:
        return _fail("no live quote for " + symbol)
    lot_err = _lot_check(account, broker_sym, lot)
    if lot_err:
        return _fail(lot_err)
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
    bid = float(spots[broker_sym][0])
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
    p = ptype.upper().replace(" ", "").replace("_", "")
    commands = [("PENDING", broker_sym, p, f"{lot:.2f}", f"{price:.8f}",
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

@app.get("/api/stopexe/preview")
@rate_limit(max_requests=300, window=60)
def api_stopexe_preview():
    """Bracket prices for a StopExe straddle - same math EXECUTE uses."""
    try:
        account = int(request.args.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    symbol = str(request.args.get("symbol", "")).strip()
    if not symbol:
        return _fail("symbol is required")
    try:
        dpips = float(request.args.get("pips",
                                       request.args.get("distance_pips", 1.0)))
    except (TypeError, ValueError):
        return _fail("bad distance")
    if not 0.5 <= dpips <= 100:
        return _fail("distance must be 0.5..100 pips")
    try:
        slpips = float(request.args.get("sl_pips", request.args.get(
            "sl", stopexe_mod.STOPEXE_SL_PIPS)))
    except (TypeError, ValueError):
        return _fail("bad sl")
    if not 0.5 <= slpips <= 100:
        return _fail("sl must be 0.5..100 pips")
    try:
        lv = stopexe_mod.stopexe_levels(symbol, account, dpips, slpips)
    except ValueError as exc:
        return _fail(str(exc))
    return _ok(lv)


@app.post("/api/stopexe")
@rate_limit(max_requests=60, window=60)
def api_stopexe():
    """StopExe: open a BUY STOP (ask + 1.0 pips) + SELL STOP (bid - 1.0
    pips) straddle of `lot` around the live price - 2.0 pips bracket total
    (gold at 4150 -> 4151.0 / 4149.0), both legs flighted async in one exec
    command, each leg carrying its 1.2-pip protective SL."""
    d = request.get_json(silent=True) or {}
    try:
        account = int(d.get("account", 0))
    except (TypeError, ValueError):
        return _fail("bad account")
    if account not in (1, 2):
        return _fail("account must be 1 or 2")
    symbol = str(d.get("symbol", "")).strip()
    if not symbol:
        return _fail("symbol is required")
    try:
        lot = float(d.get("lot", 0))
    except (TypeError, ValueError):
        return _fail("bad lot")
    if lot <= 0 or lot > 100:
        return _fail("lot out of range")
    try:
        dpips = float(d.get("distance_pips", d.get("pips", 1.0)))
    except (TypeError, ValueError):
        return _fail("bad distance")
    if not 0.5 <= dpips <= 100:
        return _fail("distance must be 0.5..100 pips")
    try:
        slpips = float(d.get("sl_pips", d.get("sl",
                                              stopexe_mod.STOPEXE_SL_PIPS)))
    except (TypeError, ValueError):
        return _fail("bad sl")
    if not 0.5 <= slpips <= 100:
        return _fail("sl must be 0.5..100 pips")
    try:
        lv = stopexe_mod.stopexe_levels(symbol, account, dpips, slpips)
    except ValueError as exc:
        return _fail(str(exc))
    try:
        _sym = broker_prober.get_prober().report()["terminals"] \
            .get(str(account), {}).get(lv["broker_symbol"])
        if _sym is None:
            for _k, _v in broker_prober.get_prober().report()[
                    "terminals"].get(str(account), {}).items():
                if str(_k).upper() == str(lv["broker_symbol"]).upper():
                    _sym = _v
                    break
        if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
            return _fail(f"{symbol} is DISABLED for trading by the broker "
                         f"on account {account} (trade_mode=0, retcode 10017)")
    except Exception:
        pass
    lot_err = _lot_check(account, lv["broker_symbol"], lot)
    if lot_err:
        return _fail(lot_err)
    try:
        res = stopexe_mod.place_stopexe(account, symbol, lot, dpips, slpips)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc), code=500)
    return _ok(res)


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
    broker_pair = resolve_symbol(pair, account)
    try:
        _sym = broker_prober.get_prober().report()["terminals"].get(str(account), {}).get(broker_pair)
        if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
            return _fail(f"{pair} is DISABLED for trading by the broker on "
                         f"account {account} (trade_mode=0, retcode 10017) - "
                         f"pick a tradeable symbol (e.g. XAUUSD)")
    except Exception:
        pass
    lot_err = _lot_check(account, broker_pair, lot)
    if lot_err:
        return _fail(lot_err)
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
            _rsym = resolve_symbol(updates["pair"], int(cur["account"]))
            _sym = broker_prober.get_prober().report()["terminals"] \
                .get(str(cur["account"]), {}).get(_rsym)
            if _sym is not None and int(_sym.get("trade_mode", 4)) == 0:
                return _fail(f"{updates['pair']} is DISABLED for trading by "
                             f"the broker on account {cur['account']} "
                             f"(trade_mode=0, retcode 10017)")
        except Exception:
            pass
    _flot = updates.get("lot", cur["lot"])
    _fpair = resolve_symbol(updates.get("pair", cur["pair"]),
                            int(cur["account"]))
    _lerr = _lot_check(int(cur["account"]), _fpair, float(_flot))
    if _lerr:
        return _fail(_lerr)
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
    # re-arm news brackets saved before a restart - a release 20 minutes
    # out must not be dropped because the app bounced
    news_mod.start()

    def _shutdown(*_):
        log.info("shutting down - stopping scheduler + metrics observer...")
        if _scheduler:
            _scheduler.stop_flag.set()
        metrics.stop_observer()
        broker_prober.stop_prober()
        news_mod.stop()
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