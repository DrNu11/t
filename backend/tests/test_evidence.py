"""Smoke tests for src_python/evidence.py — 证据层 / 校验层 / 反幻觉层."""

import json
import sqlite3

import decision_guard
import evidence


def _factors(sentiment=0.0, confirmation=0.0, trend=0.0, funding=0.0):
    return {
        "news_sentiment": {"score": sentiment, "explanation": "s"},
        "market_confirmation": {"score": confirmation, "explanation": "c"},
        "trend": {"score": trend, "explanation": "t"},
        "funding": {"score": funding, "explanation": "f"},
    }


def test_to_evidence_score_normalises_to_0_10():
    assert evidence.to_evidence_score(-1) == 0.0
    assert evidence.to_evidence_score(0) == 5.0
    assert evidence.to_evidence_score(1) == 10.0
    assert evidence.to_evidence_score(5) == 10.0
    assert evidence.to_evidence_score(None) == 5.0


def test_build_evidence_keeps_source_and_range():
    table = evidence.build_evidence(_factors(sentiment=0.6))
    assert set(table) == {"news_sentiment", "market_confirmation", "trend", "funding"}
    assert table["news_sentiment"]["evidence_score"] == 8.0
    assert table["news_sentiment"]["source"] == "s"
    assert all(0 <= item["evidence_score"] <= 10 for item in table.values())


def test_chi_square_small_sample_not_significant():
    result = evidence.chi_square_test(wins=4, total=5)
    assert result["sufficient_sample"] is False
    assert result["significant"] is False
    assert result["p_value"] == 1.0


def test_chi_square_balanced_sample_has_high_p_value():
    result = evidence.chi_square_test(wins=10, total=20)
    assert result["chi_square"] == 0.0
    assert result["p_value"] == 1.0
    assert result["significant"] is False
    assert result["sufficient_sample"] is True


def test_chi_square_skewed_sample_is_significant():
    result = evidence.chi_square_test(wins=90, total=100)
    assert result["chi_square"] == 64.0
    assert result["p_value"] < 0.05
    assert result["significant"] is True
    assert result["losses"] == 10


def test_significance_weight_tiers():
    assert evidence.significance_weight(evidence.chi_square_test(90, 100)) == 1.0
    assert evidence.significance_weight(evidence.chi_square_test(10, 20)) == 0.70
    assert evidence.significance_weight(evidence.chi_square_test(2, 3)) == 0.80


def test_detect_contradictions_flags_bullish_news_against_tape():
    found = evidence.detect_contradictions(_factors(sentiment=0.8, confirmation=-0.9, trend=-0.7, funding=-0.6))
    types = {item["type"] for item in found}
    assert types == {"sentiment_vs_trend", "sentiment_vs_confirmation", "sentiment_vs_funding"}


def test_detect_contradictions_empty_when_aligned():
    assert evidence.detect_contradictions(_factors(sentiment=0.8, confirmation=0.9, trend=0.7, funding=0.6)) == []


def test_unified_confidence_penalises_contradictions_and_clamps():
    test = evidence.chi_square_test(90, 100)
    clean = evidence.unified_confidence(0.9, test, [])
    noisy = evidence.unified_confidence(0.9, test, [{"type": "x", "detail": "d"}] * 5)
    assert clean["confidence"] == 90.0
    assert noisy["contradiction_penalty"] == 36.0
    assert noisy["confidence"] == 54.0
    assert 0 <= noisy["confidence"] <= 100


def test_evaluate_gates_low_confidence_to_hold():
    result = evidence.evaluate(_factors(sentiment=0.8, trend=-0.9), final_score=0.2,
                               action="BUY", wins=1, total=2)
    assert result["gated_action"] == "HOLD"
    assert result["confidence"] < evidence.CONFIDENCE_GATE
    assert result["contradictions"]
    assert "观望" in result["verdict"]


def test_evaluate_passes_gate_with_strong_aligned_evidence():
    result = evidence.evaluate(_factors(sentiment=0.9, confirmation=0.9, trend=0.9, funding=0.5),
                               final_score=0.9, action="BUY", wins=90, total=100)
    assert result["gated_action"] == "BUY"
    assert result["confidence"] >= evidence.CONFIDENCE_GATE
    assert result["significance"]["significant"] is True
    assert result["contradictions"] == []


def _seed_settled(
    conn,
    asset,
    verdicts,
    *,
    quality_status="verified",
    action="BUY",
    evidence_action="BUY",
    gate_reason=evidence.PASSED_TRADE_GATE_REASON,
    run_id=1,
):
    for verdict in verdicts:
        news_id = conn.execute(
            """INSERT INTO raw_news
                   (source, content, timestamp, status, quality_status)
               VALUES ('test', ?, '2026-08-15 10:00:00', 'DONE', ?)""",
            (f"{asset} {verdict}", quality_status),
        ).lastrowid
        conn.execute(
            """INSERT INTO ai_decisions
                   (news_id, sentiment_score, suggested_action, reasoning, target_asset,
                    settled, is_correct, evidence_action, trade_gate_reason,
                    paper_trading_run_id)
               VALUES (?, 0.8, ?, 'test', ?, 1, ?, ?, ?, ?)""",
            (
                news_id, action, asset, verdict, evidence_action,
                gate_reason, run_id,
            ),
        )
    conn.commit()


def _seed_quick_sim(
    conn,
    asset,
    verdicts,
    *,
    gate_passed=1,
    quality_status="verified",
    evidence_action="BUY",
    gate_reason=evidence.PASSED_TRADE_GATE_REASON,
    run_id=1,
):
    for verdict in verdicts:
        news_id = conn.execute(
            """INSERT INTO raw_news
                   (source, content, timestamp, status, quality_status)
               VALUES ('test', ?, '2026-08-15 10:00:00', 'DONE', ?)""",
            (f"{asset} qs {verdict}", quality_status),
        ).lastrowid
        decision_id = conn.execute(
            """INSERT INTO ai_decisions
                   (news_id, sentiment_score, suggested_action, reasoning,
                    target_asset, settled, evidence_action, trade_gate_reason,
                    paper_trading_run_id)
               VALUES (?, 0.8, 'BUY', 'test', ?, 0, ?, ?, ?)""",
            (news_id, asset, evidence_action, gate_reason, run_id),
        ).lastrowid
        conn.execute(
            """INSERT INTO quick_sim_trades
                   (decision_id, news_id, asset, action, score, gate_passed, notional_usdt,
                    entry_price, entry_ts, horizon_minutes, verdict, settled)
               VALUES (?, ?, ?, 'BUY', 0.8, ?, 100, 100, 1, 5, ?, 1)""",
            (decision_id, news_id, asset, gate_passed, verdict),
        )
    conn.commit()


def test_collect_settled_sample_falls_back_to_market(temp_db):
    _seed_settled(temp_db, "BTC", ["WIN"] * 6 + ["LOSS"] * 4)
    sample = evidence.collect_settled_sample("XAU", connection=temp_db)
    assert sample["scope"] == "market"
    assert sample["supplemented"] is True
    assert sample["total"] == 10
    assert sample["wins"] == 6
    test = evidence.chi_square_test(sample["wins"], sample["total"], scope=sample["scope"])
    assert test["sufficient_sample"] is True
    assert "全市场自动补齐" in test["note"]


def test_collect_settled_sample_uses_same_lane(temp_db):
    _seed_settled(temp_db, "ETH", ["WIN"] * 5 + ["LOSS"] * 3)
    sample = evidence.collect_settled_sample("BTC", connection=temp_db)
    assert sample["scope"] == "lane"
    assert sample["total"] == 8
    assert sample["wins"] == 5


def test_collect_settled_sample_merges_quick_sim_without_double_count(temp_db):
    _seed_settled(temp_db, "XAU", ["WIN", "LOSS"])
    _seed_quick_sim(temp_db, "XAU", ["WIN"] * 6)
    sample = evidence.collect_settled_sample("XAUUSD", connection=temp_db)
    assert sample["scope"] == "asset"
    assert sample["total"] == 8
    assert sample["wins"] == 7
    assert sample["supplemented"] is False


def test_collect_settled_sample_rejects_every_research_ai_lane(temp_db):
    # Eight formal losses are the only rows allowed to teach the engine.
    _seed_settled(temp_db, "BTC", ["LOSS"] * 8)
    _seed_settled(
        temp_db, "BTC", ["WIN"] * 2, quality_status="unverified",
    )
    _seed_settled(
        temp_db, "BTC", ["WIN"] * 2, action="HOLD", evidence_action="HOLD",
    )
    _seed_settled(
        temp_db, "BTC", ["WIN"] * 2, evidence_action="HOLD",
    )
    _seed_settled(
        temp_db, "BTC", ["WIN"] * 2, gate_reason="旧宽松闸门通过",
    )
    _seed_settled(temp_db, "BTC", ["WIN"] * 2, run_id=None)

    sample = evidence.collect_settled_sample("BTC", connection=temp_db)

    assert sample["scope"] == "asset"
    assert sample["total"] == 8
    assert sample["wins"] == 0


def test_collect_settled_sample_only_accepts_gate_passed_quick_sim(temp_db):
    _seed_quick_sim(temp_db, "XAU", ["LOSS"] * 8, gate_passed=1)
    _seed_quick_sim(temp_db, "XAU", ["WIN"] * 6, gate_passed=0)
    _seed_quick_sim(
        temp_db, "XAU", ["WIN"] * 6,
        gate_passed=1, quality_status="unverified",
    )

    sample = evidence.collect_settled_sample("XAU", connection=temp_db)

    assert sample["total"] == 8
    assert sample["wins"] == 0


def test_collect_settled_sample_fails_closed_on_legacy_schema():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE raw_news (id INTEGER PRIMARY KEY, quality_status TEXT)"
        )
        connection.execute(
            """CREATE TABLE ai_decisions (
                   id INTEGER PRIMARY KEY, news_id INTEGER, settled INTEGER,
                   is_correct TEXT, target_asset TEXT, suggested_action TEXT
               )"""
        )
        connection.execute(
            "INSERT INTO raw_news VALUES (1, 'verified')"
        )
        connection.execute(
            "INSERT INTO ai_decisions VALUES (1, 1, 1, 'WIN', 'BTC', 'BUY')"
        )

        sample = evidence.collect_settled_sample("BTC", connection=connection)

        assert sample["total"] == 0
        assert sample["wins"] == 0
    finally:
        connection.close()


def test_decision_guard_uses_auto_sample_scope():
    result = decision_guard.evaluate_decision({
        "sentiment_score": 0.8,
        "target_asset": "XAU",
        "market_confirmation": "unknown",
        "decision_context": "{}",
        "cluster_size": 1,
        "history_sample": {"total": 10, "wins": 8, "scope": "market"},
    })
    assert result["significance"]["sample_size"] == 10
    assert result["significance"]["scope"] == "market"
    assert "全市场自动补齐" in result["factors"]["historical_confidence"]["explanation"]


def test_decision_guard_neutralizes_market_factors_without_quality_approval():
    context = {
        "assets": {
            "BTC": {
                "status": "ok",
                "decision_eligible": False,
                "change_24h_pct": 8,
                "funding_rate_pct": 0.2,
                "stats_7d": {"trend": "Strong Bull", "atr_pct": 4},
            },
        },
    }
    result = decision_guard.evaluate_decision({
        "sentiment_score": 0.2,
        "target_asset": "BTC",
        "market_confirmation": "confirmed",
        "decision_context": json.dumps(context),
        "cluster_size": 1,
    })
    for name in ("market_confirmation", "trend", "volatility", "funding"):
        assert result["factors"][name]["score"] == 0.0
        assert "质量门禁" in result["factors"][name]["explanation"]
