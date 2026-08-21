"""共享的多证据交易闸门，不依赖 FastAPI。"""

import json
from typing import Any, Dict

import evidence


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


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


def evaluate_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        context = json.loads(row.get("decision_context") or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    asset = str(row.get("target_asset") or "BTC").upper()
    asset_context = assets.get(asset) or assets.get("BTC") or {}
    if not isinstance(asset_context, dict):
        asset_context = {}
    market_eligible = (
        asset_context.get("decision_eligible") is True
        and asset_context.get("status") == "ok"
    )
    stats = (
        asset_context.get("stats_7d")
        if market_eligible and isinstance(asset_context.get("stats_7d"), dict)
        else {}
    )
    confirmation_text = str(row.get("market_confirmation") or "unknown").lower()
    confirmation = 0.0
    if market_eligible:
        confirmation = 1.0 if confirmation_text in {"confirmed", "strong", "yes", "positive"} else (-1.0 if confirmation_text in {"rejected", "opposite", "negative"} else _clamp(asset_context.get("change_24h_pct", 0) / 5))
    trend_text = str(stats.get("trend", "unknown")).lower()
    trend = 1.0 if "strong bull" in trend_text else 0.6 if "bull" in trend_text else -1.0 if "strong bear" in trend_text else -0.6 if "bear" in trend_text else 0.0
    atr_pct = _clamp(stats.get("atr_pct", 0), 0, 100)
    funding_pct = _clamp(asset_context.get("funding_rate_pct", 0), -1, 1) if market_eligible else 0.0
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
    factors = {
        "news_sentiment": {"score": _clamp(row.get("sentiment_score")), "explanation": "AI 新闻情绪分"},
        "market_confirmation": {"score": confirmation, "explanation": f"市场确认={confirmation_text}" if market_eligible else "行情质量门禁未通过"},
        "trend": {"score": trend, "explanation": f"快照趋势={stats.get('trend', 'Unknown')}" if market_eligible else "行情质量门禁未通过"},
        "volatility": {"score": _clamp(1 - atr_pct / 10) if market_eligible else 0.0, "explanation": f"ATR={atr_pct:.3f}%" if market_eligible else "行情质量门禁未通过"},
        "funding": {"score": _clamp(-funding_pct / 0.1), "explanation": f"资金费率={funding_pct:.4f}%" if market_eligible else "行情质量门禁未通过"},
        "cluster_heat": {"score": _clamp((cluster - 1) / 4), "explanation": f"新闻聚合数量={cluster}"},
        "historical_confidence": {"score": _clamp(((wins / settled) - 0.5) * 2) if settled else 0.0, "explanation": f"{scope_note}已结算 {settled} 条，胜 {wins} 条"},
    }
    weights = {"news_sentiment": 0.30, "market_confirmation": 0.20, "trend": 0.15, "volatility": 0.10, "funding": 0.10, "cluster_heat": 0.10, "historical_confidence": 0.05}

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
        funding_value, _ = _macro_value(macro_rows, "funding", asset)
        ratio_value, _ = _macro_value(macro_rows, "ls_ratio", asset)
        fng_value, fng_payload = _macro_value(macro_rows, "crypto_fng")
        fed_value, fed_payload = _macro_value(macro_rows, "next_move_bp")
        flow_value, _ = _macro_value(macro_rows, "taker_buy_sell", asset)

        structure_parts = []
        if funding_value is not None:
            structure_parts.append(-_clamp(float(funding_value) / 0.1))
        if ratio_value is not None:
            structure_parts.append(-_clamp((float(ratio_value) - 1.0) / 1.5))
        if structure_parts:
            structure_score = sum(structure_parts) / len(structure_parts)
            factors["structure"] = {"score": _clamp(structure_score), "explanation": "资金费率/多空比盘面结构"}
            weights["structure"] = 0.08

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
