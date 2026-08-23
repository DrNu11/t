"""共享的多证据交易闸门，不依赖 FastAPI。"""

import json
import math
from typing import Any, Dict, Optional

import evidence
from decision_features import normalize_analysis


_DEFAULT_IMPACT_WINDOW = {
    "short": {"min_minutes": 0, "max_minutes": 120},
    "medium": {"min_minutes": 120, "max_minutes": 4320},
    "long": {"min_minutes": 4320, "max_minutes": 43200},
}


def _finite_number(value: Any) -> Optional[float]:
    """Parse a finite JSON number, rejecting bools and malformed strings."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bounded_number(value: Any, low: float = 0.0, high: float = 1.0) -> Optional[float]:
    number = _finite_number(value)
    if number is None or number < low or number > high:
        return None
    return number


def _probability(value: Any) -> Optional[float]:
    """Accept JSON ratios and explicit percentage strings, never guess units."""

    if isinstance(value, str) and value.strip().endswith("%"):
        number = _finite_number(value.strip()[:-1])
        if number is None or number < 0.0 or number > 100.0:
            return None
        return number / 100.0
    return _bounded_number(value)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "是"}
    return False


def _canonical(value: Any, aliases: Dict[str, str], default: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    return aliases.get(text, default)


def _normalise_window(value: Any) -> Dict[str, Dict[str, int]]:
    """Validate LLM-provided impact intervals and fill unsafe gaps."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = None
    supplied = value if isinstance(value, dict) else {}
    result: Dict[str, Dict[str, int]] = {}
    for horizon, defaults in _DEFAULT_IMPACT_WINDOW.items():
        raw = supplied.get(horizon)
        raw = raw if isinstance(raw, dict) else {}
        minimum = _finite_number(raw.get("min_minutes"))
        maximum = _finite_number(raw.get("max_minutes"))
        if (
            minimum is None
            or maximum is None
            or minimum < 0
            or maximum < minimum
            or maximum > 525_600
        ):
            result[horizon] = dict(defaults)
        else:
            result[horizon] = {
                "min_minutes": int(round(minimum)),
                "max_minutes": int(round(maximum)),
            }
    return result


def normalize_ai_analysis(
    result: Dict[str, Any],
    *,
    score: Any = None,
    action: Any = None,
) -> Dict[str, Any]:
    """Validate LLM analysis fields and deterministically fill legacy gaps.

    Dynamic fields are accepted only when their type and range are valid.  A
    legacy model response therefore cannot silently become the old all-50/50,
    zero-force, medium-horizon record: its signed score and event metadata are
    converted into a conservative, auditable feature set.
    """

    source = result if isinstance(result, dict) else {}
    score_value = _finite_number(score)
    if score_value is None:
        score_value = _finite_number(source.get("sentiment_score"))
    score_value = max(-1.0, min(1.0, score_value or 0.0))

    action_value = str(action or source.get("suggested_action") or "HOLD").strip().upper()
    if action_value not in {"BUY", "SELL", "HOLD"}:
        action_value = "HOLD"

    strength = _canonical(source.get("event_strength"), {
        "low": "low", "weak": "low", "低": "low",
        "medium": "medium", "moderate": "medium", "mid": "medium", "中": "medium",
        "high": "high", "strong": "high", "critical": "high", "高": "high",
    }, "medium")
    prediction = _canonical(source.get("prediction_type"), {
        "continuation": "continuation", "trend": "continuation", "延续": "continuation",
        "reversal": "reversal", "mean-reversion": "reversal", "反转": "reversal",
        "breakout": "breakout", "shock": "breakout", "突破": "breakout",
    }, "continuation")
    phase = _canonical(source.get("event_phase"), {
        "early": "early", "initial": "early", "breaking": "early", "早期": "early", "突发": "early",
        "mid": "mid", "middle": "mid", "developing": "mid", "中期": "mid",
        "late": "late", "priced-in": "late", "mature": "late", "后期": "late",
    }, "mid")
    market_confirmation = _canonical(source.get("market_confirmation"), {
        "positive": "positive", "confirmed": "positive", "yes": "positive",
        "negative": "negative", "rejected": "negative", "opposite": "negative",
        "unknown": "unknown", "none": "unknown", "n/a": "unknown",
    }, "unknown")
    direct = _boolean(source.get("direct_catalyst"))

    expected_raw = _canonical(source.get("expected_horizon"), {
        "intraday": "intraday", "short": "intraday", "<24h": "intraday",
        "1-3d": "1-3d", "swing": "1-3d", "medium": "1-3d",
        "1w+": "1w+", "long": "1w+", "macro": "1w+",
    }, "")
    timeframe_raw = _canonical(source.get("timeframe_match"), {
        "intraday": "intraday", "short": "intraday",
        "swing": "swing", "medium": "swing", "1-3d": "swing",
        "macro": "macro", "long": "macro", "1w+": "macro",
    }, "")

    explicit_type = _canonical(source.get("analysis_type"), {
        "conflict": "conflict", "shock": "conflict", "event": "conflict",
        "冲突": "conflict", "突发": "conflict", "冲击": "conflict",
        "trend": "trend", "trend-force": "trend", "force": "trend",
        "momentum": "trend", "趋势": "trend", "力量": "trend",
    }, "")
    bull = _probability(source.get("bullish_probability"))
    bear = _probability(source.get("bearish_probability"))
    supplied_bull_force = _bounded_number(source.get("bullish_force"))
    supplied_bear_force = _bounded_number(source.get("bearish_force"))
    # Detect the exact legacy/default signature seen in production.  A
    # directional score plus 50/50 and zero/zero is internally inconsistent,
    # so those values are not accepted as genuine LLM analysis.
    stale_default_signature = bool(
        bull == 0.5
        and bear == 0.5
        and supplied_bull_force == 0.0
        and supplied_bear_force == 0.0
        and abs(score_value) >= 0.10
    )
    if stale_default_signature:
        bull = bear = None
        supplied_bull_force = supplied_bear_force = None
        explicit_type = ""

    analysis_type = explicit_type or (
        "conflict"
        if direct or strength == "high" or (prediction == "breakout" and phase == "early")
        else "trend"
    )

    def _fallback_probabilities() -> tuple[float, float]:
        signed_score = score_value
        if abs(signed_score) < 1e-9 and action_value in {"BUY", "SELL"}:
            base = {"low": 0.15, "medium": 0.30, "high": 0.50}[strength]
            signed_score = base if action_value == "BUY" else -base
        reliability = {"low": 0.65, "medium": 0.82, "high": 1.0}[strength]
        reliability *= {"early": 1.0, "mid": 0.88, "late": 0.70}[phase]
        reliability *= {"continuation": 0.90, "reversal": 0.78, "breakout": 1.0}[prediction]
        if direct:
            reliability = min(1.0, reliability + 0.10)
        edge = max(-0.90, min(0.90, signed_score * reliability))
        return 0.5 + edge / 2.0, 0.5 - edge / 2.0

    probability_fallback = False
    if bull is None and bear is None:
        probability_fallback = True
        bull, bear = _fallback_probabilities()
    elif bull is None:
        bull = 1.0 - bear
    elif bear is None:
        bear = 1.0 - bull
    else:
        total = bull + bear
        if total <= 0.0:
            probability_fallback = True
            bull, bear = _fallback_probabilities()
        else:
            bull, bear = bull / total, bear / total

    uncertainty = _bounded_number(source.get("uncertainty"))
    uncertainty_fallback = uncertainty is None
    if uncertainty is None:
        uncertainty = 1.0 - abs(bull - bear)

    strength_factor = {"low": 0.35, "medium": 0.60, "high": 0.85}[strength]
    phase_factor = {"early": 1.0, "mid": 0.85, "late": 0.65}[phase]
    prediction_factor = {"continuation": 1.0, "reversal": 0.82, "breakout": 0.95}[prediction]
    intensity = strength_factor * phase_factor * prediction_factor
    intensity += 0.15 * abs(score_value) + (0.10 if direct else 0.0)
    intensity = max(0.10, min(1.0, intensity))
    bull_force = supplied_bull_force
    bear_force = supplied_bear_force
    force_fallback = bull_force is None or bear_force is None
    if bull_force is None:
        bull_force = bull * intensity
    if bear_force is None:
        bear_force = bear * intensity

    explicit_horizon = _canonical(source.get("impact_horizon"), {
        "short": "short", "intraday": "short", "minutes": "short", "短期": "short",
        "medium": "medium", "swing": "medium", "1-3d": "medium", "中期": "medium",
        "long": "long", "macro": "long", "1w+": "long", "长期": "long",
    }, "")
    if stale_default_signature:
        explicit_horizon = ""
    if explicit_horizon:
        horizon = explicit_horizon
    elif expected_raw:
        horizon = {"intraday": "short", "1-3d": "medium", "1w+": "long"}[expected_raw]
    elif timeframe_raw:
        horizon = {"intraday": "short", "swing": "medium", "macro": "long"}[timeframe_raw]
    elif direct or prediction == "breakout" or prediction == "reversal" or phase == "late":
        horizon = "short"
    elif strength == "high" and prediction == "continuation":
        horizon = "long"
    else:
        horizon = "medium"

    expected = expected_raw or {"short": "intraday", "medium": "1-3d", "long": "1w+"}[horizon]
    timeframe = timeframe_raw or {"short": "intraday", "medium": "swing", "long": "macro"}[horizon]

    # Retain the legacy trade-plan fields, but overwrite every dynamic value
    # with the validated result above.
    base = normalize_analysis(source, score=score_value, action=action_value)
    base.update({
        "analysis_type": analysis_type,
        "bullish_probability": round(bull, 6),
        "bearish_probability": round(bear, 6),
        "uncertainty": round(uncertainty, 6),
        "bullish_force": round(bull_force, 6),
        "bearish_force": round(bear_force, 6),
        "impact_horizon": horizon,
        "impact_window": _normalise_window(source.get("impact_window")),
        "dual_side_candidate": bool(
            action_value in {"BUY", "SELL"}
            and uncertainty >= 0.40
            and abs(bull - bear) <= 0.20
        ),
        "prediction_type": prediction,
        "event_phase": phase,
        "market_confirmation": market_confirmation,
        "expected_horizon": expected,
        "event_strength": strength,
        "direct_catalyst": direct,
        "timeframe_match": timeframe,
        "invalidation_condition": str(source.get("invalidation_condition") or "").strip()[:200],
        "analysis_basis": {
            "mode": "deterministic_fallback" if any((
                not explicit_type,
                probability_fallback,
                uncertainty_fallback,
                force_fallback,
                not explicit_horizon,
            )) else "llm",
            "fallback_fields": [
                name for name, used in (
                    ("analysis_type", not explicit_type),
                    ("probabilities", probability_fallback),
                    ("uncertainty", uncertainty_fallback),
                    ("forces", force_fallback),
                    ("impact_horizon", not explicit_horizon),
                ) if used
            ],
            "score": round(score_value, 6),
            "event_strength": strength,
            "prediction_type": prediction,
            "event_phase": phase,
            "direct_catalyst": direct,
        },
    })
    return base


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    number = _finite_number(value)
    if number is None:
        return 0.0
    return max(low, min(high, number))


def _macro_rows(context: Dict[str, Any]) -> list[Dict[str, Any]]:
    macro = context.get("macro_context") if isinstance(context.get("macro_context"), dict) else {}
    layers = macro.get("layers") if isinstance(macro.get("layers"), dict) else {}
    rows: list[Dict[str, Any]] = []
    for values in layers.values():
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
    return rows


def _macro_value(rows: list[Dict[str, Any]], key: str, asset: str = "") -> Any:
    asset = str(asset or "").upper()
    for item in rows:
        metric = str(item.get("metric_key") or "")
        if metric == key or (asset and metric == f"{asset}.{key}"):
            if (
                item.get("status") == "ok"
                and item.get("value") is not None
                and item.get("decision_eligible") is True
            ):
                return item.get("value"), item.get("payload") or {}
    return None, {}


def _price_structure_factor(asset_context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a directional factor only for an eligible, bounded aggregate.

    ``market_structure.trend_score`` is the multi-timeframe engine's aggregate
    output.  Older experimental snapshots may have placed the same aggregate
    under ``aggregate``; accepting that shape keeps replay read-compatible
    without relaxing the quality gate.
    """

    if (
        asset_context.get("status") != "ok"
        or asset_context.get("decision_eligible") is not True
    ):
        return None
    structure = asset_context.get("market_structure")
    if not isinstance(structure, dict):
        return None
    if structure.get("decision_eligible") is not True:
        return None
    status = str(structure.get("status") or "").strip().lower()
    if status not in {"ok", "partial"}:
        return None

    aggregate = structure.get("aggregate")
    aggregate = aggregate if isinstance(aggregate, dict) else structure
    trend_score = _finite_number(aggregate.get("trend_score"))
    if trend_score is None or not -1.0 <= trend_score <= 1.0:
        return None

    trend_label = str(
        aggregate.get("trend") or structure.get("trend") or "unknown"
    ).strip().lower()
    alignment = str(
        aggregate.get("alignment") or structure.get("alignment") or "unknown"
    ).strip().lower()
    alignment_score = _finite_number(
        aggregate.get("alignment_score", structure.get("alignment_score"))
    )
    alignment_note = alignment
    if alignment_score is not None and 0.0 <= alignment_score <= 1.0:
        alignment_note = f"{alignment} ({alignment_score:.2f})"
    return {
        "score": trend_score,
        "explanation": (
            f"价格结构趋势={trend_label}; "
            f"多周期一致性={alignment_note}; 状态={status}"
        ),
    }


def evaluate_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        context = json.loads(row.get("decision_context") or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    asset = str(row.get("target_asset") or "").upper()
    # Never borrow BTC's tape for another or an unspecified asset.  Missing
    # same-asset data must remain unavailable rather than become false proof.
    asset_context = assets.get(asset, {}) if asset else {}
    if not isinstance(asset_context, dict):
        asset_context = {}
    market_eligible = (
        asset_context.get("decision_eligible") is True
        and asset_context.get("status") == "ok"
    )
    has_structure_contract = isinstance(asset_context.get("market_structure"), dict)
    if "stats_7d_decision_eligible" in asset_context:
        stats_eligible = market_eligible and asset_context.get("stats_7d_decision_eligible") is True
    else:
        # Pre-structure snapshots remain replay-compatible. New structured
        # snapshots must carry an explicit field-level approval.
        stats_eligible = market_eligible and not has_structure_contract
    stats = (
        asset_context.get("stats_7d")
        if stats_eligible and isinstance(asset_context.get("stats_7d"), dict)
        else {}
    )
    if "change_24h_decision_eligible" in asset_context:
        confirmation_eligible = (
            market_eligible and asset_context.get("change_24h_decision_eligible") is True
        )
    else:
        confirmation_eligible = market_eligible and not has_structure_contract
    confirmation_text = str(row.get("market_confirmation") or "unknown").lower()
    confirmation = 0.0
    if confirmation_eligible:
        confirmation = 1.0 if confirmation_text in {"confirmed", "strong", "yes", "positive"} else (-1.0 if confirmation_text in {"rejected", "opposite", "negative"} else _clamp(asset_context.get("change_24h_pct", 0) / 5))
    trend_text = str(stats.get("trend", "unknown")).lower()
    trend = 1.0 if "strong bull" in trend_text else 0.6 if "bull" in trend_text else -1.0 if "strong bear" in trend_text else -0.6 if "bear" in trend_text else 0.0
    atr_value = _finite_number(stats.get("atr_pct"))
    atr_pct = max(0.0, min(100.0, atr_value)) if atr_value is not None else 0.0
    if "funding_decision_eligible" in asset_context:
        funding_eligible = market_eligible and asset_context.get("funding_decision_eligible") is True
    else:
        funding_eligible = market_eligible and not has_structure_contract
    funding_pct = _clamp(asset_context.get("funding_rate_pct", 0), -1, 1) if funding_eligible else 0.0
    cluster = max(1, int(row.get("cluster_size") or 1))
    sample = row.get("history_sample") if isinstance(row.get("history_sample"), dict) else {}
    if sample:
        settled = int(sample.get("total") or 0)
        wins = int(sample.get("wins") or 0)
        scope = str(sample.get("scope") or "asset")
    else:
        settled = int(row.get("history_total") or 0)
        wins = int(row.get("history_wins") or 0)
        scope = str(row.get("history_scope") or "asset")
    scope_note = {
        "asset": "同品种",
        "lane": "同赛道自动补齐",
        "market": "全市场自动补齐",
    }.get(scope, "同品种")
    price_structure = _price_structure_factor(asset_context)
    legacy_trend_score = trend if price_structure is None else 0.0
    legacy_trend_explanation = (
        f"快照趋势={stats.get('trend', 'Unknown')}"
        if stats_eligible and price_structure is None
        else "由合格多周期盘面结构替代，避免重复计权"
        if price_structure is not None
        else "旧趋势字段质量门禁未通过"
    )
    factors = {
        "news_sentiment": {"score": _clamp(row.get("sentiment_score")), "explanation": "AI 新闻情绪分"},
        "market_confirmation": {"score": confirmation, "explanation": f"市场确认={confirmation_text}" if confirmation_eligible else "24h行情字段质量门禁未通过"},
        "trend": {"score": legacy_trend_score, "explanation": legacy_trend_explanation},
        # ATR describes risk/position sizing, not direction.  Keep it visible
        # for audit compatibility, but never let high/low volatility vote BUY
        # or SELL.
        "volatility": {
            "score": 0.0,
            "explanation": (
                f"ATR={atr_pct:.3f}% (仅风险观测，不参与方向评分)"
                if market_eligible else "行情质量门禁未通过"
            ),
        },
        "funding": {"score": _clamp(-funding_pct / 0.1), "explanation": f"资金费率={funding_pct:.4f}%" if funding_eligible else "资金费率字段质量门禁未通过"},
        # Cluster size is corroboration density, not a bullish vote.  Keep the
        # observation for confidence/audit consumers while removing it from
        # the signed direction score.
        "cluster_heat": {
            "score": 0.0,
            "observed_count": cluster,
            "role": "confidence_only",
            "explanation": f"新闻聚合数量={cluster} (仅置信观测，不参与方向评分)",
        },
        # Historical win rate already enters evidence significance/confidence
        # through ``wins``/``settled`` below.  Treating it as a positive signed
        # factor would systematically cancel valid bearish conclusions.
        "historical_confidence": {
            "score": 0.0,
            "observed_win_rate": round(wins / settled, 6) if settled else None,
            "sample_size": settled,
            "role": "confidence_only",
            "explanation": (
                f"{scope_note}已结算 {settled} 条，胜 {wins} 条 "
                "(仅置信/显著性观测，不参与方向评分)"
            ),
        },
    }
    weights = {
        "news_sentiment": 0.30,
        "market_confirmation": 0.20,
        "trend": 0.0 if price_structure is not None else 0.15,
        "volatility": 0.0,
        "funding": 0.10,
        "cluster_heat": 0.0,
        "historical_confidence": 0.0,
    }

    if price_structure is not None:
        factors["price_structure"] = price_structure
        # After normalisation this remains roughly 10–15% in common factor
        # sets: meaningful corroboration, never a standalone trigger.
        weights["price_structure"] = 0.10

    # New structured fields are optional.  They are only added when the
    # worker has persisted them, preserving the old factor shape for legacy
    # callers/tests and old decisions.
    feature_blob = row.get("decision_features") if isinstance(row.get("decision_features"), dict) else context.get("decision_features")
    if isinstance(feature_blob, dict):
        bull = _clamp(feature_blob.get("bullish_probability"), 0.0, 1.0)
        bear = _clamp(feature_blob.get("bearish_probability"), 0.0, 1.0)
        factors["direction_probability"] = {
            "score": _clamp(bull - bear),
            "explanation": f"方向概率 bull={bull:.2f}, bear={bear:.2f}",
        }
        factors["news_force"] = {
            "score": _clamp(
                _clamp(feature_blob.get("bullish_force"), 0.0, 1.0)
                - _clamp(feature_blob.get("bearish_force"), 0.0, 1.0)
            ),
            "explanation": "新闻上涨/下跌力量差",
        }
        weights["direction_probability"] = 0.08
        weights["news_force"] = 0.07

    macro_rows = _macro_rows(context)
    if macro_rows:
        ratio_value, _ = _macro_value(macro_rows, "ls_ratio", asset)
        fng_value, fng_payload = _macro_value(macro_rows, "crypto_fng")
        fed_value, fed_payload = _macro_value(macro_rows, "next_move_bp")
        flow_value, _ = _macro_value(macro_rows, "taker_buy_sell", asset)

        # Funding already has a dedicated same-asset factor above.  Reusing
        # macro funding here would silently double its directional weight.
        # Long/short ratio is a distinct positioning signal and stays
        # optional when the provider has no eligible observation.
        ratio_number = _finite_number(ratio_value)
        if ratio_number is not None and ratio_number >= 0.0:
            factors["positioning"] = {
                "score": -_clamp((ratio_number - 1.0) / 1.5),
                "explanation": f"多空持仓比={ratio_number:.3f}",
            }
            weights["positioning"] = 0.08

        if fng_value is not None:
            factors["sentiment_regime"] = {
                "score": _clamp((float(fng_value) - 50.0) / 50.0),
                "explanation": f"Fear&Greed={float(fng_value):.1f} {fng_payload.get('classification', '')}".strip(),
            }
            weights["sentiment_regime"] = 0.05

        if fed_value is not None:
            # Rate hikes are normally risk-off for crypto and supportive for
            # USD/defensive gold; keep this as a small, explainable factor.
            fed_score = -_clamp(float(fed_value) / 25.0)
            if asset in {"XAU", "GOLD"}:
                fed_score = -fed_score
            factors["fed_expectation"] = {
                "score": _clamp(fed_score),
                "explanation": f"ZQ-EFFR={float(fed_value):+.1f}bp ({fed_payload.get('next_move', 'N/A')})",
            }
            weights["fed_expectation"] = 0.05

        if flow_value is not None:
            factors["flow"] = {
                "score": _clamp(float(flow_value) - 1.0),
                "explanation": f"主动买卖比={float(flow_value):.3f}",
            }
            weights["flow"] = 0.05

    # Keep the final score on the same scale after optional factors are added.
    weight_total = sum(weights.values()) or 1.0
    weights = {name: value / weight_total for name, value in weights.items()}
    # JSON consumers legitimately assert this probability-like contract
    # exactly.  Close binary floating-point residue into the final positive
    # weight while leaving observation-only zero weights untouched.
    positive_names = [name for name, value in weights.items() if value > 0.0]
    if positive_names:
        closing_name = positive_names[-1]
        weights[closing_name] = 1.0 - sum(
            value for name, value in weights.items() if name != closing_name
        )
    final_score = sum(factors[name]["score"] * weight for name, weight in weights.items())
    raw_action = "BUY" if final_score >= 0.3 else "SELL" if final_score <= -0.3 else "HOLD"
    validation = evidence.evaluate(factors, final_score, raw_action, wins, settled, scope=scope)
    return {
        "factors": factors,
        "weights": weights,
        "final_score": round(final_score, 4),
        "raw_action": raw_action,
        "action": validation["gated_action"],
        "confidence": validation["confidence"],
        "evidence": validation["evidence"],
        "significance": validation["significance"],
        "contradictions": validation["contradictions"],
        "confidence_detail": validation["confidence_detail"],
        "verdict": validation["verdict"],
    }
