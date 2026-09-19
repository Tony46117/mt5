"""broker_prober tests: filling decision logic + probe parsing (EA monkeypatched)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import broker_prober as bp


def test_best_filling_from_flags():
    assert bp.best_filling_from_flags(2) == "IOC"       # IOC only
    assert bp.best_filling_from_flags(1) == "FOK"       # FOK only
    assert bp.best_filling_from_flags(3) == "IOC"       # both -> IOC first
    assert bp.best_filling_from_flags(0) == "RETURN"    # neither -> exchange


def _fake_detail(sym="EURUSD"):
    # sym|digits|filling_flags|trade_exemode|trade_mode|order_mode|
    # stops_level|freeze_level|vol_min|vol_max|vol_step|spread_pts
    return f"{sym}|5|3|2|4|1|10|5|0.01|100.00|0.01|15"


class FakeCmd:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def ping(self):
        return (not self.fail, "pong 0")

    def send_batch(self, *commands, timeout=None):
        self.sent.extend(commands)
        if self.fail:
            return [(False, "terminal not responding")] * len(commands)
        return [(True, _fake_detail(s[1])) for s in commands]


@pytest.fixture()
def prober(monkeypatch):
    p = bp.BrokerProber()
    monkeypatch.setattr(bp.BrokerProber, "_sender",
                        staticmethod(lambda inst: FakeCmd()))
    yield p


def test_probe_terminal_parses_fields(prober):
    data = prober.probe_terminal(1)
    assert "EURUSD" in data
    info = data["EURUSD"]
    assert info["digits"] == 5
    assert info["filling_flags"] == 3
    assert info["best"] == "IOC"
    assert info["trade_mode_name"] == "FULL"
    assert info["volume_step"] == 0.01
    assert info["spread_pts"] == 15
    # cached in the prober
    assert prober.best_filling(1, "EURUSD") == "IOC"
    assert "GBPUSD" in prober.report()["terminals"]["1"]   # fake answers all


def test_probe_terminal_ping_failure_is_empty(monkeypatch):
    p = bp.BrokerProber()
    monkeypatch.setattr(bp.BrokerProber, "_sender",
                        staticmethod(lambda inst: FakeCmd(fail=True)))
    assert p.probe_terminal(1) == {}
    assert p.best_filling(1, "EURUSD") is None


def test_probe_all_skips_logged_out(monkeypatch):
    # patch the name broker_prober actually uses (its own import)
    monkeypatch.setattr(bp, "read_accounts", lambda: {1: {}, 2: {}})
    p = bp.BrokerProber()
    calls = []
    monkeypatch.setattr(p, "probe_terminal",
                        lambda inst, symbols=bp.PROBE_SYMBOLS: calls.append(inst) or {})
    p.probe_all()
    assert calls == []                                    # nobody logged in


def test_report_shape(prober):
    prober.probe_terminal(1)
    rep = prober.report()
    assert rep["ok"] is True
    assert "1" in rep["probed_at"]
    assert rep["terminals"]["1"]["EURUSD"]["best"] == "IOC"
