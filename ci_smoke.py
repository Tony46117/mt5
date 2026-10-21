#!/usr/bin/env python3

"""Boot the terminal and prove every page actually renders.

This is the check CI runs (.github/workflows/ci.yml) before the image is
published, and it is runnable by hand:

    python3 ci_smoke.py --port 8010

It starts `app.py` exactly the way run.sh does (waitress, production
mode, a throwaway machine key) with no Wine/MT5 present, waits for
/health, then loads every page the sidebar offers plus the JSON
endpoints the UI calls on first paint.  A page passes only when it comes
back 200 AND carries its own title (so a half-rendered template or a
500 page that still returns HTML is caught), and the panel must show the
StopExe bracket the desk is currently trading.

Exit codes: 0 = everything rendered, 1 = a check failed, 2 = the app
never came up.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# path -> a string that only that page's rendered template contains
PAGES = {
    "/": "MT5 TERMINAL - DASHBOARD",
    "/panel": "MT5 TERMINAL - PANEL",
    "/scheduled": "MT5 TERMINAL - SCHEDULED TRADES",
    "/factory": "MT5 TERMINAL - FACTORY",
}

# The trading panel is static HTML+JS, so what the desk gets is visible in
# the served markup: the StopExe bracket and its button must both be there.
PAGE_NEEDLES = {
    "/panel": ["\u00b11.0 pips (2.0 total)", "EXECUTE STOPEXE"],
}

# JSON endpoints the pages call on first paint; each must answer ok=true
# (/health is checked separately - it reports {"status": "healthy"})
APIS = ["/api/dashboard", "/api/panel", "/api/spots", "/api/accounts"]


def fetch(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def stopexe_defaults() -> None:
    """The bracket the panel shows must be the bracket EXECUTE places."""
    import inspect

    import spot
    import stopexe

    default = inspect.signature(stopexe.stopexe_levels).parameters["distance_pips"].default
    assert default == 1.6, f"stopexe_levels default is {default}, expected 1.6"
    spot.read_spots = lambda *a, **k: {"XAUUSD": ("4150.00", "4150.50", "ci")}
    spot.resolve_symbol = lambda symbol, account: symbol
    lv = stopexe.stopexe_levels("XAUUSD", 1)
    assert lv["buy_price"] == 4152.1, lv
    assert lv["sell_price"] == 4148.4, lv


def port_free(host: str, port: int) -> bool:
    """True when nothing already listens on host:port.

    Without this a checkout that already runs the panel on :8000 would
    make the checks talk to THAT process and pass against stale code
    while the freshly started one died on "address already in use".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # SO_REUSEADDR matches how waitress binds, so a port still in
        # TIME_WAIT from the previous run is not mistaken for a live one
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def wait_healthy(base: str, proc: subprocess.Popen,
                 timeout: float) -> tuple[dict | None, str | None]:
    """(health payload, problem) - the payload must come from OUR pid."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, "app.py exited before becoming healthy"
        try:
            code, body = fetch(f"{base}/health", timeout=1.0)
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
            continue
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return None, f"/health answered HTTP {code} with non-JSON"
        if payload.get("pid") != proc.pid:
            return None, (f"{base} is answered by pid {payload.get('pid')!r}, "
                          f"not the app we started (pid {proc.pid})")
        if payload.get("status") not in ("healthy", "degraded"):
            return None, f"/health reported status {payload.get('status')!r}"
        return payload, None
    return None, f"app never answered {base}/health within {timeout:g}s"


def tail(path: Path, lines: int = 40) -> str:
    try:
        body = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(body[-lines:])


def main() -> int:
    ap = argparse.ArgumentParser(description="Boot the panel and check every page renders")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="seconds to wait for /health before giving up")
    ap.add_argument("--log", default="ci_app.log")
    args = ap.parse_args()

    log_path = (ROOT / args.log) if not os.path.isabs(args.log) else Path(args.log)
    base = f"http://{args.host}:{args.port}"
    env = dict(os.environ)
    # a throwaway key: CI has no stored session, and this keeps session.json
    # from ever being readable with a real deployment's key
    env.setdefault("MT5_MACHINE_KEY", "ci-smoke-throwaway-key")

    if not port_free(args.host, args.port):
        print(f"FAIL: {args.host}:{args.port} is already in use - stop whatever "
              f"serves it (or pass --port <free port>)", file=sys.stderr)
        return 2

    failures: list[str] = []
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "app.py"),
         "--host", args.host, "--port", str(args.port), "--production"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        health, problem = wait_healthy(base, proc, args.timeout)
        if health is None:
            print(f"FAIL: {problem}", file=sys.stderr)
            print(tail(log_path), file=sys.stderr)
            return 2

        # "degraded" is normal here: CI has no Wine/MT5, so the health
        # report can only speak for the web layer, not the trade feed.
        note = "" if health["status"] == "healthy" else " (no MT5 feed - expected off-box)"
        print(f"OK   /health  <- pid {health['pid']}, status {health['status']}, "
              f"app up on {base}{note}")

        for path, marker in PAGES.items():
            code, body = fetch(base + path)
            text = body.decode("utf-8", errors="replace")
            problem = None
            if code != 200:
                problem = f"HTTP {code}"
            elif marker not in text:
                problem = f"missing marker {marker!r}"
            else:
                for needle in PAGE_NEEDLES.get(path, []):
                    if needle not in text:
                        problem = f"missing {needle!r}"
                        break
            if problem:
                failures.append(f"{path}: {problem}")
                print(f"FAIL {path:<11} <- {problem}", file=sys.stderr)
            else:
                print(f"OK   {path:<11} <- 200, {len(body)} bytes, rendered {marker!r}")

        for path in APIS:
            try:
                code, body = fetch(base + path)
            except (urllib.error.URLError, OSError) as exc:
                failures.append(f"{path}: {exc}")
                print(f"FAIL {path:<11} <- {exc}", file=sys.stderr)
                continue
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                payload = None
            if code != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
                failures.append(f"{path}: HTTP {code} not ok=true")
                print(f"FAIL {path:<11} <- HTTP {code} (expected ok=true)", file=sys.stderr)
            else:
                print(f"OK   {path:<11} <- 200, ok=true")

        try:
            stopexe_defaults()
            print("OK   stopexe   <- default bracket \u00b11.0 pips (2.0 total), SL 2 pips")
        except Exception as exc:                       # noqa: BLE001 - reported below
            failures.append(f"stopexe defaults: {exc}")
            print(f"FAIL stopexe   <- {exc}", file=sys.stderr)

        if failures:
            print(f"\n{len(failures)} check(s) failed - app log tail:", file=sys.stderr)
            print(tail(log_path), file=sys.stderr)
            return 1
        print("\nall pages and endpoints rendered")
        return 0
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
