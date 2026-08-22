"""离线测试：Hermes 多 agent 写作 + 已结算技能沉淀。"""

import pytest

import hermes_agent

PASSED_REASON = "证据充分，允许输出方向性结论"


def _seed_settled(
    conn,
    asset,
    action,
    prediction_type,
    verdicts,
    *,
    quality_status="verified",
    evidence_action=None,
    gate_reason=PASSED_REASON,
    paper_run_id=1,
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
                    prediction_type, settled, is_correct, forward_pnl, evidence_action,
                    trade_gate_reason, paper_trading_run_id)
               VALUES (?, 0.6, ?, 'test', ?, ?, 1, ?, 0.4, ?, ?, ?)""",
            (
                news_id, action, asset, prediction_type, verdict,
                evidence_action or action, gate_reason, paper_run_id,
            ),
        )
    conn.commit()


def test_write_desks_maps_war_to_gold_risk():
    desks = hermes_agent.write_desks("中东开战，原油设施遇袭，市场避险升温")
    by_desk = {item["desk"]: item["brief"] for item in desks}
    assert set(by_desk) == {"news", "macro", "risk", "trader"}
    assert "gold" in by_desk["macro"]
    assert "风险资产" in by_desk["risk"]
    assert "不下最终单" in by_desk["trader"]


def test_persist_news_writing_stores_raw_not_verdict(temp_db):
    packet = hermes_agent.persist_news_writing(
        1,
        "鲍威尔暗示降息，BTC 短线波动加大",
        source="techflow",
        market_context="BTC 1H trend=bull",
        connection=temp_db,
    )
    temp_db.commit()
    rows = temp_db.execute(
        "SELECT observation_type, source, content FROM hermes_observations ORDER BY id"
    ).fetchall()
    types = {row["observation_type"] for row in rows}
    assert "news" in types
    assert "market" in types
    assert "desk" in types
    assert any("鲍威尔" in row["content"] for row in rows)
    assert "Hermes Multi-Agent Writing" in packet["writing_context"]
    assert all("BUY" not in row["content"] or "不下最终单" in row["content"] for row in rows)


def test_refresh_skills_requires_settled_sample(temp_db):
    _seed_settled(temp_db, "BTC", "BUY", "continuation", ["WIN"] * 3 + ["LOSS"] * 2)
    assert hermes_agent.refresh_skills(temp_db) == []
    _seed_settled(temp_db, "BTC", "BUY", "continuation", ["WIN"] * 4 + ["LOSS"] * 1)
    skills = hermes_agent.refresh_skills(temp_db)
    assert len(skills) == 1
    assert skills[0]["asset"] == "BTC"
    assert skills[0]["sample_size"] == 10
    assert skills[0]["wins"] == 7
    loaded = hermes_agent.load_skills(temp_db)
    assert loaded[0]["skill_key"] == "BTC|BUY|continuation"


def test_build_prompt_context_includes_settled_skills(temp_db):
    _seed_settled(temp_db, "XAU", "SELL", "reversal", ["WIN"] * 6 + ["LOSS"] * 2)
    hermes_agent.refresh_skills(temp_db)
    text = hermes_agent.build_prompt_context("地缘冲突升级", connection=temp_db)
    assert "Hermes Settled Skills" in text
    assert "XAU reversal SELL" in text


@pytest.mark.parametrize(
    "invalidate_sql",
    [
        "UPDATE raw_news SET quality_status='unverified'",
        "UPDATE ai_decisions SET evidence_action='HOLD'",
        "UPDATE ai_decisions SET trade_gate_reason='research_only'",
        "UPDATE ai_decisions SET paper_trading_run_id=NULL",
    ],
)
def test_refresh_skills_fail_closed_and_removes_stale_skill(temp_db, invalidate_sql):
    _seed_settled(temp_db, "BTC", "BUY", "continuation", ["WIN"] * 8)
    assert len(hermes_agent.refresh_skills(temp_db)) == 1
    assert len(hermes_agent.load_skills(temp_db)) == 1

    temp_db.execute(invalidate_sql)
    temp_db.commit()

    assert hermes_agent.refresh_skills(temp_db) == []
    assert hermes_agent.load_skills(temp_db) == []
