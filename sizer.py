#!/usr/bin/env python3.12

"""Position sizer on top of broker probing.

Every number comes from the broker itself (broker_prober catalogue / PROBE
reply), never from hardcoded tables:

  * volume_min / volume_max / volume_step per symbol
  * contract_size / tick_value / tick_size per symbol (tick_value is already
    in the account's deposit currency, so margin math auto-handles KES, USC
    cents, USD, EUR, ... without per-currency branches)
  * live price (probe ask/bid, falling back to the spots feed)
  * account balance / equity / free margin + leverage + currency from the EA
    header (via monitor.account_info, same source the prober uses)

What it answers per symbol:

  * margin for 1.0 lot (estimate, see margin_per_lot)
  * margin for the broker minimum lot
  * maximum lot in ONE position the funds cover (floored to the broker step,
    capped to the broker maximum)
  * maximum POSITIONS of minimum-lot size the funds cover
  * whether even the minimum lot is affordable
  * USD equivalent of the balance (enhanced converter: USC cents, KES via
    live USDKES, any other ISO code via live XXXUSD / USDXXX, static fallback)

Margin formula (primary, exact for MT5):

    margin_per_lot = price * tick_value / tick_size / leverage

because one full price point of profit is tick_value/tick_size in deposit
currency for `contract` units, scaled to the full notional `price`.  This is
identical to contract * base_to_account / leverage for forex, but needs no
base/quote parsing and works for gold, indices and CFDs too - as long as the
broker reported a live tick_value/tick_size (EA v1.73+).  Older EAs omit them:
then we fall back to contract * base_to_account / leverage using live spots
(see _fallback_margin), clearly marked as "fallback".

Funds basis: balance by default (as requested); --funds equity|free switches
to equity or free margin.  Free margin is what the broker will actually check,
balance is what the instruction asked for - both are reported.

Usage:
    python sizer.py                      # both terminals, compact table
    python sizer.py --terminal 1 --symbol XAUUSD
    python sizer.py --json               # machine-readable
    python sizer.py --funds free --top 20
    python sizer.py --affordable         # only min-lots that fit the funds

Data provenance (app_run.log login churn: demo/316 symbols <-> live/87):
    catalogue-first per login, tick-implied currency check discards the
    previous broker's cached probe, live PROBE only when needed.  The header
    shows the universe source, its age and the consistency verdict.
"""

from __future__ import annotations

import argparse
import math
import time

from config import setup_logging

log = setup_logging(__name__)

# ---------------------------------------------------------------------------
# leverage / currency helpers
# ---------------------------------------------------------------------------

def parse_leverage(lev) -> float | None:
    """'1:400' / '400' / 400 -> 400.0.  None when unparseable/zero."""
    if lev is None:
        return None
    s = str(lev).strip().replace(" ", "")
    if not s or s == "0":
        return None
    if s.startswith("1:"):
        s = s[2:]
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


# Cent-account deposit currencies: balances are in cents, 100 = 1 USD.
CENT_CURRENCIES = frozenset({
    "USC", "USCENT", "USCENTS", "US CENT", "US CENTS", "CENT", "CENTS",
    "USDCENT", "USDCENTS", "USD CENT", "USD CENTS", "USDC", "USDC.",
    "CUSD", "UCENT",
})

# Static USD-per-unit fallback when there is no live feed at all
# (live spots always win over this table).
_STATIC_USD_PER_UNIT = {
    "USD": 1.0,
    "USC": 0.01,
    "USDC": 0.01,
    "KES": 1.0 / 129.0,
    "EUR": 1.12,
    "GBP": 1.32,
    "AUD": 0.70,
    "NZD": 0.56,
    "CHF": 1.20,   # 1 CHF ~= 1.20 USD -> 1/0.83
    "JPY": 1.0 / 158.0,
    "CAD": 1.0 / 1.43,
    "CNH": 1.0 / 6.70,
    "CNY": 1.0 / 6.70,
    "SEK": 1.0 / 10.0,
    "NOK": 1.0 / 9.6,
    "ZAR": 1.0 / 16.5,
    "MXN": 1.0 / 18.2,
    "XAU": 4185.0,
    "XAG": 60.5,
}


def normalize_currency(cur: str) -> str:
    return str(cur or "").strip().upper()


def is_cent_account(cur: str) -> bool:
    c = normalize_currency(cur)
    if c in CENT_CURRENCIES:
        return True
    # HFM-style "USDc" / "KESc" cent variants: trailing c + known root
    if len(c) > 1 and c.endswith("C") and (c[:-1] in _STATIC_USD_PER_UNIT
                                           or c[:-1] in ("USD", "EUR", "KES")):
        return True
    return False


def _spots_mid(spots: dict, symbol: str) -> float | None:
    """Mid price for `symbol` from the spots feed (any suffix variant ok)."""
    if not spots:
        return None
    hit = spots.get(symbol)
    if hit is None:
        # case-insensitive / suffix-tolerant fallback: EURUSD matches
        # EURUSD.Z, EURUSDc.Z, ... - first exact-prefix hit wins
        up = symbol.upper()
        for k, v in spots.items():
            if k.upper() == up:
                hit = v
                break
        else:
            for k, v in spots.items():
                ku = k.upper()
                if ku == up or ku.startswith(up):
                    hit = v
                    break
    if not hit:
        return None
    try:
        bid, ask = float(hit[0]), float(hit[1])
    except (TypeError, ValueError, IndexError):
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if bid > 0:
        return bid
    if ask > 0:
        return ask
    return None


def _quote_age_s(spots: dict, symbol: str) -> float | None:
    """Age of `symbol`'s quote in seconds, None when unknown/unparseable.

    FX conversion prefers live rates; a stale-but-present rate (e.g. USDKES
    that stopped refreshing) still beats the static table, but the method
    string says so instead of claiming 'live'.
    """
    import datetime as _dt
    try:
        hit = spots.get(symbol)
        if hit is None:
            up = str(symbol).upper()
            for k, v in spots.items():
                if str(k).upper() == up:
                    hit = v
                    break
            else:
                return None
        ts = str(hit[2] if len(hit) > 2 else "").strip()[:19]
        if not ts:
            return None
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return time.time() - _dt.datetime.strptime(ts, fmt).timestamp()
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _fresh(mid: float | None, age_s: float | None,
           max_age_s: float = 120.0) -> str:
    """'live ...' suffix, downgraded to 'stale ...m' past max_age_s."""
    if age_s is not None and age_s > max_age_s:
        m = age_s / 60.0
        age_txt = f"{m:.0f}m stale" if m < 90 else f"{m / 60:.1f}h stale"
        return f" ({age_txt})"
    return ""


def usd_per_unit(currency: str, spots: dict | None = None) -> tuple[float | None, str]:
    """USD value of 1.0 unit of `currency` -> (rate, method).

    Order: USC-cents rule, live spots (XXXUSD / USDXXX, suffix-tolerant),
    then the static fallback table.  (None, reason) when unconvertible.
    """
    cur = normalize_currency(currency)
    if not cur or cur == "?":
        return None, "unknown currency"
    if cur in ("USD", "US$", "DOLLAR", "DOLLARS"):
        return 1.0, "USD"
    if is_cent_account(cur):
        return 0.01, "USC cents (100 = 1 USD)"
    if spots is None:
        spots = _live_spots()
    else:
        spots = spots or {}
    # live XXXUSD (e.g. EURUSD for EUR, XAUUSD for XAU)
    for quote in (f"{cur}USD", f"{cur}USD.Z", f"XAUUSD" if cur == "XAU" else "",
                  f"XAGUSD" if cur == "XAG" else ""):
        if quote and quote in spots:
            mid = _spots_mid(spots, quote)
            if mid and mid > 0:
                tag = _fresh(mid, _quote_age_s(spots, quote))
                return mid, f"live {quote} {mid:.5g}{tag}"
    # live USDXXX (e.g. USDKES for KES, USDJPY for JPY) -> invert
    for base in (f"USD{cur}", f"USD{cur}.Z"):
        if base in spots:
            mid = _spots_mid(spots, base)
            if mid and mid > 0:
                tag = _fresh(mid, _quote_age_s(spots, base))
                return 1.0 / mid, f"live 1/{base} {mid:.5g}{tag}"
    # suffix-tolerant scan: spots carry .Z / c.Z broker suffixes
    up = cur.upper()
    for k in spots:
        ku = k.upper()
        if ku.startswith(up + "USD"):
            mid = _spots_mid(spots, k)
            if mid and mid > 0:
                tag = _fresh(mid, _quote_age_s(spots, k))
                return mid, f"live {k} {mid:.5g}{tag}"
        elif ku.startswith("USD" + up):
            mid = _spots_mid(spots, k)
            if mid and mid > 0:
                tag = _fresh(mid, _quote_age_s(spots, k))
                return 1.0 / mid, f"live 1/{k} {mid:.5g}{tag}"
    if cur in _STATIC_USD_PER_UNIT:
        return _STATIC_USD_PER_UNIT[cur], "static fallback (no live quote)"
    # trailing-cent variant of a known code ("KESc" -> KES cents)
    if len(cur) > 1 and cur.endswith("C"):
        base = cur[:-1]
        r, m = usd_per_unit(base, spots)
        if r:
            return r * 0.01, f"{base} cents ({m})"
    return None, f"no rate for {cur or '?'} (no live XXXUSD/USDXXX)"


def to_usd(amount: float, currency: str, spots: dict | None = None) -> dict:
    """Convert `amount` in `currency` to USD -> dict with rate + method."""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0.0
    rate, method = usd_per_unit(currency, spots)
    if rate is None:
        return {"usd": None, "rate": None, "method": method,
                "currency": normalize_currency(currency) or "?"}
    return {"usd": round(amt * rate, 2), "rate": rate, "method": method,
            "currency": normalize_currency(currency) or "?"}


def usd_to_account(amount_usd: float, currency: str,
                   spots: dict | None = None) -> float | None:
    """Convert USD -> account currency units (inverse of usd_per_unit)."""
    rate, _ = usd_per_unit(currency, spots)
    if not rate:
        return None
    return float(amount_usd) / rate


# ---------------------------------------------------------------------------
# probe / account plumbing (all broker-sourced, no hardcodes)
# ---------------------------------------------------------------------------

def _probe_report() -> dict:
    try:
        from broker_prober import get_prober
        return get_prober().report()
    except Exception:
        return {"terminals": {}}


PROBE_STALE_S = 600.0   # mirror broker_prober.PROBE_TTL_S
PROBE_CONSIST_TOL = 0.05  # 5% tick-implied vs expected USD rate


def _probe_implied_account_per_usd(infos: dict) -> float | None:
    """Median USD->account rate implied by USD-quoted ticks.

    For a USD-quoted symbol, tick_value/(tick_size*contract) is the profit
    of a 1.0 price move per unit - i.e. units of deposit currency per USD -
    with no price feed needed (the broker's tick already embeds it).
    Median over the universe so one odd CFD cannot swing it.
    """
    vals: list[float] = []
    for sym, info in (infos or {}).items():
        try:
            split = _split_forex(sym)
            if not split or split[1] != "USD":
                continue
            tv = float(info.get("tick_value", 0) or 0)
            ts = float(info.get("tick_size", 0) or 0)
            cs = float(info.get("contract_size", 0) or 0)
            px = float(info.get("ask", 0) or info.get("bid", 0) or 0)
            if tv > 0 and ts > 0 and cs > 0 and px > 0:
                vals.append(tv / (ts * cs))
        except (TypeError, ValueError, AttributeError):
            continue
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _probe_matches_account(infos: dict, ccy: str,
                           spots: dict) -> tuple[bool, str]:
    """Does this probe universe belong to an account in `ccy`?

    The logs show login churn demo(316 symbols, KES/USD) -> live USC(87) ->
    demo again: probe data cached in a long-lived process can be the PREVIOUS
    broker's, and margins computed from it are garbage (KES ticks priced as
    USC cents or vice versa).  The tick-implied USD rate catches that without
    any extra round trips.
    """
    if not infos:
        return False, "empty probe universe"
    cur = normalize_currency(ccy)
    if not cur or cur == "?":
        return True, "account currency unknown - check skipped"
    implied = _probe_implied_account_per_usd(infos)
    if implied is None:
        return True, "no USD-quoted ticks - check skipped"
    exp_rate, _m = usd_per_unit(cur, spots)  # USD per 1 account unit
    if not exp_rate:
        return True, f"{cur} unconvertible - check skipped"
    expected = 1.0 / exp_rate  # account units per USD
    if expected <= 0:
        return True, "check skipped"
    ratio = implied / expected
    if abs(ratio - 1.0) <= PROBE_CONSIST_TOL:
        return True, (f"tick-implied {implied:.4g} {cur}/USD matches account")
    return False, (f"tick-implied {implied:.4g} vs expected "
                    f"{expected:.4g} {cur}/USD - stale broker data?")


def _catalogue_age_s(inst: int) -> float | None:
    try:
        from spot import catalogue_path
        return time.time() - catalogue_path(inst).stat().st_mtime
    except Exception:
        return None


def _probe_universe(inst: int, ccy: str,
                    spots: dict) -> tuple[dict, dict]:
    """Probe universe for `inst` that provably belongs to this account.

    Returns (infos, meta) with meta = {source, age_s, consistent, note}.
    Catalogue first (trust-gated to the current login inside spot.py, zero
    round trips); then the in-memory probe cache only when fresh AND
    currency-consistent; then one live PROBE round when logged in.
    """
    meta = {"source": "empty", "age_s": None, "consistent": False, "note": ""}
    try:
        from broker_prober import get_prober
        from spot import read_accounts
        p = get_prober()
    except Exception as exc:
        meta["note"] = f"prober unavailable ({exc})"
        return {}, meta
    # 1. catalogue: authoritative per-login, no round trips
    try:
        cat = p.load_catalogue(inst)
    except Exception as exc:
        cat = {}
        meta["note"] = f"catalogue unreadable ({exc})"
    if cat:
        ok, detail = _probe_matches_account(cat, ccy, spots)
        meta["consistent"] = ok
        if ok:
            meta.update(source="catalogue", age_s=_catalogue_age_s(inst),
                        note=detail)
            return cat, meta
        log.warning(f"sizer: terminal {inst} catalogue fails account check "
                    f"({detail}) - discarding as stale")
    # 2. in-memory probe cache: only when fresh and consistent
    try:
        with p._lock:
            mem = dict(p._data.get(inst, {}))
            last = float(p._last_pass.get(inst, 0.0) or 0.0)
    except Exception:
        mem, last = {}, 0.0
    age = (time.time() - last) if last > 0 else None
    if mem and age is not None and age <= PROBE_STALE_S:
        ok, detail = _probe_matches_account(mem, ccy, spots)
        meta["consistent"] = ok
        if ok:
            meta.update(source="probe-cache", age_s=round(age, 1), note=detail)
            return mem, meta
        log.warning(f"sizer: terminal {inst} cached probe fails account "
                    f"check ({detail}) - re-probing")
    elif mem:
        meta["note"] = (f"cached probe too old "
                        f"({age:.0f}s > {PROBE_STALE_S:.0f}s)" if age is not None
                        else "cached probe has no timestamp")
    # 3. live PROBE round (only when a login is configured)
    try:
        accs = read_accounts()
    except Exception:
        accs = {}
    if accs.get(inst, {}).get("login"):
        try:
            out = p.probe_terminal(inst)
        except Exception as exc:
            log.debug(f"sizer: probe terminal {inst} failed: {exc}")
            out = {}
        if out:
            ok, detail = _probe_matches_account(out, ccy, spots)
            meta["consistent"] = ok
            meta.update(source="live-probe", age_s=0.0, note=detail)
            if not ok:
                log.warning(f"sizer: terminal {inst} fresh probe fails "
                            f"account check ({detail})")
            return out, meta
        if not meta.get("note"):
            meta["note"] = "live probe returned nothing"
    elif not meta.get("note"):
        meta["note"] = "no login configured"
    return {}, meta


def _ensure_probed(inst: int) -> dict[str, dict]:
    """Probe data for `inst` (compat wrapper: universe only, no meta)."""
    try:
        from spot import read_spots
        spots = read_spots(max_age=5.0)
    except Exception:
        spots = {}
    try:
        from broker_prober import get_prober
        from spot import read_accounts, pick_terminal, read_header
        ccy = ""
        try:
            login = read_accounts().get(inst, {}).get("login", "")
            term = pick_terminal(login) if login else None
            if term:
                ccy = read_header(term["trades_path"]).get("currency", "") or ""
        except Exception:
            pass
        infos, _meta = _probe_universe(inst, ccy, spots)
        return infos
    except Exception:
        pass
    return {}


def _live_spots() -> dict:
    try:
        from spot import read_spots
        return read_spots(max_age=5.0)
    except Exception:
        return {}


def _account(inst: int) -> dict:
    """Balance/equity/free/leverage/currency for `inst` (prober snapshot
    first, EA header fallback)."""
    snap: dict = {}
    try:
        from broker_prober import get_prober
        snap = get_prober().account_snapshot().get(str(inst), {})
    except Exception:
        snap = {}
    if snap and (snap.get("balance") or snap.get("currency")):
        return {
            "login": str(snap.get("login", "")),
            "server": str(snap.get("server", "") or ""),
            "currency": str(snap.get("currency", "") or ""),
            "leverage": str(snap.get("leverage", "") or ""),
            "balance": _f(snap.get("balance", 0.0)),
            "equity": _f(snap.get("equity", 0.0)),
            "margin_free": _f(snap.get("margin_free", 0.0)),
            "margin": _f(snap.get("margin", 0.0)),
            # monitor.account_info maps these from the EA header's
            # account_trade_allowed / mql_allowed (server-side blocks).
            "account_trade_allowed": str(snap.get("trade_allowed", "1") or "1"),
            "mql_allowed": str(snap.get("mql_allowed", "1") or "1"),
        }
    try:
        from monitor import account_info
        a = account_info(inst)
        lev = str(a.get("leverage", "") or "").strip()
        if lev.isdigit():
            lev = f"1:{lev}"
        cur = ""
        try:
            from spot import read_accounts, pick_terminal, read_header
            login = read_accounts().get(inst, {}).get("login", "")
            term = pick_terminal(login) if login else None
            if term:
                cur = read_header(term["trades_path"]).get("currency", "") or ""
        except Exception:
            pass
        return {
            "login": str(a.get("login", "")),
            "server": str(a.get("server", "") or ""),
            "currency": cur,
            "leverage": lev,
            "balance": _f(a.get("balance", 0.0)),
            "equity": _f(a.get("equity", 0.0)),
            "margin_free": _f(a.get("margin_free", 0.0)),
            "margin": _f(a.get("margin", 0.0)),
            "account_trade_allowed": str(a.get("trade_allowed", "1") or "1"),
            "mql_allowed": str(a.get("mql_allowed", "1") or "1"),
        }
    except Exception:
        return {"login": "", "server": "", "currency": "", "leverage": "",
                "balance": 0.0, "equity": 0.0, "margin_free": 0.0, "margin": 0.0,
                "account_trade_allowed": "1", "mql_allowed": "1"}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price_of(info: dict, spots: dict) -> tuple[float | None, str]:
    for key in ("ask", "bid"):
        try:
            p = float(info.get(key, 0) or 0)
            if p > 0:
                return p, f"probe {key}"
        except (TypeError, ValueError):
            pass
    sym = str(info.get("symbol", ""))
    mid = _spots_mid(spots, sym) if sym else None
    if mid and mid > 0:
        return mid, "spots feed"
    return None, "no live price"


# Known ISO codes for the contract fallback's base/quote split (first 6
# chars of a suffixed broker symbol, e.g. EURUSDc.Z -> EUR/USD).
_FX_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNH", "CNY",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "ZAR", "MXN", "SGD", "HKD",
    "TRY", "RUB", "AED", "THB", "KES", "UGX", "TZS", "NGN", "GHS", "ZMW",
    "XAU", "XAG",
})


def _split_forex(symbol: str) -> tuple[str, str] | None:
    s = str(symbol or "").upper().strip().split(".", 1)[0].split(" ", 1)[0]
    if len(s) < 6:
        return None
    base, quote = s[:3], s[3:6]
    if base in _FX_CODES and quote in _FX_CODES:
        return base, quote
    return None


def _fallback_margin(info: dict, leverage: float, account_ccy: str,
                     price: float, spots: dict) -> tuple[float | None, str]:
    """contract * base_to_account / leverage (pre-tick EA path)."""
    try:
        contract = float(info.get("contract_size", 0) or 0)
    except (TypeError, ValueError):
        contract = 0.0
    if contract <= 0 or leverage <= 0 or price <= 0:
        return None, "no contract size"
    sym = str(info.get("symbol", ""))
    split = _split_forex(sym)
    if split is None:
        # CFD/index: assume quoted in USD, convert to account
        rate, m = usd_per_unit("USD", spots)
        usd_to_acc = None
        acc_rate, _ = usd_per_unit(account_ccy, spots)
        if acc_rate:
            usd_to_acc = 1.0 / acc_rate
        if usd_to_acc is None:
            return None, "non-FX without FX quote"
        return contract * price / leverage * usd_to_acc, \
            f"fallback CFD USD-quoted ({m})"
    base, _quote = split
    if base == "XAU" or base == "XAG":
        base_to_usd = price  # price IS XAUUSD/XAGUSD
    elif base == "USD":
        base_to_usd = 1.0
    else:
        direct = _spots_mid(spots, f"{base}USD")
        if direct and direct > 0:
            base_to_usd = direct
        else:
            inv = _spots_mid(spots, f"USD{base}")
            if inv and inv > 0:
                base_to_usd = 1.0 / inv
            else:
                return None, f"no {base}->USD rate"
    acc_rate, _ = usd_per_unit(account_ccy, spots)
    if not acc_rate:
        return None, f"no USD->{account_ccy or '?'} rate"
    base_to_acc = base_to_usd / acc_rate
    return contract / leverage * base_to_acc, "fallback contract-based"


def margin_per_lot(info: dict, leverage: float, spots: dict,
                   account_ccy: str = "") -> tuple[float | None, str]:
    """Deposit-currency margin for 1.0 lot -> (margin, method)."""
    if leverage is None or leverage <= 0:
        return None, "unknown leverage"
    price, psrc = _price_of(info, spots)
    if not price or price <= 0:
        return None, "no live price"
    try:
        tick_value = float(info.get("tick_value", 0) or 0)
        tick_size = float(info.get("tick_size", 0) or 0)
    except (TypeError, ValueError):
        tick_value, tick_size = 0.0, 0.0
    if tick_value > 0 and tick_size > 0:
        return price * tick_value / tick_size / leverage, \
            f"tick {psrc} (tick_value {tick_value:g})"
    return _fallback_margin(info, leverage, account_ccy, price, spots)


def floor_to_step(value: float, step: float) -> float:
    if step is None or step <= 0:
        return value
    try:
        n = math.floor(float(value) / float(step) + 1e-9)
    except (TypeError, ValueError, ZeroDivisionError):
        return value
    out = n * float(step)
    # kill float dust (0.30000000004): round to step precision
    try:
        dec = max(0, -int(math.floor(math.log10(float(step)) + 1e-12)))
        out = round(out, min(dec + 1, 8))
    except (TypeError, ValueError):
        pass
    return max(0.0, out)


def is_tradeable(info: dict) -> bool:
    try:
        return int(info.get("trade_mode", 4)) != 0
    except (TypeError, ValueError):
        return True


def size_symbol(symbol: str, info: dict, balance: float, leverage: float,
                account_ccy: str, spots: dict,
                funds: float | None = None) -> dict:
    """Full sizing for one symbol against `funds` (defaults to `balance`)."""
    funds = balance if funds is None else funds
    try:
        vmin = float(info.get("volume_min", 0) or 0)
        vmax = float(info.get("volume_max", 0) or 0)
        step = float(info.get("volume_step", 0) or 0)
    except (TypeError, ValueError):
        vmin, vmax, step = 0.0, 0.0, 0.0
    if step <= 0:
        step = vmin if vmin > 0 else 0.01
    tradeable = is_tradeable(info)
    margin_lot, method = (margin_per_lot(info, leverage, spots, account_ccy)
                          if tradeable else (None, "DISABLED by broker"))
    margin_min = margin_lot * vmin if margin_lot else None
    out: dict = {
        "symbol": symbol,
        "tradeable": tradeable,
        "volume_min": vmin,
        "volume_max": vmax,
        "volume_step": step,
        "price": _price_of(info, spots)[0],
        "margin_per_lot": round(margin_lot, 2) if margin_lot else None,
        "margin_method": method,
        "margin_per_min": round(margin_min, 2) if margin_min else None,
        "funds": round(float(funds), 2),
        "affordable_min": bool(tradeable and margin_min and funds >= margin_min),
        "max_lot": 0.0,
        "max_positions_min_lot": 0,
    }
    if not tradeable or not margin_lot or margin_lot <= 0 or funds <= 0:
        return out
    raw_max = funds / margin_lot
    capped = min(raw_max, vmax) if vmax > 0 else raw_max
    max_lot = floor_to_step(capped, step)
    # broker maximum is inclusive; dust below min means "cannot open"
    if max_lot < vmin - 1e-9:
        max_lot = 0.0
    out["max_lot"] = round(max_lot, 8)
    if margin_min and margin_min > 0:
        out["max_positions_min_lot"] = int(funds // margin_min)
    # minimum lot *according to margin*: broker min when affordable, else 0
    out["min_lot_affordable"] = round(vmin, 8) if out["affordable_min"] else 0.0
    return out


def account_sizing(inst: int, funds_source: str = "balance",
                   balance_override: float | None = None,
                   leverage_override: str | None = None,
                   currency_override: str = "") -> dict:
    """Sizing for every probed symbol on `inst`."""
    acc = _account(inst)
    spots = _live_spots()
    ccy = (currency_override or acc.get("currency", "") or "").strip() or "?"
    probed_ccy = (acc.get("currency", "") or "").strip() or ccy
    # Guarded universe: catalogue-first, currency-consistent, freshly
    # probed - never the previous broker's data after a login switch.
    infos, uni = _probe_universe(inst, probed_ccy, spots)
    lev = parse_leverage(leverage_override or acc.get("leverage", ""))
    bal = float(balance_override) if balance_override is not None else None
    if bal is None:
        key = {"balance": "balance", "equity": "equity",
               "free": "margin_free"}.get(funds_source, "balance")
        bal = _f(acc.get(key, 0.0))
        if key == "margin_free" and bal <= 0:
            bal = _f(acc.get("balance", 0.0))
    funds = bal
    conv = to_usd(bal, ccy, spots)
    # Tick margins arrive in the PROBED account currency.  With no override
    # that is the display currency; with --currency/--balance overrides the
    # figures are converted through USD so a hypothetical stays consistent.
    conv_factor: float | None = None
    if normalize_currency(ccy) != normalize_currency(probed_ccy):
        r_probed, _ = usd_per_unit(probed_ccy, spots)
        r_target, _ = usd_per_unit(ccy, spots)
        if r_probed and r_target:
            conv_factor = r_probed / r_target
    symbols: dict = {}
    for sym, info in infos.items():
        s = size_symbol(sym, info, bal, lev or 0.0, probed_ccy, spots,
                        funds=funds)
        if conv_factor is not None:
            for k in ("margin_per_lot", "margin_per_min"):
                if s.get(k) is not None:
                    s[k] = round(float(s[k]) * conv_factor, 2)
            # max lot/positions depend on margin - recompute in target ccy
            try:
                ml = s.get("margin_per_lot")
                mm = s.get("margin_per_min")
                if ml and ml > 0 and funds > 0:
                    vmax = float(s.get("volume_max", 0) or 0)
                    vmin = float(s.get("volume_min", 0) or 0)
                    step = float(s.get("volume_step", 0) or 0) or 0.01
                    capped = min(funds / ml, vmax) if vmax > 0 else funds / ml
                    max_lot = floor_to_step(capped, step)
                    s["max_lot"] = round(max_lot if max_lot >= vmin - 1e-9
                                         else 0.0, 8)
                    s["max_positions_min_lot"] = int(funds // mm) if mm else 0
                    s["affordable_min"] = bool(s.get("tradeable") and mm
                                               and funds >= mm)
                    s["min_lot_affordable"] = round(vmin, 8) \
                        if s["affordable_min"] else 0.0
            except (TypeError, ValueError):
                pass
            s["margin_method"] = (str(s.get("margin_method", "")) +
                                  f" -> {ccy} x{conv_factor:.4g}")
        symbols[sym] = s
    # quoted symbols first, then the rest alphabetically
    def _key(s: str) -> tuple:
        px = _price_of(infos[s], spots)[0]
        return (0 if px else 1, s)
    ordered = {s: symbols[s] for s in sorted(symbols, key=_key)}
    # Server-side trade blocks (app_run.log: account_trade_allowed=0 while an
    # account looked otherwise healthy).  Sizing is moot when the broker
    # refuses everything - report it instead of a wall of red rows.
    acct_block = str(acc.get("account_trade_allowed", "1") or "1")
    mql_block = str(acc.get("mql_allowed", "1") or "1")
    trade_blocked = acct_block == "0" or mql_block == "0"
    if acct_block == "0":
        block_reason = ("server blocked trading on this account "
                        "(account_trade_allowed=0)")
    elif mql_block == "0":
        block_reason = "EA/automated trading blocked (mql_allowed=0)"
    else:
        block_reason = ""
    n_tradeable = sum(1 for s in symbols.values() if s.get("tradeable"))
    n_priced = sum(1 for s in symbols.values()
                   if s.get("tradeable") and s.get("margin_per_lot"))
    n_aff = sum(1 for s in symbols.values() if s.get("affordable_min"))
    return {
        "terminal": inst,
        "login": acc.get("login", ""),
        "server": acc.get("server", ""),
        "currency": ccy,
        "probed_currency": probed_ccy,
        "currency_is_cent": is_cent_account(ccy),
        "leverage": acc.get("leverage", "") if leverage_override is None
        else leverage_override,
        "leverage_ratio": lev,
        "balance": round(_f(acc.get("balance", 0.0)), 2),
        "equity": round(_f(acc.get("equity", 0.0)), 2),
        "margin_free": round(_f(acc.get("margin_free", 0.0)), 2),
        "funds_basis": funds_source,
        "funds": round(float(funds), 2),
        "funds_usd": conv["usd"],
        "usd_rate": conv["rate"],
        "usd_rate_method": conv["method"],
        "balance_usd": conv["usd"],
        "n_symbols": len(ordered),
        "n_tradeable": n_tradeable,
        "n_priced": n_priced,
        "n_affordable": n_aff,
        "n_unaffordable": n_priced - n_aff,
        "universe_source": uni.get("source", "empty"),
        "universe_age_s": uni.get("age_s"),
        "probe_consistent": uni.get("consistent", False),
        "probe_note": uni.get("note", ""),
        "account_trade_allowed": acct_block,
        "mql_allowed": mql_block,
        "trade_blocked": trade_blocked,
        "trade_blocked_reason": block_reason,
        "symbols": ordered,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def all_sizings(funds_source: str = "balance") -> dict:
    return {str(i): account_sizing(i, funds_source=funds_source) for i in (1, 2)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _render(sizing: dict, symbol_filter: str = "", top: int = 0,
            affordable_only: bool = False) -> str:
    from spot import BOLD, DIM, RESET, GREEN, RED, YELLOW
    acc = sizing
    if not acc.get("login"):
        return (f"{DIM}terminal {acc.get('terminal')}: no account - "
                f"nothing to size{RESET}")
    lev = acc.get("leverage") or "?"
    ccy = acc.get("currency") or "?"
    pccy = acc.get("probed_currency", ccy) or ccy
    usd_s = (f"≈ ${acc['funds_usd']:,.2f} USD ({acc['usd_rate_method']})"
             if acc.get("funds_usd") is not None
             else f"(no USD rate: {acc.get('usd_rate_method')})")
    head = [
        f"{BOLD}TERMINAL {acc['terminal']}  {acc['login']} @ "
        f"{acc.get('server', '?')}{RESET}  {DIM}{acc['ts']}{RESET}",
        f"  Balance {acc['balance']:,.2f} {pccy}  Equity {acc['equity']:,.2f}  "
        f"Free {acc['margin_free']:,.2f}  Lev {lev}  "
        f"Funds({acc['funds_basis']}) {acc['funds']:,.2f} {ccy} {usd_s}",
    ]
    if acc.get("trade_blocked"):
        head.append(f"  {RED}{BOLD}[!] TRADING BLOCKED: "
                    f"{acc.get('trade_blocked_reason', '')}{RESET}")
        head.append(f"  {RED}    sizing below is moot until the broker "
                    f"reenables it{RESET}")
    # Where the numbers came from: catalogue (per-login, no round trips) vs
    # a live PROBE (87-symbol live universe) vs cache.  After a login switch
    # the universe changes with the broker - this line says which one this is.
    _src = acc.get("universe_source", "?")
    _age = acc.get("universe_age_s")
    _age_s = f"{_age:.0f}s old" if isinstance(_age, (int, float)) else "age n/a"
    _cons = ("consistent" if acc.get("probe_consistent")
             else f"UNCHECKED ({acc.get('probe_note', '')})")
    head.append(f"  {DIM}universe: {acc.get('n_symbols', 0)} symbols via "
                f"{_src} ({_age_s}, probe↔account {_cons}){RESET}")
    head.append(f"  {DIM}affordable on {acc['funds_basis']}: "
                f"{acc.get('n_affordable', 0)}/{acc.get('n_priced', 0)} "
                f"priced tradeable symbols"
                f"{'  (--affordable lists only these)' if not affordable_only else ''}{RESET}")
    if acc.get("currency_is_cent"):
        head.append(f"  {YELLOW}cent account (USC): 100 {ccy} = 1 USD{RESET}")
    if not acc.get("leverage_ratio"):
        head.append(f"  {YELLOW}leverage unknown - margins unavailable "
                    f"(ECN/raw or syncing){RESET}")
    rows = list(acc["symbols"].values())
    if symbol_filter:
        want = symbol_filter.upper()
        rows = [r for r in rows if want in r["symbol"].upper()]
        if not rows:
            return "\n".join(head + [
                f"  {RED}{symbol_filter!r} is unknown to this broker "
                f"({acc.get('n_symbols', 0)} symbols via "
                f"{acc.get('universe_source', '?')}){RESET}",
                f"  {DIM}the logs show such probes failing as 'unknown symbol' "
                f"(e.g. SP500m / USDTHBc.Z on the wrong broker){RESET}"])
    else:
        # compact view: quoted/tradeable first
        rows = [r for r in rows if r["tradeable"] and r["price"]] + \
               [r for r in rows if not (r["tradeable"] and r["price"])]
    if affordable_only:
        rows = [r for r in rows if r.get("affordable_min")]
        if not rows:
            return "\n".join(head + [
                f"  {YELLOW}no symbol's minimum lot fits "
                f"{acc['funds']:,.2f} {ccy} on {acc['funds_basis']}{RESET}"])
    if top > 0:
        rows = rows[:top]
    w = max([8] + [len(r["symbol"]) for r in rows]) + 2
    head.append(f"  {DIM}{'SYMBOL':<{w}}{'MIN':>7}{'MAXLOT':>9}{'MAXPOS':>8}"
                f"{'MGN/MIN':>12}{'MGN/LOT':>12}  METHOD{RESET}")
    for r in rows:
        if not r["tradeable"]:
            head.append(f"  {r['symbol']:<{w}}{DIM}DISABLED by broker{RESET}")
            continue
        if r["margin_per_lot"] is None:
            head.append(f"  {r['symbol']:<{w}}{DIM}no price/tick yet "
                        f"({r['margin_method']}){RESET}")
            continue
        ok = r["affordable_min"]
        c = GREEN if ok else RED
        head.append(
            f"  {r['symbol']:<{w}}{r['volume_min']:>7.2f}"
            f"{r['max_lot']:>9.2f}{r['max_positions_min_lot']:>8}"
            f"{c}{r['margin_per_min']:>12,.2f}{r['margin_per_lot']:>12,.2f}"
            f"{RESET}  {DIM}{r['margin_method']}{RESET}")
    head.append(f"  {DIM}MIN=broker min lot  MAXLOT=biggest single position on "
                f"{acc['funds_basis']}  MAXPOS=how many MIN lots fit  "
                f"MGN=margin in {ccy}{RESET}")
    return "\n".join(head)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Position sizer: broker-probed lots/margins per pair + "
                    "USD equivalent of any balance currency")
    ap.add_argument("--terminal", type=int, choices=(0, 1, 2), default=0,
                    help="0 = both terminals (default)")
    ap.add_argument("--symbol", type=str, default="",
                    help="filter to symbols containing this text")
    ap.add_argument("--funds", choices=("balance", "equity", "free"),
                    default="balance", help="funds basis (default balance)")
    ap.add_argument("--balance", type=float, default=None,
                    help="override funds with this amount (in account ccy)")
    ap.add_argument("--leverage", type=str, default="",
                    help="override leverage (e.g. 1:400)")
    ap.add_argument("--currency", type=str, default="",
                    help="override account currency (e.g. KES, USC, USD)")
    ap.add_argument("--top", type=int, default=0,
                    help="show only first N symbols (default all)")
    ap.add_argument("--affordable", action="store_true",
                    help="list only symbols whose minimum lot fits the funds "
                         "(short list on tiny balances, e.g. ~300 USC live)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    insts = (1, 2) if args.terminal == 0 else (args.terminal,)
    out = {str(i): account_sizing(
        i, funds_source=args.funds,
        balance_override=args.balance,
        leverage_override=(args.leverage or None),
        currency_override=args.currency) for i in insts}

    if args.json:
        import json
        print(json.dumps({"ok": True, **out}, indent=1))
        return 0
    for i in insts:
        print(_render(out[str(i)], symbol_filter=args.symbol, top=args.top,
                      affordable_only=args.affordable))
        if len(insts) > 1 and i == 1:
            print()
            print("-" * 70)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
