"""共享的多证据交易闸门，不依赖 FastAPI。"""

import json
from typing import Any, Dict

import evidence


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


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
    stats = asset_context.get("stats_7d") if isinstance(asset_context.get("stats_7d"), dict) else {}
    confirmation_text = str(row.get("market_confirmation") or "unknown").lower()
    confirmation = 1.0 if confirmation_text in {"confirmed", "strong", "yes", "positive"} else (-1.0 if confirmation_text in {"rejected", "opposite", "negative"} else _clamp(asset_context.get("change_24h_pct", 0) / 5))
    trend_text = str(stats.get("trend", "unknown")).lower()
    trend = 1.0 if "strong bull" in trend_text else 0.6 if "bull" in trend_text else -1.0 if "strong bear" in trend_text else -0.6 if "bear" in trend_text else 0.0
    atr_pct = _clamp(stats.get("atr_pct", 0), 0, 100)
    funding_pct = _clamp(asset_context.get("funding_rate_pct", 0), -1, 1)
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
        "market_confirmation": {"score": confirmation, "explanation": f"市场确认={confirmation_text}"},
        "trend": {"score": trend, "explanation": f"快照趋势={stats.get('trend', 'Unknown')}"},
        "volatility": {"score": _clamp(1 - atr_pct / 10), "explanation": f"ATR={atr_pct:.3f}%"},
        "funding": {"score": _clamp(-funding_pct / 0.1), "explanation": f"资金费率={funding_pct:.4f}%"},
        "cluster_heat": {"score": _clamp((cluster - 1) / 4), "explanation": f"新闻聚合数量={cluster}"},
        "historical_confidence": {"score": _clamp(((wins / settled) - 0.5) * 2) if settled else 0.0, "explanation": f"{scope_note}已结算 {settled} 条，胜 {wins} 条"},
    }
    weights = {"news_sentiment": 0.30, "market_confirmation": 0.20, "trend": 0.15, "volatility": 0.10, "funding": 0.10, "cluster_heat": 0.10, "historical_confidence": 0.05}
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
