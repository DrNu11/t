"""Regression tests for fail-closed prices used by simulation/labels."""

import time

import api_server
from engine import prices


def test_legacy_gold_fallback_is_off_by_default(monkeypatch):
    monkeypatch.setattr(prices, "ALLOW_LEGACY_PRICE_FALLBACKS", False)
    monkeypatch.setattr(prices, "_fetch_okx_price", lambda _symbol: None)
    monkeypatch.setattr(prices, "_fetch_gold_price", lambda: 2500.0)
    assert prices._get_current_price("XAU") is None


def test_candidate_median_cache_cannot_settle_quick_sim(monkeypatch):
    now = int(time.time())
    monkeypatch.setitem(api_server._MARKET_CACHE, "value", {
        "updated_at": now,
        "items": [{
            "asset": "BTC", "price": 100_000.0, "change24h": 1.0,
            "source": "median", "sourceCount": 4, "updated_at": now,
        }],
    })
    monkeypatch.delenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", raising=False)
    assert api_server._market_price_from_cache("BTC") is None


def test_verified_exchange_cache_is_eligible(monkeypatch):
    now = int(time.time())
    monkeypatch.setitem(api_server._MARKET_CACHE, "value", {
        "updated_at": now,
        "items": [{
            "asset": "BTC", "price": 100_000.0, "change24h": 1.0,
            "source": "Binance", "sourceCount": 1, "updated_at": now,
        }],
    })
    assert api_server._market_price_from_cache("BTC") == 100_000.0


def test_candidate_quote_is_labelled_observation_only(monkeypatch):
    now = int(time.time())
    market = {
        "updated_at": now,
        "items": [{
            "asset": "BTC", "price": 100_000.0, "change24h": 1.0,
            "source": "median", "sourceCount": 4, "updated_at": now,
        }],
    }
    monkeypatch.delenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", raising=False)
    monkeypatch.setattr(api_server, "_get_current_price", lambda _asset: None)
    pairs = api_server._replay_pairs({"tracks": ["crypto"]}, market)
    btc = next(item for item in pairs if item["asset"] == "BTC")
    assert btc["status"] == "OBSERVATION_ONLY"
    assert btc["decision_eligible"] is False
