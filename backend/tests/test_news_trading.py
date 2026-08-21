import importlib.util
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("news_trading", ROOT / "NewsTrading.py")
news_trading = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(news_trading)


def test_decimal_rounding_and_url_encoded_signature():
    client = news_trading.BinanceFutures("key", "secret")
    assert client.round_step("1.239", "0.01") == Decimal("1.23")
    assert client.round_step("0.00019", "0.0001") == Decimal("0.0001")
    first = client.create_signature({"symbol": "BTC USDT", "note": "a+b"})
    second = client.create_signature({"symbol": "BTC USDT", "note": "a+b"})
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "item, expected",
    [
        ({"action": "BUY", "score": 0.6}, "LONG"),
        ({"action": "SELL", "score": -0.6}, "SHORT"),
        ({"action": "HOLD", "score": 0.9}, None),
        ({"action": "BUY", "score": -0.9}, None),
        ({"action": "SELL", "score": 0.9}, None),
        ({"action": "BUY", "score": 0.5}, None),
    ],
)
def test_signal_direction_requires_action_score_consistency(item, expected):
    assert news_trading.signal_direction(item, 0.5) == expected


def test_first_run_establishes_baseline_without_trade(monkeypatch):
    state = {key: None for key in news_trading.SYMBOL_MAP}
    monkeypatch.setattr(news_trading, "open_and_trail", lambda *args: pytest.fail("must not trade baseline"))
    changed = news_trading.process_news(
        object(), {"BTC": {"id": 10, "action": "BUY", "score": 0.9}}, state, baseline=True
    )
    assert changed is True
    assert state["BTC"]["id"] == 10


def test_new_asset_establishes_baseline_without_trade(monkeypatch):
    state = {"BTC": {"id": 10}, "XAU": None, "WTI": None}
    monkeypatch.setattr(news_trading, "open_and_trail", lambda *args: pytest.fail("must not trade new asset baseline"))
    changed = news_trading.process_news(
        object(), {"XAU": {"id": 20, "action": "BUY", "score": 0.9}}, state
    )
    assert changed is True
    assert state["XAU"]["id"] == 20


def test_failed_trade_does_not_advance_cursor(monkeypatch):
    state = {key: None for key in news_trading.SYMBOL_MAP}
    state["BTC"] = {"id": 10}

    def fail(*args):
        raise RuntimeError("order failed")

    monkeypatch.setattr(news_trading, "open_and_trail", fail)
    with pytest.raises(RuntimeError, match="order failed"):
        news_trading.process_news(
            object(), {"BTC": {"id": 11, "action": "BUY", "score": 0.9}}, state
        )
    assert state["BTC"]["id"] == 10


def test_protection_failure_retries_then_emergency_closes(monkeypatch):
    class Client:
        trail_calls = 0
        closed = False

        def validate_symbol(self, symbol):
            return {"status": "TRADING"}

        def get_positions(self):
            return []

        def set_margin_type_isolated(self, symbol):
            return {}

        def set_leverage(self, symbol, leverage):
            return {}

        def get_price(self, symbol):
            return Decimal("30000")

        def get_symbol_filters(self, symbol):
            return {"stepSize": Decimal("0.001"), "minQty": Decimal("0.001")}

        def round_step(self, value, step):
            return news_trading.BinanceFutures.round_step(value, step)

        def market_order(self, symbol, side, quantity):
            return {"orderId": 1}

        def trailing_stop_order(self, *args):
            self.trail_calls += 1
            raise RuntimeError("protection rejected")

        def close_position(self, symbol):
            self.closed = True
            return {"orderId": 2, "reduceOnly": True}

    client = Client()
    monkeypatch.setattr(news_trading.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="紧急平仓"):
        news_trading.open_and_trail(client, "BTC", "LONG", {"id": 1})
    assert client.trail_calls == 2
    assert client.closed is True


def test_atomic_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = {"BTC": {"id": 1}}
    news_trading.save_state_atomic(state, str(path))
    assert news_trading.load_state(str(path)) == (state, True)
    assert not list(tmp_path.glob("*.tmp"))


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.pid = 4321
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_process_manager_duplicate_start_status_and_stop(tmp_path):
    from api_server import TradingProcessManager

    manager = TradingProcessManager(
        str(ROOT / "NewsTrading.py"), str(ROOT), str(tmp_path / "trading.log"), FakeProcess
    )
    assert manager.start() == {"started": True, "pid": 4321}
    assert manager.start()["reason"] == "already_running"
    status = manager.status()
    assert status["running"] is True
    assert status["pid"] == 4321
    assert "key" not in status and "secret" not in status
    assert manager.stop() == {"stopped": True, "killed": False}
    assert manager.status()["running"] is False
