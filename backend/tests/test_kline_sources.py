"""看板 K 线源：WTI 不得打币安 CL=F；451 换 host。"""

import os
import sys
from datetime import datetime, timezone

DASHBOARD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard"))
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)

import kline_sources


def test_wti_routes_to_yahoo_not_binance():
    venue, symbol = kline_sources.resolve_kline_route("WTI")
    assert venue == "yahoo"
    assert symbol == "CL=F"
    assert kline_sources.resolve_kline_route("OIL") == ("yahoo", "CL=F")
    assert kline_sources.resolve_kline_route("BTC") == ("binance", "BTCUSDT")
    assert kline_sources.resolve_kline_route("XAU") == ("binance", "XAUUSDT")


def test_binance_skips_451_then_succeeds():
    class FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "binance.vision" in url or "fapi.binance.com" in url:
            return FakeResp(451, {"msg": "restricted"})
        return FakeResp(200, [[1_700_000_000_000, "1", "2", "0.5", "1.5", "10"]])

    rows, note = kline_sources.fetch_binance_klines("BTCUSDT", 1, 2, get=fake_get)
    assert rows is not None
    assert rows[0]["Close"] == "1.5"
    assert "api.binance.com" in note
    assert any("binance.vision" in url for url in calls)
    assert not any("CL=F" in url for url in calls)


def test_yahoo_parses_cl_futures():
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "timestamp": [1_700_000_000],
                        "indicators": {
                            "quote": [{
                                "open": [70.1],
                                "high": [71.0],
                                "low": [69.5],
                                "close": [70.8],
                                "volume": [123],
                            }]
                        },
                    }]
                }
            }

    rows, note = kline_sources.fetch_yahoo_klines(
        "CL=F",
        datetime(2026, 8, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 16, 2, tzinfo=timezone.utc),
        get=lambda *args, **kwargs: FakeResp(),
    )
    assert rows[0]["Close"] == 70.8
    assert "yahoo:CL=F" in note
