#!/usr/bin/env python3
"""
Trident Agent MVP — Evidence / Validation Layer
================================================

Implements the three layers required by docs/约定规范.md on top of the existing
multi-factor engine in api_server._factor_result:

  1. 证据层 — every factor score (-1..1) is normalised to a 0-10 evidence value
     so that heterogeneous sources are comparable and auditable.
  2. 校验层 — chi-square test (df=1) on the settled win/loss sample of the same
     asset. A large p-value means the historical edge is not statistically
     significant, so the decision weight is reduced.
  3. 反幻觉层 — cross-checks factors against each other; a bullish headline
     combined with a bearish tape / adverse funding is flagged as a
     contradiction and lowers the unified confidence.

Output is a single 0-100 confidence. Below CONFIDENCE_GATE no directional
trading conclusion is produced (action degrades to HOLD).

Pure stdlib — no scipy/numpy dependency.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Below this unified confidence no directional conclusion is emitted.
CONFIDENCE_GATE = 55.0

# Minimum settled sample before the chi-square result is trusted at all.
MIN_SIGNIFICANCE_SAMPLE = 8

# p-value thresholds -> multiplier applied to the raw confidence.
_SIGNIFICANCE_TIERS = ((0.05, 1.0), (0.20, 0.85), (1.01, 0.70))

# Confidence points removed per detected contradiction (capped).
_CONTRADICTION_PENALTY = 12.0
_MAX_CONTRADICTION_PENALTY = 36.0

# A factor is considered "directional" beyond this absolute score.
_DIRECTIONAL = 0.3

# Same-lane fallback when a single asset has too few settled WIN/LOSS rows.
_ASSET_ALIASES = {
    "XAUUSD": "XAU", "GOLD": "XAU", "GC": "XAU", "XAUUSDT": "XAU",
    "WTIUSD": "WTI", "CL": "WTI", "OIL": "WTI", "CRUDE": "WTI", "WTIUSDT": "WTI",
    "BTCUSD": "BTC", "BTCUSDT": "BTC",
    "ETHUSD": "ETH", "ETHUSDT": "ETH",
}
_LANES = {
    "crypto": ("BTC", "ETH", "SOL", "BNB", "DOGE", "XRP", "ADA"),
    "gold": ("XAU",),
    "oil": ("WTI",),
}
_LANE_OF = {asset: lane for lane, assets in _LANES.items() for asset in assets}
_SCOPE_LABELS = {
    "asset": "同品种",
    "lane": "同赛道自动补齐",
    "market": "全市场自动补齐",
}

# A settled row is allowed to teach the evidence engine only when it crossed
# the same fail-closed boundary as a formal paper-trading decision.  Keep the
# exact verdict text in one place so research/legacy rows cannot drift into
# the statistical sample through a looser predicate.
PASSED_TRADE_GATE_REASON = "证据充分，允许输出方向性结论"

_FORMAL_AI_COLUMNS = {
    "id", "news_id", "settled", "is_correct", "target_asset",
    "suggested_action", "evidence_action", "trade_gate_reason",
    "paper_trading_run_id",
}
_FORMAL_NEWS_COLUMNS = {"id", "quality_status"}
_QUICK_SIM_COLUMNS = {
    "decision_id", "settled", "verdict", "asset", "gate_passed",
}


def _table_columns(connection, table: str) -> set[str]:
    """Return SQLite table columns, treating unknown/legacy schema as empty."""

    try:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except (sqlite3.DatabaseError, TypeError, IndexError):
        return set()


def formal_ai_history_available(connection) -> bool:
    """Whether the DB can prove every required formal-decision attribute."""

    return (
        _FORMAL_AI_COLUMNS <= _table_columns(connection, "ai_decisions")
        and _FORMAL_NEWS_COLUMNS <= _table_columns(connection, "raw_news")
    )


def formal_ai_history_predicate(
    decision_alias: str = "ad", news_alias: str = "rn",
) -> str:
    """Canonical SQL predicate for formal historical learning samples."""

    return (
        f"LOWER(TRIM(COALESCE({news_alias}.quality_status, ''))) = 'verified' "
        f"AND UPPER(TRIM(COALESCE({decision_alias}.suggested_action, ''))) IN ('BUY', 'SELL') "
        f"AND UPPER(TRIM(COALESCE({decision_alias}.evidence_action, 'HOLD'))) "
        f"= UPPER(TRIM(COALESCE({decision_alias}.suggested_action, ''))) "
        f"AND COALESCE({decision_alias}.trade_gate_reason, '') = ? "
        f"AND {decision_alias}.paper_trading_run_id IS NOT NULL"
    )


def to_evidence_score(score: float) -> float:
    """Map a factor score in [-1, 1] onto the 0-10 evidence scale."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    return round((max(-1.0, min(1.0, value)) + 1.0) * 5.0, 2)


def build_evidence(factors: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Structure every factor as a 0-10 evidence item with its source note."""
    return {
        name: {
            "evidence_score": to_evidence_score(payload.get("score")),
            "raw_score": payload.get("score"),
            "source": payload.get("explanation", ""),
        }
        for name, payload in factors.items()
    }


def normalize_asset(asset: Any) -> str:
    token = str(asset or "").strip().upper()
    if not token or token == "NONE":
        return ""
    return _ASSET_ALIASES.get(token, token)


def lane_assets(asset: Any) -> Tuple[str, ...]:
    canonical = normalize_asset(asset)
    if not canonical:
        return ()
    lane = _LANE_OF.get(canonical)
    if not lane:
        return (canonical,)
    return _LANES[lane]


def query_assets(assets: Sequence[str]) -> Tuple[str, ...]:
    """Canonical names plus stored aliases so XAU also matches XAUUSD."""
    reverse: Dict[str, List[str]] = {}
    for alias, canonical in _ASSET_ALIASES.items():
        reverse.setdefault(canonical, []).append(alias)
    tokens: List[str] = []
    for asset in assets:
        canonical = normalize_asset(asset)
        if not canonical:
            continue
        tokens.append(canonical)
        tokens.extend(reverse.get(canonical, []))
    return tuple(dict.fromkeys(tokens))


def _count_pool(connection, sql: str, params: Sequence[Any] = ()) -> Tuple[int, int]:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        return 0, 0
    if isinstance(row, (sqlite3.Row, dict)):
        payload = dict(row)
        total = int(payload.get("total") or 0)
        wins = int(payload.get("wins") or 0)
    else:
        total = int(row[0] or 0)
        wins = int(row[1] or 0)
    return total, max(0, min(wins, total))


def _ai_pool(connection, assets: Sequence[str] = ()) -> Tuple[int, int]:
    if not formal_ai_history_available(connection):
        return 0, 0
    sql = """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN UPPER(ad.is_correct)='WIN' THEN 1 ELSE 0 END) AS wins
             FROM ai_decisions ad
             INNER JOIN raw_news rn ON rn.id = ad.news_id
             WHERE ad.settled=1 AND UPPER(ad.is_correct) IN ('WIN','LOSS')
               AND """ + formal_ai_history_predicate()
    params: List[Any] = [PASSED_TRADE_GATE_REASON]
    tokens = query_assets(assets)
    if tokens:
        placeholders = ",".join("?" * len(tokens))
        sql += f" AND UPPER(ad.target_asset) IN ({placeholders})"
        params.extend(tokens)
    return _count_pool(connection, sql, params)


def _quick_sim_pool(connection, assets: Sequence[str] = (),
                    exclude_decision_ids: Optional[Sequence[int]] = None) -> Tuple[int, int]:
    if (
        not _QUICK_SIM_COLUMNS <= _table_columns(connection, "quick_sim_trades")
        or not formal_ai_history_available(connection)
    ):
        return 0, 0
    sql = """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN UPPER(qs.verdict)='WIN' THEN 1 ELSE 0 END) AS wins
             FROM quick_sim_trades qs
             INNER JOIN ai_decisions ad ON ad.id = qs.decision_id
             INNER JOIN raw_news rn ON rn.id = ad.news_id
             WHERE qs.settled=1 AND qs.gate_passed=1
               AND UPPER(qs.verdict) IN ('WIN','LOSS')
               AND """ + formal_ai_history_predicate()
    params: List[Any] = [PASSED_TRADE_GATE_REASON]
    tokens = query_assets(assets)
    if tokens:
        placeholders = ",".join("?" * len(tokens))
        sql += f" AND UPPER(qs.asset) IN ({placeholders})"
        params.extend(tokens)
    if exclude_decision_ids:
        placeholders = ",".join("?" * len(exclude_decision_ids))
        sql += f" AND qs.decision_id NOT IN ({placeholders})"
        params.extend(int(item) for item in exclude_decision_ids)
    return _count_pool(connection, sql, params)


def _settled_decision_ids(connection, assets: Sequence[str] = ()) -> List[int]:
    if not formal_ai_history_available(connection):
        return []
    sql = """SELECT ad.id FROM ai_decisions ad
             INNER JOIN raw_news rn ON rn.id = ad.news_id
             WHERE ad.settled=1 AND UPPER(ad.is_correct) IN ('WIN','LOSS')
               AND """ + formal_ai_history_predicate()
    params: List[Any] = [PASSED_TRADE_GATE_REASON]
    tokens = query_assets(assets)
    if tokens:
        placeholders = ",".join("?" * len(tokens))
        sql += f" AND UPPER(ad.target_asset) IN ({placeholders})"
        params.extend(tokens)
    return [int(row[0]) for row in connection.execute(sql, params).fetchall()]


def _merge_pool(connection, assets: Sequence[str] = ()) -> Tuple[int, int]:
    ai_total, ai_wins = _ai_pool(connection, assets)
    qs_total, qs_wins = _quick_sim_pool(
        connection, assets, exclude_decision_ids=_settled_decision_ids(connection, assets)
    )
    return ai_total + qs_total, ai_wins + qs_wins


def collect_settled_sample(asset: Any, connection=None) -> Dict[str, Any]:
    """Expand the chi-square sample from asset -> lane -> whole market.

    Only real WIN/LOSS rows are counted. Nothing is fabricated.
    """
    import db as _db

    owned = connection is None
    connection = connection or _db.get_connection()
    try:
        canonical = normalize_asset(asset)
        peers = tuple(dict.fromkeys(
            [canonical, *lane_assets(canonical)] if canonical else ()
        ))
        scopes: List[Tuple[str, Sequence[str]]] = []
        if peers:
            scopes.append(("asset", (canonical,)))
            if len(peers) > 1:
                scopes.append(("lane", peers))
        scopes.append(("market", ()))
        chosen = {"scope": "market", "assets": (), "total": 0, "wins": 0}
        breakdown: List[Dict[str, Any]] = []
        for scope, assets in scopes:
            total, wins = _merge_pool(connection, assets)
            item = {"scope": scope, "assets": list(assets), "total": total, "wins": wins}
            breakdown.append(item)
            if chosen["total"] < MIN_SIGNIFICANCE_SAMPLE and total > chosen["total"]:
                chosen = item
            if total >= MIN_SIGNIFICANCE_SAMPLE:
                chosen = item
                break
        return {
            "asset": canonical or str(asset or "").upper() or "NONE",
            "scope": chosen["scope"],
            "assets": list(chosen["assets"]),
            "total": int(chosen["total"]),
            "wins": int(chosen["wins"]),
            "supplemented": chosen["scope"] != "asset",
            "breakdown": breakdown,
        }
    finally:
        if owned:
            connection.close()


def chi_square_test(wins: int, total: int, scope: str = "asset") -> Dict[str, Any]:
    """Goodness-of-fit test of `wins` against a 50/50 null hypothesis.

    Returns the chi-square statistic (df=1), its p-value and whether the
    sample is large enough / significant enough to be trusted.
    For df=1 the survival function is exactly erfc(sqrt(x / 2)).
    """
    total = max(int(total or 0), 0)
    wins = max(min(int(wins or 0), total), 0)
    losses = total - wins
    scope_label = _SCOPE_LABELS.get(scope, _SCOPE_LABELS["asset"])
    if total < MIN_SIGNIFICANCE_SAMPLE:
        return {
            "chi_square": 0.0, "p_value": 1.0, "sample_size": total, "wins": wins,
            "losses": losses, "significant": False, "sufficient_sample": False,
            "scope": scope, "edge_direction": "insufficient",
            "note": (
                f"{scope_label}已结算样本 {total} 条，不足 {MIN_SIGNIFICANCE_SAMPLE} 条，"
                "历史胜率不参与显著性判定"
            ),
        }
    expected = total / 2.0
    statistic = ((wins - expected) ** 2 + (losses - expected) ** 2) / expected
    p_value = math.erfc(math.sqrt(statistic / 2.0))
    positive_edge = wins > losses
    edge_direction = "positive" if positive_edge else "negative" if wins < losses else "neutral"
    return {
        "chi_square": round(statistic, 4),
        "p_value": round(p_value, 4),
        "sample_size": total,
        "wins": wins,
        "losses": losses,
        # The chi-square p-value is two-sided.  Only a statistically reliable
        # win rate above 50% is positive evidence for reusing the strategy;
        # a significant failure rate must never be interpreted as confidence.
        "significant": positive_edge and p_value <= 0.05,
        "edge_direction": edge_direction,
        "sufficient_sample": True,
        "scope": scope,
        "note": (
            f"{scope_label}样本 {total} 条胜 {wins} 条，"
            f"双侧χ²={statistic:.3f}，p={p_value:.4f}，"
            f"历史边际={edge_direction}"
        ),
    }


def significance_weight(test: Dict[str, Any]) -> float:
    """Return a confidence multiplier for a *positive* historical edge."""
    if not test.get("sufficient_sample"):
        return 0.80
    total = max(int(test.get("sample_size") or 0), 0)
    wins = max(min(int(test.get("wins") or 0), total), 0)
    if total and wins < total / 2.0:
        # A strategy that loses more often than it wins is vetoed.  The
        # two-sided p-value may be tiny, but that proves failure, not edge.
        return 0.0
    if total and wins == total / 2.0:
        # No demonstrated edge: even a maximal raw factor score stays below
        # the 55-point execution gate.
        return 0.50
    p_value = float(test.get("p_value", 1.0))
    for threshold, weight in _SIGNIFICANCE_TIERS:
        if p_value <= threshold:
            return weight
    return _SIGNIFICANCE_TIERS[-1][1]


def _score(factors: Dict[str, Dict[str, Any]], name: str) -> float:
    try:
        return float(factors.get(name, {}).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def detect_contradictions(factors: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    """Cross-check factors against each other and flag conflicting evidence."""
    sentiment = _score(factors, "news_sentiment")
    confirmation = _score(factors, "market_confirmation")
    trend = _score(factors, "trend")
    price_structure = _score(factors, "price_structure")
    structure_is_primary = "price_structure" in factors
    market_trend = price_structure if structure_is_primary else trend
    trend_type = "sentiment_vs_price_structure" if structure_is_primary else "sentiment_vs_trend"
    trend_label = "多周期盘面结构" if structure_is_primary else "趋势结构"
    funding = _score(factors, "funding")
    found: List[Dict[str, str]] = []
    if sentiment > _DIRECTIONAL and market_trend < -_DIRECTIONAL:
        found.append({"type": trend_type, "detail": f"新闻偏多但{trend_label}向下，方向证据互相冲突"})
    if sentiment < -_DIRECTIONAL and market_trend > _DIRECTIONAL:
        found.append({"type": trend_type, "detail": f"新闻偏空但{trend_label}向上，方向证据互相冲突"})
    if sentiment > _DIRECTIONAL and confirmation < -_DIRECTIONAL:
        found.append({"type": "sentiment_vs_confirmation", "detail": "新闻偏多但盘面未确认（价格反向），存在证伪风险"})
    if sentiment < -_DIRECTIONAL and confirmation > _DIRECTIONAL:
        found.append({"type": "sentiment_vs_confirmation", "detail": "新闻偏空但盘面反向走强，存在证伪风险"})
    if sentiment > _DIRECTIONAL and funding < -_DIRECTIONAL:
        found.append({"type": "sentiment_vs_funding", "detail": "新闻偏多但资金费率显示多头拥挤，追多性价比低"})
    if sentiment < -_DIRECTIONAL and funding > _DIRECTIONAL:
        found.append({"type": "sentiment_vs_funding", "detail": "新闻偏空但资金费率显示空头拥挤，追空性价比低"})
    return found


def unified_confidence(final_score: float, test: Dict[str, Any],
                       contradictions: List[Dict[str, str]]) -> Dict[str, Any]:
    """Fuse factor strength, statistical significance and contradictions into 0-100."""
    base = min(abs(float(final_score or 0.0)), 1.0) * 100.0
    weight = significance_weight(test)
    penalty = min(len(contradictions) * _CONTRADICTION_PENALTY, _MAX_CONTRADICTION_PENALTY)
    value = max(0.0, min(100.0, base * weight - penalty))
    return {
        "confidence": round(value, 2),
        "base": round(base, 2),
        "significance_weight": weight,
        "contradiction_penalty": penalty,
        "gate": CONFIDENCE_GATE,
        "passed_gate": value >= CONFIDENCE_GATE,
    }


def evaluate(factors: Dict[str, Dict[str, Any]], final_score: float,
             action: str, wins: int, total: int, scope: str = "asset") -> Dict[str, Any]:
    """Full evidence -> validation -> anti-hallucination pipeline.

    Returns the evidence table, the chi-square result, contradictions, the
    unified 0-100 confidence and the gated action.
    """
    test = chi_square_test(wins, total, scope=scope)
    contradictions = detect_contradictions(factors)
    confidence = unified_confidence(final_score, test, contradictions)
    gated_action = action if confidence["passed_gate"] else "HOLD"
    if confidence["passed_gate"]:
        verdict = "证据充分，允许输出方向性结论"
    elif contradictions:
        verdict = "证据互相矛盾且统一置信度低于闸门，仅输出观望结论"
    else:
        verdict = f"统一置信度 {confidence['confidence']} 低于闸门 {CONFIDENCE_GATE}，仅输出观望结论"
    return {
        "evidence": build_evidence(factors),
        "significance": test,
        "contradictions": contradictions,
        "confidence": confidence["confidence"],
        "confidence_detail": confidence,
        "gated_action": gated_action,
        "verdict": verdict,
    }
