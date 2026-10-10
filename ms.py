#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import os
import re
import select
import sys
import time
import datetime as dt

from config import CONFIG, setup_logging

from spot import read_spots, candles, feed_age, BOLD, DIM, RESET, BLUE, RED, YELLOW, CYAN

log = setup_logging(__name__)

SEC = CONFIG.ms_candle_seconds
WINDOW_DEFAULT = CONFIG.ms_window
MAX_BARS = CONFIG.ms_max_bars
SYMBOL_DEFAULT = CONFIG.default_symbol

GLYPH_WICK = "│"
GLYPH_BODY = "█"
GLYPH_GRID = "·"

CANDLE_COLORS = {"up": BLUE, "down": RED, "dim": DIM}

class CandleBuilder:

    __slots__ = ("bars", "cur", "last_mid", "symbol")

    def __init__(self, symbol: str):
        self.bars: list[dict] = []
        self.cur: dict | None = None
        self.last_mid = 0.0
        self.symbol = symbol

    def seed_from_m1(self) -> None:
        self.bars = []
        for b in candles(self.symbol, limit=MAX_BARS):
            self.bars.append({
                "t": int(b["t"]) * 60,
                "o": float(b["o"]), "h": float(b["h"]),
                "l": float(b["l"]), "c": float(b["c"]),
                "v": int(b["v"]),
            })

    def tick(self) -> bool:
        bid, ask, _ = read_spots().get(self.symbol, ("", "", ""))
        if not bid or not ask:
            return False
        try:
            mid = (float(bid) + float(ask)) / 2.0
        except ValueError:
            return False
        if mid <= 0:
            return False

        bucket = int(time.time() // SEC) * SEC
        changed = False

        if self.cur and bucket != self.cur["t"]:
            self.bars.append(self.cur)
            self.bars = self.bars[-MAX_BARS:]
            self.cur = None
            changed = True

        if self.cur is None:
            self.cur = {"t": bucket, "o": mid, "h": mid, "l": mid,
                        "c": mid, "v": 1}
            changed = True
        elif mid != self.last_mid:
            self.cur["h"] = max(self.cur["h"], mid)
            self.cur["l"] = min(self.cur["l"], mid)
            self.cur["c"] = mid
            self.cur["v"] += 1
            changed = True

        self.last_mid = mid
        return changed

    def window(self, n: int) -> list[dict]:
        all_bars = self.bars + ([self.cur] if self.cur else [])
        return all_bars[-max(1, n):]

def _row_of(p: float, hi: float, lo: float, rows: int) -> int:
    r = int((hi - p) / (hi - lo) * (rows - 1))
    return max(0, min(rows - 1, r))

def available_symbols(prefer: str = "") -> list[str]:
    """Quoted symbols, stable order (preferred + majors first)."""
    try:
        spots = read_spots()
    except Exception:
        spots = {}
    live: list[str] = []
    for s, q in spots.items():
        try:
            if float(q[0]) > 0:
                live.append(s)
        except (TypeError, ValueError, IndexError):
            continue
    if not live:
        return [prefer or SYMBOL_DEFAULT]
    majors = ("EURUSD", "XAUUSD247", "XAUUSD", "GBPUSD", "USDJPY",
              "GBPJPY", "AUDUSD", "USDCAD", "XAGUSD")
    rank = {s: i for i, s in enumerate(majors)}
    live.sort(key=lambda s: (rank.get(s, 99), s))
    if prefer and prefer in live:
        live.remove(prefer)
        live.insert(0, prefer)
    elif prefer and prefer not in live:
        live.insert(0, prefer)
    return live

def digits_for(symbol: str) -> int:
    """Quote precision off a live price (right for suffixed variants)."""
    try:
        bid = (read_spots().get(symbol) or ("",))[0] or ""
        if "." in str(bid):
            return max(0, len(str(bid).split(".")[1]))
    except Exception:
        pass
    s = symbol.upper()
    return 3 if s.startswith("XAG") else (2 if s.startswith("XAU") else 5)

def render_bar(symbols: list[str], sel: int,
               width: int) -> tuple[list[str], list[tuple[int, int, int, str]]]:
    """Symbol buttons.  Returns (lines, [(line, x0, x1, symbol), ...])
    with plain-text coordinates for click mapping."""
    lines: list[str] = []
    mapping: list[tuple[int, int, int, str]] = []
    parts: list[str] = []
    plain = ""
    li = 0
    for i, s in enumerate(symbols):
        key = str((i + 1) % 10) if i < 10 else "-"
        label = f"[{key}]{s}"
        if plain and len(plain) + len(label) + 1 > width:
            lines.append("".join(parts))
            parts, plain = [], ""
            li += 1
        x0 = len(plain)
        parts.append((f"{BOLD}{BLUE}{label}{RESET}" if i == sel
                      else f"{DIM}{label}{RESET}") + " ")
        mapping.append((li, x0, x0 + len(label), s))
        plain += label + " "
    if parts:
        lines.append("".join(parts))
    return lines, mapping

_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([mM])")

def parse_clicks(buf: str) -> tuple[list[tuple[int, int]], str]:
    """Pull xterm SGR mouse presses out of `buf`; return (clicks, rest)."""
    clicks: list[tuple[int, int]] = []

    def _sub(m: re.Match) -> str:
        if m.group(4) == "M" and int(m.group(1)) in (0, 1, 2):
            clicks.append((int(m.group(2)), int(m.group(3))))
        return ""

    return clicks, _MOUSE_RE.sub(_sub, buf)

class UiInput:
    """Non-blocking keyboard + xterm mouse clicks.  Silent no-op when
    stdin is not a tty (piped/--once use)."""

    def __init__(self) -> None:
        self.ok = False
        self._old = None
        try:
            if sys.stdin.isatty():
                import termios
                import tty
                self._old = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
                sys.stdout.write("\033[?1000h\033[?1006h")
                sys.stdout.flush()
                self.ok = True
        except Exception:
            self.ok = False

    def close(self) -> None:
        try:
            if self.ok:
                sys.stdout.write("\033[?1000l\033[?1006l")
                sys.stdout.flush()
                if self._old is not None:
                    import termios
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                                      self._old)
        except Exception:
            pass
        self.ok = False

    def drain(self) -> str:
        if not self.ok:
            return ""
        try:
            out = ""
            for _ in range(8):
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if not r:
                    break
                chunk = os.read(sys.stdin.fileno(), 256).decode(
                    errors="replace")
                if not chunk:
                    break
                out += chunk
            return out
        except Exception:
            return ""

def render_candles(bars: list[dict], width: int, height: int,
                   digits: int = 5) -> str:
    if not bars:
        return f"{DIM}  collecting data...{RESET}"

    rows = max(6, height)
    gutter = 10
    chart_w = max(12, width - gutter)

    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    if hi - lo < 1e-9:
        band = max(abs(hi) * 1e-6, 1e-6)
        hi += band
        lo -= band
    pad = (hi - lo) * 0.06
    hi += pad
    lo -= pad
    span = hi - lo

    n = len(bars)
    slot = chart_w / n
    # Floor 1 (was 2): with 30 narrow candles a 2-wide body bleeds into the
    # neighbours; proportional width keeps wide candles wide when few bars.
    body_w = max(1, int(slot * 0.62))

    grid: list[list[tuple[str, str]]] = [
        [(" ", "") for _ in range(chart_w)] for _ in range(rows)]

    for gy in range(0, rows, max(3, rows // 5)):
        for x in range(chart_w):
            grid[gy][x] = (GLYPH_GRID, "dim")

    for i, b in enumerate(bars):
        col = "up" if b["c"] >= b["o"] else "down"
        cx = min(chart_w - 1, int(i * slot + slot / 2))
        x0 = max(0, cx - body_w // 2)
        x1 = min(chart_w - 1, x0 + body_w - 1)

        r_top, r_bot = _row_of(b["h"], hi, lo, rows), _row_of(b["l"], hi, lo, rows)
        r_ob = _row_of(max(b["o"], b["c"]), hi, lo, rows)
        r_oc = _row_of(min(b["o"], b["c"]), hi, lo, rows)

        for r in range(r_top, r_bot + 1):
            ch, _ = grid[r][cx]
            if ch != GLYPH_BODY:
                grid[r][cx] = (GLYPH_WICK, col)
        for r in range(r_ob, r_oc + 1):
            for x in range(x0, x1 + 1):
                grid[r][x] = (GLYPH_BODY, col)

    last = bars[-1]
    last_col = "up" if last["c"] >= last["o"] else "down"
    last_row = _row_of(last["c"], hi, lo, rows)

    def cell(ch: str, tag: str) -> str:
        if ch == " " or not tag:
            return ch
        return f"{CANDLE_COLORS[tag]}{ch}{RESET}"

    lines: list[str] = []
    for r in range(rows):
        p = hi - r * span / (rows - 1)
        if r == last_row:
            lab = f"{BOLD}{CANDLE_COLORS[last_col]}{p:>9.{digits}f} {RESET}"
        else:
            lab = f"{DIM}{p:>9.{digits}f} {RESET}"
        lines.append(lab + "".join(cell(ch, tag) for ch, tag in grid[r]))
    return "\n".join(lines)

def draw(cb: CandleBuilder, symbol: str, width: int, height: int,
         window_n: int, symbols: list[str],
         sel: int) -> tuple[str, list[tuple[int, int, int, str]]]:
    bars = cb.window(window_n)
    age = feed_age(1)
    now = dt.datetime.now().strftime("%H:%M:%S")

    if age > 30:
        status = f"{RED}STALE {age:.0f}s{RESET}"
    elif age > 5:
        status = f"{YELLOW}LAG {age:.0f}s{RESET}"
    else:
        status = f"{BLUE}LIVE {age*1000:.0f}ms{RESET}"

    digits = digits_for(symbol)
    bar_lines, bar_map = render_bar(symbols, sel, width)
    bar_map = [(li + 3, x0, x1, s) for li, x0, x1, s in bar_map]
    bar_txt = "\n".join(bar_lines)
    hint = (f"{DIM}click a button or press 1-9 · [ ] cycle · q quit{RESET}"
            if symbols else "")

    if len(bars) < 2:
        return ((f"{BOLD}{CYAN}┌─ MS {SEC}s ─────────────────────────────────────────┐{RESET}\n"
                 f"{BOLD}{CYAN}│{RESET} {symbol:<7} {DIM}{now}{RESET}  {status}  "
                 f"{DIM}warming up...{RESET}\n"
                 f"{BOLD}{CYAN}└──────────────────────────────────────────────────┘{RESET}\n"
                 f"{bar_txt}\n{hint}"), bar_map)

    last, first = bars[-1], bars[0]
    delta = last["c"] - first["o"]
    dcol = BLUE if delta >= 0 else RED

    head = (f"{BOLD}{CYAN}┌─ MS {SEC}s ─────────────────────────────────────────┐{RESET}\n"
            f"{BOLD}{CYAN}│{RESET} {CYAN}{symbol:<7}{RESET} {DIM}{now}{RESET}  {status}  "
            f"last {BOLD}{last['c']:.{digits}f}{RESET}  "
            f"{dcol}{delta:+.{digits}f}{RESET}  "
            f"{DIM}{len(bars)}/{window_n} bars{RESET}\n"
            f"{BOLD}{CYAN}└──────────────────────────────────────────────────┘{RESET}")

    return (head + "\n" + bar_txt + "\n" + hint + "\n"
            + render_candles(bars, width, height, digits), bar_map)

def main() -> int:
    ap = argparse.ArgumentParser(description="Live 15s candlestick terminal chart")
    ap.add_argument("--symbol", default=SYMBOL_DEFAULT)
    ap.add_argument("--once", action="store_true", help="draw one frame and exit")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="tick poll seconds (default 0.1)")
    ap.add_argument("--width", type=int, default=110, help="chart columns")
    ap.add_argument("--height", type=int, default=22, help="chart rows")
    ap.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                    help=f"candles shown (default {WINDOW_DEFAULT})")
    args = ap.parse_args()
    window_n = max(2, args.window)

    builders: dict[str, CandleBuilder] = {}

    def builder_for(sym: str) -> CandleBuilder:
        cb = builders.get(sym)
        if cb is None:
            cb = CandleBuilder(sym)
            cb.seed_from_m1()
            builders[sym] = cb
        return cb

    symbols = available_symbols(args.symbol)
    try:
        sel = symbols.index(args.symbol)
    except ValueError:
        sel = 0
        symbols.insert(0, args.symbol)
    symbol = symbols[sel]
    cb = builder_for(symbol)

    if args.once:
        cb.tick()
        frame, _ = draw(cb, symbol, args.width, args.height, window_n,
                        symbols, sel)
        print(frame)
        return 0

    log.info(f"MS {SEC}s {symbol} chart starting ({window_n}-candle window)")
    if not CONFIG.wineprefix.exists():
        log.warning(f"no MT5 wine prefix found at {CONFIG.wineprefix} - waiting for the bridge")

    ui = UiInput()
    bar_map: list[tuple[int, int, int, str]] = []
    last_frame = ""
    first = True
    no_data_warned = False
    last_relist = 0.0

    def switch(ns: int) -> None:
        nonlocal sel, symbol, cb, last_frame
        if not symbols:
            return
        sel = ns % len(symbols)
        symbol = symbols[sel]
        cb = builder_for(symbol)
        last_frame = ""          # force full redraw on switch

    try:
        while True:
            # --- input: clicks + keys (bounded, non-blocking) ---
            data = ui.drain()
            if data:
                clicks, data = parse_clicks(data)
                for x, y in clicks:
                    for li, x0, x1, s in bar_map:
                        if li == y - 1 and x0 <= x - 1 <= x1:
                            try:
                                switch(symbols.index(s))
                            except ValueError:
                                pass
                            break
                for ch in data:
                    if ch in ("q", "Q", "\x03"):
                        raise KeyboardInterrupt
                    elif ch in "1234567890":
                        idx = (int(ch) - 1) % 10
                        if idx < len(symbols):
                            switch(idx)
                    elif ch == "[":
                        switch(sel - 1)
                    elif ch == "]":
                        switch(sel + 1)
            # --- symbol list refresh (new broker symbols appear) ---
            now_m = time.monotonic()
            if now_m - last_relist > 5.0:
                last_relist = now_m
                fresh = available_symbols(symbol)
                if fresh != symbols:
                    symbols = fresh
                    try:
                        sel = symbols.index(symbol)
                    except ValueError:
                        sel = 0
                        symbol = symbols[0]
                        cb = builder_for(symbol)
                    last_frame = ""
            cb.tick()
            frame, bar_map = draw(cb, symbol, args.width, args.height,
                                  window_n, symbols, sel)

            if "warming up" in frame and not no_data_warned:
                log.warning("no live quote data - ensure MT5 terminals are running with SpotDump EA attached")
                no_data_warned = True

            if frame != last_frame:
                if first:
                    sys.stdout.write("\033[2J\033[H" + frame + "\033[?25l")
                    first = False
                else:
                    sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
                last_frame = frame
            time.sleep(max(args.interval, 0.02))
    except KeyboardInterrupt:
        log.info("MS chart stopped")
        sys.stdout.write("\033[?25h\nbye!\n")
    finally:
        ui.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
