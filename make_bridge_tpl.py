#!/usr/bin/env python3.12

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import setup_logging
from spot import TERMINALS

log = setup_logging(__name__)

TPL_NAME = "Bridge.tpl"

MINIMAL_TPL = """<chart>
id=130000000000000001
symbol=EURUSD
period_type=0
period_size=1
digits=5
tick_size=0.000000
position_time=0
scale_fix=0
scale_fixed_min=0.000000
scale_fixed_max=0.000000
scale_fix11=0
scale_bar=0
scale_bar_val=1.000000
scale=8
mode=1
fore=0
grid=1
volume=0
scroll=1
shift=1
shift_size=10.000000
fixed_pos=0.000000
ohlc=0
bidline=1
askline=0
lastline=0
days=1
descriptions=0
window_left=0
window_top=0
window_right=100
window_bottom=100
window_type=1
window_bg=0
window_fg=0
window_expert=0
expertmode=1
expert=SpotDump.ex5
expert_time=0
period_flags=0
</chart>
"""

def _read_text_utf16(path: Path) -> str:
    return path.read_text(encoding="utf-16", errors="replace")

def minimal_chart_text() -> str:
    """The canonical minimalist chart body (CRLF, ready for UTF-16).

    Shared by the Bridge template and by spot.ensure_minimal_profile(),
    so a chart opened from the template and a chart opened from the boot
    profile are byte-identical - which is what makes the terminals come
    up the same, minimal way.
    """
    return MINIMAL_TPL.replace("\r\n", "\n").replace("\n", "\r\n")

def build_tpl_text() -> str:
    """Canonical minimal Bridge template - deliberately deterministic.

    The old version preferred copying a pre-existing ADX.tpl (often a
    busy, indicator-laden broker template).  That is why terminal 1 could
    open cluttered while terminal 2 opened minimal.  The minimal text is
    also what guarantees the SpotDump EA is attached to the chart.
    """
    return minimal_chart_text()

def needs_install(dst: Path, tpl_text: str) -> bool:
    if not dst.exists():
        return True
    try:
        current = _read_text_utf16(dst)
    except OSError:
        return True
    # compare CONTENT, not just a marker: an install that already carried
    # the old (non-minimal) template would otherwise never be upgraded,
    # so terminal 1 stayed cluttered forever
    return (current.replace("\r\n", "\n")
            != tpl_text.replace("\r\n", "\n"))

def install() -> bool:
    tpl_text = build_tpl_text()
    ok = True
    for inst in (1, 2):
        tpl_dir = TERMINALS[inst]["dir"] / "MQL5" / "Profiles" / "Templates"
        legacy_dir = TERMINALS[inst]["dir"] / "Profiles" / "Templates"
        try:
            tpl_dir.mkdir(parents=True, exist_ok=True)
            dst = tpl_dir / TPL_NAME
            if needs_install(dst, tpl_text):
                dst.write_text(tpl_text, encoding="utf-16")
                log.info(f"terminal {inst}: wrote minimal {TPL_NAME}")
            else:
                log.debug(f"terminal {inst}: {TPL_NAME} already minimal")
            legacy_dir.mkdir(parents=True, exist_ok=True)
            legacy_dst = legacy_dir / TPL_NAME
            if needs_install(legacy_dst, tpl_text):
                legacy_dst.write_text(tpl_text, encoding="utf-16")
                log.info(f"terminal {inst}: wrote {legacy_dst}")
        except OSError as exc:
            log.error(f"terminal {inst}: template install failed: {exc}")
            ok = False
    return ok

if __name__ == "__main__":
    raise SystemExit(0 if install() else 1)
