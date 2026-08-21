import asyncio
import json

import api_server


def test_paper_metrics_buy_and_sell():
    params = {**api_server.strategy_store.default_params(), "notional_usdt": 100, "leverage": 2}
    buy = api_server._paper_metrics({
        "entry_price": 100, "action": "BUY", "max_price": 105,
        "min_price": 98, "settled": 0, "strategy_params": params,
    }, 103)
    assert buy["current_pnl_pct"] == 3
    assert buy["current_pnl_usdt"] == 6
    assert buy["live_mfe_pct"] == 5
    assert buy["live_mae_pct"] == 2

    sell = api_server._paper_metrics({
        "entry_price": 100, "action": "SELL", "max_price": 104,
        "min_price": 95, "settled": 0, "strategy_params": params,
    }, 97)
    assert sell["current_pnl_pct"] == 3
    assert sell["current_pnl_usdt"] == 6
    assert sell["live_mfe_pct"] == 5
    assert sell["live_mae_pct"] == 4

    unavailable = api_server._paper_metrics({
        "entry_price": 100, "action": "BUY", "max_price": 100,
        "min_price": 100, "settled": 0, "strategy_params": params,
    }, None)
    assert unavailable["current_pnl_pct"] is None
    assert unavailable["current_pnl_usdt"] is None
    assert unavailable["pricing_status"] == "UNAVAILABLE"


def test_event_rows_include_unanalyzed_and_latest_decision(temp_db, monkeypatch):
    news_rows = [
        ("source-a", "pending news", "2026-08-05 10:00:00", "PENDING", 0),
        ("source-b", "failed news", "2026-08-05 10:01:00", "FAILED", 0),
        ("source-c", "analyzed news", "2026-08-05 10:02:00", "DONE", 0),
        ("source-d", "noise news", "2026-08-05 10:03:00", "PENDING", 1),
    ]
    temp_db.executemany(
        "INSERT INTO raw_news (source, content, timestamp, status, is_noise) VALUES (?, ?, ?, ?, ?)",
        news_rows,
    )
    temp_db.execute(
        """
        INSERT INTO ai_decisions
            (news_id, sentiment_score, suggested_action, reasoning, created_at, market_category, target_asset)
        VALUES (3, 0.25, 'HOLD', 'older decision', '2026-08-05 10:03:00', 'MACRO', 'NONE')
        """
    )
    latest_cursor = temp_db.execute(
        """
        INSERT INTO ai_decisions
            (news_id, sentiment_score, suggested_action, reasoning, created_at, market_category, target_asset)
        VALUES (3, 0.85, 'BUY', 'latest decision', '2026-08-05 10:04:00', 'GOLD', 'XAU')
        """
    )
    latest_decision_id = latest_cursor.lastrowid
    temp_db.commit()

    db_path = temp_db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(api_server, "DB_PATH", db_path)
    events = asyncio.run(api_server._fetch_event_rows(limit=50))

    assert [event["news_id"] for event in events] == [3, 2, 1]
    assert len({event["news_id"] for event in events}) == 3

    analyzed = events[0]
    assert analyzed["id"] == 3
    assert analyzed["decision_id"] == latest_decision_id
    assert analyzed["analysis_status"] == "DONE"
    assert analyzed["action"] == "BUY"
    assert analyzed["score"] == 0.85
    assert analyzed["reason"] == "latest decision"

    failed = events[1]
    assert failed["decision_id"] is None
    assert failed["analysis_status"] == "FAILED"
    assert failed["action"] == "HOLD"
    assert failed["score"] == 0.0
    assert failed["reason"] == "AI 分析失败"
    assert failed["market_category"] == "OTHER"
    assert failed["target_asset"] == "NONE"

    pending = events[2]
    assert pending["decision_id"] is None
    assert pending["analysis_status"] == "PENDING"
    assert pending["reason"] == "待 AI 分析"


def test_today_events_counts_shanghai_day(temp_db, monkeypatch):
    today = api_server.datetime.now(api_server.TZ_SHANGHAI).strftime("%Y-%m-%d")
    temp_db.executemany(
        "INSERT INTO raw_news (source, content, timestamp, status, is_noise) VALUES (?, ?, ?, ?, ?)",
        [
            ("s", "a", f"{today} 10:00:00", "DONE", 0),
            ("s", "b", f"{today} 11:00:00", "PENDING", 0),
            ("s", "c", f"{today} 12:00:00", "DONE", 1),
            ("s", "d", "2020-01-01 10:00:00", "DONE", 0),
        ],
    )
    temp_db.commit()
    monkeypatch.setattr(api_server, "DB_PATH", temp_db.execute("PRAGMA database_list").fetchone()[2])
    result = asyncio.run(api_server.get_today_events())
    assert result["today"] == today
    assert result["news_count"] == 2
    assert result["analyzed_count"] == 1
    assert result["total_count"] == 3


def test_ingest_external_news_survives_missing_ts(tmp_path, monkeypatch):
    import sqlite3
    import config

    db_file = tmp_path / "legacy_raw.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(api_server, "DB_PATH", str(db_file))
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL, content TEXT NOT NULL,"
        " timestamp TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'PENDING',"
        " is_noise INTEGER NOT NULL DEFAULT 0,"
        " relevance_score REAL NOT NULL DEFAULT 0.0)"
    )
    conn.execute(
        "CREATE TABLE paper_trading_settings ("
        "id INTEGER PRIMARY KEY, is_running INTEGER NOT NULL DEFAULT 0,"
        " tracks TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO paper_trading_settings (id) VALUES (1)")
    conn.commit()
    conn.close()
    inserted = api_server._ingest_external_news_sync([
        {"id": "x1", "title": "美联储暗示降息", "summary": "宏观快讯", "source": "TechFlow 深潮"},
    ])
    assert inserted == 1
    check = sqlite3.connect(str(db_file))
    assert check.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0] == 1
    check.close()


def test_ingest_external_news_preserves_provider_timestamp(temp_db, monkeypatch):
    monkeypatch.setattr(api_server, "evaluate_news", lambda *args: {"is_noise": 0, "relevance_score": 0.9})
    assert api_server._ingest_external_news_sync([{
        "id": "jin-1",
        "title": "金十快讯",
        "summary": "黄金波动",
        "published_at": "2026-08-21 10:18:00",
        "source": "金十",
    }]) == 1
    timestamp = temp_db.execute("SELECT timestamp FROM raw_news").fetchone()[0]
    assert timestamp.startswith("2026-08-21T10:18:00+08:00")


def test_get_events_does_not_cap_at_50(temp_db, monkeypatch):
    rows = [
        (f"src-{i}", f"news {i}", f"2026-08-16 10:{i:02d}:00", "PENDING", 0)
        for i in range(60)
    ]
    temp_db.executemany(
        "INSERT INTO raw_news (source, content, timestamp, status, is_noise) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    temp_db.commit()
    monkeypatch.setattr(api_server, "DB_PATH", temp_db.execute("PRAGMA database_list").fetchone()[2])
    events = asyncio.run(api_server.get_events(limit=2000))
    assert len(events) == 60


def test_unknown_analysis_status_falls_back_to_pending():
    assert api_server._normalize_analysis_status("UNKNOWN", False) == "PENDING"
    assert api_server._normalize_analysis_status("DONE", False) == "PENDING"
    assert api_server._normalize_analysis_status("FAILED", False) == "FAILED"
    assert api_server._normalize_analysis_status("PROCESSING", True) == "DONE"


def test_ai_models_get_and_put(tmp_path, monkeypatch):
    state_path = tmp_path / "runtime" / "ai_model.json"
    monkeypatch.setattr(api_server.config, "AI_MODEL_STATE_PATH", str(state_path))

    initial = asyncio.run(api_server.get_ai_models())
    assert initial["selected"] == api_server.config.DEFAULT_AI_MODEL_ID
    assert initial["models"] == [
        {"id": "DeepSeek-V4-Flash-0731", "label": "DeepSeek V4 Flash 0731 (Aiping)"},
    ]

    updated = asyncio.run(api_server.select_ai_model(
        api_server.AIModelSelection(model_id="DeepSeek-V4-Flash-0731")
    ))
    assert updated["selected"] == "DeepSeek-V4-Flash-0731"
    assert asyncio.run(api_server.get_ai_models())["selected"] == "DeepSeek-V4-Flash-0731"


def test_data_copilot_uses_aiping_client_model_and_extra_body(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps({"reply": "完成", "sql": "SELECT 1"}) if len(calls) == 1 else "摘要"
            message = type("Message", (), {"content": content})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    fake_client = type("Client", (), {
        "chat": type("Chat", (), {"completions": FakeCompletions()})(),
    })()
    monkeypatch.setattr(api_server, "_agent_llm_client", lambda: fake_client)

    reply, sql = api_server._llm_text_to_sql_sync("查询", "ALL")
    summary = api_server._summarize_results_sync("查询", [{"id": 1}])

    assert (reply, sql, summary) == ("完成", "SELECT 1", "摘要")
    assert len(calls) == 2
    for call in calls:
        assert call["model"] == "DeepSeek-V4-Flash-0731"
        assert call["extra_body"] == api_server.config.AIPING_EXTRA_BODY


def test_ai_models_put_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(api_server.config, "AI_MODEL_STATE_PATH", str(tmp_path / "ai_model.json"))
    try:
        asyncio.run(api_server.select_ai_model(
            api_server.AIModelSelection(model_id="unknown/model")
        ))
    except api_server.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("unknown model must return HTTP 400")
