#!/usr/bin/env python3.12

"""News module - bracket an economic release, then get flat again.

The panel's NEWS panel slides through the next week of events (the feed is
factory.py's ForexFactory calendar, high impact by default with an opt-out
toggle).  Choosing an event and pressing TRADE arms the chosen number of
buy stops and sell stops 5 s BEFORE the release; with the closer
toggle on, every position on that account is closed 5 s AFTER the release,
so the whole execution window is 10 s wide (T-5 .. T+5).

Timing is the whole point of this module, so the work is done by one thread
per plan: it sleeps to the deadline on the wall clock (fine-grained sleep
in the last few ms, no coarse polling), fires the straddle stack in ONE
batch (stopexe.place_stopexe_multi), and closes through close.py's fast
path - CLOSEALL ack, no 15 s book verification - so the fill and the exit
land inside their seconds instead of drifting.

Plans are persisted to news_plans.json and re-armed on boot: a restart
before the release must not silently drop the trade.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid
from pathlib import Path

from config import setup_logging

log = setup_logging(__name__)

# Open 5 s before the release, close 5 s after it -> a 10 s execution gap.
OPEN_LEAD_S = 5.0
CLOSE_LAG_S = 5.0
WEEK_S = 7 * 86400
HOURS_PAST = 1.0
PLAN_FILE = Path(__file__).resolve().parent / "news_plans.json"

# Currencies -> Natural Earth ISO A3 codes, used by the mini map.  The nine
# watchlist currencies come from factory; the rest cover releases the
# calendar also carries (TRY, ZAR, MXN, ...) so their map still draws.
EXTRA_CCY_ISO: dict[str, list[str]] = {
    "TRY": ["TUR"], "ZAR": ["ZAF"], "MXN": ["MEX"], "BRL": ["BRA"],
    "INR": ["IND"], "KRW": ["KOR"], "SEK": ["SWE"], "NOK": ["NOR"],
    "DKK": ["DNK"], "PLN": ["POL"], "HUF": ["HUN"], "CZK": ["CZE"],
    "SGD": ["SGP"], "HKD": ["HKG"], "IDR": ["IDN"], "MYR": ["MYS"],
    "PHP": ["PHL"], "THB": ["THA"], "ILS": ["ISR"], "SAR": ["SAU"],
    "AED": ["ARE"], "EGP": ["EGY"], "NGN": ["NGA"], "KES": ["KEN"],
    "RON": ["ROU"], "RUB": ["RUS"], "UAH": ["UKR"], "CLP": ["CHL"],
    "COP": ["COL"], "ARS": ["ARG"], "PEN": ["PER"], "VND": ["VNM"],
    "TWD": ["TWN"], "PKR": ["PAK"], "BDT": ["BGD"], "LKR": ["LKA"],
}

COUNTRY_NAME: dict[str, str] = {
    "USD": "UNITED STATES", "EUR": "EUROZONE", "GBP": "UNITED KINGDOM",
    "JPY": "JAPAN", "CHF": "SWITZERLAND", "CAD": "CANADA",
    "AUD": "AUSTRALIA", "NZD": "NEW ZEALAND", "CNY": "CHINA",
    "TRY": "TURKEY", "ZAR": "SOUTH AFRICA", "MXN": "MEXICO",
    "BRL": "BRAZIL", "INR": "INDIA", "KRW": "SOUTH KOREA",
    "SEK": "SWEDEN", "NOK": "NORWAY", "DKK": "DENMARK",
    "PLN": "POLAND", "HUF": "HUNGARY", "CZK": "CZECHIA",
    "SGD": "SINGAPORE", "HKD": "HONG KONG", "IDR": "INDONESIA",
    "MYR": "MALAYSIA", "PHP": "PHILIPPINES", "THB": "THAILAND",
    "ILS": "ISRAEL", "SAR": "SAUDI ARABIA", "AED": "UAE",
    "EGP": "EGYPT", "NGN": "NIGERIA", "KES": "KENYA", "RON": "ROMANIA",
    "RUB": "RUSSIA", "UAH": "UKRAINE", "CLP": "CHILE", "COP": "COLOMBIA",
    "ARS": "ARGENTINA", "PEN": "PERU", "VND": "VIETNAM", "TWD": "TAIWAN",
    "PKR": "PAKISTAN", "BDT": "BANGLADESH", "LKR": "SRI LANKA",
}


# ---------------------------------------------------------------- feed ----

def _event_ms(raw: str | int | float | dt.datetime | None) -> int | None:
    """Any of the feed's datetime shapes -> epoch milliseconds (UTC)."""
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        d = raw
    elif isinstance(raw, (int, float)):
        v = float(raw)
        return int(v * 1000 if v < 1e11 else v)      # seconds vs millis
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def ccy_iso(ccy: str) -> list[str]:
    """ISO A3 codes for a news currency ([] when the map has no shape)."""
    code = str(ccy or "").upper()
    if code in EXTRA_CCY_ISO:
        return list(EXTRA_CCY_ISO[code])
    try:
        import factory as fx
        return list(getattr(fx, "WATCH_ISO", {}).get(code, []))
    except Exception:
        return []


def country_name(ccy: str) -> str:
    code = str(ccy or "").upper()
    if code in COUNTRY_NAME:
        return COUNTRY_NAME[code]
    codes = ccy_iso(code)
    return codes[0] if codes else ""


def week_events(events: list[dict], high_only: bool = True,
                now: float | None = None) -> dict:
    """Events in a rolling week (1 h back .. 7 d ahead), high impact first.

    `high_only` is the panel's filter toggle: on = only High impact, off =
    every impact so medium/low releases can be traded too.
    """
    now = time.time() if now is None else float(now)
    start_ms = int((now - HOURS_PAST * 3600.0) * 1000)
    end_ms = int((now + WEEK_S) * 1000)
    rows: list[dict] = []
    for e in events or []:
        ms = _event_ms(e.get("dt"))
        if ms is None or not (start_ms <= ms <= end_ms):
            continue
        impact = str(e.get("impact", "") or "").strip()
        if high_only and impact.lower() != "high":
            continue
        ccy = str(e.get("currency", "?") or "?").upper()
        rows.append({
            "dt_ms": ms,
            "dt": dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc)
                    .isoformat(timespec="seconds"),
            "currency": ccy,
            "impact": impact or "-",
            "title": str(e.get("title", "") or ""),
            "forecast": str(e.get("forecast", "-") or "-"),
            "previous": str(e.get("previous", "-") or "-"),
            "country": country_name(ccy),
            "has_map": bool(ccy_iso(ccy)),
            "in_seconds": round(ms / 1000.0 - now, 1),
        })
    rows.sort(key=lambda r: r["dt_ms"])
    return {
        "events": rows,
        "count": len(rows),
        "high_only": bool(high_only),
        "week_start_ms": start_ms,
        "week_end_ms": end_ms,
        "open_lead_s": OPEN_LEAD_S,
        "close_lag_s": CLOSE_LAG_S,
    }


# ------------------------------------------------------------ mini map ----

_map_cache: dict[str, dict] = {}
_map_lock = threading.Lock()


def _fmt(v: float) -> str:
    """Compact fixed-point for path data (1 decimal is plenty at 100 px)."""
    return f"{v:.1f}".rstrip("0").rstrip(".")


def country_map(ccy: str) -> dict:
    """Tiny SVG path for `ccy`'s country (Eurozone = its member states).

    Borders come from factory.py's cached Natural Earth geometry - the same
    public-domain survey outlines as the globe - projected into a 100x100
    box with a cos(lat) x-scale so the shapes are not stretched.  The result
    is cached per currency and shipped with a long browser cache.
    """
    code = str(ccy or "").upper()
    with _map_lock:
        cached = _map_cache.get(code)
    if cached is not None:
        return cached
    isos = set(ccy_iso(code))
    result: dict = {"ccy": code, "name": country_name(code),
                    "iso": sorted(isos), "d": "", "w": 100, "h": 100}
    if not isos:
        return result
    try:
        import math
        import factory as fx
        fx._ensure_borders()
        rings: list[list[tuple[float, float]]] = []
        for entry in getattr(fx, "BORDERS", []):
            try:
                iso, la, lo = entry[0], entry[1], entry[2]
            except Exception:
                continue
            if iso not in isos:
                continue
            pts = [(float(x), float(y)) for x, y in zip(lo, la)]
            if len(pts) >= 2:
                rings.append(pts)
        if not rings:
            return result
        lons = [p[0] for r in rings for p in r]
        lats = [p[1] for r in rings for p in r]
        lon0, lon1 = min(lons), max(lons)
        lat0, lat1 = min(lats), max(lats)
        mid = (lat0 + lat1) / 2.0
        kx = max(0.15, math.cos(math.radians(mid)))
        span_x = max(1e-6, (lon1 - lon0) * kx)
        span_y = max(1e-6, lat1 - lat0)
        scale = 92.0 / max(span_x, span_y)          # 4 px padding each side
        off_x = 50.0 - (lon0 + (lon1 - lon0) / 2.0) * kx * scale
        off_y = 50.0 + (lat0 + (lat1 - lat0) / 2.0) * scale

        def project(lon: float, lat: float) -> tuple[float, float]:
            return (lon * kx * scale + off_x, off_y - lat * scale)

        parts: list[str] = []
        for ring in rings:
            xy = [project(lon, lat) for lon, lat in ring]
            # Drop points closer than 0.4 px to keep the payload small; a
            # 100 px thumbnail cannot show more anyway.
            keep = [xy[0]]
            for p in xy[1:]:
                if abs(p[0] - keep[-1][0]) + abs(p[1] - keep[-1][1]) >= 0.4:
                    keep.append(p)
            if len(keep) < 2:
                continue
            parts.append("M" + " L".join(f"{_fmt(x)},{_fmt(y)}"
                                         for x, y in keep) + "Z")
        result["d"] = " ".join(parts)
        result["bbox"] = [round(lon0, 2), round(lat0, 2),
                          round(lon1, 2), round(lat1, 2)]
    except Exception as exc:
        log.warning(f"news map for {code} unavailable: {exc}")
        return result
    with _map_lock:
        _map_cache[code] = result
    return result


# --------------------------------------------------------------- quotes ----

_probe_kick: dict[int, float] = {}


def _digits_of(price: str) -> int:
    try:
        return max(0, len(str(price).split(".")[1]))
    except (IndexError, ValueError):
        return 5


def quotes(account: int) -> dict:
    """Symbols the broker probe says are tradeable AND quoting right now.

    The news panel picks its pair from this list, so what it offers is what
    the terminal can actually fill (trade_mode and volume limits included),
    never a hand-typed symbol the broker does not quote.  While the probe is
    still warming up (first pass after boot) the live quote feed stands in,
    flagged `probed: false`, and a probe of that terminal is kicked off - the
    panel must never open empty just because the catalogue is not ready.
    """
    syms: dict[str, dict] = {}
    probed_at = None
    try:
        from broker_prober import get_prober
        prober = get_prober()
        rep = prober.report()
        syms = rep.get("terminals", {}).get(str(account), {}) or {}
        probed_at = rep.get("probed_at", {}).get(str(account))
    except Exception as exc:
        return {"account": account, "symbols": [], "source": "unavailable",
                "error": str(exc), "probed_at": None}
    try:
        from spot import read_spots
        spots = read_spots()
    except Exception:
        spots = {}
    upper = {str(k).upper(): v for k, v in spots.items()}
    rows: list[dict] = []
    for sym, info in syms.items():
        info = info or {}
        try:
            if int(info.get("trade_mode", 4)) == 0:
                continue
        except (TypeError, ValueError):
            pass
        row = spots.get(sym) or upper.get(str(sym).upper())
        if not row or not row[0]:
            continue
        try:
            bid = float(row[0])
        except (TypeError, ValueError):
            continue
        rows.append({
            "symbol": str(sym),
            "digits": info.get("digits"),
            "bid": bid,
            "ask": float(row[1]) if row[1] else None,
            "volume_min": info.get("volume_min"),
            "volume_max": info.get("volume_max"),
            "volume_step": info.get("volume_step"),
            "spread_pts": info.get("spread_pts"),
            "probed": True,
        })
    source = "broker probe"
    if not rows:
        source = "live quotes · probe warming up"
        for sym, row in spots.items():
            if not row or not row[0]:
                continue
            try:
                bid = float(row[0])
            except (TypeError, ValueError):
                continue
            rows.append({
                "symbol": str(sym),
                "digits": _digits_of(row[0]),
                "bid": bid,
                "ask": float(row[1]) if row[1] else None,
                "volume_min": None, "volume_max": None,
                "volume_step": None, "spread_pts": None,
                "probed": False,
            })
        now = time.time()
        if now - _probe_kick.get(account, 0.0) > 60.0:
            _probe_kick[account] = now
            try:
                threading.Thread(target=prober.probe_terminal,
                                 args=(int(account),), daemon=True,
                                 name=f"news-probe-{account}").start()
            except Exception:
                pass
    rows.sort(key=lambda r: r["symbol"])
    return {"account": int(account), "symbols": rows,
            "source": source, "probed_at": probed_at}


# --------------------------------------------------------------- plans ----

_plans: dict[str, dict] = {}
_threads: dict[str, threading.Thread] = {}
_cancels: dict[str, threading.Event] = {}
_plans_lock = threading.Lock()
_loaded = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sleep_until(ts: float, cancel: threading.Event | None = None) -> None:
    """Wall-clock sleep with millisecond accuracy in the last stretch."""
    while True:
        rem = ts - time.time()
        if rem <= 0:
            return
        if cancel is not None and cancel.is_set():
            return
        if rem > 0.25:
            time.sleep(min(rem - 0.05, 0.2))
        elif rem > 0.005:
            time.sleep(rem - 0.004)
        elif rem > 0.001:
            time.sleep(0.001)
        else:
            time.sleep(0)


def _public(plan: dict) -> dict:
    return {
        "id": plan["id"],
        "account": plan["account"],
        "symbol": plan["symbol"],
        "lot": plan["lot"],
        "orders": plan["orders"],
        "buy_orders": plan.get("buy_orders", plan["orders"]),
        "sell_orders": plan.get("sell_orders", plan["orders"]),
        "closer": plan["closer"],
        "distance_pips": plan.get("distance_pips", 1.0),
        "sl_pips": plan.get("sl_pips"),
        "event_ms": plan["event_ms"],
        "event": plan["event"],
        "open_ms": plan["open_ms"],
        "close_ms": plan["close_ms"],
        "status": plan.get("status", "scheduled"),
        "detail": plan.get("detail", ""),
        "created_ms": plan.get("created_ms"),
        "fired_ms": plan.get("fired_ms"),
        "closed_ms": plan.get("closed_ms"),
        "exec_ms": plan.get("exec_ms"),
        "close_ms_taken": plan.get("close_taken_ms"),
        "buy_price": plan.get("buy_price"),
        "sell_price": plan.get("sell_price"),
        "buy_sl": plan.get("buy_sl"),
        "sell_sl": plan.get("sell_sl"),
        "tickets": plan.get("tickets", []),
    }


def _update(pid: str, **fields) -> dict | None:
    with _plans_lock:
        plan = _plans.get(pid)
        if plan is None:
            return None
        plan.update(fields)
        snap = dict(plan)
    _save()
    return snap


def _save() -> None:
    """Persist plans that still need an action (never the threads).

    A fired plan without a closer is FINISHED ("placed") and must not be
    written back: re-arming it after a restart would arm a second straddle
    for a release that already happened.
    """
    with _plans_lock:
        rows = [{k: v for k, v in p.items() if k != "event_raw"}
                for p in _plans.values()
                if p.get("status") in ("scheduled", "arming", "armed",
                                       "closing")]
    try:
        tmp = PLAN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        tmp.replace(PLAN_FILE)
    except OSError as exc:
        log.warning(f"news: could not persist plans: {exc}")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for row in raw if isinstance(raw, list) else []:
        try:
            pid = str(row["id"])
            row["event"] = dict(row.get("event") or {})
            row.setdefault("status", "scheduled")
            row.setdefault("buy_orders", row.get("orders", 1))
            row.setdefault("sell_orders", row.get("orders", 1))
            with _plans_lock:
                _plans[pid] = row
        except Exception:
            continue


def _arm(pid: str) -> None:
    cancel = threading.Event()
    thread = threading.Thread(target=_run, args=(pid, cancel), daemon=True,
                              name=f"news-plan-{pid}")
    with _plans_lock:
        _cancels[pid] = cancel
        _threads[pid] = thread
    thread.start()


def _run(pid: str, cancel: threading.Event) -> None:
    with _plans_lock:
        plan = dict(_plans.get(pid) or {})
    if not plan:
        return
    event_ts = plan["event_ms"] / 1000.0
    open_at = event_ts - OPEN_LEAD_S
    try:
        if plan.get("fired_ms"):
            # Restarted after the straddle went in: never fire it twice -
            # resume straight at the closer (if this plan has one).
            out = {"detail": plan.get("detail", ""),
                   "ms": plan.get("exec_ms") or 0.0,
                   "buy_price": plan.get("buy_price"),
                   "sell_price": plan.get("sell_price"),
                   "buy_sl": plan.get("buy_sl"),
                   "sell_sl": plan.get("sell_sl"),
                   "tickets": plan.get("tickets", [])}
        else:
            _sleep_until(open_at, cancel)
            if cancel.is_set():
                return
            _update(pid, status="arming", armed_ms=_now_ms())
            out = _fire(pid)
            if out is None:
                return
        if not plan["closer"]:
            _update(pid, status="placed", detail=out["detail"],
                    exec_ms=out["ms"], buy_price=out["buy_price"],
                    sell_price=out["sell_price"], buy_sl=out["buy_sl"],
                    sell_sl=out["sell_sl"], tickets=out["tickets"],
                    fired_ms=_now_ms())
            return
        _update(pid, status="armed", detail=out["detail"],
                exec_ms=out["ms"], buy_price=out["buy_price"],
                sell_price=out["sell_price"], buy_sl=out["buy_sl"],
                sell_sl=out["sell_sl"], tickets=out["tickets"],
                fired_ms=_now_ms())
        _sleep_until(event_ts + CLOSE_LAG_S, cancel)
        if cancel.is_set():
            return
        _update(pid, status="closing", clip_ms=_now_ms())
        close_ms, detail = _close(plan["account"])
        ok = "closed" if not str(detail).lower().startswith("failed") else "close-failed"
        _update(pid, status=ok, detail=f"{out['detail']} | closer: {detail}",
                closed_ms=_now_ms(), close_taken_ms=close_ms)
    except Exception as exc:                 # a plan must never kill the app
        log.exception(f"news plan {pid} failed: {exc}")
        _update(pid, status="failed", detail=str(exc))


def _fire(pid: str) -> dict | None:
    with _plans_lock:
        plan = dict(_plans.get(pid) or {})
    if not plan:
        return None
    import stopexe
    from config import CONFIG

    with _plans_lock:
        _plans[pid]["status"] = "firing"
    try:
        out = stopexe.place_stopexe_counts(
            int(plan["account"]), plan["symbol"], float(plan["lot"]),
            int(plan.get("buy_orders", plan["orders"])),
            int(plan.get("sell_orders", plan["orders"])),
            float(plan.get("distance_pips", 1.0)),
            float(plan.get("sl_pips", stopexe.STOPEXE_SL_PIPS)),
            timeout=CONFIG.exec_timeout_seconds)
    except Exception as exc:
        _update(pid, status="failed", detail=f"STOPEXE rejected - {exc}")
        return None
    log.info(f"news plan {pid}: {out['detail']} [{out['ms']} ms]")
    return {"detail": out["detail"], "ms": out["ms"],
            "buy_price": out["buy_price"], "sell_price": out["sell_price"],
            "buy_sl": out["buy_sl"], "sell_sl": out["sell_sl"],
            "tickets": out.get("tickets", [])}


def _close(account: int) -> tuple[float, str]:
    """Close ALL positions on `account` through close.py's fast path."""
    import close as closer

    t0 = time.perf_counter()
    try:
        res = closer.close_all_fast(None, accounts=[account])
    except Exception as exc:
        return (time.perf_counter() - t0) * 1000.0, f"failed: {exc}"
    row = res["accounts"].get(account) or {"ok": False,
                                           "detail": "no result"}
    mark = "ok" if row.get("ok") else "failed"
    return (time.perf_counter() - t0) * 1000.0, f"{mark}: {row.get('detail')}"


def plan_event(account: int, symbol: str, lot: float, orders: int,
               event: dict, closer: bool,
               distance_pips: float = 1.0,
               sl_pips: float | None = None,
               buy_orders: int | None = None,
               sell_orders: int | None = None) -> dict:
    """Arm one news event: straddle at T-5 s, optional flat at T+5 s.

    `event` is the calendar row the panel selected (dt_ms/dt, title,
    currency, impact); it is copied into the plan so a later feed refresh
    cannot change what was traded.  `orders` is the legacy per-side count;
    `buy_orders`/`sell_orders` let the panel size each side separately.
    """
    _load()
    account = int(account)
    if account not in (1, 2):
        raise ValueError("account must be 1 or 2")
    symbol = str(symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    lot = float(lot)
    if not 0 < lot <= 100:
        raise ValueError("lot out of range")
    orders = int(orders)
    if not 1 <= orders <= 50:
        raise ValueError("orders must be 1..50")
    buy = int(orders if buy_orders is None else buy_orders)
    sell = int(orders if sell_orders is None else sell_orders)
    if not 0 <= buy <= 50 or not 0 <= sell <= 50:
        raise ValueError("buy/sell orders must be 0..50 each")
    if buy + sell <= 0:
        raise ValueError("at least one buy or sell stop is required")
    ms = _event_ms(event.get("dt_ms", event.get("dt")))
    if ms is None:
        raise ValueError("event needs a release time")
    now = _now_ms()
    if ms <= now - int(CLOSE_LAG_S * 1000):
        raise ValueError("that release is already over - slide to the next one")
    import stopexe
    sl = float(stopexe.STOPEXE_SL_PIPS if sl_pips is None else sl_pips)
    pid = uuid.uuid4().hex[:8]
    plan = {
        "id": pid,
        "account": account,
        "symbol": symbol,
        "lot": lot,
        "orders": max(buy, sell),
        "buy_orders": buy,
        "sell_orders": sell,
        "closer": bool(closer),
        "distance_pips": float(distance_pips),
        "sl_pips": sl,
        "event_ms": ms,
        "event": {
            "dt": str(event.get("dt", "")),
            "currency": str(event.get("currency", "")),
            "impact": str(event.get("impact", "")),
            "title": str(event.get("title", "")),
            "forecast": str(event.get("forecast", "-")),
            "previous": str(event.get("previous", "-")),
            "country": str(event.get("country", "")),
        },
        "open_ms": ms - int(OPEN_LEAD_S * 1000),
        "close_ms": (ms + int(CLOSE_LAG_S * 1000)) if closer else None,
        "status": "scheduled",
        "detail": "",
        "created_ms": now,
        "fired_ms": None,
        "closed_ms": None,
        "exec_ms": None,
        "close_taken_ms": None,
        "tickets": [],
    }
    with _plans_lock:
        _plans[pid] = plan
    _save()
    _arm(pid)
    return _public(plan)


def cancel(pid: str) -> dict:
    """Cancel an armed plan.  A plan already firing is left to finish."""
    with _plans_lock:
        plan = _plans.get(str(pid))
        if plan is None:
            raise ValueError("no such news plan")
        status = plan.get("status", "scheduled")
        if status in ("fired", "firing", "closing", "closed", "placed"):
            raise ValueError(f"plan {pid} already {status} - nothing to cancel")
        plan["status"] = "cancelled"
    ev = _cancels.get(str(pid))
    if ev is not None:
        ev.set()
    _save()
    return _public(plan)


def plans() -> list[dict]:
    _load()
    with _plans_lock:
        rows = [_public(p) for p in _plans.values()]
    rows.sort(key=lambda r: r["event_ms"])
    return rows


def start() -> None:
    """Re-arm persisted plans (called once from app.main)."""
    _load()
    now = _now_ms()
    armed = 0
    with _plans_lock:
        pending = [dict(p) for p in _plans.values()
                   if p.get("status") in ("scheduled", "arming", "armed",
                                          "closing")]
    for plan in pending:
        pid = plan["id"]
        if plan["event_ms"] + int(CLOSE_LAG_S * 1000) <= now:
            _update(pid, status="expired",
                    detail="release passed while the app was down")
            continue
        _arm(pid)
        armed += 1
    if armed:
        log.info(f"news: re-armed {armed} saved news plan(s)")


def stop() -> None:
    for ev in list(_cancels.values()):
        ev.set()
