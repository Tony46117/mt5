#!/usr/bin/env python3.12

"""StopExe - stop-order execution framework (straddle stops).

One EXECUTE opens a two-leg straddle around the live price:

    BUY STOP  at ask + 2 pips
    SELL STOP at bid - 2 pips        (4-pip bracket total)

Both legs travel in ONE exec command (EA v1.77 STOPEXE): one file, one
pass, back-to-back OrderSends, one ack - either both legs rest on the
broker together or neither stands (a half straddle is pulled).

Pip size follows the quote itself:

    * XAU* / GOLD  -> 1.0 per pip, so gold at 4150 straddles
      4155 / 4145 exactly like the desk expects
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
                   distance_pips: float = 2.0) -> dict:
    """Bracket prices for a StopExe straddle.  Raises ValueError with a
    human message when there is no live quote."""
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
    }


def place_stopexe(account: int, symbol: str, lot: float,
                  distance_pips: float = 2.0,
                  timeout: float = 10.0) -> dict:
    """Place the two-leg StopExe straddle in ONE exec round trip.

    Returns a JSON-friendly dict.  The EA places both legs back-to-back
    and acks `buyprice|buyticket|sellprice|sell...`; a half straddle is
    pulled server-side, never left standing.  Terminals still on an EA
    older than v1.77 (no STOPEXE command) fall back to the two-PENDING
    batch path.
    """
    import database as db
    from executor import sender_for

    lv = stopexe_levels(symbol, account, distance_pips)
    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    t0 = time.perf_counter()
    try:
        ok, detail = sender.send(
            "STOPEXE", broker_sym, f"{lot:.2f}",
            f"{lv['buy_price']:.{digits}f}", f"{lv['sell_price']:.{digits}f}",
            str(STOPEXE_MAGIC), STOPEXE_COMMENT, timeout=timeout)
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0
    if not ok and "unknown cmd" in detail:
        # Old EA - same bracket through two PENDINGs in one batch.
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
        "legs": legs,
        "ok": ok_cnt,
        "n": 2,
        "ms": round(ms, 1),
    }
    if ok_cnt == 2:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - BUY STOP @ "
                         f"{lv['buy_price']} (#{legs[0]['ticket']}) + SELL STOP @ "
                         f"{lv['sell_price']} (#{legs[1]['ticket']})")
        out["tickets"] = [legs[0]["ticket"], legs[1]["ticket"]]
        return out
    raise ValueError(f"STOPEXE rejected - {detail}")


def _legs_from_stopexe_detail(lv: dict, ok: bool, detail: str) -> list[dict]:
    """`buyprice|buyticket|sellprice|sell...` -> two leg dicts."""
    parts = str(detail or "").split("|")
    buy_ticket = parts[1] if ok and len(parts) > 1 else ""
    sell_ticket = parts[3] if ok and len(parts) > 3 else ""
    return [
        {"type": "BUYSTOP", "price": lv["buy_price"], "ok": ok,
         "ticket": buy_ticket, "detail": detail},
        {"type": "SELLSTOP", "price": lv["sell_price"], "ok": ok,
         "ticket": sell_ticket, "detail": detail},
    ]


def _place_stopexe_batch(account: int, symbol: str, lot: float,
                         lv: dict, ms_offset: float = 0.0) -> dict:
    """Fallback for pre-v1.77 EAs: two PENDINGs in one batch."""
    import database as db
    from executor import sender_for

    broker_sym = lv["broker_symbol"]
    digits = lv["digits"]
    sender = sender_for(account)
    commands = [
        ("PENDING", broker_sym, "BUYSTOP", f"{lot:.2f}",
         f"{lv['buy_price']:.{digits}f}", "0", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
        ("PENDING", broker_sym, "SELLSTOP", f"{lot:.2f}",
         f"{lv['sell_price']:.{digits}f}", "0", "0",
         str(STOPEXE_MAGIC), STOPEXE_COMMENT),
    ]
    t0 = time.perf_counter()
    try:
        results = list(sender.send_batch(*commands, timeout=20.0))
    except Exception as exc:
        raise ValueError(str(exc))
    ms = (time.perf_counter() - t0) * 1000.0 + ms_offset
    legs = []
    for (ptype, price), (ok, detail) in zip(
            (("BUYSTOP", lv["buy_price"]), ("SELLSTOP", lv["sell_price"])),
            results):
        ticket = detail.split("|")[1] if ok and "|" in detail else ""
        legs.append({"type": ptype, "price": price, "ok": ok,
                     "ticket": ticket, "detail": detail})
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
        "legs": legs,
        "ok": ok_cnt,
        "n": 2,
        "ms": round(ms, 1),
    }
    if ok_cnt == 2:
        out["detail"] = (f"STOPEXE {symbol} {lot:g} - BUY STOP @ "
                         f"{lv['buy_price']} (#{legs[0]['ticket']}) + SELL STOP @ "
                         f"{lv['sell_price']} (#{legs[1]['ticket']})")
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
