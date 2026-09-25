#!/usr/bin/env python3.12

from __future__ import annotations

import argparse
import sys
import time
import datetime as dt

from config import CONFIG, setup_logging

from spot import read_spots, candles, feed_age, BOLD, DIM, RESET, BLUE, RED, YELLOW, CYAN

log = setup_logging(__name__)

SEC = CONFIG.ms_candle_seconds
WINDOW = CONFIG.ms_window
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

    def window(self) -> list[dict]:
        all_bars = self.bars + ([self.cur] if self.cur else [])
        return all_bars[-WINDOW:]

def _row_of(p: float, hi: float, lo: float, rows: int) -> int:
    r = int((hi - p) / (hi - lo) * (rows - 1))
    return max(0, min(rows - 1, r))

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
    body_w = max(2, int(slot * 0.62))

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

def draw(cb: CandleBuilder, symbol: str, width: int, height: int) -> str:
    bars = cb.window()
    age = feed_age(1)
    now = dt.datetime.now().strftime("%H:%M:%S")

    if age > 30:
        status = f"{RED}STALE {age:.0f}s{RESET}"
    elif age > 5:
        status = f"{YELLOW}LAG {age:.0f}s{RESET}"
    else:
        status = f"{BLUE}LIVE {age*1000:.0f}ms{RESET}"

    digits = 3 if symbol.upper().startswith("XAG") else (2 if symbol.upper().startswith("XAU") else 5)

    if len(bars) < 2:
        return (f"{BOLD}{CYAN}┌─ MS {SEC}s ─────────────────────────────────────────┐{RESET}\n"
                f"{BOLD}{CYAN}│{RESET} {symbol:<7} {DIM}{now}{RESET}  {status}  "
                f"{DIM}warming up...{RESET}\n"
                f"{BOLD}{CYAN}└──────────────────────────────────────────────────┘{RESET}")

    last, first = bars[-1], bars[0]
    delta = last["c"] - first["o"]
    dcol = BLUE if delta >= 0 else RED

    head = (f"{BOLD}{CYAN}┌─ MS {SEC}s ─────────────────────────────────────────┐{RESET}\n"
            f"{BOLD}{CYAN}│{RESET} {CYAN}{symbol:<7}{RESET} {DIM}{now}{RESET}  {status}  "
            f"last {BOLD}{last['c']:.{digits}f}{RESET}  "
            f"{dcol}{delta:+.{digits}f}{RESET}  "
            f"{DIM}{len(bars)}/{WINDOW} bars{RESET}\n"
            f"{BOLD}{CYAN}└──────────────────────────────────────────────────┘{RESET}")

    return head + "\n" + render_candles(bars, width, height, digits)

def main() -> int:
    ap = argparse.ArgumentParser(description="Live 15s candlestick terminal chart")
    ap.add_argument("--symbol", default=SYMBOL_DEFAULT)
    ap.add_argument("--once", action="store_true", help="draw one frame and exit")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="tick poll seconds (default 0.1)")
    ap.add_argument("--width", type=int, default=110, help="chart columns")
    ap.add_argument("--height", type=int, default=22, help="chart rows")
    args = ap.parse_args()

    cb = CandleBuilder(args.symbol)
    cb.seed_from_m1()

    if args.once:
        cb.tick()
        print(draw(cb, args.symbol, args.width, args.height))
        return 0

    log.info(f"MS {SEC}s {args.symbol} chart starting ({WINDOW}-candle window)")
    if not CONFIG.wineprefix.exists():
        log.warning(f"no MT5 wine prefix found at {CONFIG.wineprefix} - waiting for the bridge")

    last_frame = ""
    first = True
    no_data_warned = False
    try:
        while True:
            cb.tick()
            frame = draw(cb, args.symbol, args.width, args.height)

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
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
