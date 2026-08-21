"""Smoke tests for engine core logic — VIP detect, dedup hash, junk filter,
and parent/child signal aggregation helpers."""

import json

from engine.utils import _content_hash, _detect_vip, _is_content_junk
from engine.webhook import _is_english_text
from engine.ai_worker import (
    _bump_parent_score, _call_llm_sync, _find_active_parent, build_model_config,
    direction_consistent, reconcile_score_action,
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
