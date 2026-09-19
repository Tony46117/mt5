#!/usr/bin/env python3.12
"""make_bridge_tpl.py - install Bridge.tpl (expertmode=1) into both terminals.

The chart-level 'Allow algo trading' permission (expertmode) defaults to 0 on
charts MT5 creates for a config-attached EA, which leaves MQL_TRADE_ALLOWED=0
and every OrderSend dies with retcode 10027 even when the algo button is ON.
A start-config 'Template=' that carries expertmode=1 fixes that layer too.

Derives Bridge.tpl from the bundled ADX.tpl when present (keeps any user
chart styling); otherwise writes a minimal chart template.  Idempotent:
skips the write when the installed template already has expertmode=1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG, setup_logging
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


def _candidate_sources() -> list[Path]:
    """Existing templates to derive from, best first (user's own styling)."""
    out: list[Path] = []
    src = CONFIG.spot_dump_src.parent / "Profiles" / "Templates" / "ADX.tpl"
    out.append(src)
    for inst in (1, 2):
        out.append(TERMINALS[inst]["dir"] / "Profiles" / "Templates" / "ADX.tpl")
        out.append(TERMINALS[inst]["dir"] / "MQL5" / "Profiles" / "Templates" / "ADX.tpl")
    return [p for p in out if p.exists()]


def _read_text_utf16(path: Path) -> str:
    return path.read_text(encoding="utf-16", errors="replace")


def build_tpl_text() -> str:
    """Bridge.tpl text: any base template with expertmode forced to 1."""
    for src in _candidate_sources():
        try:
            text = _read_text_utf16(src)
        except OSError as exc:
            log.warning(f"cannot read {src}: {exc}")
            continue
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        lines: list[str] = []
        seen_expertmode = False
        for line in text.splitlines():
            s = line.strip().lower()
            if s.startswith("expertmode"):
                lines.append("expertmode=1")
                seen_expertmode = True
            else:
                lines.append(line)
        if not seen_expertmode:
            lines.append("expertmode=1")
        return "\r\n".join(lines) + "\r\n"
    log.info("no base template found - writing minimal Bridge.tpl")
    return MINIMAL_TPL.replace("\r\n", "\n").replace("\n", "\r\n")


def needs_install(dst: Path, tpl_text: str) -> bool:
    if not dst.exists():
        return True
    try:
        return "expertmode=1" not in _read_text_utf16(dst).lower()
    except OSError:
        return True


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
                log.info(f"terminal {inst}: wrote {dst}")
            else:
                log.info(f"terminal {inst}: {TPL_NAME} already has expertmode=1")
            # legacy dir too - older builds look in Profiles\\Templates first
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
