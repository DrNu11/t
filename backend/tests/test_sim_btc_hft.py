"""自动买卖出场规则离线单测，不打行情。"""

from __future__ import annotations

import importlib.util
import os
import sys

SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "sim_btc_buy_1h.py")
)
SPEC = importlib.util.spec_from_file_location("sim_btc_buy_1h", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules["sim_btc_buy_1h"] = mod
SPEC.loader.exec_module(mod)


def _exit(**kwargs):
    base = dict(
        entry=100, peak=100, trough=100, spot=100, held_sec=2,
        take_profit_pct=0.3, stop_loss_pct=0.5, trail_pct=0.5, max_hold_sec=45,
        side="BUY",
    )
    base.update(kwargs)
    return mod.decide_exit(**base)


def test_buy_take_profit():
    assert _exit(spot=100.4, peak=100.4, side="BUY") == "TP"


def test_buy_hard_stop():
    assert _exit(spot=99.4, side="BUY") == "SL"


def test_sell_take_profit():
    assert _exit(spot=99.6, trough=99.6, side="SELL") == "TP"


def test_sell_hard_stop():
    assert _exit(spot=100.6, peak=100.6, side="SELL") == "SL"


def test_buy_trail_from_peak():
    assert _exit(
        entry=100, peak=101, trough=100, spot=100.4, held_sec=10,
        take_profit_pct=2.0, stop_loss_pct=2.0, trail_pct=0.5, side="BUY",
    ) == "TRAIL"


def test_sell_trail_from_trough():
    assert _exit(
        entry=100, peak=100, trough=99, spot=99.6, held_sec=10,
        take_profit_pct=2.0, stop_loss_pct=2.0, trail_pct=0.5, side="SELL",
    ) == "TRAIL"


def test_time_stop():
    assert _exit(spot=100.02, peak=100.05, held_sec=45, max_hold_sec=45) == "TIME"


def test_hold_inside_band():
    assert _exit(spot=100.08, peak=100.1, held_sec=8) is None


def test_signed_pnl_sides():
    assert abs(mod.signed_pnl_pct("BUY", 100, 101) - 1.0) < 1e-9
    assert abs(mod.signed_pnl_pct("SELL", 100, 99) - 1.0) < 1e-9
    assert abs(mod.signed_pnl_pct("SELL", 100, 101) + 1.0) < 1e-9


def test_pick_prefers_strict_then_unused_sell():
    sides = {
        "BUY": {"id": 108, "side": "BUY", "score": 0.688, "gate": "strict"},
        "SELL": {"id": 190, "side": "SELL", "score": -0.45, "gate": "floor"},
    }
    first = mod.pick_trade_signal(sides, [])
    assert first["side"] == "BUY"
    second = mod.pick_trade_signal(sides, [108])
    assert second["side"] == "SELL"
    assert mod.pick_trade_signal(sides, [108, 190]) is None


def test_parse_okx_and_gate_tickers():
    okx = {"data": [{"last": "63063.7"}]}
    gate = [{"last": "63064.2"}]
    assert abs(mod.parse_ticker_payload("okx", okx) - 63063.7) < 1e-9
    assert abs(mod.parse_ticker_payload("gate", gate) - 63064.2) < 1e-9
