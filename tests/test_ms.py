"""ms.py renderer + CandleBuilder tests (no bridge needed - pure functions)."""

import time

import ms


def test_render_candles_basic():
    bars = [
        {"o": 1.1000, "h": 1.1050, "l": 1.0980, "c": 1.1030},
        {"o": 1.1030, "h": 1.1080, "l": 1.1020, "c": 1.1070},
        {"o": 1.1070, "h": 1.1090, "l": 1.0950, "c": 1.0960},
        {"o": 1.0960, "h": 1.1020, "l": 1.0940, "c": 1.1010},
        {"o": 1.1010, "h": 1.1060, "l": 1.1000, "c": 1.1050},
    ]
    out = ms.render_candles(bars, width=80, height=18)
    assert isinstance(out, str) and out.strip()
    lines = out.splitlines()
    assert len(lines) == 18, "height must be honoured"
    # bodies are block glyphs
    assert ms.GLYPH_BODY in out
    # blue ANSI code present (up candles), no green anywhere
    assert "\033[94m" in out
    assert "\033[92m" not in out, "no green allowed in the chart theme"


def test_render_candles_doji_and_flat():
    # doji (o == c) and fully flat series must not crash
    bars = [{"o": 1.1, "h": 1.1, "l": 1.1, "c": 1.1}] * 3
    out = ms.render_candles(bars, width=60, height=10)
    assert out.strip()
    assert "1.10000" in out


def test_render_candles_empty():
    assert "collecting" in ms.render_candles([], width=60, height=10)


def test_render_candles_width_respected():
    bars = [{"o": 1.0 + i * 0.001, "h": 1.002 + i * 0.001,
             "l": 0.999 + i * 0.001, "c": 1.001 + i * 0.001} for i in range(5)]
    out = ms.render_candles(bars, width=70, height=12)
    for line in out.splitlines():
        # visible length (ANSI codes stripped) fits within width+slack
        import re
        visible = re.sub(r"\033\[[0-9;]*m", "", line)
        assert len(visible) <= 70 + 2, f"line too wide: {len(visible)}"


def test_candlebuilder_tick_and_window():
    cb = ms.CandleBuilder("EURUSD")
    assert cb.window() == []
    # drive tick() with a patched read_spots; mid = p (bid=p-0.0001, ask=p+0.0001)
    prices = [1.1000, 1.1004, 1.0990]
    state = {"i": 0}

    def fake_read_spots():
        p = prices[min(len(prices) - 1, state["i"])]
        state["i"] += 1
        return {"EURUSD": (str(p - 0.0001), str(p + 0.0001), "ts")}

    ms.read_spots = fake_read_spots
    for _ in range(3):
        cb.tick()
    assert cb.cur is not None
    assert cb.cur["o"] == 1.1000
    assert cb.cur["h"] == 1.1004, "high must include mid of tick 2"
    assert cb.cur["l"] == 1.0990, "low must include mid of tick 3"
    assert cb.cur["c"] == 1.0990
    assert cb.cur["v"] == 3
    assert cb.window() == [cb.cur]


def test_candlebuilder_new_bucket_rolls_bar():
    cb = ms.CandleBuilder("EURUSD")
    cb.cur = {"t": int(time.time() // ms.SEC) * ms.SEC - ms.SEC,
              "o": 1.1, "h": 1.1, "l": 1.0995, "c": 1.0998, "v": 5}
    prices = [1.1000]

    def fake_read_spots():
        return {"EURUSD": (str(prices[0] - 0.0001), str(prices[0] + 0.0001), "ts")}

    ms.read_spots = fake_read_spots
    changed = cb.tick()
    assert changed
    assert len(cb.bars) == 1, "completed candle must move into history"
    assert cb.cur["t"] == int(time.time() // ms.SEC) * ms.SEC


def test_draw_header_contains_status():
    cb = ms.CandleBuilder("EURUSD")
    now_bucket = int(time.time() // ms.SEC) * ms.SEC
    cb.bars = [{"t": now_bucket - ms.SEC, "o": 1.1, "h": 1.105, "l": 1.099, "c": 1.104, "v": 9}]
    cb.cur = {"t": now_bucket, "o": 1.104, "h": 1.106, "l": 1.1035, "c": 1.1055, "v": 3}
    out = ms.draw(cb, "EURUSD", 100, 18)
    assert "MS 15s" in out
    assert "bars" in out
    assert "\033[92m" not in out, "no green in the header either"
