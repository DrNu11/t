"""Smoke tests for src_python/db.py — schema migration + SQL safety gate."""

import sqlite3

import pytest

import db


def test_migrate_idempotent_in_memory():
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    db.migrate(conn)  # second run must not raise
    conn.close()


def test_tables_exist(temp_db):
    tables = {
        r[0] for r in temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "raw_news" in tables
    assert "ai_decisions" in tables
    assert {"market_ticks", "factor_snapshots", "signal_performance",
            "strategies", "strategy_versions", "backtest_runs",
            "paper_trading_settings", "paper_trading_runs", "macro_events",
            "data_quality_audit", "data_quarantine"} <= tables


def test_timeseries_columns_and_indexes(temp_db):
    ticks = {r[1] for r in temp_db.execute("PRAGMA table_info(market_ticks)")}
    assert {"symbol", "ts", "price", "change24h", "source", "source_count"} <= ticks
    factors = {r[1] for r in temp_db.execute("PRAGMA table_info(factor_snapshots)")}
    assert {"news_id", "asset", "ts", "final_score", "confidence", "p_value",
            "chi_square", "sample_size", "contradictions", "factors", "evidence"} <= factors
    perf = {r[1] for r in temp_db.execute("PRAGMA table_info(signal_performance)")}
    assert {"decision_id", "asset", "ts", "action", "is_correct",
            "forward_pnl", "mfe_pct", "mae_pct"} <= perf
    indexes = {r[0] for r in temp_db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_market_ticks_symbol_ts", "idx_factor_snapshots_asset_ts",
            "idx_signal_perf_asset_ts"} <= indexes


def test_ai_decisions_key_columns(temp_db):
    cols = {r[1] for r in temp_db.execute("PRAGMA table_info(ai_decisions)")}
    expected = {
        "parent_id", "child_count", "aggregation_key", "cluster_size",
        "reasoning_path", "vip_tag",
        "doubao_action", "doubao_reasoning", "extra_models_consensus",
        "entry_price", "exit_price", "max_price", "min_price",
        "max_price_time", "min_price_time",
        "is_correct", "settled", "entry_time",
        "mfe_pct", "mae_pct", "forward_pnl", "mfe_time_mins",
        "prediction_type", "event_phase", "market_confirmation",
        "expected_horizon", "invalidation_condition", "decision_context",
        "event_strength", "direct_catalyst", "timeframe_match",
        "strategy_id", "strategy_version_id",
    }
    assert expected <= cols
    strategy_cols = {r[1] for r in temp_db.execute("PRAGMA table_info(strategies)")}
    assert {"is_current", "current_version_id"} <= strategy_cols
    indexes = {r[0] for r in temp_db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_ai_strategy_version" in indexes


def test_paper_trading_defaults(temp_db):
    row = temp_db.execute(
        "SELECT is_running, tracks FROM paper_trading_settings WHERE id=1"
    ).fetchone()
    assert row[0] == 0
    assert row[1] == "[]"


def test_raw_news_columns(temp_db):
    cols = {r[1] for r in temp_db.execute("PRAGMA table_info(raw_news)")}
    assert {"is_noise", "relevance_score", "status", "content", "ts",
            "quality_status", "quality_reason"} <= cols


def test_insert_raw_news_works_without_ts_column():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL, content TEXT NOT NULL,"
        " timestamp TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'PENDING',"
        " is_noise INTEGER NOT NULL DEFAULT 0,"
        " relevance_score REAL NOT NULL DEFAULT 0.0)"
    )
    news_id = db.insert_raw_news(
        conn,
        source="TechFlow 深潮",
        content="compat insert",
        timestamp="2026-08-16 10:00:00",
        ts=1786860000,
    )
    row = conn.execute("SELECT id, source, content FROM raw_news WHERE id=?", (news_id,)).fetchone()
    assert row[0] == news_id
    assert row[1] == "TechFlow 深潮"
    conn.close()


def test_migrate_backfills_old_schema():
    """A DB created with the oldest two-table schema gets all columns added."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL, content TEXT NOT NULL,"
        " timestamp TEXT NOT NULL DEFAULT (datetime('now')),"
        " status TEXT NOT NULL DEFAULT 'PENDING'"
        " CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED')))"
    )
    conn.execute(
        "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " news_id INTEGER NOT NULL, sentiment_score REAL NOT NULL,"
        " suggested_action TEXT NOT NULL, reasoning TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " status TEXT NOT NULL DEFAULT 'UNREAD'"
        " CHECK (status IN ('UNREAD','APPROVED','REJECTED','REVIEWED','AUTO_APPROVED')),"
        " market_category TEXT NOT NULL DEFAULT 'OTHER',"
        " target_asset TEXT NOT NULL DEFAULT 'NONE',"
        " FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE)"
    )
    db.migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    assert "mfe_pct" in cols and "vip_tag" in cols and "parent_id" in cols
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(raw_news)")}
    assert "is_noise" in rcols
    conn.close()


def test_assert_readonly_sql_allows_select():
    sql = "SELECT id, sentiment_score FROM ai_decisions WHERE settled = 0"
    assert db.assert_readonly_sql(sql) == sql


@pytest.mark.parametrize("bad", [
    "INSERT INTO raw_news (source, content) VALUES ('a', 'b')",
    "UPDATE ai_decisions SET settled = 1",
    "DELETE FROM raw_news",
    "DROP TABLE ai_decisions",
    "ALTER TABLE ai_decisions ADD COLUMN x TEXT",
    "SELECT 1; DELETE FROM raw_news",
])
def test_assert_readonly_sql_blocks_writes(bad):
    with pytest.raises(ValueError):
        db.assert_readonly_sql(bad)
