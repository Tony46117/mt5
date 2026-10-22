#!/usr/bin/env python3.12

"""StopExe - stop-order execution framework (straddle stops).

One EXECUTE opens a two-leg straddle around the live price:
BUY STOP  at ask + 0.5 pips  with SL at entry - 0.5 pips

     SELL STOP at bid - 0.5 pips  with SL at entry + 0.5 pips   (1-pip bracket)

Both distances are widened automatically when the broker's stops/freeze
level for that symbol demands it (e.g. HFM NZDUSD -> 3.1 pips).  The SL
is also floored just beyond the live bid/ask spread (plus an optional
`spread_cushion_pips`), so a tight stop cannot start already violated when
the stop order triggers.  The cushion is adjustable per request and via
MT5_STOPEXE_SPREAD_CUSHION_PIPS.

Both legs travel in ONE exec command (EA v1.80 STOPEXESL): one file, one
pass, both legs flighted async in the SAME tick with the SL attached,
one ack - either both legs rest on the broker together or neither
stands (a half straddle is pulled).  The SL rides on the same
OrderSend, so protection is concurrent with the fill - one broker
round trip total, no unprotected window.  Terminals still on an older
EA (no STOPEXESL command) fall back to two PENDINGs with SLs in one
batch (same single round trip).

`place_stopexe_multi` arms N straddles (N buy stops + N sell stops) in
ONE `send_batch`: all command files land before the EA's next drain, so
the whole stack is flighted in a single round trip instead of N serial
ones.  `place_stopexe_counts` accepts separate buy/sell counts: equal
counts use that straddle path, unequal counts use one PENDING batch with
the same SLs - that is what the news module uses to bracket a release
5 s out.  The audit rows are written on a daemon thread so logging never
sits on the execution path.

Pip size follows the quote itself:

    * XAU* / GOLD  -> 1.0 per pip, so gold at 4150 straddles
      4151.6 / 4148.4 exactly like the desk expects
    * XAG* / SILVER -> 0.1 per pip (5 pips = 0.50)
    * 5- and 3-digit forex -> 10 * point (0.0001 / 0.01)
    * everything else -> 1 * point

The bracket (and its protective SL) is never tighter than the broker's
own SYMBOL_TRADE_STOPS_LEVEL / SYMBOL_TRADE_FREEZE_LEVEL.  Those levels
are read per account and symbol from the terminal catalogue, converted
from points to pips, and given one point of cushion: HFM's NZDUSD (30
points = 3.0 pips) therefore arms at 3.1, while FXPro's zero-level pairs
keep the desk's 0.5-pip bracket.  No symbol-name hardcoding.

Digits come from the broker probe when available, else from the live
price string (a quoted price always carries its own precision).
"""

from __future__ import annotations

import os
import threading
import time

from config import setup_logging

log = setup_logging(__name__)

STOPEXE_MAGIC = 777003
STOPEXE_COMMENT = "stopexe"
# The EA keeps MAX_STOPEXE (8) async straddles in flight and answers
# "stopexe busy - retry" beyond that, so bigger stacks go out in chunks: each
# chunk is acked before the next is written, which means the slots are free.
STOPEXE_SLOTS = 8
# Protective stop attached to every straddle leg at OrderSend time.
STOPEXE_SL_PIPS = 0.5
# Default offset from the live price to each stop entry (one pip total).
STOPEXE_DISTANCE_PIPS = 0.5
# Extra clearance past the live spread added to the SL floor.  Zero still
# keeps one point of clearance, so the SL can never sit exactly at the
# spread where a fresh tick would immediately stop the leg out.
STOPEXE_SPREAD_CUSHION_PIPS = max(
    0.0, float(os.getenv("MT5_STOPEXE_SPREAD_CUSHION_PIPS", "0") or 0))
# The broker compares the level against the bid/ask AT OrderSend, not against
# the quote we priced from, so a level sitting exactly on its minimum is
# rejected as soon as the price ticks ("sell stop must sit BELOW the bid",
# retcode 10015).  One point of cushion keeps the level outside the minimum
# without widening the bracket more than the broker forces.
STOP_CUSHION_POINTS = float(os.getenv("MT5_STOP_CUSHION_POINTS", "1") or 1)


def symbol_trade_levels(symbol: str, account: int | None = None) -> dict:
    """Broker trade levels for `symbol` on `account`.

    Reads the terminal's own catalogue first (written by the EA, no round
    trip); when that is unavailable, falls back to the broker probe.  The
    fields mirror PROBE: digits, stops_level and freeze_level are points.
    """
    sym = str(symbol or "")
    try:
        inst = int(account) if account is not None else None
    except (TypeError, ValueError):
        inst = None
    if inst in (1, 2):
        try:
            from spot import read_symbol_catalogue
            rows = read_symbol_catalogue(inst)
            row = rows.get(sym)
            if row is None:
                for key, value in rows.items():
                    if str(key).upper() == sym.upper():
                        row = value
                        break
            if row and len(row) >= 8:
                return {"digits": max(0, int(row[1])),
                        "stops_level": max(0, int(row[6])),
                        "freeze_level": max(0, int(row[7]))}
        except Exception:
            pass
    try:
        import broker_prober
        terminals = broker_prober.get_prober().report()["terminals"]
        targets = [str(inst)] if inst in (1, 2) else ["1", "2"]
        for key in targets:
            rows = terminals.get(key) or {}
            info = rows.get(sym)
            if info is None:
                for name, value in rows.items():
                    if str(name).upper() == sym.upper():
                        info = value
                        break
            if info and info.get("digits") is not None:
                return {"digits": max(0, int(info["digits"])),
                        "stops_level": max(0, int(info.get("stops_level") or 0)),
                        "freeze_level": max(0, int(info.get("freeze_level") or 0))}
    except Exception:
        pass
    return {}


def broker_min_distance_pips(symbol: str, account: int | None = None,
                             digits: int | None = None) -> float:
    """Smallest offset this broker/trade server accepts for `symbol`, in pips.

    The minimum is per broker AND per symbol: HFM quotes NZDUSD with
    SYMBOL_TRADE_STOPS_LEVEL = 30 points (3.0 pips), while FXPro and most
    majors report 0 and keep the desk's 0.5-pip bracket.  Converting points
    to pips keeps gold/silver/3- and 5-digit forex correct automatically.
    """
    info = symbol_trade_levels(symbol, account)
    d = int(digits if digits is not None else (info.get("digits")
                                               or digits_for(symbol)))
    levels = max(int(info.get("stops_level") or 0),
                 int(info.get("freeze_level") or 0))
    if levels <= 0:
        return 0.0
    point = 10.0 ** (-max(0, d))
    pip = pip_size(symbol, d)
    return round(levels * point / pip + STOP_CUSHION_POINTS * point / pip, 4)


def min_distance_pips(symbol: str, account: int | None = None,
                      digits: int | None = None) -> float:
    """Compatibility name for `broker_min_distance_pips`."""
    return broker_min_distance_pips(symbol, account, digits)


def pip_size(symbol: str, digits: int) -> float:
    """Price distance of ONE pip for `symbol` quoted with `digits`."""
    s = str(symbol or "").upper()
    if "XAU" in s or "GOLD" in s:
        return 1.0
    if "XAG" in s or "SILVER" in s or "XPT" in s or "XPD" in s:
        return 0.1
    point = 10.0 ** (-max(0, int(digits)))
    if int(digits) in (3, 5):
        return 10.0 * point
    return point


def digits_for(symbol: str, price: str = "") -> int:
    """Quote digits: broker probe first, live price string second."""
    try:
        import broker_prober
        rep = broker_prober.get_prober().report()["terminals"]
        for key in ("1", "2"):
            info = (rep.get(key) or {}).get(symbol)
            if info is None:
                for k, v in (rep.get(key) or {}).items():
                    if str(k).upper() == str(symbol).upper():
                        info = v
                        break
            if info and info.get("digits") is not None:
                return max(0, int(info["digits"]))
    except Exception:
        pass
    if price and "." in str(price):
        try:
            return max(0, len(str(price).split(".")[1]))
        except (IndexError, ValueError):
            pass
    s = str(symbol or "").upper()
    if s.startswith("XAU"):
        return 2
    if s.startswith("XAG"):
        return 3
    if "JPY" in s:
        return 3
    return 5


def stopexe_levels(symbol: str, account: int,
                   distance_pips: float = STOPEXE_DISTANCE_PIPS,
                   sl_pips: float = STOPEXE_SL_PIPS,
                   spread_cushion_pips: float | None = None) -> dict:
    """Bracket prices for a StopExe straddle, SLs included.  Raises
    ValueError with a human message when there is no live quote."""
    from spot import read_spots, resolve_symbol
    broker_sym = resolve_symbol(symbol, account)
    spots = read_spots()
    row = spots.get(broker_sym)
    if not row or not row[0]:
        # case-insensitive fallback (Deriv "Boom 1000 Index" etc.)
        for k, v in spots.items():
            if (str(k).upper() == str(broker_sym).upper()
                    and v and v[0]):
                broker_sym, row = k, v
                break
    if not row or not row[0]:
        raise ValueError(f"no live quote for {symbol}")
    bid_s, ask_s, _ts = row
    try:
        bid, ask = float(bid_s), float(ask_s)
    except (TypeError, ValueError):
        raise ValueError(f"no live quote for {symbol}")
    if bid <= 0 or ask <= 0:
        raise ValueError(f"no live quote for {symbol}")
    digits = digits_for(broker_sym, bid_s)
    pip = pip_size(broker_sym, digits)
    tick = 10 ** (-digits)
    spread = max(0.0, ask - bid)
    spread_pips = round(spread / pip, 4) if pip > 0 else 0.0
    if spread_cushion_pips is None:
        cushion_pips = STOPEXE_SPREAD_CUSHION_PIPS
    else:
        cushion_pips = max(0.0, float(spread_cushion_pips))
    level_pips = broker_min_distance_pips(broker_sym, account, digits)
    dist_pips = max(float(distance_pips), level_pips)
    dist = dist_pips * pip
    buy_price = round(ask + dist, digits)
    sell_price = round(bid - dist, digits)
    if buy_price <= ask:
        buy_price = round(ask + tick, digits)
    if sell_price >= bid:
        sell_price = round(bid - tick, digits)
    # Protective stops: the desk's 0.5 pip, widened automatically to the
    # broker's stops/freeze level for this symbol AND to just beyond the
    # live bid/ask spread (the EA applies the same floor on its side),
    # attached AT OrderSend time - never a second trip, so the legs are
    # never unprotected on the broker.  Without the spread floor a 0.5-pip
    # SL on a 0.7-pip-spread symbol would be stopped out on the next tick.
    spread_sl_pips = (spread_pips + tick / pip + cushion_pips
                      if spread > 0 and pip > 0 else 0.0)
    sl_pips = round(max(float(sl_pips), level_pips, spread_sl_pips), 4)
    sl_dist = sl_pips * pip
    buy_sl = round(buy_price - sl_dist, digits)
    sell_sl = round(sell_price + sl_dist, digits)
    if buy_sl >= buy_price:
        buy_sl = round(buy_price - tick, digits)
    if sell_sl <= sell_price:
        sell_sl = round(sell_price + tick, digits)
    return {
        "symbol": symbol,
        "broker_symbol": broker_sym,
        "account": account,
        "bid": bid,
        "ask": ask,
        "spread": round(spread, digits + 2),
        "spread_pips": spread_pips,
        "spread_cushion_pips": round(cushion_pips, 4),
        "digits": digits,
        "pip": pip,
        "distance_pips": dist_pips,
        "distance_price": round(dist, digits + 2),
        "buy_price": buy_price,
        "sell_price": sell_price,
        "sl_pips": sl_pips,
        "sl_distance_price": round(sl_dist, digits + 2),
        "buy_sl": buy_sl,
        "sell_sl": sell_sl,
    }


def place_stopexe(account: int, symbol: str, lot: float,
                   distance_pips: float = STOPEXE_DISTANCE_PIPS,
                   sl_pips: float = STOPEXE_SL_PIPS,
                   timeout: float = 10.0,
                   spread_cushion_pips: float | None = None) -> dict:
    """Place ONE two-leg StopExe straddle (see `place_stopexe_multi`)."""
    return place_stopexe_multi(account, symbol, lot, 1, distance_pips,
                               sl_pips, timeout, spread_cushion_pips)


def place_stopexe_multi(account: int, symbol: str, lot: float,
                        orders: int = 1,
                        distance_pips: float = STOPEXE_DISTANCE_PIPS,
                        sl_pips: float = STOPEXE_SL_PIPS,
                        timeout: float = 20.0,
                        spread_cushion_pips: float | None = None) -> dict:
    """Arm `orders` straddles - N buy stops + N sell stops - in ONE trip.

    Every STOPEXESL command file is written before the EA's next drain, so
    the whole stack is flighted in about the time of a single straddle
    instead of N serial round trips (the news module arms its bracket 5 s
    before a release, so the extra round trips used to eat the window).
    Each leg still carries its broker-adjusted SL on the same OrderSend.

    Returns a JSON-friendly dict.  The EA places both legs back-to-back
    and acks `buyprice|buyticket|sellprice|sell...|buysl|sellsl`; a half
    straddle is pulled server-side, never left standing.  Terminals still
    on an EA without STOPEXESL fall back to the 2N-PENDING batch path
    (same bracket + same SLs, one batch round trip).
    """
    from executor import sender_for

    n = int(orders)
    if not 1 <= n <= 50:
        raise ValueError("orders must be 1..50")
    lv = stopexe_levels(symbol, account, distance_pips, sl_pips,
                        spread_cushion_pips)
    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    # STOPEXESL (EA v1.78+): new name on purpose - an older EA answers
    # "unknown cmd" WITHOUT placing anything, so the fallback below can
    # retry with SLs instead of leaving a half-parsed, SL-less straddle.
    command = ("STOPEXESL", broker_sym, f"{lot:.2f}",
               f"{lv['buy_price']:.{digits}f}",
               f"{lv['sell_price']:.{digits}f}",
               f"{lv['buy_sl']:.{digits}f}",
               f"{lv['sell_sl']:.{digits}f}",
               str(STOPEXE_MAGIC), STOPEXE_COMMENT)
    # More straddles than the EA keeps in flight: send the next wave as soon
    # as the previous one acks, so the surplus still rests (a single wave of
    # >8 answers "stopexe busy" for the overflow).
    t0 = time.perf_counter()
    results: list = []
    for start in range(0, n, STOPEXE_SLOTS):
        chunk = [command] * min(STOPEXE_SLOTS, n - start)
        try:
            results.extend(sender.send_batch(*chunk, timeout=timeout))
        except Exception as exc:
            raise ValueError(str(exc))
        if start + len(chunk) < n:
            time.sleep(0.05)
    ms = (time.perf_counter() - t0) * 1000.0
    if results and all((not ok) and "unknown cmd" in str(detail).lower()
                       for ok, detail in results):
        # Old EA - same bracket + same SLs through 2N PENDINGs in one
        # concurrent batch (PENDING has carried SL since long ago).
        return _place_stopexe_batch(account, symbol, lot, lv,
                                    ms_offset=ms, orders=n)
    straddles = [_straddle_from_detail(lv, ok, detail)
                 for ok, detail in results]
    return _stopexe_result(symbol, account, lot, lv, straddles, ms)


def place_stopexe_counts(account: int, symbol: str, lot: float,
                         buy_orders: int, sell_orders: int,
                         distance_pips: float = STOPEXE_DISTANCE_PIPS,
                         sl_pips: float = STOPEXE_SL_PIPS,
                         timeout: float = 20.0,
                         spread_cushion_pips: float | None = None) -> dict:
    """Arm an arbitrary number of buy stops and sell stops in ONE batch.

    Equal counts use the atomic STOPEXESL straddle path.  Unequal counts
    fly 0..50 BUYSTOPs + 0..50 SELLSTOPs through the PENDING batch, each
    leg carrying the same protective SL on the OrderSend.
    """
    buy = int(buy_orders)
    sell = int(sell_orders)
    if not 0 <= buy <= 50 or not 0 <= sell <= 50:
        raise ValueError("buy/sell orders must be 0..50 each")
    if buy + sell <= 0:
        raise ValueError("at least one buy or sell stop is required")
    if buy == sell:
        out = place_stopexe_multi(account, symbol, lot, buy, distance_pips,
                                  sl_pips, timeout, spread_cushion_pips)
        out.setdefault("buy_orders", buy)
        out.setdefault("sell_orders", sell)
        return out

    from executor import sender_for

    lv = stopexe_levels(symbol, account, distance_pips, sl_pips,
                        spread_cushion_pips)
    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    buy_cmd = ("PENDING", broker_sym, "BUYSTOP", f"{lot:.2f}",
               f"{lv['buy_price']:.{digits}f}",
               f"{lv['buy_sl']:.{digits}f}", "0",
               str(STOPEXE_MAGIC), STOPEXE_COMMENT)
    sell_cmd = ("PENDING", broker_sym, "SELLSTOP", f"{lot:.2f}",
                f"{lv['sell_price']:.{digits}f}",
                f"{lv['sell_sl']:.{digits}f}", "0",
                str(STOPEXE_MAGIC), STOPEXE_COMMENT)
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*([buy_cmd] * buy + [sell_cmd] * sell),
                                         timeout=timeout))
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0

    legs: list[dict] = []
    for i, (ok, detail) in enumerate(results):
        is_buy = i < buy
        legs.append({
            "type": "BUYSTOP" if is_buy else "SELLSTOP",
            "price": lv["buy_price"] if is_buy else lv["sell_price"],
            "ok": bool(ok),
            "ticket": detail.split("|")[1] if ok and "|" in detail else "",
            "sl": lv["buy_sl"] if is_buy else lv["sell_sl"],
            "detail": detail,
        })
    placed = [leg for leg in legs if leg["ok"]]
    bad = next((leg for leg in legs if not leg["ok"]), None)
    if not placed:
        raise ValueError(f"STOPEXE rejected - {bad['detail'] if bad else 'no ack'}")
    out = {
        "symbol": symbol,
        "broker_symbol": broker_sym,
        "account": account,
        "lot": lot,
        "bid": lv["bid"],
        "ask": lv["ask"],
        "buy_price": lv["buy_price"],
        "sell_price": lv["sell_price"],
        "distance_pips": lv["distance_pips"],
        "buy_sl": lv["buy_sl"],
        "sell_sl": lv["sell_sl"],
        "sl_pips": lv["sl_pips"],
        "legs": legs,
        "ok": len(placed),
        "orders": buy + sell,
        "buy_orders": buy,
        "sell_orders": sell,
        "n": len(legs),
        "ms": round(ms, 1),
        "tickets": [leg["ticket"] for leg in placed],
    }
    threading.Thread(target=_log_legs, args=(account, symbol, lot, legs, ms),
                     daemon=True, name="stopexe-log").start()
    if len(placed) == len(legs):
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - {buy} BUY STOP(S) @ "
                         f"{lv['buy_price']} SL {lv['buy_sl']} + {sell} SELL "
                         f"STOP(S) @ {lv['sell_price']} SL {lv['sell_sl']}")
    else:
        out["detail"] = (f"PARTIAL {symbol} - {len(placed)}/{len(legs)} stops "
                         f"placed; rejected: {bad['detail'] if bad else 'no ack'} "
                         f"- CLOSE deletes the rest")
    return out


def _straddle_from_detail(lv: dict, ok: bool, detail: str) -> dict:
    return {"ok": bool(ok), "detail": detail,
            "legs": _legs_from_stopexe_detail(lv, ok, detail)}


def _log_legs(account: int, symbol: str, lot: float, legs: list[dict],
              ms: float) -> None:
    """Audit rows for a straddle stack, off the execution path."""
    import database as db

    for leg in legs:
        try:
            db.log_fired(0, account, symbol, leg["type"], lot, "stopexe",
                         leg["ticket"], leg["ok"], leg["detail"], ms=ms)
        except Exception as exc:
            log.warning(f"stopexe: could not log {leg['type']} leg: {exc}")


def _stopexe_result(symbol: str, account: int, lot: float, lv: dict,
                    straddles: list[dict], ms: float) -> dict:
    """Fold per-straddle acks into the panel's JSON result."""
    legs = [leg for s in straddles for leg in s["legs"]]
    total = len(straddles)
    full = [s for s in straddles if s["ok"]]
    bad = next((s for s in straddles if not s["ok"]), None)
    out = {
        "symbol": symbol,
        "broker_symbol": lv["broker_symbol"],
        "account": account,
        "lot": lot,
        "bid": lv["bid"],
        "ask": lv["ask"],
        "buy_price": lv["buy_price"],
        "sell_price": lv["sell_price"],
        "distance_pips": lv["distance_pips"],
        "buy_sl": lv["buy_sl"],
        "sell_sl": lv["sell_sl"],
        "sl_pips": lv["sl_pips"],
        "legs": legs,
        "ok": len(full),
        "orders": total,
        "n": len(legs),
        "ms": round(ms, 1),
    }
    if legs:
        threading.Thread(target=_log_legs,
                         args=(account, symbol, lot, legs, ms),
                         daemon=True, name="stopexe-log").start()
    if not full:
        placed = [leg for leg in legs if leg["ok"]]
        if placed:
            good = placed[0]
            out["detail"] = (f"PARTIAL {symbol} - {good['type']} @ "
                             f"{good['price']} (#{good['ticket']}) placed, "
                             f"the rest REJECTED: "
                             f"{bad['detail'] if bad else 'no ack'} - delete "
                             f"#{good['ticket']} if the straddle is unwanted")
            out["tickets"] = [leg["ticket"] for leg in placed]
            return out
        raise ValueError(f"STOPEXE rejected - {bad['detail'] if bad else 'no ack'}")
    if total == 1:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - BUY STOP @ "
                         f"{lv['buy_price']} SL {lv['buy_sl']} "
                         f"(#{legs[0]['ticket']}) + SELL STOP @ "
                         f"{lv['sell_price']} SL {lv['sell_sl']} "
                         f"(#{legs[1]['ticket']})")
        out["tickets"] = [legs[0]["ticket"], legs[1]["ticket"]]
    else:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} x{total} - "
                         f"{len(full)}/{total} straddles armed "
                         f"(BUY STOP @ {lv['buy_price']} SL {lv['buy_sl']} / "
                         f"SELL STOP @ {lv['sell_price']} SL {lv['sell_sl']})")
        out["tickets"] = [leg["ticket"] for leg in legs if leg["ok"]]
    if len(full) < total:
        placed = sum(1 for leg in legs if leg["ok"])
        out["detail"] += (f" - {total - len(full)} straddle(s) failed "
                          f"({bad['detail'] if bad else 'no ack'}); "
                          f"{placed} leg(s) rest on the broker - CLOSE "
                          f"deletes them")
    return out


def _legs_from_stopexe_detail(lv: dict, ok: bool, detail: str) -> list[dict]:
    """`buyprice|buyticket|sellprice|sell...[|buysl|sellsl]` -> leg dicts.

    v1.78 acks 6 fields (SLs confirmed); older acks carry 4 (SLs then
    are the levels we sent - the batch fallback always sets them).
    """
    parts = str(detail or "").split("|")
    buy_ticket = parts[1] if ok and len(parts) > 1 else ""
    sell_ticket = parts[3] if ok and len(parts) > 3 else ""
    try:
        buy_sl = float(parts[4]) if ok and len(parts) > 4 and parts[4] else lv.get("buy_sl")
    except (TypeError, ValueError):
        buy_sl = lv.get("buy_sl")
    try:
        sell_sl = float(parts[5]) if ok and len(parts) > 5 and parts[5] else lv.get("sell_sl")
    except (TypeError, ValueError):
        sell_sl = lv.get("sell_sl")
    return [
        {"type": "BUYSTOP", "price": lv["buy_price"], "ok": ok,
         "ticket": buy_ticket, "sl": buy_sl, "detail": detail},
        {"type": "SELLSTOP", "price": lv["sell_price"], "ok": ok,
         "ticket": sell_ticket, "sl": sell_sl, "detail": detail},
    ]


def modify_pending_sl(account: int, ticket: str | int, sl: float,
                      tp: float = 0.0, timeout: float = 10.0) -> dict:
    """Attach (or move) SL/TP on a resting pending order - EA v1.78+.

    Sends MODIFY in its own round trip; callers patching two legs at
    once should use `sender_for(account).send_batch` with two MODIFYs
    so both patches fly concurrently.  Raises ValueError when the EA
    predates MODIFY (restart the terminal to pick up v1.78).
    """
    from executor import sender_for

    sender = sender_for(account)
    try:
        ok, detail = sender.send("MODIFY", str(ticket), f"{float(sl):.8f}",
                                 f"{float(tp):.8f}", timeout=timeout)
    except Exception as exc:
        raise ValueError(str(exc))
    if not ok and "unknown cmd" in detail:
        raise ValueError("terminal EA predates MODIFY - restart terminal "
                         "to pick up v1.78, then retry")
    if not ok:
        raise ValueError(f"MODIFY #{ticket} rejected - {detail}")
    return {"ok": True, "ticket": str(ticket), "sl": float(sl),
            "tp": float(tp), "detail": detail}


def _place_stopexe_batch(account: int, symbol: str, lot: float,
                          lv: dict, ms_offset: float = 0.0,
                          orders: int = 1) -> dict:
    """Fallback for EAs without STOPEXESL: SL-carrying PENDINGs in ONE
    concurrent batch (single round trip, same protection).

    `orders` straddles become 2 x orders PENDING commands - all written
    before the EA's next drain, so the stack still costs one round trip.
    """
    from executor import sender_for

    n = max(1, int(orders))
    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    one = [
        ("PENDING", broker_sym, "BUYSTOP", f"{lot:.2f}",
         f"{lv['buy_price']:.{digits}f}", f"{lv['buy_sl']:.{digits}f}", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
        ("PENDING", broker_sym, "SELLSTOP", f"{lot:.2f}",
         f"{lv['sell_price']:.{digits}f}", f"{lv['sell_sl']:.{digits}f}", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
    ]
    commands = one * n
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*commands, timeout=20.0))
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0 + ms_offset
    straddles: list[dict] = []
    for i in range(n):
        legs = []
        for (ptype, price, sl), (ok, detail) in zip(
                (("BUYSTOP", lv["buy_price"], lv["buy_sl"]),
                 ("SELLSTOP", lv["sell_price"], lv["sell_sl"])),
                results[i * 2:i * 2 + 2]):
            ticket = detail.split("|")[1] if ok and "|" in detail else ""
            legs.append({"type": ptype, "price": price, "ok": ok,
                         "ticket": ticket, "sl": sl, "detail": detail})
        straddles.append({"ok": all(leg["ok"] for leg in legs),
                          "legs": legs,
                          "detail": " | ".join(f"{leg['type']}: {leg['detail']}"
                                               for leg in legs)})
    return _stopexe_result(symbol, account, lot, lv, straddles, ms)
