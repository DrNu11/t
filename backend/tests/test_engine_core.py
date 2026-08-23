"""Smoke tests for engine core logic — VIP detect, dedup hash, junk filter,
and parent/child signal aggregation helpers."""

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import decision_guard
import engine.ai_worker as ai_worker_module
from engine.utils import _content_hash, _detect_vip, _is_content_junk
from engine.webhook import _is_english_text
from engine.ai_worker import (
    _analysis_features_for_persist, _authoritative_news_timestamp,
    _build_performance_context, _claim_pending_batch,
    _bump_parent_score, _call_llm_sync, _find_active_parent,
    _mark_news_terminal_status, _recover_processing_on_worker_start,
    build_model_config,
    build_news_context_package, direction_consistent,
    evaluate_news_context_eligibility, quality_allows_paper_position,
    evaluate_target_market_context_eligibility, reconcile_score_action,
    select_current_learning_context, _settled_sample_for_context,
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


# ── 历史新闻不得读取当前盘面/宏观快照 ──────────────────────

def _context_snapshot_epoch() -> float:
    return datetime(
        2026, 8, 22, 14, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
    ).timestamp()


def test_news_context_timestamp_contract_accepts_full_iso_and_epoch():
    snapshot_epoch = _context_snapshot_epoch()
    news_epoch = snapshot_epoch - 120
    accepted = (
        "2026-08-22T06:03:00Z",
        "2026-08-22 14:03:00",
        int(news_epoch),
        int(news_epoch * 1000),
    )

    for timestamp in accepted:
        result = evaluate_news_context_eligibility(
            timestamp, int(snapshot_epoch * 1000),
        )
        assert result["market_context_eligible"] is True
        assert result["timestamp_mismatch"] is False
        assert result["market_context_reason"] == "within_realtime_window"
        assert result["context_age_seconds"] == 120.0


def test_news_context_timestamp_contract_is_strict_and_fail_closed():
    snapshot_epoch = _context_snapshot_epoch()
    cases = {
        "2026-08-22T13:59:59+08:00": "news_timestamp_outside_realtime_window",
        "2026-08-22T14:05:01+08:00": "news_timestamp_after_snapshot",
        "2026-08-22": "news_timestamp_unparseable",
        "14:03": "news_timestamp_unparseable",
        "14:03:00": "news_timestamp_unparseable",
        "14:03:00.123": "news_timestamp_unparseable",
        "approximately now": "news_timestamp_unparseable",
        "": "news_timestamp_missing",
    }

    for timestamp, reason in cases.items():
        result = evaluate_news_context_eligibility(timestamp, snapshot_epoch)
        assert result["market_context_eligible"] is False
        assert result["timestamp_mismatch"] is True
        assert result["market_context_reason"] == reason

    invalid_snapshot = evaluate_news_context_eligibility(
        "2026-08-22T14:03:00+08:00", "not-an-epoch",
    )
    assert invalid_snapshot["market_context_reason"] == "snapshot_epoch_invalid"


def test_historical_news_context_omits_all_current_market_and_macro_payloads():
    snapshot_epoch = _context_snapshot_epoch()
    snapshot = {
        "epoch_ms": int(snapshot_epoch * 1000),
        "status": "ok",
        "summary": "CURRENT MARKET SUMMARY",
        "assets": {"BTC": {
            "status": "ok",
            "decision_eligible": True,
            "market_structure": {"trend": "bull"},
        }},
        "macro": {"dxy": {"value": 99.0}},
    }
    macro = {
        "status": "ok", "ok": 1, "total": 1, "ts": snapshot_epoch,
        "summary": "CURRENT MACRO SUMMARY",
        "layers": {"fed": {"rate": 3.5}},
    }

    historical = build_news_context_package(
        "2025-08-22T14:04:00+08:00", snapshot, macro,
    )
    context = json.loads(historical["decision_context"])

    assert historical["market_context"] == ""
    assert context["market_context_eligible"] is False
    assert context["timestamp_mismatch"] is True
    assert context["market_context_reason"] == "news_timestamp_outside_realtime_window"
    assert "assets" not in context
    assert "summary" not in context
    assert "macro" not in context
    assert "macro_context" not in context


def test_live_news_context_retains_audited_market_and_macro_payloads():
    snapshot_epoch = _context_snapshot_epoch()
    snapshot = {
        "epoch_ms": int(snapshot_epoch * 1000),
        "status": "ok",
        "summary": "CURRENT MARKET SUMMARY",
        "assets": {"BTC": {
            "status": "ok",
            "decision_eligible": True,
            "market_structure": {"trend": "bull"},
        }},
    }
    macro = {
        "status": "ok", "ok": 1, "total": 1, "ts": snapshot_epoch,
        "summary": "CURRENT MACRO SUMMARY",
        "layers": {"fed": {"rate": 3.5}},
    }

    live = build_news_context_package(
        "2026-08-22T14:03:00+08:00", snapshot, macro,
    )
    context = json.loads(live["decision_context"])

    assert live["market_context"] == (
        "CURRENT MARKET SUMMARY\n\nCURRENT MACRO SUMMARY"
    )
    assert context["market_context_eligible"] is True
    assert context["timestamp_mismatch"] is False
    assert context["assets"]["BTC"]["market_structure"]["trend"] == "bull"
    assert context["macro_context"]["layers"]["fed"]["rate"] == 3.5


def test_live_timestamp_cannot_promote_an_unavailable_snapshot():
    snapshot_epoch = _context_snapshot_epoch()
    package = build_news_context_package(
        "2026-08-22T14:04:00+08:00",
        {"epoch_ms": int(snapshot_epoch * 1000), "status": "down", "assets": {}},
    )
    context = json.loads(package["decision_context"])

    assert package["market_context"] == ""
    assert context["market_context_eligible"] is False
    assert context["timestamp_mismatch"] is False
    assert context["market_context_reason"] == "snapshot_not_decision_ready"


def test_target_market_context_requires_verified_same_asset():
    snapshot = {
        "assets": {
            "BTC": {"status": "ok", "decision_eligible": True},
            "XAU": {"status": "unavailable", "decision_eligible": False},
        }
    }
    context_gate = {"market_context_eligible": True, "market_context_reason": "within_realtime_window"}

    btc = evaluate_target_market_context_eligibility("BTCUSDT", snapshot, context_gate)
    xau = evaluate_target_market_context_eligibility("GOLD", snapshot, context_gate)
    eth = evaluate_target_market_context_eligibility("ETH", snapshot, context_gate)

    assert btc["target_market_context_eligible"] is True
    assert btc["target_asset_context_key"] == "BTC"
    assert xau["target_market_context_eligible"] is False
    assert xau["target_asset_context_key"] == "XAU"
    assert eth["target_market_context_eligible"] is False


def test_raw_news_epoch_is_authoritative_over_legacy_clock_text():
    assert _authoritative_news_timestamp({
        "ts": 1787378580,
        "timestamp": "14:03:00",
    }) == 1787378580
    assert _authoritative_news_timestamp({
        "ts": None,
        "timestamp": "2026-08-22T14:03:00+08:00",
    }) == "2026-08-22T14:03:00+08:00"


def test_historical_event_cannot_receive_current_learning_context():
    selected = select_current_learning_context(
        {
            "market_context_eligible": False,
            "timestamp_mismatch": True,
        },
        performance_context="CURRENT PERFORMANCE",
        hermes_skills=[{"asset": "BTC", "win_rate": 1.0}],
    )

    assert selected == {
        "eligible": False,
        "performance_context": "",
        "hermes_skills": [],
    }
    live_selected = select_current_learning_context(
        {
            "market_context_eligible": True,
            "timestamp_mismatch": False,
        },
        performance_context="CURRENT PERFORMANCE",
        hermes_skills=[{"asset": "BTC", "win_rate": 1.0}],
    )
    assert live_selected["performance_context"] == "CURRENT PERFORMANCE"
    assert live_selected["hermes_skills"] == [
        {"asset": "BTC", "win_rate": 1.0},
    ]


def test_historical_event_does_not_read_current_settled_sample(monkeypatch, temp_db):
    def unexpected_read(*args, **kwargs):
        raise AssertionError("settled evidence must not be read")

    monkeypatch.setattr(
        ai_worker_module.evidence, "collect_settled_sample", unexpected_read,
    )
    assert _settled_sample_for_context(
        "BTC",
        target_market_context_eligible=False,
        connection=temp_db,
    ) == {}


def test_processing_lease_recovers_orphans_but_not_active_claims(
    temp_db, monkeypatch,
):
    now_epoch = 1_787_378_700
    rows = (
        ("stale", "PROCESSING", now_epoch - 901),
        ("orphan", "PROCESSING", None),
        ("active", "PROCESSING", now_epoch - 60),
        ("pending", "PENDING", None),
    )
    ids = {}
    for content, status, lease_started_at in rows:
        cursor = temp_db.execute(
            """INSERT INTO raw_news
               (source, content, timestamp, ts, status, is_noise,
                quality_status, processing_started_at)
               VALUES ('test', ?, '2026-08-22T14:03:00+08:00', ?, ?, 0,
                       'verified', ?)""",
            (content, now_epoch - 120, status, lease_started_at),
        )
        ids[content] = cursor.lastrowid
    temp_db.commit()
    db_path = temp_db.execute("PRAGMA database_list").fetchone()[2]

    def open_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(ai_worker_module, "_open_db", open_test_db)
    monkeypatch.setattr(ai_worker_module.time, "time", lambda: now_epoch)

    claimed = _claim_pending_batch()
    claimed_ids = {row["id"] for row in claimed}
    assert claimed_ids == {ids["stale"], ids["orphan"], ids["pending"]}

    states = {
        row["content"]: (row["status"], row["processing_started_at"])
        for row in temp_db.execute(
            "SELECT content, status, processing_started_at FROM raw_news"
        )
    }
    assert states["active"] == ("PROCESSING", now_epoch - 60)
    assert states["stale"] == ("PROCESSING", now_epoch)
    assert states["orphan"] == ("PROCESSING", now_epoch)
    assert states["pending"] == ("PROCESSING", now_epoch)


def test_processing_lease_recovers_stale_claim_on_worker_restart(
    temp_db, monkeypatch,
):
    now_epoch = 1_787_378_700
    cursor = temp_db.execute(
        """INSERT INTO raw_news
           (source, content, timestamp, ts, status, is_noise,
            quality_status, processing_started_at)
           VALUES ('test', 'fresh-crash', '2026-08-22T14:03:00+08:00', ?,
                   'PROCESSING', 0, 'verified', ?)""",
        (now_epoch - 901, now_epoch - 901),
    )
    news_id = cursor.lastrowid
    temp_db.commit()
    db_path = temp_db.execute("PRAGMA database_list").fetchone()[2]

    def open_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(ai_worker_module, "_open_db", open_test_db)
    monkeypatch.setattr(ai_worker_module.time, "time", lambda: now_epoch)

    assert _recover_processing_on_worker_start() is True
    claimed = _claim_pending_batch()

    assert [row["id"] for row in claimed] == [news_id]
    state = temp_db.execute(
        "SELECT status, processing_started_at FROM raw_news WHERE id = ?",
        (news_id,),
    ).fetchone()
    assert tuple(state) == ("PROCESSING", now_epoch)


def test_terminal_status_clears_processing_lease(temp_db):
    ids = []
    for content in ("done", "failed"):
        cursor = temp_db.execute(
            """INSERT INTO raw_news
               (source, content, timestamp, status, quality_status,
                processing_started_at)
               VALUES ('test', ?, '2026-08-22T14:03:00+08:00',
                       'PROCESSING', 'verified', 1787378700)""",
            (content,),
        )
        ids.append(cursor.lastrowid)

    _mark_news_terminal_status(temp_db, ids[0], "DONE")
    _mark_news_terminal_status(temp_db, ids[1], "FAILED")
    rows = temp_db.execute(
        "SELECT status, processing_started_at FROM raw_news ORDER BY id"
    ).fetchall()
    assert [(row["status"], row["processing_started_at"]) for row in rows] == [
        ("DONE", None),
        ("FAILED", None),
    ]


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
