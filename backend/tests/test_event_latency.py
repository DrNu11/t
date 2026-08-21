"""Low-latency event change detection tests."""

import asyncio

import api_server


def test_incremental_change_detection(temp_db, monkeypatch):
    db_path = temp_db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(api_server, "DB_PATH", db_path)

    raw_cursor, decision_cursor = asyncio.run(api_server._fetch_change_cursors())
    news_id = temp_db.execute(
        "INSERT INTO raw_news(source, content, timestamp, status, is_noise) VALUES(?, ?, ?, ?, 0)",
        ("test", "特朗普宣布新的关税政策", "2026-08-15 10:00:00", "PENDING"),
    ).lastrowid
    temp_db.commit()

    changed = asyncio.run(
        api_server._fetch_incremental_news_ids(raw_cursor, decision_cursor)
    )
    assert changed == [news_id]

    raw_cursor, decision_cursor = asyncio.run(api_server._fetch_change_cursors())
    temp_db.execute(
        """
        INSERT INTO ai_decisions(news_id, sentiment_score, suggested_action, reasoning, vip_tag)
        VALUES(?, 0.5, 'BUY', 'test', '[VIP:TRUMP]')
        """,
        (news_id,),
    )
    temp_db.commit()

    changed = asyncio.run(
        api_server._fetch_incremental_news_ids(raw_cursor, decision_cursor)
    )
    assert changed == [news_id]


def test_sse_payload_has_millisecond_emission_time(monkeypatch):
    queue = asyncio.Queue()
    monkeypatch.setattr(api_server, "_SSE_QUEUES", [queue])
    monkeypatch.setattr(api_server.time, "time", lambda: 1_786_772_999.123)

    asyncio.run(api_server._broadcast_sse({"id": 1}))
    payload = queue.get_nowait()
    assert '"server_emitted_at_ms": 1786772999123' in payload
