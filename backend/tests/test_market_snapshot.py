import asyncio
import time
from types import SimpleNamespace

import market_snapshot


# Keep fixtures near the real clock while pinning them to 00:05 UTC. This makes
# the next 15m close deterministic without accidentally crossing a 1h close.
_DAY_MS = 24 * 60 * 60 * 1000
BASE_MS = ((int(time.time() * 1000) // _DAY_MS) * _DAY_MS) + (5 * 60 * 1000)


def _rows(timeframe: str, as_of_ms: int, count: int = 240):
    interval = market_snapshot._STRUCTURE_TIMEFRAME_MS[timeframe]
    current_open = (as_of_ms // interval) * interval
    starts = [current_open - ((count - 1 - index) * interval) for index in range(count)]
    rows = []
    for index, opened_at in enumerate(starts):
        open_price = 100.0 + (index * 0.1)
        close = open_price + 0.05
        rows.append([opened_at, open_price, close + 0.1, open_price - 0.1, close, 1000 + index])
    return rows


class FakeSyncExchange:
    def __init__(self, as_of_ms=BASE_MS, failures=None):
        self.as_of_ms = as_of_ms
        self.ticker_ts_ms = as_of_ms
        self.funding_ts_ms = as_of_ms
        self.failures = set(failures or [])
        self.ohlcv_calls = []

    def fetch_ticker(self, symbol):
        return {
            "last": 120.0 if symbol.startswith("BTC") else 2400.0,
            "percentage": 1.25,
            "timestamp": self.ticker_ts_ms,
        }

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001, "timestamp": self.funding_ts_ms}

    def fetch_ohlcv(self, symbol, timeframe, limit, params):
        self.ohlcv_calls.append((symbol, timeframe, limit))
        if (symbol, timeframe) in self.failures:
            raise RuntimeError("offline fixture")
        return _rows(timeframe, self.as_of_ms, count=limit)


class FakeAsyncExchange(FakeSyncExchange):
    async def fetch_ticker(self, symbol):
        return super().fetch_ticker(symbol)

    async def fetch_funding_rate(self, symbol):
        return super().fetch_funding_rate(symbol)

    async def fetch_ohlcv(self, symbol, timeframe, limit, params):
        return super().fetch_ohlcv(symbol, timeframe, limit, params)


def _verified_quality(*args, **kwargs):
    return SimpleNamespace(
        quality_status="verified",
        reason="fixture verified",
        decision_eligible=True,
    )


def _fake_analysis(timeframes, *, as_of_ms, source, source_decision_eligible):
    count = len(timeframes)
    return {
        "status": "ok" if count == 4 else "partial" if count else "unavailable",
        "decision_eligible": source_decision_eligible and count == 4,
        "trend_score": 0.4,
        "trend": "bullish",
        "confidence": 0.8,
        "alignment": "bullish",
        "alignment_score": 0.75,
        "timeframes": {key: {"bars_used": len(value)} for key, value in timeframes.items()},
        "quality": {"source": source},
        "warnings": [],
    }


def _install_common(monkeypatch, exchange):
    market_snapshot._STRUCTURE_OHLCV_CACHE.clear()
    market_snapshot._STRUCTURE_OHLCV_RETRY_AFTER.clear()
    monkeypatch.setattr(market_snapshot, "HAS_CCXT", True)
    monkeypatch.setattr(market_snapshot, "ccxt", SimpleNamespace(__version__="fixture"))
    monkeypatch.setattr(market_snapshot, "ccxt_async", SimpleNamespace(__version__="fixture"))
    monkeypatch.setattr(market_snapshot, "_EXCHANGE", exchange)
    monkeypatch.setattr(market_snapshot.time, "time", lambda: exchange.as_of_ms / 1000.0)
    monkeypatch.setattr(market_snapshot.config, "JIN10_ENABLED", False)
    monkeypatch.setattr(market_snapshot.data_quality, "assess", _verified_quality)
    monkeypatch.setattr(market_snapshot, "analyze_market_structure", _fake_analysis)


def test_closed_candle_filter_and_cache_refresh_at_next_close(monkeypatch):
    exchange = FakeSyncExchange()
    _install_common(monkeypatch, exchange)

    first = market_snapshot._fetch_structure_timeframes_sync("BTC/USDT:USDT", BASE_MS)
    second = market_snapshot._fetch_structure_timeframes_sync("BTC/USDT:USDT", BASE_MS + 1_000)

    assert set(first) == {"15m", "1h", "4h", "1d"}
    assert all(len(rows) >= 220 for rows in first.values())
    assert all(
        row[0] + market_snapshot._STRUCTURE_TIMEFRAME_MS[timeframe] <= BASE_MS
        for timeframe, rows in first.items()
        for row in rows
    )
    assert second == first
    assert len(exchange.ohlcv_calls) == 4

    second["15m"][0][4] = -1
    untouched = market_snapshot._fetch_structure_timeframes_sync("BTC/USDT:USDT", BASE_MS + 2_000)
    assert untouched["15m"][0][4] > 0
    assert len(exchange.ohlcv_calls) == 4

    # The 15m cache must refresh.  Depending on the wall-clock alignment, the
    # 1h boundary may have crossed as well; either outcome is valid.
    later = (BASE_MS // (15 * 60 * 1000)) * (15 * 60 * 1000) + (16 * 60 * 1000)
    exchange.as_of_ms = later
    refreshed = market_snapshot._fetch_structure_timeframes_sync("BTC/USDT:USDT", later)
    assert len(exchange.ohlcv_calls) >= 5
    assert refreshed["15m"][-1][0] > first["15m"][-1][0]


def test_filter_preserves_conflicting_duplicates_for_quality_audit():
    rows = _rows("15m", BASE_MS, count=4)
    duplicate = list(rows[-2])
    duplicate[4] += 0.02
    rows.insert(-1, duplicate)
    rows.insert(0, [float(rows[0][0]) + 0.5, 1, 2, 1, 2, 10])

    closed = market_snapshot._filter_closed_candles(rows, "15m", BASE_MS)

    duplicate_ts = duplicate[0]
    assert sum(row[0] == duplicate_ts for row in closed) == 2
    assert len({row[4] for row in closed if row[0] == duplicate_ts}) == 2
    assert all(not isinstance(row[0], float) or row[0].is_integer() for row in closed)


def test_expired_cache_fails_closed_when_refresh_is_offline(monkeypatch):
    symbol = "BTC/USDT:USDT"
    exchange = FakeSyncExchange()
    _install_common(monkeypatch, exchange)
    assert market_snapshot._fetch_closed_timeframe_sync(symbol, "15m", BASE_MS)
    expires_at = market_snapshot._STRUCTURE_OHLCV_CACHE[(symbol, "15m")]["expires_at_ms"]
    exchange.as_of_ms = expires_at
    exchange.failures.add((symbol, "15m"))

    result = market_snapshot._fetch_closed_timeframe_sync(symbol, "15m", expires_at)

    assert result is None
    assert len(exchange.ohlcv_calls) == 2
    assert market_snapshot._fetch_closed_timeframe_sync(symbol, "15m", expires_at + 1_000) is None
    assert len(exchange.ohlcv_calls) == 2


def test_sync_snapshot_isolates_partial_and_unavailable_timeframes(monkeypatch):
    btc = "BTC/USDT:USDT"
    xau = "XAU/USDT:USDT"
    failures = {(btc, "4h")} | {(xau, timeframe) for timeframe in market_snapshot._STRUCTURE_TIMEFRAMES}
    exchange = FakeSyncExchange(failures=failures)
    _install_common(monkeypatch, exchange)

    snapshot = market_snapshot.get_snapshot_sync()

    assert snapshot["assets"]["BTC"]["market_structure"]["status"] == "partial"
    assert snapshot["assets"]["BTC"]["market_structure"]["decision_eligible"] is False
    assert snapshot["assets"]["XAU"]["market_structure"]["status"] == "unavailable"
    assert snapshot["assets"]["XAU"]["market_structure"]["decision_eligible"] is False
    assert snapshot["assets"]["XAU"]["market_structure"]["timeframes"] == {}
    assert "盘面结构[OKX]" in snapshot["summary"]
    assert "status=partial" in snapshot["summary"]
    assert "status=unavailable" in snapshot["summary"]


def test_structure_cannot_bypass_data_quality_gate(monkeypatch):
    exchange = FakeSyncExchange()
    _install_common(monkeypatch, exchange)
    monkeypatch.setattr(
        market_snapshot.data_quality,
        "assess",
        lambda *args, **kwargs: SimpleNamespace(
            quality_status="rejected", reason="not allowlisted", decision_eligible=False
        ),
    )

    snapshot = market_snapshot.get_snapshot_sync()

    for asset in snapshot["assets"].values():
        structure = asset["market_structure"]
        assert structure["decision_eligible"] is False
        assert structure["quality"]["source"] == "OKX"
        assert structure["quality"]["source_decision_eligible"] is False


def test_real_structure_seam_rejects_stale_cached_timeframes(monkeypatch):
    # Restore the real public seam because common snapshot tests use a compact stub.
    from market_structure import analyze_market_structure

    monkeypatch.setattr(market_snapshot, "analyze_market_structure", analyze_market_structure)
    timeframes = {
        timeframe: market_snapshot._filter_closed_candles(
            _rows(timeframe, BASE_MS), timeframe, BASE_MS
        )
        for timeframe in market_snapshot._STRUCTURE_TIMEFRAMES
    }

    structure = market_snapshot._analyze_structure_safe(
        timeframes,
        as_of_ms=BASE_MS + (3 * 24 * 60 * 60 * 1000),
        source_decision_eligible=True,
    )

    assert structure["decision_eligible"] is False
    assert structure["status"] == "unavailable"
    assert structure["quality"]["source"] == "OKX"
    assert structure["quality"]["source_decision_eligible"] is True


def test_async_snapshot_exposes_same_market_structure_contract(monkeypatch):
    exchange = FakeAsyncExchange()
    _install_common(monkeypatch, exchange)
    monkeypatch.setattr(market_snapshot, "_EXCHANGE_ASYNC", exchange)

    snapshot = asyncio.run(market_snapshot.get_snapshot())

    assert snapshot["assets"]["BTC"]["market_structure"]["status"] == "ok"
    assert snapshot["assets"]["XAU"]["market_structure"]["status"] == "ok"
    assert all(
        asset["market_structure"]["quality"]["source_decision_eligible"] is True
        for asset in snapshot["assets"].values()
    )


def test_legacy_stats_keep_keys_but_summary_uses_bar_semantics():
    candles = _rows("4h", BASE_MS, count=44)[:-1]
    stats = market_snapshot._compute_7d_stats(candles, "4h")
    structure = _fake_analysis(
        {"4h": candles},
        as_of_ms=BASE_MS,
        source="OKX",
        source_decision_eligible=False,
    )
    structure["decision_eligible"] = False
    asset = {
        "price": 120.0,
        "change_24h_str": "+1.00%",
        "funding_rate_str": "+0.0100%",
        "status": "ok",
        "decision_eligible": True,
        "stats_7d": stats,
        "market_structure": structure,
    }

    summary = market_snapshot._build_summary({"BTC": asset}, {}, "partial")

    assert stats["n_up_days"] == stats["up_bars"]
    assert stats["n_down_days"] == stats["down_bars"]
    assert stats["source_tf"] == "4h"
    assert stats["atr_source_tf"] == "4h"
    assert "上涨K线" in summary and "下跌K线" in summary
    assert "ATR(7×4h)" in summary
    assert "连涨" not in summary and "连跌" not in summary


def test_legacy_stats_reject_nonfinite_and_conflicting_rows():
    clean = _rows("1d", BASE_MS, count=9)[:-1]
    nonfinite = [list(row) for row in clean]
    nonfinite[-1][4] = float("nan")
    conflicting = [list(row) for row in clean]
    changed = list(conflicting[-1])
    changed[2] += 1.0
    changed[4] += 0.5
    conflicting.append(changed)

    assert market_snapshot._compute_7d_stats(nonfinite, "1d")["return_7d_str"] == "N/A"
    assert market_snapshot._compute_7d_stats(conflicting, "1d")["return_7d_str"] == "N/A"


def test_legacy_four_hour_alternation_is_not_a_strong_trend():
    interval = market_snapshot._STRUCTURE_TIMEFRAME_MS["4h"]
    opened_at = BASE_MS - (43 * interval)
    price = 100.0
    candles = []
    for index in range(43):
        open_price = price
        price += 0.10 if index % 2 == 0 else -0.09
        candles.append([
            opened_at + (index * interval),
            open_price,
            max(open_price, price) + 0.2,
            min(open_price, price) - 0.2,
            price,
            1_000.0,
        ])

    stats = market_snapshot._compute_7d_stats(candles, "4h")

    assert stats["up_bars"] == 21
    assert stats["down_bars"] == 21
    assert stats["trend"] == "Ranging"
    assert stats["trend_strength"] == "low"


def test_jin10_quote_does_not_promote_okx_structure_or_funding(monkeypatch):
    monkeypatch.setattr(market_snapshot.data_quality, "assess", _verified_quality)
    assets = {
        "XAU": {
            "source": "OKX",
            "decision_eligible": False,
            "funding_decision_eligible": False,
            "stats_7d_decision_eligible": False,
            "market_structure": market_snapshot._unavailable_market_structure("no ohlcv"),
        }
    }
    market_snapshot._apply_jin10_xau(assets, {
        "asset": "XAU",
        "symbol": "XAUUSD",
        "price": 2_400.0,
        "change_pct": 0.2,
        "source": "金十",
        "event_ts": BASE_MS // 1000,
    }, as_of_ms=BASE_MS)

    row = assets["XAU"]
    assert row["source"] == "金十"
    assert row["symbol"] == "XAUUSD"
    assert row["venue"] == "Jin10"
    assert row["instrument_id"] == "XAUUSD"
    assert row["instrument_type"] == "spot_quote"
    assert row["quote_source"] == "金十"
    assert row["quote_venue"] == "Jin10"
    assert row["quote_instrument_id"] == "XAUUSD"
    assert row["quote_instrument_type"] == "spot_quote"
    assert row["decision_eligible"] is True
    assert row["structure_source"] == "OKX"
    assert row["structure_venue"] == "OKX"
    assert row["structure_instrument_id"] == "XAU-USDT-SWAP"
    assert row["structure_instrument_type"] == "perpetual_swap"
    assert row["market_structure"]["decision_eligible"] is False
    assert row["funding_source"] == "OKX"
    assert row["funding_venue"] == "OKX"
    assert row["funding_instrument_id"] == "XAU-USDT-SWAP"
    assert row["funding_instrument_type"] == "perpetual_swap"
    assert row["funding_decision_eligible"] is False
    assert row["stats_7d_decision_eligible"] is False


def test_latest_structure_event_uses_confirmation_time_not_event_kind():
    older_choch = {"kind": "CHoCH", "confirmed_at": 100, "provisional": False}
    newer_bos = {"kind": "BOS", "confirmed_at": 200, "provisional": False}

    selected = market_snapshot._latest_confirmed_structure_event({
        "latest_choch": older_choch,
        "latest_bos": newer_bos,
    })

    assert selected == newer_bos


def test_summary_only_injects_eligible_frames_without_fake_zeroes():
    structure = {
        "source": "OKX",
        "status": "partial",
        "decision_eligible": True,
        "trend": "bullish",
        "trend_score": 0.4,
        "alignment": "mixed",
        "alignment_score": 0.5,
        "confidence": 0.6,
        "timeframes": {
            "15m": {
                "status": "partial",
                "decision_eligible": False,
                "trend": "bearish",
                "trend_score": -1.0,
                "indicators": {"rsi14": 0.0, "atr_pct": 0.0},
                "structure": {},
            },
            "1h": {
                "status": "ok",
                "decision_eligible": True,
                "trend": "bullish",
                "trend_score": 0.4,
                "indicators": {"ema200": None, "rsi14": None, "atr_pct": None},
                "structure": {
                    "latest_choch": {"kind": "CHoCH", "direction": "bearish", "confirmed_at": 100, "provisional": False},
                    "latest_bos": {"kind": "BOS", "direction": "bullish", "confirmed_at": 200, "provisional": False},
                },
            },
        },
    }
    asset = {
        "price": 120.0,
        "change_24h_str": "+1.00%",
        "funding_rate_str": "N/A",
        "funding_decision_eligible": False,
        "status": "ok",
        "decision_eligible": True,
        "stats_7d": market_snapshot._empty_7d_stats(),
        "stats_7d_decision_eligible": False,
        "market_structure": structure,
    }

    summary = market_snapshot._build_summary({"BTC": asset}, {}, "partial")

    assert "15m:" not in summary
    assert "1h:" in summary
    assert "RSI=N/A" in summary and "ATR=N/A" in summary
    assert "BOS:bullish" in summary
    assert "CHoCH:bearish" not in summary


def test_nonfinite_ticker_and_funding_fail_closed(monkeypatch):
    class NonFiniteExchange:
        def fetch_ticker(self, symbol):
            return {"last": float("nan"), "percentage": float("inf")}

        def fetch_funding_rate(self, symbol):
            return {"fundingRate": float("nan")}

    monkeypatch.setattr(market_snapshot, "_EXCHANGE", NonFiniteExchange())
    monkeypatch.setattr(market_snapshot, "_MAX_RETRIES", 0)

    assert market_snapshot._safe_float(float("nan")) == 0.0
    assert market_snapshot._safe_float(float("inf")) == 0.0
    assert market_snapshot._fetch_ticker_sync("BTC/USDT:USDT") is None
    assert market_snapshot._fetch_funding_rate_sync("BTC/USDT:USDT") is None


def test_bid_ask_midpoint_rejects_nonfinite_side():
    assert market_snapshot._extract_price({
        "bid": float("nan"), "ask": 101.0,
    }) is None


def test_bid_ask_midpoint_rejects_nonpositive_side():
    assert market_snapshot._extract_price({
        "bid": -100.0, "ask": 101.0,
    }) is None


def test_bid_ask_midpoint_rejects_crossed_market():
    assert market_snapshot._extract_price({
        "bid": 102.0, "ask": 101.0,
    }) is None


def test_snapshot_uses_exchange_timestamp_and_rejects_stale_quote(monkeypatch):
    exchange = FakeSyncExchange()
    exchange.ticker_ts_ms = BASE_MS - market_snapshot._QUOTE_MAX_AGE_MS - 1
    _install_common(monkeypatch, exchange)

    snapshot = market_snapshot.get_snapshot_sync()

    for asset in snapshot["assets"].values():
        assert asset["quote_at"] == exchange.ticker_ts_ms
        assert asset["quote_freshness"] == "source_timestamp_stale"
        assert asset["decision_eligible"] is False
        assert asset["market_structure"]["decision_eligible"] is False


def test_funding_has_an_independent_source_time_gate(monkeypatch):
    exchange = FakeSyncExchange()
    exchange.funding_ts_ms = BASE_MS - market_snapshot._FUNDING_MAX_AGE_MS - 1
    _install_common(monkeypatch, exchange)

    snapshot = market_snapshot.get_snapshot_sync()

    for asset in snapshot["assets"].values():
        assert asset["decision_eligible"] is True
        assert asset["quote_freshness"] == "source_timestamp_fresh"
        assert asset["funding_at"] == exchange.funding_ts_ms
        assert asset["funding_freshness"] == "source_timestamp_stale"
        assert asset["funding_decision_eligible"] is False


def test_missing_or_future_source_timestamps_fail_closed(monkeypatch):
    class TimestampLessExchange(FakeSyncExchange):
        def fetch_ticker(self, symbol):
            row = super().fetch_ticker(symbol)
            row.pop("timestamp")
            return row

        def fetch_funding_rate(self, symbol):
            return {"fundingRate": 0.0}

    exchange = TimestampLessExchange()
    _install_common(monkeypatch, exchange)
    snapshot = market_snapshot.get_snapshot_sync()
    assert all(asset["decision_eligible"] is False for asset in snapshot["assets"].values())
    assert all(asset["quote_freshness"] == "source_timestamp_missing" for asset in snapshot["assets"].values())
    assert all(asset["funding_decision_eligible"] is False for asset in snapshot["assets"].values())

    future = BASE_MS + market_snapshot._SOURCE_MAX_FUTURE_SKEW_MS + 1
    assert market_snapshot._source_freshness(
        future,
        BASE_MS,
        max_age_ms=market_snapshot._QUOTE_MAX_AGE_MS,
    ) == (False, "source_timestamp_in_future")


def test_snapshot_exposes_exact_okx_instrument_identity(monkeypatch):
    exchange = FakeSyncExchange()
    _install_common(monkeypatch, exchange)

    snapshot = market_snapshot.get_snapshot_sync()

    assert snapshot["assets"]["BTC"]["instrument_id"] == "BTC-USDT-SWAP"
    assert snapshot["assets"]["XAU"]["instrument_id"] == "XAU-USDT-SWAP"
    assert all(
        asset["venue"] == "OKX" and asset["instrument_type"] == "perpetual_swap"
        for asset in snapshot["assets"].values()
    )
    assert all(
        asset["quote_venue"] == "OKX"
        and asset["structure_venue"] == "OKX"
        and asset["funding_venue"] == "OKX"
        and asset["quote_instrument_id"] == asset["instrument_id"]
        and asset["structure_instrument_id"] == asset["instrument_id"]
        and asset["funding_instrument_id"] == asset["instrument_id"]
        for asset in snapshot["assets"].values()
    )


def test_empty_snapshot_keeps_structure_envelope_for_old_callers(monkeypatch):
    monkeypatch.setattr(market_snapshot, "HAS_CCXT", False)

    snapshot = market_snapshot.get_snapshot_sync()

    assert snapshot["status"] == "down"
    for asset in snapshot["assets"].values():
        assert "stats_7d" in asset
        assert asset["stats_7d"]["n_up_days"] == 0
        assert asset["market_structure"]["status"] == "unavailable"
        assert asset["market_structure"]["decision_eligible"] is False
