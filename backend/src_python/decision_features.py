"""Normalize AI output into an auditable, trading-safe feature set.

The worker historically persisted one signed ``sentiment_score``.  This
module derives the additional fields needed by the UI, paper trading and
backtests while keeping the old score/action contract intact.  It deliberately
does not place orders; ``dual_side_candidate`` is a review hint only.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    number = _number(value, default)
    return max(low, min(high, number if number is not None else default))


def _canonical_type(value: Any, *, direct_catalyst: bool, strength: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"conflict", "shock", "event", "冲突", "突发", "冲击"}:
        return "conflict"
    if text in {"trend", "trend_force", "force", "momentum", "趋势", "力量"}:
        return "trend"
    # A direct, high-impact catalyst is treated as a conflict-style event;
    # ordinary continuation/technical news is a trend-style input.
    return "conflict" if direct_catalyst or strength == "high" else "trend"


def _horizon(value: Any, expected: str, timeframe: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "short": "short", "intraday": "short", "minutes": "short",
        "medium": "medium", "swing": "medium", "1-3d": "medium", "hours": "medium",
        "long": "long", "macro": "long", "1w+": "long", "weeks": "long",
    }
    if text in aliases:
        return aliases[text]
    return aliases.get(str(expected or "").lower(), aliases.get(str(timeframe or "").lower(), "medium"))


def normalize_analysis(
    result: Dict[str, Any],
    *,
    score: Optional[float] = None,
    action: Optional[str] = None,
) -> Dict[str, Any]:
    """Return validated dynamic direction/force/interval fields.

    Existing model responses may omit every new field.  In that case the
    signed score is converted to conservative probabilities and forces, so
    old rows and fallback/keyword analysis remain fully usable.
    """

    source = result if isinstance(result, dict) else {}
    numeric_score = _clamp(source.get("sentiment_score", score), -1.0, 1.0, 0.0)
    normalized_action = str(source.get("suggested_action", action or "HOLD")).upper()
    if normalized_action not in {"BUY", "SELL", "HOLD"}:
        normalized_action = "HOLD"

    bull_raw = _number(source.get("bullish_probability"))
    bear_raw = _number(source.get("bearish_probability"))
    if bull_raw is None and bear_raw is None:
        bull = (numeric_score + 1.0) / 2.0
        bear = 1.0 - bull
    elif bull_raw is None:
        bear = _clamp(bear_raw, 0.0, 1.0, 0.5)
        bull = 1.0 - bear
    elif bear_raw is None:
        bull = _clamp(bull_raw, 0.0, 1.0, 0.5)
        bear = 1.0 - bull
    else:
        bull = _clamp(bull_raw, 0.0, 1.0, 0.5)
        bear = _clamp(bear_raw, 0.0, 1.0, 0.5)
        total = bull + bear
        if total <= 0:
            bull, bear = 0.5, 0.5
        else:
            bull, bear = bull / total, bear / total

    uncertainty = _clamp(source.get("uncertainty"), 0.0, 1.0, 1.0 - abs(bull - bear))
    # A supplied uncertainty is authoritative; otherwise the probability gap
    # is the transparent fallback used for paired-paper-trade suggestions.
    if source.get("uncertainty") is None:
        uncertainty = 1.0 - abs(bull - bear)

    strength = str(source.get("event_strength") or "medium").strip().lower()
    if strength not in {"low", "medium", "high"}:
        strength = "medium"
    direct = bool(source.get("direct_catalyst", False))
    analysis_type = _canonical_type(source.get("analysis_type"), direct_catalyst=direct, strength=strength)

    bull_force = _clamp(source.get("bullish_force"), 0.0, 1.0, max(numeric_score, 0.0))
    bear_force = _clamp(source.get("bearish_force"), 0.0, 1.0, max(-numeric_score, 0.0))
    expected = str(source.get("expected_horizon") or "1-3d")
    timeframe = str(source.get("timeframe_match") or "intraday")
    horizon = _horizon(source.get("impact_horizon"), expected, timeframe)

    window = source.get("impact_window")
    if isinstance(window, str):
        try:
            window = json.loads(window)
        except (TypeError, json.JSONDecodeError):
            window = None
    if not isinstance(window, dict):
        window = {
            "short": {"min_minutes": 0, "max_minutes": 120},
            "medium": {"min_minutes": 120, "max_minutes": 4320},
            "long": {"min_minutes": 4320, "max_minutes": 43200},
        }

    entry_zone = str(source.get("entry_zone") or "").strip()[:160]
    take_profit = _number(source.get("take_profit_pct"))
    stop_loss = _number(source.get("stop_loss_pct"))
    if take_profit is not None:
        take_profit = max(0.0, min(100.0, take_profit))
    if stop_loss is not None:
        stop_loss = max(0.0, min(100.0, stop_loss))
    exit_policy = str(source.get("exit_policy") or "horizon_or_signal_flip").strip()[:120]

    return {
        "analysis_type": analysis_type,
        "bullish_probability": round(bull, 6),
        "bearish_probability": round(bear, 6),
        "uncertainty": round(uncertainty, 6),
        "bullish_force": round(bull_force, 6),
        "bearish_force": round(bear_force, 6),
        "impact_horizon": horizon,
        "impact_window": window,
        "entry_zone": entry_zone,
        "take_profit_pct": take_profit,
        "stop_loss_pct": stop_loss,
        "exit_policy": exit_policy,
        # This flag is intentionally advisory.  A separate risk-controlled
        # executor must approve paired legs before any real order is sent.
        "dual_side_candidate": bool(
            normalized_action in {"BUY", "SELL"}
            and uncertainty >= 0.40
            and abs(bull - bear) <= 0.20
        ),
    }


def merge_into_context(context_blob: Any, features: Dict[str, Any], macro: Any = None) -> str:
    """Merge structured features/macro layers into decision_context JSON."""

    try:
        context = json.loads(context_blob or "{}") if isinstance(context_blob, str) else context_blob
    except (TypeError, json.JSONDecodeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    context["decision_features"] = features
    if isinstance(macro, dict):
        context["macro_context"] = {
            "status": macro.get("status"),
            "ok": macro.get("ok"),
            "total": macro.get("total"),
            "ts": macro.get("ts"),
            "layers": macro.get("layers") or {},
        }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
