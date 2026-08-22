"""Smoke tests for engine core logic — VIP detect, dedup hash, junk filter,
and parent/child signal aggregation helpers."""

import json
import sqlite3

import decision_guard
import engine.ai_worker as ai_worker_module
from engine.utils import _content_hash, _detect_vip, _is_content_junk
from engine.webhook import _is_english_text
from engine.ai_worker import (
    _analysis_features_for_persist, _build_performance_context,
    _bump_parent_score, _call_llm_sync, _find_active_parent, build_model_config,
    direction_consistent, quality_allows_paper_position, reconcile_score_action,
)


# ── _detect_vip ──────────────────────────────────────────────────────────

def test_detect_vip_trump():
    assert _detect_vip("Trump says something about tariffs") == ("[VIP:TRUMP]", "Trump")


def test_detect_vip_powell_chinese():
    assert _detect_vip("鲍威尔发表最新讲话") == ("[VIP:FED]", "鲍威尔")


def test_detect_vip_no_match():
    assert _detect_vip("普通财经文本没有任何关键人物") == ("", "")


# ── _content_hash ────────────────────────────────────────────────────────

def test_content_hash_stable():
    h1 = _content_hash("title", "http://x", "body text")
    h2 = _content_hash("title", "http://x", "body text")
    assert h1 == h2
    assert len(h1) == 16


def test_content_hash_differs():
    assert _content_hash("a", "http://x", "b") != _content_hash("c", "http://x", "b")


# ── _is_content_junk ─────────────────────────────────────────────────────

def test_is_content_junk_positive():
    assert _is_content_junk("无具体内容") is True          # _CONTENT_BLACKLIST 真实条目
    assert _is_content_junk("暂无内容，稍后再看") is True     # _CONTENT_BLACKLIST 真实条目
    assert _is_content_junk("short") is True                 # 过短


def test_is_content_junk_negative():
    text = "Federal Reserve cuts interest rates by 25bps amid inflation concerns"
    assert _is_content_junk(text) is False


# ── 父子聚合 (_find_active_parent / _bump_parent_score) ─────────────────

def _insert_parent(conn, score=0.5, action="BUY", category="GOLD", asset="XAU"):
    cur = conn.execute("INSERT INTO raw_news (source, content) VALUES ('test', 'x')")
    news_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO ai_decisions"
        " (news_id, sentiment_score, suggested_action, reasoning,"
        "  market_category, target_asset)"
        " VALUES (?, ?, ?, 'parent', ?, ?)",
        (news_id, score, action, category, asset),
    )
    conn.commit()
    return cur.lastrowid


def test_find_active_parent_hit(temp_db):
    parent_id = _insert_parent(temp_db)
    assert _find_active_parent(temp_db, "GOLD", "XAU", "BUY") == parent_id


def test_find_active_parent_miss(temp_db):
    _insert_parent(temp_db)
    assert _find_active_parent(temp_db, "GOLD", "XAU", "SELL") is None      # 方向不同
    assert _find_active_parent(temp_db, "CRYPTO", "BTC", "BUY") is None     # 资产不同
    assert _find_active_parent(temp_db, "GOLD", "XAU", "HOLD") is None      # HOLD 不参与聚合


def test_bump_parent_score_more_extreme(temp_db):
    parent_id = _insert_parent(temp_db, score=0.5)
    assert _bump_parent_score(temp_db, parent_id, 0.8, "child") is True
    row = temp_db.execute(
        "SELECT sentiment_score FROM ai_decisions WHERE id = ?", (parent_id,)
    ).fetchone()
    assert abs(row[0] - 0.8) < 1e-9


def test_bump_parent_score_less_extreme_noop(temp_db):
    parent_id = _insert_parent(temp_db, score=0.5)
    assert _bump_parent_score(temp_db, parent_id, 0.3, "child") is False
    row = temp_db.execute(
        "SELECT sentiment_score FROM ai_decisions WHERE id = ?", (parent_id,)
    ).fetchone()
    assert abs(row[0] - 0.5) < 1e-9


def test_build_model_config_uses_aiping_endpoint_and_options():
    model = build_model_config("DeepSeek-V4-Flash-0731")
    assert model["id"] == "DeepSeek-V4-Flash-0731"
    assert model["label"] == "DeepSeek V4 Flash 0731 (Aiping)"
    assert model["api_base"] == "https://www.aiping.cn/api/v1"
    assert model["json_mode"] is True
    assert model["extra_body"]["enable_thinking"] is False
    assert model["extra_body"]["provider"] == {
        "only": [], "order": [], "input_price_range": [],
        "output_price_range": [], "input_length_range": [],
        "output_length_range": [], "throughput_range": [],
        "latency_range": [], "sort": None,
    }


def test_news_llm_request_uses_aiping_endpoint_model_and_extra_body(monkeypatch):
    captured = {}

    class FakeResponse:
        def read(self):
            result = {
                "sentiment_score": 0.2, "suggested_action": "HOLD",
                "reasoning": "离线测试", "market_category": "OTHER",
                "target_asset": "NONE", "reasoning_path": "测试路径",
            }
            return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse()

    monkeypatch.setattr("engine.ai_worker.urllib.request.build_opener", lambda *args: FakeOpener())
    model = build_model_config("DeepSeek-V4-Flash-0731")
    _call_llm_sync("offline news content", model)

    assert captured["url"] == "https://www.aiping.cn/api/v1/chat/completions"
    assert captured["payload"]["model"] == "DeepSeek-V4-Flash-0731"
    assert captured["payload"]["enable_thinking"] is False
    assert captured["payload"]["provider"] == model["extra_body"]["provider"]


def test_news_llm_request_injects_hermes_writing_context(monkeypatch):
    captured = {}

    class FakeResponse:
        def read(self):
            result = {
                "sentiment_score": 0.2, "suggested_action": "HOLD",
                "reasoning": "离线测试", "market_category": "OTHER",
                "target_asset": "NONE", "reasoning_path": "测试路径",
            }
            return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()

    class FakeOpener:
        def open(self, request, timeout):
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse()

    monkeypatch.setattr("engine.ai_worker.urllib.request.build_opener", lambda *args: FakeOpener())
    model = build_model_config("DeepSeek-V4-Flash-0731")
    _call_llm_sync(
        "offline news content",
        model,
        writing_context="[Hermes Multi-Agent Writing]\n交易席: 不下最终单。",
    )
    user = captured["payload"]["messages"][1]["content"]
    assert "[Hermes Multi-Agent Writing]" in user
    assert "offline news content" in user


def test_news_llm_request_injects_active_strategy_context(monkeypatch):
    captured = {}

    class FakeResponse:
        def read(self):
            result = {
                "sentiment_score": 0.2, "suggested_action": "HOLD",
                "reasoning": "离线测试", "market_category": "OTHER",
                "target_asset": "NONE", "reasoning_path": "测试路径",
            }
            return json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()

    class FakeOpener:
        def open(self, request, timeout):
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse()

    monkeypatch.setattr("engine.ai_worker.urllib.request.build_opener", lambda *args: FakeOpener())
    model = build_model_config("DeepSeek-V4-Flash-0731")
    _call_llm_sync(
        "offline news content", model,
        strategy_context="[活动策略版本配置]\n先分析盘面结构",
    )
    user = captured["payload"]["messages"][1]["content"]
    assert "[活动策略版本配置]" in user
    assert "先分析盘面结构" in user


def test_reconcile_hold_weak_score_is_neutral():
    assert reconcile_score_action(0.05, "HOLD") == (0.0, "HOLD")
    assert reconcile_score_action(0.0, "HOLD") == (0.0, "HOLD")
    assert direction_consistent("HOLD", 0.05) is True
    assert direction_consistent("HOLD", 0.0) is True


def test_reconcile_hold_strong_score_becomes_directional():
    assert reconcile_score_action(0.45, "HOLD") == (0.45, "BUY")
    assert reconcile_score_action(-0.45, "HOLD") == (-0.45, "SELL")


def test_reconcile_flips_opposite_score_to_action():
    assert reconcile_score_action(0.45, "SELL") == (-0.45, "SELL")
    assert reconcile_score_action(-0.45, "BUY") == (0.45, "BUY")
    assert reconcile_score_action(0.0, "SELL") == (-0.05, "SELL")
    assert reconcile_score_action(0.0, "BUY") == (0.05, "BUY")
    assert direction_consistent("SELL", -0.45) is True
    assert direction_consistent("BUY", 0.05) is True
    assert direction_consistent("SELL", 0.45) is False


def test_reconcile_rejects_non_finite_scores():
    assert reconcile_score_action(float("nan"), "HOLD") == (0.0, "HOLD")
    assert reconcile_score_action(float("inf"), "BUY") == (0.05, "BUY")


def test_only_verified_news_can_open_paper_position():
    assert quality_allows_paper_position("verified") is True
    assert quality_allows_paper_position(" VERIFIED ") is True
    assert quality_allows_paper_position("legacy") is False
    assert quality_allows_paper_position("candidate") is False
    assert quality_allows_paper_position("unverified") is False
    assert quality_allows_paper_position(None) is False


def test_news_llm_legacy_output_gets_dynamic_analysis(monkeypatch):
    """Old model contracts must not persist as all-default dynamic fields."""

    class FakeResponse:
        def read(self):
            result = {
                "sentiment_score": -0.62,
                "suggested_action": "SELL",
                "reasoning": "突发政策冲击，短期风险偏空",
                "market_category": "GOLD",
                "target_asset": "XAU",
                "reasoning_path": "政策冲击 -> 风险重定价",
                "event_strength": "high",
                "direct_catalyst": "true",
                "prediction_type": "reversal",
                "event_phase": "early",
                "expected_horizon": "intraday",
                "timeframe_match": "intraday",
            }
            body = {"choices": [{"message": {"content": json.dumps(result)}}]}
            return json.dumps(body).encode()

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    monkeypatch.setattr(
        "engine.ai_worker.urllib.request.build_opener",
        lambda *args: FakeOpener(),
    )
    model = build_model_config("DeepSeek-V4-Flash-0731")
    result = _call_llm_sync("offline breaking policy news", model)

    assert result["prediction_type"] == "reversal"
    assert result["event_phase"] == "early"
    assert result["analysis_type"] == "conflict"
    assert result["impact_horizon"] == "short"
    assert result["bearish_probability"] > result["bullish_probability"]
    assert result["bearish_force"] > result["bullish_force"] > 0.0
    assert abs(
        result["bullish_probability"] + result["bearish_probability"] - 1.0
    ) < 1e-6


def test_vip_score_is_normalized_once_with_provenance_preserved(monkeypatch):
    raw_result = {
        "sentiment_score": 0.4,
        "suggested_action": "BUY",
        "reasoning": "关税突发上调，风险偏好受刺激",
        "market_category": "CRYPTO",
        "target_asset": "BTC",
        "event_strength": "medium",
        "prediction_type": "continuation",
        "event_phase": "mid",
        "direct_catalyst": False,
    }

    class FakeResponse:
        def read(self):
            body = {"choices": [{"message": {"content": json.dumps(raw_result)}}]}
            return json.dumps(body).encode()

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    calls = []
    original = decision_guard.normalize_ai_analysis

    def tracked_normalize(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "engine.ai_worker.urllib.request.build_opener",
        lambda *args: FakeOpener(),
    )
    monkeypatch.setattr(
        ai_worker_module.decision_guard, "normalize_ai_analysis", tracked_normalize,
    )

    result = _call_llm_sync(
        "Trump announces a major tariff change for crypto markets",
        build_model_config("DeepSeek-V4-Flash-0731"),
    )
    persisted = _analysis_features_for_persist(
        result, result["sentiment_score"], result["suggested_action"],
    )

    assert result["sentiment_score"] == 0.5
    assert len(calls) == 1
    assert persisted["analysis_basis"]["mode"] == "deterministic_fallback"
    assert persisted["analysis_basis"]["score"] == 0.5
    expected = original(raw_result, score=0.5, action="BUY")
    assert persisted["bullish_probability"] == expected["bullish_probability"]
    assert persisted["bullish_force"] == expected["bullish_force"]


def test_historical_performance_prompt_uses_only_formal_decisions(
    temp_db, monkeypatch,
):
    def seed(verdict, *, quality="verified", gate_reason=None, run_id=1):
        news_id = temp_db.execute(
            """INSERT INTO raw_news
                   (source, content, timestamp, status, quality_status)
               VALUES ('test', 'history', '2026-08-22 10:00:00', 'DONE', ?)""",
            (quality,),
        ).lastrowid
        temp_db.execute(
            """INSERT INTO ai_decisions
                   (news_id, sentiment_score, suggested_action, reasoning,
                    target_asset, prediction_type, settled, is_correct,
                    forward_pnl, evidence_action, trade_gate_reason,
                    paper_trading_run_id)
               VALUES (?, 0.6, 'BUY', 'history', 'BTC', 'continuation',
                       1, ?, ?, 'BUY', ?, ?)""",
            (
                news_id,
                verdict,
                -1.0 if verdict == "LOSS" else 1.0,
                gate_reason or "证据充分，允许输出方向性结论",
                run_id,
            ),
        )

    for _ in range(8):
        seed("LOSS")
    for _ in range(8):
        seed("WIN", quality="unverified")
        seed("WIN", gate_reason="旧宽松闸门通过")
    temp_db.commit()

    db_path = temp_db.execute("PRAGMA database_list").fetchone()[2]

    def open_test_db():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(ai_worker_module, "_open_db", open_test_db)
    context = _build_performance_context()

    assert "BTC continuation BUY: sample: 8 win rate: 0%" in context
    assert "BTC continuation BUY: sample: 24" not in context
