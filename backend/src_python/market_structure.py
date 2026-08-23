"""Causal, provider-agnostic multi-timeframe market-structure analysis.

The only public seam accepts already-fetched CCXT OHLCV.  It performs no I/O,
never emits BUY/SELL, and only uses candles known to be closed at ``as_of_ms``.
TA-Lib is required for formal decision eligibility.  A small, deterministic
native implementation keeps offline replay and research output observable when
the binary dependency is unavailable or incomplete, but native values can
never authorize production execution.  Swing/BOS/CHoCH fields are explicitly
research context (SMC-style), never a standalone execution signal.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

try:  # Native calculations remain available for research/replay only.
    import numpy as _np
    import talib as _talib

    _HAS_TALIB = True
except Exception:  # pragma: no cover - depends on the deployment image.
    _np = None
    _talib = None
    _HAS_TALIB = False


_TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}
_TIMEFRAME_WEIGHTS = {"15m": 0.15, "1h": 0.25, "4h": 0.30, "1d": 0.30}
_MAX_INPUT_ROWS = 100_000
_MAX_BARS = 10_000
_MAX_ABS_OHLCV = 1e100
_SWING_SIDE = 2
_MIN_TREND_BARS = 20
_MIN_DECISION_BARS = 50

# These are the TA-Lib outputs consumed by the formal trend calculation or by
# its existing completeness gate.  Any missing value makes the whole
# timeframe research-only; silently mixing a native value into production
# scoring would make the algorithm depend on deployment accidents.
_FORMAL_TALIB_COMPONENTS = (
    "ema20",
    "ema50",
    "ema200",
    "rsi14",
    "macd.line",
    "macd.signal",
    "macd.histogram",
    "atr14",
    "bollinger.upper",
    "bollinger.middle",
    "bollinger.lower",
)

# A reciprocal adjacent-close ratio of 1.5 means either a +50% jump or a
# -33.3% fall.  Both are extreme for currently supported BTC/XAU 15m–1d bars
# and require independent provider confirmation before they may vote in a
# decision.  The candle remains observable; only its timeframe is quarantined.
_PRICE_JUMP_RATIO_THRESHOLD = 1.5
_MAX_QUARANTINE_EVENTS = 8

_Candle = Tuple[int, float, float, float, float, float]


def _runtime_indicator_engine() -> str:
    """Name the observable engine without implying execution authority."""
    return "talib" if _HAS_TALIB else "native_research"


def _ta_last(function: Any, *arrays: Sequence[float], **kwargs: Any) -> Optional[float]:
    """Return the last finite value from a TA-Lib series, if available."""
    if not _HAS_TALIB or _np is None or function is None:
        return None
    try:
        converted = [_np.asarray(values, dtype="float64") for values in arrays]
        output = function(*converted, **kwargs)
    except Exception:
        # A malformed/short provider response must not take down the ticker
        # snapshot.  The caller will use the deterministic native fallback.
        return None
    if isinstance(output, tuple):
        # Callers which need a tuple (MACD/BBANDS) use _ta_tuple below.
        return None
    try:
        value = float(output[-1])
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _ta_tuple(function: Any, *arrays: Sequence[float], **kwargs: Any) -> Optional[Tuple[Optional[float], ...]]:
    """Return the final finite values for a TA-Lib multi-output function."""
    if not _HAS_TALIB or _np is None or function is None:
        return None
    try:
        converted = [_np.asarray(values, dtype="float64") for values in arrays]
        output = function(*converted, **kwargs)
    except Exception:
        return None
    if not isinstance(output, tuple):
        return None
    values: List[Optional[float]] = []
    for series in output:
        try:
            value = float(series[-1])
        except (TypeError, ValueError, IndexError, OverflowError):
            values.append(None)
            continue
        values.append(value if math.isfinite(value) else None)
    return tuple(values)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> Optional[int]:
    result = _number(value)
    if result is None or result < 0 or result > 10**16 or abs(result - int(result)) > 1e-6:
        return None
    return int(result)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prefer(primary: Optional[float], fallback: Optional[float]) -> Optional[float]:
    """Prefer a valid TA-Lib zero as well as non-zero values."""
    return primary if primary is not None else fallback


def _clean(value: Optional[float], digits: int = 10) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    result = round(float(value), digits)
    return 0.0 if result == 0 else result


def _rejections() -> Dict[str, int]:
    return {
        "malformed": 0,
        "invalid_ohlcv": 0,
        "future_or_unclosed": 0,
        "duplicate": 0,
        "conflicting_duplicate": 0,
        "truncated": 0,
    }


def _empty_quarantine() -> Dict[str, Any]:
    return {
        "active": False,
        "reason": None,
        "rule": "adjacent_close_reciprocal_ratio_threshold",
        "threshold_ratio": _PRICE_JUMP_RATIO_THRESHOLD,
        "event_count": 0,
        "max_ratio": 1.0,
        "events": [],
    }


def _empty_quality() -> Dict[str, Any]:
    return {
        "received": 0,
        "valid": 0,
        "used": 0,
        "first_open_at": None,
        "last_closed_at": None,
        "staleness_ms": None,
        "stale": True,
        "gap_count": 0,
        "gap_segments": 0,
        "gap_ratio": 0.0,
        "severe_gaps": False,
        "quarantine": _empty_quarantine(),
        "rejected": _rejections(),
        "indicator_engine": _runtime_indicator_engine(),
        "indicator_decision_eligible": False,
        "indicator_quality_reason": "no_closed_candles",
        "indicator_missing": [],
    }


def _empty_indicators() -> Dict[str, Any]:
    return {
        "ema20": None,
        "ema50": None,
        "ema200": None,
        "rsi14": None,
        "adx14": None,
        "macd": {"line": None, "signal": None, "histogram": None},
        "atr14": None,
        "atr_pct": None,
        "bollinger": {"middle": None, "upper": None, "lower": None, "width_pct": None},
        "obv": None,
        "vwap": None,
        "volume_ratio": None,
    }


def _empty_structure() -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "swing_sequence": [],
        "latest_swing_high": None,
        "latest_swing_low": None,
        "support": None,
        "resistance": None,
        "latest_bos": None,
        "latest_choch": None,
    }


def _empty_timeframe(interval_ms: int, quality: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "analysis_eligible": False,
        "decision_eligible": False,
        "indicator_engine": _runtime_indicator_engine(),
        "interval_ms": interval_ms,
        "bars_used": 0,
        "last_closed_at": None,
        "trend_score": 0.0,
        "trend": "unknown",
        "confidence": 0.0,
        "indicators": _empty_indicators(),
        "structure": _empty_structure(),
        "quality": quality or _empty_quality(),
    }


def _base_result(as_of_ms: Optional[int], source: str) -> Dict[str, Any]:
    engine = _runtime_indicator_engine()
    engine_reason = None if _HAS_TALIB else "talib_unavailable"
    return {
        "schema_version": "1.0",
        "as_of_ms": as_of_ms,
        "source": source,
        "status": "unavailable",
        "indicator_engine": engine,
        "decision_eligible": False,
        "trend_score": 0.0,
        "trend": "unknown",
        "confidence": 0.0,
        "alignment": "unknown",
        "alignment_score": 0.0,
        "available_timeframes": [],
        "timeframes": {},
        "research": {
            "native_indicator_fallback": {
                "status": "not_used" if _HAS_TALIB else "active",
                "execution_eligible": False,
                "note": "native指标仅用于研究/回放，不能触发正式交易",
            },
            "smc": {
                "status": "research_only",
                "execution_eligible": False,
                "features": ["confirmed_swings", "BOS", "CHoCH", "support_resistance"],
            },
        },
        "quality": {
            "requested_timeframes": [],
            "analyzed_timeframes": [],
            "unsupported_timeframes": [],
            "decision_ready_timeframes": [],
            "quarantined_timeframes": [],
            "closed_bar_policy": "timestamp_plus_interval_lte_as_of",
            "indicator_engine": engine,
            "indicator_policy": "talib_required_for_formal_decisions",
            "indicator_decision_eligible": False,
            "indicator_quality_reason": engine_reason,
            "indicator_missing_by_timeframe": {},
            "smc": {
                "status": "research_only",
                "execution_eligible": False,
                "method": "causal_confirmed_swings_bos_choch",
            },
        },
        "warnings": [],
        "reason": "",
    }


def _price_jump_quarantine(candles: Sequence[_Candle]) -> Dict[str, Any]:
    """Describe extreme adjacent-close jumps without discarding observations.

    Detection uses the absolute log-price difference, which is symmetric for
    a doubling and a halving and cannot overflow for finite positive prices.
    Reported ratios are bounded to the module's existing finite OHLCV limit so
    the result remains valid strict JSON even for pathological input.
    """
    result = _empty_quarantine()
    if len(candles) < 2:
        return result

    threshold_log = math.log(_PRICE_JUMP_RATIO_THRESHOLD)
    max_log_ratio = 0.0
    event_count = 0
    events: List[Dict[str, Any]] = []
    report_log_cap = math.log(_MAX_ABS_OHLCV)

    for previous, current in zip(candles, candles[1:]):
        previous_close = previous[4]
        current_close = current[4]
        log_ratio = abs(math.log(current_close) - math.log(previous_close))
        max_log_ratio = max(max_log_ratio, log_ratio)
        if log_ratio < threshold_log - 1e-12:
            continue

        event_count += 1
        directional_ratio = current_close / previous_close
        return_candidate = (
            (directional_ratio - 1.0) * 100.0
            if math.isfinite(directional_ratio) else None
        )
        return_pct = (
            return_candidate
            if return_candidate is not None and math.isfinite(return_candidate)
            else None
        )
        reported_ratio = min(
            _MAX_ABS_OHLCV,
            math.exp(min(log_ratio, report_log_cap)),
        )
        events.append({
            "previous_at": previous[0],
            "at": current[0],
            "previous_close": previous_close,
            "close": current_close,
            "ratio": _clean(reported_ratio, 6),
            "return_pct": _clean(return_pct, 6),
        })

    max_ratio = min(
        _MAX_ABS_OHLCV,
        math.exp(min(max_log_ratio, report_log_cap)),
    )
    result.update({
        "active": event_count > 0,
        "reason": "extreme_adjacent_close_jump_unconfirmed" if event_count else None,
        "event_count": event_count,
        "max_ratio": _clean(max_ratio, 6) or 1.0,
        "events": events[-_MAX_QUARANTINE_EVENTS:],
    })
    return result


def _sanitize(rows: Any, interval_ms: int, as_of_ms: int) -> Tuple[List[_Candle], Dict[str, Any]]:
    quality = _empty_quality()
    rejected = quality["rejected"]
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        rejected["malformed"] = 1
        return [], quality

    # Conflicting rows for one timestamp are all discarded instead of making
    # input order decide which market fact is trusted.
    by_timestamp: Dict[int, Optional[_Candle]] = {}
    for index, row in enumerate(rows):
        if index >= _MAX_INPUT_ROWS:
            rejected["truncated"] += 1
            break
        quality["received"] += 1
        if isinstance(row, (str, bytes, Mapping)) or not isinstance(row, Sequence) or len(row) < 6:
            rejected["malformed"] += 1
            continue
        ts = _timestamp(row[0])
        values = [_number(row[column]) for column in range(1, 6)]
        if ts is None or any(value is None for value in values):
            rejected["invalid_ohlcv"] += 1
            continue
        open_, high, low, close, volume = (float(value) for value in values if value is not None)
        if (
            open_ <= 0 or high <= 0 or low <= 0 or close <= 0 or volume < 0
            or max(open_, high, low, close, volume) > _MAX_ABS_OHLCV
            or high < low or not low <= open_ <= high or not low <= close <= high
        ):
            rejected["invalid_ohlcv"] += 1
            continue
        quality["valid"] += 1
        if ts > as_of_ms or ts + interval_ms > as_of_ms:
            rejected["future_or_unclosed"] += 1
            continue
        candle: _Candle = (ts, open_, high, low, close, volume)
        if ts in by_timestamp:
            rejected["duplicate"] += 1
            existing = by_timestamp[ts]
            if existing is not None and existing != candle:
                by_timestamp[ts] = None
                rejected["conflicting_duplicate"] += 1
            continue
        by_timestamp[ts] = candle

    candles = sorted(candle for candle in by_timestamp.values() if candle is not None)
    if len(candles) > _MAX_BARS:
        rejected["truncated"] += len(candles) - _MAX_BARS
        candles = candles[-_MAX_BARS:]
    quality["used"] = len(candles)
    if not candles:
        return candles, quality

    quality["quarantine"] = _price_jump_quarantine(candles)
    gaps = [max(0, math.ceil((right[0] - left[0]) / interval_ms) - 1) for left, right in zip(candles, candles[1:])]
    gap_count = sum(gaps)
    last_closed_at = candles[-1][0] + interval_ms
    staleness_ms = max(0, as_of_ms - last_closed_at)
    gap_ratio = gap_count / (len(candles) + gap_count) if gap_count else 0.0
    quality.update({
        "first_open_at": candles[0][0],
        "last_closed_at": last_closed_at,
        "staleness_ms": staleness_ms,
        "stale": staleness_ms > 2 * interval_ms,
        "gap_count": gap_count,
        "gap_segments": sum(1 for gap in gaps if gap),
        "gap_ratio": _clean(gap_ratio, 6) or 0.0,
        "severe_gaps": gap_ratio > 0.20 or any(gap >= 10 for gap in gaps),
    })
    return candles, quality


def _ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current += (values[index] - current) * alpha
        result[index] = current
    return result


def _rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, period + 1)]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, period + 1)]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        average_gain = ((average_gain * (period - 1)) + max(delta, 0.0)) / period
        average_loss = ((average_loss * (period - 1)) + max(-delta, 0.0)) / period
    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + average_gain / average_loss))


def _atr(candles: Sequence[_Candle], period: int = 14) -> Optional[float]:
    if len(candles) <= period:
        return None
    ranges = [
        max(candles[index][2] - candles[index][3],
            abs(candles[index][2] - candles[index - 1][4]),
            abs(candles[index][3] - candles[index - 1][4]))
        for index in range(1, len(candles))
    ]
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = ((current * (period - 1)) + value) / period
    return current


def _indicator_bundle(candles: Sequence[_Candle]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Calculate observable indicators plus their formal-engine provenance.

    Values always fall back to deterministic native calculations where one is
    available.  ``formal_decision_eligible`` is intentionally independent from
    value availability: a native fallback is useful evidence for research and
    replay, but it is never silently promoted into the production algorithm.
    """
    closes = [candle[4] for candle in candles]
    highs = [candle[2] for candle in candles]
    lows = [candle[3] for candle in candles]
    volumes = [candle[5] for candle in candles]

    # Every formal component keeps its TA-Lib value separate from the value
    # eventually displayed.  This prevents a native fallback from being
    # mistaken for a successful formal-engine calculation.
    ema20_native = _ema(closes, 20)[-1] if closes else None
    ema50_native = _ema(closes, 50)[-1] if closes else None
    ema200_native = _ema(closes, 200)[-1] if closes else None
    ema20_ta = _ta_last(getattr(_talib, "EMA", None), closes, timeperiod=20)
    ema50_ta = _ta_last(getattr(_talib, "EMA", None), closes, timeperiod=50)
    ema200_ta = _ta_last(getattr(_talib, "EMA", None), closes, timeperiod=200)
    ema20 = _prefer(ema20_ta, ema20_native)
    ema50 = _prefer(ema50_ta, ema50_native)
    ema200 = _prefer(ema200_ta, ema200_native)

    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd_values = [a - b for a, b in zip(fast, slow) if a is not None and b is not None]
    macd_line_native = macd_values[-1] if macd_values else None
    signal_series = _ema(macd_values, 9)
    macd_signal_native = signal_series[-1] if signal_series else None
    macd_histogram_native = (
        macd_line_native - macd_signal_native
        if macd_line_native is not None and macd_signal_native is not None else None
    )
    macd_ta = _ta_tuple(
        getattr(_talib, "MACD", None), closes,
        fastperiod=12, slowperiod=26, signalperiod=9,
    )
    macd_line = macd_ta[0] if macd_ta and macd_ta[0] is not None else macd_line_native
    macd_signal = macd_ta[1] if macd_ta and macd_ta[1] is not None else macd_signal_native
    macd_histogram = macd_ta[2] if macd_ta and macd_ta[2] is not None else macd_histogram_native

    atr_native = _atr(candles)
    atr14_ta = _ta_last(
        getattr(_talib, "ATR", None), highs, lows, closes, timeperiod=14,
    )
    atr14 = _prefer(atr14_ta, atr_native)
    atr_pct = atr14 / closes[-1] * 100.0 if atr14 is not None and closes[-1] else None

    rsi_native = _rsi(closes)
    rsi14_ta = _ta_last(getattr(_talib, "RSI", None), closes, timeperiod=14)
    rsi14 = _prefer(rsi14_ta, rsi_native)
    adx14 = _ta_last(
        getattr(_talib, "ADX", None), highs, lows, closes, timeperiod=14,
    )

    if len(closes) >= 20:
        window = closes[-20:]
        middle = sum(window) / 20
        deviation = math.sqrt(sum((value - middle) ** 2 for value in window) / 20)
        upper, lower = middle + 2 * deviation, middle - 2 * deviation
        bollinger_native = {
            "middle": _clean(middle), "upper": _clean(upper), "lower": _clean(lower),
            "width_pct": _clean((upper - lower) / middle * 100.0 if middle else None),
        }
    else:
        bollinger_native = _empty_indicators()["bollinger"]
    bb_ta = _ta_tuple(
        getattr(_talib, "BBANDS", None), closes,
        timeperiod=20, nbdevup=2, nbdevdn=2, matype=0,
    )
    if bb_ta and all(value is not None for value in bb_ta):
        # TA-Lib returns upper, middle, lower in that order.
        bb_upper, bb_middle, bb_lower = bb_ta
        bollinger = {
            "middle": _clean(bb_middle), "upper": _clean(bb_upper), "lower": _clean(bb_lower),
            "width_pct": _clean(
                (float(bb_upper) - float(bb_lower)) / float(bb_middle) * 100.0
                if bb_middle else None
            ),
        }
    else:
        bollinger = bollinger_native

    obv_native = 0.0 if candles else None
    for previous, current in zip(candles, candles[1:]):
        if current[4] > previous[4]:
            obv_native += current[5]
        elif current[4] < previous[4]:
            obv_native -= current[5]
    obv = _prefer(_ta_last(getattr(_talib, "OBV", None), closes, volumes), obv_native)
    total_volume = sum(candle[5] for candle in candles)
    vwap = (
        sum(((candle[2] + candle[3] + candle[4]) / 3.0) * candle[5] for candle in candles) / total_volume
        if total_volume > 0 else None
    )
    volume_ratio = None
    if len(volumes) >= 21:
        baseline_values = volumes[-21:-1]
        baseline = sum(baseline_values) / len(baseline_values)
        if baseline > 0:
            volume_ratio = volumes[-1] / baseline
    values = {
        "ema20": _clean(ema20), "ema50": _clean(ema50), "ema200": _clean(ema200),
        "rsi14": _clean(rsi14), "adx14": _clean(adx14),
        "macd": {"line": _clean(macd_line), "signal": _clean(macd_signal), "histogram": _clean(macd_histogram)},
        "atr14": _clean(atr14), "atr_pct": _clean(atr_pct), "bollinger": bollinger,
        "obv": _clean(obv), "vwap": _clean(vwap), "volume_ratio": _clean(volume_ratio),
    }
    talib_components = {
        "ema20": ema20_ta,
        "ema50": ema50_ta,
        "ema200": ema200_ta,
        "rsi14": rsi14_ta,
        "macd.line": macd_ta[0] if macd_ta and len(macd_ta) > 0 else None,
        "macd.signal": macd_ta[1] if macd_ta and len(macd_ta) > 1 else None,
        "macd.histogram": macd_ta[2] if macd_ta and len(macd_ta) > 2 else None,
        "atr14": atr14_ta,
        "bollinger.upper": bb_ta[0] if bb_ta and len(bb_ta) > 0 else None,
        "bollinger.middle": bb_ta[1] if bb_ta and len(bb_ta) > 1 else None,
        "bollinger.lower": bb_ta[2] if bb_ta and len(bb_ta) > 2 else None,
    }
    missing = [
        name for name in _FORMAL_TALIB_COMPONENTS
        if talib_components.get(name) is None
    ]
    formal_ready = bool(_HAS_TALIB and not missing)
    if formal_ready:
        engine = "talib"
        reason = None
    elif _HAS_TALIB:
        engine = "talib_with_native_fallback_research"
        reason = "talib_incomplete"
    else:
        engine = "native_research"
        reason = "talib_unavailable"
        # Report the whole contract when the module is absent.  That is more
        # actionable than presenting every missing function as an independent
        # runtime failure.
        missing = list(_FORMAL_TALIB_COMPONENTS)
    return values, {
        "indicator_engine": engine,
        "formal_engine": "talib",
        "formal_decision_eligible": formal_ready,
        "quality_reason": reason,
        "required_components": list(_FORMAL_TALIB_COMPONENTS),
        "missing_components": missing,
    }


def _indicators(candles: Sequence[_Candle]) -> Dict[str, Any]:
    """Compatibility wrapper returning observable indicator values only."""
    return _indicator_bundle(candles)[0]


def _swings(candles: Sequence[_Candle], interval_ms: int) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    side = _SWING_SIDE
    for index in range(side, len(candles) - side):
        left = candles[index - side:index]
        right = candles[index + 1:index + side + 1]
        confirmed_at = candles[index + side][0] + interval_ms
        # Adjacent OHLC bars commonly share a turning-point high/low because
        # one bar closes where the next opens.  Strictness on the left and a
        # non-strict right comparison deterministically selects the first bar
        # of that plateau without requiring any unconfirmed future candle.
        if (
            candles[index][2] > max(row[2] for row in left)
            and candles[index][2] >= max(row[2] for row in right)
        ):
            result.append({
                "pivot_type": "high", "kind": "SH", "price": _clean(candles[index][2]),
                "at": candles[index][0], "confirmed_at": confirmed_at,
                "lag_bars": side, "provisional": False, "_confirm_index": index + side,
            })
        if (
            candles[index][3] < min(row[3] for row in left)
            and candles[index][3] <= min(row[3] for row in right)
        ):
            result.append({
                "pivot_type": "low", "kind": "SL", "price": _clean(candles[index][3]),
                "at": candles[index][0], "confirmed_at": confirmed_at,
                "lag_bars": side, "provisional": False, "_confirm_index": index + side,
            })
    result.sort(key=lambda item: (item["_confirm_index"], item["at"], item["pivot_type"]))
    previous: Dict[str, Optional[float]] = {"high": None, "low": None}
    for item in result:
        old = previous[item["pivot_type"]]
        price = float(item["price"])
        if old is not None and item["pivot_type"] == "high":
            item["kind"] = "HH" if price > old else "LH" if price < old else "SH"
        elif old is not None:
            item["kind"] = "HL" if price > old else "LL" if price < old else "SL"
        previous[item["pivot_type"]] = price
    return result


def _public(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if item is None else {key: value for key, value in item.items() if not key.startswith("_")}


def _events(candles: Sequence[_Candle], swings: Sequence[Dict[str, Any]], interval_ms: int) -> List[Dict[str, Any]]:
    confirmed: Dict[int, List[Dict[str, Any]]] = {}
    for swing in swings:
        confirmed.setdefault(int(swing["_confirm_index"]), []).append(swing)
    high: Optional[Dict[str, Any]] = None
    low: Optional[Dict[str, Any]] = None
    broken: set[Tuple[str, int, float]] = set()
    regime = 0
    result: List[Dict[str, Any]] = []
    for index, candle in enumerate(candles):
        for swing in confirmed.get(index, []):
            if swing["pivot_type"] == "high":
                high = swing
            else:
                low = swing
        direction, level = 0, None
        high_key = ("high", int(high["at"]), float(high["price"])) if high else None
        low_key = ("low", int(low["at"]), float(low["price"])) if low else None
        if high and high_key not in broken and candle[4] > float(high["price"]):
            broken.add(high_key)
            direction, level = 1, high
        elif low and low_key not in broken and candle[4] < float(low["price"]):
            broken.add(low_key)
            direction, level = -1, low
        if not direction or level is None:
            continue
        result.append({
            "kind": "CHoCH" if regime and direction != regime else "BOS",
            "direction": "bullish" if direction > 0 else "bearish",
            "level": level["price"], "source_swing_kind": level["kind"],
            "pivot_at": level["at"], "at": candle[0],
            "confirmed_at": candle[0] + interval_ms, "lag_bars": 0, "provisional": False,
        })
        regime = direction
    return result


def _structure(candles: Sequence[_Candle], interval_ms: int) -> Dict[str, Any]:
    swings = _swings(candles, interval_ms)
    events = _events(candles, swings, interval_ms)
    high = next((item for item in reversed(swings) if item["pivot_type"] == "high"), None)
    low = next((item for item in reversed(swings) if item["pivot_type"] == "low"), None)
    close = candles[-1][4]
    support_swing = next((
        item for item in reversed(swings)
        if item["pivot_type"] == "low" and float(item["price"]) <= close
    ), None)
    resistance_swing = next((
        item for item in reversed(swings)
        if item["pivot_type"] == "high" and float(item["price"]) >= close
    ), None)
    bos = next((item for item in reversed(events) if item["kind"] == "BOS"), None)
    choch = next((item for item in reversed(events) if item["kind"] == "CHoCH"), None)
    support = _public(support_swing)
    resistance = _public(resistance_swing)
    if support:
        support["role"] = "support"
    if resistance:
        resistance["role"] = "resistance"
    return {
        "status": "ok" if high and low else "partial" if swings else "unavailable",
        "swing_sequence": [_public(item) for item in swings[-12:]],
        "latest_swing_high": _public(high), "latest_swing_low": _public(low),
        "support": support, "resistance": resistance,
        "latest_bos": bos, "latest_choch": choch,
    }


def _research_structure_score(structure: Mapping[str, Any]) -> Optional[float]:
    """Return an SMC-style research score, never used for execution scoring."""
    values = {"HH": 1.0, "HL": 1.0, "LH": -1.0, "LL": -1.0, "SH": 0.0, "SL": 0.0}
    sequence = structure.get("swing_sequence")
    scores: List[float] = []
    if isinstance(sequence, list):
        scores = [
            values.get(str(item.get("kind")), 0.0)
            for item in sequence[-4:]
            if isinstance(item, Mapping)
        ]
    score: Optional[float] = sum(scores) / len(scores) if scores else None
    structure_events = [
        item for item in (structure.get("latest_choch"), structure.get("latest_bos"))
        if isinstance(item, Mapping)
    ]
    event = max(structure_events, key=lambda item: int(item.get("confirmed_at") or 0)) if structure_events else None
    if event is not None:
        event_score = 1.0 if event.get("direction") == "bullish" else -1.0
        score = event_score if score is None else (score + event_score) / 2
    return _clean(_clamp(score, -1, 1), 6) if score is not None else None


def _trend(candles: Sequence[_Candle], indicators: Mapping[str, Any], structure: Mapping[str, Any]) -> Tuple[float, str, float]:
    """Compute an indicator trend without using SMC-style structure.

    ``structure`` is intentionally not folded into this score.  Its swing and
    BOS/CHoCH fields are SMC-style research context and are exposed separately
    for an analyst/LLM to inspect.  The enclosing quality gate decides whether
    the values came entirely from TA-Lib and may reach ``decision_guard`` or
    came from the native research fallback and must remain non-executable.
    """
    if len(candles) < _MIN_TREND_BARS:
        return 0.0, "unknown", 0.0
    close = candles[-1][4]
    atr = indicators.get("atr14")
    scale = float(atr) if isinstance(atr, (float, int)) and atr > 0 else close * 0.01
    components: List[Tuple[float, float]] = []
    chain = [close] + [
        float(value) for value in (indicators.get("ema20"), indicators.get("ema50"), indicators.get("ema200"))
        if isinstance(value, (float, int))
    ]
    if len(chain) > 1:
        value = sum(math.tanh((left - right) / max(scale, 1e-12)) for left, right in zip(chain, chain[1:])) / (len(chain) - 1)
        components.append((value, 0.45))
    macd = indicators.get("macd")
    histogram = macd.get("histogram") if isinstance(macd, Mapping) else None
    if isinstance(histogram, (float, int)):
        components.append((math.tanh(float(histogram) / max(scale * 0.2, 1e-12)), 0.20))
    rsi = indicators.get("rsi14")
    if isinstance(rsi, (float, int)):
        components.append((_clamp((float(rsi) - 50) / 25, -1, 1), 0.10))
    if not components:
        return 0.0, "unknown", 0.0
    weight = sum(item[1] for item in components)
    score = _clamp(sum(value * item_weight for value, item_weight in components) / weight, -1, 1)
    label = "bullish" if score >= 0.20 else "bearish" if score <= -0.20 else "range"
    agreement = 1 - min(1.0, sum(abs(value - score) * item_weight for value, item_weight in components) / (2 * weight))
    confidence = _clamp(0.45 * weight + 0.30 * agreement + 0.25 * min(1, len(candles) / 200), 0, 1)
    return _clean(score, 6) or 0.0, label, _clean(confidence, 6) or 0.0


def _analyze_timeframe(timeframe: str, candles: Sequence[_Candle], quality: Dict[str, Any]) -> Dict[str, Any]:
    interval_ms = _TIMEFRAME_MS[timeframe]
    if not candles:
        return _empty_timeframe(interval_ms, quality)
    indicators, indicator_quality = _indicator_bundle(candles)
    formal_engine_ready = indicator_quality["formal_decision_eligible"] is True
    quality.update({
        "indicator_engine": indicator_quality["indicator_engine"],
        "indicator_decision_eligible": formal_engine_ready,
        "indicator_quality_reason": indicator_quality["quality_reason"],
        "indicator_missing": indicator_quality["missing_components"],
    })
    structure = _structure(candles, interval_ms)
    score, label, confidence = _trend(candles, indicators, structure)
    research_score = _research_structure_score(structure)
    quarantined = bool(
        isinstance(quality.get("quarantine"), Mapping)
        and quality["quarantine"].get("active") is True
    )
    complete = (
        indicators["ema200"] is not None and indicators["rsi14"] is not None
        and indicators["macd"]["signal"] is not None and indicators["atr14"] is not None
        and indicators["bollinger"]["middle"] is not None
    )
    if quarantined:
        status = "quarantined"
        # Keep the candles, indicators and confirmed structure observable, but
        # expose no formal direction from an unconfirmed price-scale jump.
        score, label, confidence = 0.0, "unknown", 0.0
    elif quality["stale"]:
        status = "unavailable"
    elif quality["severe_gaps"]:
        status = "partial"
    elif not formal_engine_ready:
        # Native/hybrid values remain visible for research and replay, but a
        # production consumer must see that the formal indicator contract was
        # not satisfied.
        status = "partial"
    else:
        status = "ok" if complete else "partial"
    analysis_eligible = (
        status in {"ok", "partial"} and len(candles) >= _MIN_DECISION_BARS
        and label != "unknown" and not quality["severe_gaps"] and not quarantined
        and not quality["stale"]
    )
    eligible = bool(analysis_eligible and formal_engine_ready)
    return {
        "status": status,
        "analysis_eligible": analysis_eligible,
        "decision_eligible": eligible,
        "indicator_engine": indicator_quality["indicator_engine"],
        "interval_ms": interval_ms, "bars_used": len(candles),
        "last_closed_at": candles[-1][0] + interval_ms,
        "trend_score": score, "trend": label, "confidence": confidence,
        "indicators": indicators, "structure": structure,
        "research": {
            "smc_score": research_score,
            "execution_eligible": False,
            "native_indicator_fallback": {
                "used": indicator_quality["indicator_engine"] != "talib",
                "execution_eligible": False,
            },
            "note": "SMC-style swings/BOS/CHoCH仅研究参考，不进入正式方向评分",
        },
        "quality": quality,
    }


def _aggregate(results: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate observable research output from data-quality-safe frames.

    Formal execution is gated separately by ``decision_eligible``.  Using the
    analysis-only flag here preserves deterministic replay output when TA-Lib
    is missing without granting that fallback any trading authority.
    """
    usable = {
        timeframe: item for timeframe, item in results.items()
        if item.get("analysis_eligible") is True
        and item["trend"] in {"bullish", "bearish", "range"}
    }
    if not usable:
        return {
            "trend_score": 0.0, "trend": "unknown", "confidence": 0.0,
            "alignment": "unknown", "alignment_score": 0.0, "available_timeframes": [],
        }
    total = sum(_TIMEFRAME_WEIGHTS[key] for key in usable)
    score = sum(item["trend_score"] * _TIMEFRAME_WEIGHTS[key] for key, item in usable.items()) / total
    label = "bullish" if score >= 0.20 else "bearish" if score <= -0.20 else "range"
    directions = {item["trend"] for item in usable.values()}
    if directions == {"range"}:
        alignment, alignment_score = "range", 1.0
    elif "bullish" in directions and "bearish" in directions:
        vote = sum((1 if item["trend"] == "bullish" else -1 if item["trend"] == "bearish" else 0) * _TIMEFRAME_WEIGHTS[key] for key, item in usable.items())
        alignment, alignment_score = "mixed", abs(vote / total)
    else:
        alignment = "bullish" if "bullish" in directions else "bearish"
        alignment_score = sum(_TIMEFRAME_WEIGHTS[key] for key, item in usable.items() if item["trend"] == alignment) / total
    base_confidence = sum(item["confidence"] * _TIMEFRAME_WEIGHTS[key] for key, item in usable.items()) / total
    confidence = base_confidence * (0.6 + 0.4 * alignment_score) * (0.75 + 0.25 * min(1, len(usable) / 2))
    return {
        "trend_score": _clean(_clamp(score, -1, 1), 6) or 0.0, "trend": label,
        "confidence": _clean(_clamp(confidence, 0, 1), 6) or 0.0,
        "alignment": alignment, "alignment_score": _clean(_clamp(alignment_score, 0, 1), 6) or 0.0,
        "available_timeframes": [key for key in _TIMEFRAME_MS if key in usable],
    }


def analyze_market_structure(
    timeframes: Any,
    *,
    as_of_ms: Any,
    source: Any,
    source_decision_eligible: bool = False,
) -> Dict[str, Any]:
    """Return bounded, JSON-safe indicators and confirmed structure.

    ``timeframes`` maps supported timeframe names to rows shaped as
    ``[timestamp_ms, open, high, low, close, volume]``.  Timestamps are candle
    open times.  Bad or stale data fail closed and never become trade advice.
    """
    normalized_as_of = _timestamp(as_of_ms)
    normalized_source = source.strip()[:128] if isinstance(source, str) and source.strip() else "unknown"
    output = _base_result(normalized_as_of, normalized_source)
    if normalized_as_of is None:
        output["warnings"].append("invalid_as_of_ms")
        return output
    if not isinstance(timeframes, Mapping):
        output["warnings"].append("invalid_timeframes")
        return output

    requested = [str(key) for key in timeframes]
    supported = [key for key in _TIMEFRAME_MS if key in timeframes]
    unsupported = sorted(key for key in requested if key not in _TIMEFRAME_MS)
    output["quality"]["requested_timeframes"] = requested
    output["quality"]["unsupported_timeframes"] = unsupported
    output["quality"]["source_decision_eligible"] = source_decision_eligible is True
    output["quality"]["observed_at"] = normalized_as_of
    if unsupported:
        output["warnings"].append("unsupported_timeframes_ignored")

    results: Dict[str, Dict[str, Any]] = {}
    for timeframe in supported:
        candles, quality = _sanitize(timeframes[timeframe], _TIMEFRAME_MS[timeframe], normalized_as_of)
        results[timeframe] = _analyze_timeframe(timeframe, candles, quality)

    missing_by_timeframe = {
        key: list(item["quality"].get("indicator_missing") or [])
        for key, item in results.items()
        if item["quality"].get("indicator_missing")
    }
    incomplete_timeframes = [
        key for key, item in results.items()
        if item["quality"].get("indicator_quality_reason") == "talib_incomplete"
    ]
    indicator_frames = [item for item in results.values() if item.get("bars_used", 0) > 0]
    if not _HAS_TALIB:
        indicator_engine = "native_research"
        indicator_engine_ready = False
        indicator_reason = "talib_unavailable"
    elif incomplete_timeframes:
        indicator_engine = "talib_with_native_fallback_research"
        indicator_engine_ready = False
        indicator_reason = "talib_incomplete"
    elif indicator_frames:
        indicator_engine = "talib"
        indicator_engine_ready = all(
            item["quality"].get("indicator_decision_eligible") is True
            for item in indicator_frames
        )
        indicator_reason = None if indicator_engine_ready else "talib_incomplete"
    else:
        indicator_engine = "talib"
        indicator_engine_ready = False
        indicator_reason = "no_closed_candles"
    output["indicator_engine"] = indicator_engine
    output["quality"].update({
        "indicator_engine": indicator_engine,
        "indicator_decision_eligible": indicator_engine_ready,
        "indicator_quality_reason": indicator_reason,
        "indicator_missing_by_timeframe": missing_by_timeframe,
    })
    output["research"]["native_indicator_fallback"]["status"] = (
        "not_used" if indicator_engine == "talib" else "active"
    )
    if not _HAS_TALIB and supported:
        output["warnings"].append("talib_unavailable")
    elif incomplete_timeframes:
        output["warnings"].append(
            f"talib_incomplete:{','.join(incomplete_timeframes)}"
        )
    quarantined = [
        key for key, item in results.items()
        if item.get("status") == "quarantined"
    ]
    output["quality"]["quarantined_timeframes"] = quarantined
    if quarantined:
        output["warnings"].append(f"price_jump_quarantined:{','.join(quarantined)}")
    data_ready = [key for key, item in results.items() if item["analysis_eligible"] is True]
    aggregate = _aggregate(results)
    # Never leave an apparently eligible nested result when the provider
    # itself was not explicitly approved by the caller.  Aggregate first so
    # an unapproved source can still be inspected, without becoming tradable.
    for item in results.values():
        item["decision_eligible"] = bool(
            item["decision_eligible"]
            and source_decision_eligible is True
            and normalized_source != "unknown"
        )
    output["timeframes"] = results
    output.update(aggregate)
    # Keep a named aggregate object for API consumers while retaining the
    # flat fields used by the existing AI decision context.
    output["aggregate"] = dict(aggregate)
    output["quality"]["analyzed_timeframes"] = aggregate["available_timeframes"]
    if not aggregate["available_timeframes"]:
        output["warnings"].append("insufficient_fresh_closed_candles")
        output["reason"] = ";".join(output["warnings"])
        return output

    output["status"] = "ok" if results and all(
        item["status"] == "ok" for item in results.values()
    ) else "partial"
    formally_ready = [
        key for key in aggregate["available_timeframes"]
        if key in data_ready and results[key]["decision_eligible"]
    ]
    # A partial TA-Lib failure fails the complete multi-timeframe snapshot
    # closed.  Keeping individually healthy names out of this list avoids
    # presenting a mixed-engine aggregate as production-ready.
    ready = formally_ready if indicator_engine_ready else []
    output["quality"]["decision_ready_timeframes"] = ready
    multi_timeframe_ready = len(ready) >= 2 and any(key in {"1h", "4h", "1d"} for key in ready)
    output["decision_eligible"] = bool(
        source_decision_eligible is True and normalized_source != "unknown"
        and indicator_engine_ready and multi_timeframe_ready
        and output["confidence"] > 0
    )
    if source_decision_eligible is not True:
        output["warnings"].append("source_not_decision_eligible")
    elif not indicator_engine_ready:
        output["warnings"].append("formal_indicator_engine_not_ready")
    elif not multi_timeframe_ready:
        output["warnings"].append("insufficient_multi_timeframe_confirmation")
    output["reason"] = ";".join(output["warnings"]) if output["warnings"] else ""
    return output


__all__ = ["analyze_market_structure"]
