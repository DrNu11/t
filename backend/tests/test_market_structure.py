"""Offline contract tests for the causal MarketStructure layer."""

import json
import math
import time

import pytest

import market_structure


class _FakeTalib:
    """Function identities used to exercise the formal-engine contract."""

    EMA = object()
    MACD = object()
    ATR = object()
    RSI = object()
    ADX = object()
    BBANDS = object()
    OBV = object()


def _install_complete_talib(monkeypatch, *, fail_component=None):
    """Install deterministic TA-Lib-shaped outputs without a binary wheel."""

    fake = _FakeTalib()

    def fake_last(function, *arrays, **kwargs):
        if function is fake.EMA:
            period = int(kwargs["timeperiod"])
            component = f"ema{period}"
            if fail_component == component:
                return None
            return market_structure._ema(arrays[0], period)[-1]
        if function is fake.ATR:
            if fail_component == "atr14":
                return None
            highs, lows, closes = arrays
            period = int(kwargs["timeperiod"])
            ranges = [
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - closes[index - 1]),
                    abs(lows[index] - closes[index - 1]),
                )
                for index in range(1, len(closes))
            ]
            current = sum(ranges[:period]) / period
            for value in ranges[period:]:
                current = ((current * (period - 1)) + value) / period
            return current
        if function is fake.RSI:
            if fail_component == "rsi14":
                return None
            return market_structure._rsi(arrays[0], int(kwargs["timeperiod"]))
        if function is fake.ADX:
            return 25.0
        if function is fake.OBV:
            closes, volumes = arrays
            value = 0.0
            for previous, current, volume in zip(closes, closes[1:], volumes[1:]):
                value += volume if current > previous else -volume if current < previous else 0.0
            return value
        return None

    def fake_tuple(function, *arrays, **kwargs):
        closes = arrays[0]
        if function is fake.MACD:
            fast = market_structure._ema(closes, int(kwargs["fastperiod"]))
            slow = market_structure._ema(closes, int(kwargs["slowperiod"]))
            line_values = [
                left - right for left, right in zip(fast, slow)
                if left is not None and right is not None
            ]
            line = line_values[-1]
            signal = market_structure._ema(
                line_values, int(kwargs["signalperiod"])
            )[-1]
            histogram = line - signal
            values = (line, signal, histogram)
            if fail_component and fail_component.startswith("macd."):
                index = {"macd.line": 0, "macd.signal": 1, "macd.histogram": 2}[fail_component]
                values = tuple(None if position == index else value for position, value in enumerate(values))
            return values
        if function is fake.BBANDS:
            period = int(kwargs["timeperiod"])
            window = closes[-period:]
            middle = sum(window) / period
            deviation = math.sqrt(sum((value - middle) ** 2 for value in window) / period)
            values = (middle + 2 * deviation, middle, middle - 2 * deviation)
            if fail_component and fail_component.startswith("bollinger."):
                index = {
                    "bollinger.upper": 0,
                    "bollinger.middle": 1,
                    "bollinger.lower": 2,
                }[fail_component]
                values = tuple(None if position == index else value for position, value in enumerate(values))
            return values
        return None

    monkeypatch.setattr(market_structure, "_HAS_TALIB", True)
    monkeypatch.setattr(market_structure, "_talib", fake)
    monkeypatch.setattr(market_structure, "_ta_last", fake_last)
    monkeypatch.setattr(market_structure, "_ta_tuple", fake_tuple)


def _fixture_rows(timeframe: str, as_of_ms: int, count: int = 260):
    interval = market_structure._TIMEFRAME_MS[timeframe]
    last_open = ((as_of_ms - interval) // interval) * interval
    rows = []
    previous = 100.0
    for index in range(count):
        opened_at = last_open - (count - 1 - index) * interval
        close = 100.0 + index * 0.08 + 1.8 * math.sin(index / 4.0)
        open_price = previous
        high = max(open_price, close) + 0.35
        low = min(open_price, close) - 0.35
        rows.append([opened_at, open_price, high, low, close, 1000.0 + index * 3])
        previous = close
    return rows


def _all_timeframes(as_of_ms: int):
    return {
        timeframe: _fixture_rows(timeframe, as_of_ms)
        for timeframe in market_structure._TIMEFRAME_MS
    }


def _pattern_rows(timeframe: str, as_of_ms: int, *, slope: float, wave: float = 1.0, count: int = 260):
    interval = market_structure._TIMEFRAME_MS[timeframe]
    start = as_of_ms - count * interval
    rows = []
    previous = 100.0
    for index in range(count):
        close = 100.0 + slope * index + wave * math.sin(index * math.pi / 5)
        rows.append([
            start + index * interval,
            previous,
            max(previous, close) + 0.5,
            min(previous, close) - 0.5,
            close,
            1_000.0 + index,
        ])
        previous = close
    return rows


def test_structure_is_causal_and_multitimeframe(monkeypatch):
    _install_complete_talib(monkeypatch)
    as_of_ms = ((int(time.time() * 1000) - 30_000) // 60_000) * 60_000
    result = market_structure.analyze_market_structure(
        _all_timeframes(as_of_ms),
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )

    assert result["source"] == "OKX"
    assert result["status"] == "ok"
    assert set(result["available_timeframes"]) == {"15m", "1h", "4h", "1d"}
    assert result["decision_eligible"] is True
    assert result["quality"]["closed_bar_policy"] == "timestamp_plus_interval_lte_as_of"
    assert result["quality"]["indicator_engine"] == "talib"
    assert result["quality"]["indicator_decision_eligible"] is True
    assert result["quality"]["smc"]["execution_eligible"] is False
    assert result["research"]["smc"]["execution_eligible"] is False

    for timeframe, item in result["timeframes"].items():
        assert item["status"] == "ok"
        assert item["decision_eligible"] is True
        assert item["bars_used"] == 260
        assert item["last_closed_at"] <= as_of_ms
        assert item["indicators"]["ema200"] is not None
        assert item["indicators"]["atr14"] is not None
        assert item["indicators"]["rsi14"] is not None
        assert item["indicators"]["adx14"] is None or 0 <= item["indicators"]["adx14"] <= 100
        assert item["structure"]["status"] in {"ok", "partial"}
        assert all(not event["provisional"] for event in item["structure"]["swing_sequence"])


def test_talib_unavailable_is_native_research_only_and_fails_closed(monkeypatch):
    monkeypatch.setattr(market_structure, "_HAS_TALIB", False)
    monkeypatch.setattr(market_structure, "_talib", None)
    monkeypatch.setattr(market_structure, "_np", None)
    as_of_ms = 1_800_000_000_000

    result = market_structure.analyze_market_structure(
        _all_timeframes(as_of_ms),
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )

    assert result["indicator_engine"] == "native_research"
    assert result["decision_eligible"] is False
    assert result["trend"] != "unknown"
    assert set(result["available_timeframes"]) == set(market_structure._TIMEFRAME_MS)
    assert result["quality"]["indicator_decision_eligible"] is False
    assert result["quality"]["indicator_quality_reason"] == "talib_unavailable"
    assert result["quality"]["decision_ready_timeframes"] == []
    assert "talib_unavailable" in result["warnings"]
    assert "formal_indicator_engine_not_ready" in result["warnings"]
    assert result["research"]["native_indicator_fallback"]["execution_eligible"] is False
    for item in result["timeframes"].values():
        assert item["analysis_eligible"] is True
        assert item["decision_eligible"] is False
        assert item["indicator_engine"] == "native_research"
        assert item["quality"]["indicator_quality_reason"] == "talib_unavailable"
        assert item["research"]["native_indicator_fallback"]["used"] is True
        assert item["research"]["execution_eligible"] is False


def test_talib_required_component_failure_never_uses_hybrid_for_execution(monkeypatch):
    _install_complete_talib(monkeypatch, fail_component="atr14")
    as_of_ms = 1_800_000_000_000

    result = market_structure.analyze_market_structure(
        _all_timeframes(as_of_ms),
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )

    assert result["indicator_engine"] == "talib_with_native_fallback_research"
    assert result["decision_eligible"] is False
    assert result["trend"] != "unknown"
    assert result["quality"]["indicator_quality_reason"] == "talib_incomplete"
    assert result["quality"]["indicator_decision_eligible"] is False
    assert result["quality"]["decision_ready_timeframes"] == []
    assert "talib_incomplete:15m,1h,4h,1d" in result["warnings"]
    assert "formal_indicator_engine_not_ready" in result["warnings"]
    for timeframe, item in result["timeframes"].items():
        assert item["indicators"]["atr14"] is not None
        assert item["analysis_eligible"] is True
        assert item["decision_eligible"] is False
        assert item["quality"]["indicator_quality_reason"] == "talib_incomplete"
        assert "atr14" in result["quality"]["indicator_missing_by_timeframe"][timeframe]


def test_in_progress_and_future_bars_never_enter_analysis():
    as_of_ms = ((int(time.time() * 1000) - 30_000) // 60_000) * 60_000
    rows = _fixture_rows("1h", as_of_ms, count=80)
    interval = market_structure._TIMEFRAME_MS["1h"]
    rows.append([as_of_ms, 200, 201, 199, 200.5, 1000])
    rows.append([as_of_ms + interval, 201, 202, 200, 201.5, 1000])
    result = market_structure.analyze_market_structure(
        {"1h": rows}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True
    )
    item = result["timeframes"]["1h"]
    assert item["bars_used"] == 80
    assert item["last_closed_at"] <= as_of_ms
    assert item["quality"]["rejected"]["future_or_unclosed"] == 2


def test_source_gate_and_invalid_payload_fail_closed():
    as_of_ms = ((int(time.time() * 1000) - 30_000) // 60_000) * 60_000
    rows = _fixture_rows("1h", as_of_ms)
    gated = market_structure.analyze_market_structure(
        {"1h": rows}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=False
    )
    assert gated["decision_eligible"] is False
    assert "source_not_decision_eligible" in gated["warnings"]
    assert gated["quality"]["source_decision_eligible"] is False

    invalid = market_structure.analyze_market_structure(
        {"1h": [[1, "nan", 2, 1, 1.5, 10], "bad", {"timestamp": 2}]},
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )
    assert invalid["status"] == "unavailable"
    assert invalid["decision_eligible"] is False
    assert invalid["timeframes"]["1h"]["quality"]["used"] == 0
    assert "insufficient_fresh_closed_candles" in invalid["warnings"]


def test_smc_style_structure_is_research_only_not_formal_direction():
    as_of_ms = ((int(time.time() * 1000) - 30_000) // 60_000) * 60_000
    candles = [
        tuple(row) for row in _fixture_rows("1h", as_of_ms)
        if row[0] + market_structure._TIMEFRAME_MS["1h"] <= as_of_ms
    ]
    indicators = market_structure._indicators(candles)
    bullish = {"swing_sequence": [{"kind": "HH"}, {"kind": "HL"}], "latest_bos": {"direction": "bullish", "confirmed_at": 2}}
    bearish = {"swing_sequence": [{"kind": "LH"}, {"kind": "LL"}], "latest_bos": {"direction": "bearish", "confirmed_at": 2}}
    assert market_structure._trend(candles, indicators, bullish) == market_structure._trend(candles, indicators, bearish)

    result = market_structure.analyze_market_structure(
        {"1h": _fixture_rows("1h", as_of_ms)},
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )
    assert result["timeframes"]["1h"]["research"]["execution_eligible"] is False


def test_bearish_and_range_regimes_are_bounded_and_json_safe():
    as_of_ms = 1_800_000_000_000
    bearish = {
        timeframe: _pattern_rows(timeframe, as_of_ms, slope=-0.2)
        for timeframe in ("15m", "1h")
    }
    bearish_result = market_structure.analyze_market_structure(
        bearish, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert bearish_result["trend"] == "bearish"
    assert -1.0 <= bearish_result["trend_score"] <= 1.0
    assert 0.0 <= bearish_result["confidence"] <= 1.0

    ranging = {
        timeframe: _pattern_rows(timeframe, as_of_ms, slope=0.0, wave=0.0)
        for timeframe in ("15m", "1h")
    }
    range_result = market_structure.analyze_market_structure(
        ranging, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert range_result["trend"] == "range"
    assert range_result["timeframes"]["1h"]["indicators"]["rsi14"] == 50.0
    json.dumps(range_result, allow_nan=False)


def test_future_rows_cannot_change_indicators_structure_or_score():
    as_of_ms = 1_800_000_000_000
    rows = _pattern_rows("1h", as_of_ms, slope=0.15)
    baseline = market_structure.analyze_market_structure(
        {"1h": rows}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    augmented = list(rows)
    interval = market_structure._TIMEFRAME_MS["1h"]
    augmented.extend([
        [as_of_ms - interval // 2, 1, 50_000, 1, 40_000, 1_000_000],
        [as_of_ms + interval, 1, 80_000, 1, 70_000, 1_000_000],
    ])
    after = market_structure.analyze_market_structure(
        {"1h": augmented}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    before_frame = baseline["timeframes"]["1h"]
    after_frame = after["timeframes"]["1h"]
    assert after_frame["indicators"] == before_frame["indicators"]
    assert after_frame["structure"] == before_frame["structure"]
    assert after_frame["trend_score"] == before_frame["trend_score"]
    assert after_frame["quality"]["rejected"]["future_or_unclosed"] == 2


def test_unordered_duplicates_are_deterministic_and_conflicts_fail_closed():
    as_of_ms = 1_800_000_000_000
    rows = _pattern_rows("1h", as_of_ms, slope=0.1, count=60)
    exact_duplicates = list(reversed(rows)) + [list(rows[7]), list(rows[42])]
    result = market_structure.analyze_market_structure(
        {"1h": exact_duplicates}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert result["timeframes"]["1h"]["bars_used"] == 60
    assert result["timeframes"]["1h"]["quality"]["rejected"]["duplicate"] == 2

    conflicts = []
    for row in rows:
        changed = list(row)
        changed[2] += 0.1
        changed[4] += 0.1
        conflicts.extend([row, changed])
    failed = market_structure.analyze_market_structure(
        {"1h": conflicts}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert failed["status"] == "unavailable"
    assert failed["decision_eligible"] is False
    assert failed["timeframes"]["1h"]["quality"]["rejected"]["conflicting_duplicate"] == 60


def test_stale_single_timeframe_and_implicit_source_authority_fail_closed(monkeypatch):
    _install_complete_talib(monkeypatch)
    as_of_ms = 1_800_000_000_000
    single = market_structure.analyze_market_structure(
        {"15m": _pattern_rows("15m", as_of_ms, slope=0.1)},
        as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert single["decision_eligible"] is False
    assert "insufficient_multi_timeframe_confirmation" in single["warnings"]

    implicit = market_structure.analyze_market_structure(
        _all_timeframes(as_of_ms), as_of_ms=as_of_ms, source="unreviewed",
    )
    assert implicit["decision_eligible"] is False
    assert all(item["decision_eligible"] is False for item in implicit["timeframes"].values())

    stale_as_of = as_of_ms + 3 * market_structure._TIMEFRAME_MS["1d"]
    stale = market_structure.analyze_market_structure(
        _all_timeframes(as_of_ms), as_of_ms=stale_as_of,
        source="OKX", source_decision_eligible=True,
    )
    assert stale["decision_eligible"] is False
    assert all(item["quality"]["stale"] is True for item in stale["timeframes"].values())


def test_severe_gap_timeframe_cannot_pollute_decision_aggregate():
    as_of_ms = 1_800_000_000_000
    clean = {
        timeframe: _pattern_rows(timeframe, as_of_ms, slope=0.2)
        for timeframe in ("15m", "1h")
    }
    baseline = market_structure.analyze_market_structure(
        clean, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    with_gap = dict(clean)
    bearish_4h = _pattern_rows("4h", as_of_ms, slope=-0.2)
    with_gap["4h"] = [row for index, row in enumerate(bearish_4h) if index % 3 == 0 or index == len(bearish_4h) - 1]
    result = market_structure.analyze_market_structure(
        with_gap, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert result["timeframes"]["4h"]["quality"]["severe_gaps"] is True
    assert result["timeframes"]["4h"]["decision_eligible"] is False
    assert result["trend_score"] == baseline["trend_score"]
    assert result["available_timeframes"] == baseline["available_timeframes"]


def test_structure_entities_are_confirmed_and_volume_ratio_has_full_baseline():
    as_of_ms = 1_800_000_000_000
    result = market_structure.analyze_market_structure(
        {"1h": _pattern_rows("1h", as_of_ms, slope=0.03, wave=3.0)},
        as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    structure = result["timeframes"]["1h"]["structure"]
    for swing in structure["swing_sequence"]:
        assert swing["confirmed_at"] <= as_of_ms
        assert swing["lag_bars"] == 2
        assert swing["provisional"] is False
    for key in ("support", "resistance", "latest_bos", "latest_choch"):
        item = structure[key]
        if item is not None:
            assert item["confirmed_at"] <= as_of_ms
            assert item["provisional"] is False

    twenty = _pattern_rows("1h", as_of_ms, slope=0.05, count=20)
    twenty_result = market_structure.analyze_market_structure(
        {"1h": twenty}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert twenty_result["timeframes"]["1h"]["indicators"]["volume_ratio"] is None
    twenty_one = _pattern_rows("1h", as_of_ms, slope=0.05, count=21)
    twenty_one_result = market_structure.analyze_market_structure(
        {"1h": twenty_one}, as_of_ms=as_of_ms, source="OKX", source_decision_eligible=True,
    )
    assert twenty_one_result["timeframes"]["1h"]["indicators"]["volume_ratio"] is not None


def test_single_timeframe_near_two_x_price_jump_is_quarantined_not_scored():
    as_of_ms = 1_800_000_000_000
    clean_15m = _pattern_rows("15m", as_of_ms, slope=0.1)
    clean_1h = _pattern_rows("1h", as_of_ms, slope=0.1)
    jumped_15m = [list(row) for row in clean_15m]
    previous_close = jumped_15m[-2][4]
    jumped_15m[-1][1] = previous_close
    jumped_15m[-1][4] = previous_close * 1.999
    jumped_15m[-1][2] = jumped_15m[-1][4] + 0.5
    jumped_15m[-1][3] = previous_close - 0.5

    result = market_structure.analyze_market_structure(
        {"15m": jumped_15m, "1h": clean_1h},
        as_of_ms=as_of_ms,
        source="OKX",
        source_decision_eligible=True,
    )

    frame = result["timeframes"]["15m"]
    quarantine = frame["quality"]["quarantine"]
    assert frame["status"] == "quarantined"
    assert frame["decision_eligible"] is False
    assert frame["bars_used"] == len(jumped_15m)
    assert frame["indicators"]["ema20"] is not None
    assert frame["trend"] == "unknown"
    assert frame["trend_score"] == 0.0
    assert quarantine["active"] is True
    assert quarantine["event_count"] == 1
    assert quarantine["max_ratio"] >= 1.999
    assert quarantine["threshold_ratio"] == 1.5
    assert quarantine["rule"] == "adjacent_close_reciprocal_ratio_threshold"
    assert result["quality"]["quarantined_timeframes"] == ["15m"]
    assert result["available_timeframes"] == ["1h"]
    assert result["decision_eligible"] is False
    assert "price_jump_quarantined:15m" in result["warnings"]
    json.dumps(result, allow_nan=False)


def test_price_halving_uses_the_same_symmetric_quarantine_rule():
    as_of_ms = 1_800_000_000_000
    rows = _pattern_rows("1h", as_of_ms, slope=0.1)
    previous_close = rows[-2][4]
    rows[-1][1] = previous_close
    rows[-1][4] = previous_close / 2.0
    rows[-1][2] = previous_close + 0.5
    rows[-1][3] = rows[-1][4] - 0.5

    result = market_structure.analyze_market_structure(
        {"1h": rows}, as_of_ms=as_of_ms,
        source="OKX", source_decision_eligible=True,
    )

    quarantine = result["timeframes"]["1h"]["quality"]["quarantine"]
    assert quarantine["active"] is True
    assert quarantine["event_count"] == 1
    assert quarantine["events"][0]["return_pct"] == -50.0
    assert result["timeframes"]["1h"]["decision_eligible"] is False
