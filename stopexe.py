#!/usr/bin/env python3.12

"""StopExe - stop-order execution framework (straddle stops).

One EXECUTE opens a two-leg straddle around the live price:

    BUY STOP  at ask + 1.6 pips  with SL at entry - 2 pips
    SELL STOP at bid - 1.6 pips  with SL at entry + 2 pips   (3.2-pip bracket)

Both legs travel in ONE exec command (EA v1.80 STOPEXESL): one file, one
pass, both legs flighted async in the SAME tick with the SL attached,
one ack - either both legs rest on the broker together or neither
stands (a half straddle is pulled).  The SL rides on the same
OrderSend, so protection is concurrent with the fill - one broker
round trip total, no unprotected window.  Terminals still on an older
EA (no STOPEXESL command) fall back to two PENDINGs with SLs in one
batch (same single round trip).

Pip size follows the quote itself:

    * XAU* / GOLD  -> 1.0 per pip, so gold at 4150 straddles
      4151.6 / 4148.4 exactly like the desk expects
    * XAG* / SILVER -> 0.1 per pip (5 pips = 0.50)
    * 5- and 3-digit forex -> 10 * point (0.0001 / 0.01)
    * everything else -> 1 * point

Digits come from the broker probe when available, else from the live
price string (a quoted price always carries its own precision).
"""

from __future__ import annotations

import time

from config import setup_logging

log = setup_logging(__name__)

STOPEXE_MAGIC = 777003
STOPEXE_COMMENT = "stopexe"
# Protective stop attached to every straddle leg at OrderSend time.
STOPEXE_SL_PIPS = 2.0


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
                   distance_pips: float = 1.6,
                   sl_pips: float = STOPEXE_SL_PIPS) -> dict:
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
    dist = float(distance_pips) * pip
    buy_price = round(ask + dist, digits)
    sell_price = round(bid - dist, digits)
    if buy_price <= ask:
        buy_price = round(ask + 10 ** (-digits), digits)
    if sell_price >= bid:
        sell_price = round(bid - 10 ** (-digits), digits)
    # Protective stops: 2 pips against the leg (buy SL below its entry,
    # sell SL above its entry), attached AT OrderSend time - never a
    # second trip, so the legs are never unprotected on the broker.
    sl_pips = float(sl_pips)
    sl_dist = sl_pips * pip
    tick = 10 ** (-digits)
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
        "digits": digits,
        "pip": pip,
        "distance_pips": float(distance_pips),
        "distance_price": round(dist, digits + 2),
        "buy_price": buy_price,
        "sell_price": sell_price,
        "sl_pips": sl_pips,
        "sl_distance_price": round(sl_dist, digits + 2),
        "buy_sl": buy_sl,
        "sell_sl": sell_sl,
    }


def place_stopexe(account: int, symbol: str, lot: float,
                   distance_pips: float = 1.6,
                   sl_pips: float = STOPEXE_SL_PIPS,
                   timeout: float = 10.0) -> dict:
    """Place the two-leg StopExe straddle in ONE exec round trip, each
    leg carrying its 2-pip protective SL from the same OrderSend.

    Returns a JSON-friendly dict.  The EA places both legs back-to-back
    and acks `buyprice|buyticket|sellprice|sell...|buysl|sellsl`; a half
    straddle is pulled server-side, never left standing.  Terminals
    still on an EA without STOPEXESL fall back to the two-PENDING batch
    path (same bracket + same SLs, one batch round trip).
    """
    import database as db
    from executor import sender_for

    lv = stopexe_levels(symbol, account, distance_pips, sl_pips)
    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    t0 = time.perf_counter()
    try:
        # STOPEXESL (EA v1.78+): new name on purpose - an older EA
        # answers "unknown cmd" WITHOUT placing anything, so the
        # fallback below can retry with SLs instead of leaving a
        # half-parsed, SL-less straddle on the broker.
        ok, detail = sender.send(
            "STOPEXESL", broker_sym, f"{lot:.2f}",
            f"{lv['buy_price']:.{digits}f}", f"{lv['sell_price']:.{digits}f}",
            f"{lv['buy_sl']:.{digits}f}", f"{lv['sell_sl']:.{digits}f}",
            str(STOPEXE_MAGIC), STOPEXE_COMMENT, timeout=timeout)
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0
    if not ok and "unknown cmd" in detail:
        # Old EA - same bracket + same SLs through two PENDINGs in one
        # concurrent batch (PENDING has carried SL since long ago).
        return _place_stopexe_batch(account, symbol, lot, lv, ms_offset=ms)
    legs = _legs_from_stopexe_detail(lv, ok, detail)
    try:
        for leg in legs:
            db.log_fired(0, account, symbol, leg["type"], lot, "stopexe",
                         leg["ticket"], leg["ok"], leg["detail"], ms=ms)
    except Exception as exc:
        log.warning(f"stopexe: could not log legs: {exc}")
    ok_cnt = sum(1 for leg in legs if leg["ok"])
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
        "ok": ok_cnt,
        "n": 2,
        "ms": round(ms, 1),
    }
    if ok_cnt == 2:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - BUY STOP @ "
                         f"{lv['buy_price']} SL {lv['buy_sl']} "
                         f"(#{legs[0]['ticket']}) + SELL STOP @ "
                         f"{lv['sell_price']} SL {lv['sell_sl']} "
                         f"(#{legs[1]['ticket']})")
        out["tickets"] = [legs[0]["ticket"], legs[1]["ticket"]]
        return out
    raise ValueError(f"STOPEXE rejected - {detail}")


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
                          lv: dict, ms_offset: float = 0.0) -> dict:
    """Fallback for EAs without STOPEXESL: two SL-carrying PENDINGs in
    ONE concurrent batch (single round trip, same protection)."""
    import database as db
    from executor import sender_for

    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    commands = [
        ("PENDING", broker_sym, "BUYSTOP", f"{lot:.2f}",
         f"{lv['buy_price']:.{digits}f}", f"{lv['buy_sl']:.{digits}f}", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
        ("PENDING", broker_sym, "SELLSTOP", f"{lot:.2f}",
         f"{lv['sell_price']:.{digits}f}", f"{lv['sell_sl']:.{digits}f}", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
    ]
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*commands, timeout=20.0))
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0 + ms_offset
    legs = []
    for (ptype, price, sl), (ok, detail) in zip(
            (("BUYSTOP", lv["buy_price"], lv["buy_sl"]),
             ("SELLSTOP", lv["sell_price"], lv["sell_sl"])),
            results):
        ticket = detail.split("|")[1] if ok and "|" in detail else ""
        legs.append({"type": ptype, "price": price, "ok": ok,
                     "ticket": ticket, "sl": sl, "detail": detail})
        try:
            db.log_fired(0, account, symbol, ptype, lot, "stopexe",
                         ticket, ok, detail, ms=ms)
        except Exception as exc:
            log.warning(f"stopexe: could not log {ptype} leg: {exc}")
    ok_cnt = sum(1 for leg in legs if leg["ok"])
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
        "ok": ok_cnt,
        "n": 2,
        "ms": round(ms, 1),
    }
    if ok_cnt == 2:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - BUY STOP @ "
                         f"{lv['buy_price']} SL {lv['buy_sl']} "
                         f"(#{legs[0]['ticket']}) + SELL STOP @ "
                         f"{lv['sell_price']} SL {lv['sell_sl']} "
                         f"(#{legs[1]['ticket']})")
        out["tickets"] = [legs[0]["ticket"], legs[1]["ticket"]]
        return out
    if ok_cnt == 1:
        good = legs[0] if legs[0]["ok"] else legs[1]
        bad = legs[1] if legs[0]["ok"] else legs[0]
        out["detail"] = (f"PARTIAL {symbol} - {good['type']} @ {good['price']} "
                         f"(#{good['ticket']}) placed, {bad['type']} REJECTED: "
                         f"{bad['detail']} - delete #{good['ticket']} if the "
                         f"straddle is unwanted")
        out["tickets"] = [good["ticket"]]
        return out
    raise ValueError(f"STOPEXE rejected - BUY STOP: {legs[0]['detail']} | "
                     f"SELL STOP: {legs[1]['detail']}")
