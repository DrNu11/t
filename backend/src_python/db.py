#!/usr/bin/env python3
"""
Trident Agent MVP — Database Layer
===================================

Single authoritative schema + migration entry point for trident_event_bus.db.

Consolidates what used to live in three places:
  - backend/init_db.py                     (two CREATE TABLE statements)
  - engine.py  :: _ensure_db_exists()      (~20 incremental ALTER TABLEs)
  - api_server.py :: _migrate_schema()     (ALTER TABLE subset)

`migrate(conn)` is idempotent: CREATE TABLE IF NOT EXISTS with the full
latest column set, then a per-column existence check (PRAGMA table_info)
before any ALTER TABLE — no try/except swallowing.
"""

from __future__ import annotations

import sqlite3
import time
from typing import List, Optional, Tuple

import config

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_db_path() -> str:
    """Return the event-bus DB path (TRIDENT_DB_PATH override aware)."""
    return config.DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def insert_raw_news(
    conn: sqlite3.Connection,
    *,
    source: str,
    content: str,
    timestamp: str,
    status: str = "PENDING",
    is_noise: int = 0,
    relevance_score: float = 0.0,
    ts: Optional[int] = None,
) -> int:
    """写入 raw_news。生产库若尚未 migrate 出 ts 列，自动降级，避免整批入库失败。"""
    existing = table_columns(conn, "raw_news")
    columns = ["source", "content", "timestamp", "status", "is_noise", "relevance_score"]
    values: List[object] = [source, content, timestamp, status, int(is_noise), float(relevance_score)]
    if "ts" in existing:
        columns.append("ts")
        values.append(int(ts if ts is not None else time.time()))
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO raw_news ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Schema — latest complete definitions
# ---------------------------------------------------------------------------

_CREATE_RAW_NEWS = """
    CREATE TABLE IF NOT EXISTS raw_news (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT    NOT NULL,
        content         TEXT    NOT NULL,
        timestamp       TEXT    NOT NULL DEFAULT (datetime('now')),
        ts              INTEGER,
        status          TEXT    NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
        is_noise        INTEGER NOT NULL DEFAULT 0,
        relevance_score REAL    NOT NULL DEFAULT 0.0
    );
"""

_CREATE_AI_DECISIONS = """
    CREATE TABLE IF NOT EXISTS ai_decisions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id           INTEGER NOT NULL,
        sentiment_score   REAL    NOT NULL,
        suggested_action  TEXT    NOT NULL,
        reasoning         TEXT    NOT NULL,
        created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
        status            TEXT    NOT NULL DEFAULT 'UNREAD'
            CHECK (status IN ('UNREAD', 'APPROVED', 'REJECTED', 'REVIEWED', 'AUTO_APPROVED')),
        market_category   TEXT    NOT NULL DEFAULT 'OTHER',
        target_asset      TEXT    NOT NULL DEFAULT 'NONE',
        parent_id         INTEGER DEFAULT NULL,
        child_count       INTEGER DEFAULT 0,
        aggregation_key   TEXT    DEFAULT '',
        cluster_size      INTEGER DEFAULT 1,
        reasoning_path    TEXT    DEFAULT '',
        vip_tag           TEXT    DEFAULT '',
        doubao_action     TEXT    DEFAULT 'HOLD',
        doubao_reasoning  TEXT    DEFAULT '',
        extra_models_consensus TEXT DEFAULT '',
        entry_price       REAL    DEFAULT NULL,
        exit_price        REAL    DEFAULT NULL,
        max_price         REAL    DEFAULT NULL,
        min_price         REAL    DEFAULT NULL,
        max_price_time    INTEGER DEFAULT 0,
        min_price_time    INTEGER DEFAULT 0,
        is_correct        TEXT    DEFAULT '',
        settled           INTEGER DEFAULT 0,
        entry_time        TEXT    DEFAULT '',
        prediction_type        TEXT    DEFAULT 'continuation',
        event_phase            TEXT    DEFAULT 'mid',
        market_confirmation    TEXT    DEFAULT 'unknown',
        expected_horizon       TEXT    DEFAULT '1-3d',
        invalidation_condition TEXT    DEFAULT '',
        decision_context       TEXT    DEFAULT '{}',
        mfe_pct           REAL    DEFAULT NULL,
        mae_pct           REAL    DEFAULT NULL,
        forward_pnl       REAL    DEFAULT NULL,
        mfe_time_mins     REAL    DEFAULT NULL,
        event_strength    TEXT    DEFAULT 'medium',
        direct_catalyst   INTEGER DEFAULT 0,
        timeframe_match   TEXT    DEFAULT 'intraday',
        paper_trading_run_id INTEGER DEFAULT NULL,
        agent_model_id       TEXT    DEFAULT '',
        evidence_confidence  REAL    DEFAULT NULL,
        evidence_action      TEXT    DEFAULT 'HOLD',
        trade_gate_reason    TEXT    DEFAULT '',
        FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE
    );
"""

# Incremental columns for DBs created by older CREATE TABLE versions.
# (name, column definition) — added via ALTER TABLE only when missing.
_RAW_NEWS_COLUMNS: List[Tuple[str, str]] = [
    ("is_noise",        "INTEGER NOT NULL DEFAULT 0"),
    ("relevance_score", "REAL    NOT NULL DEFAULT 0.0"),
    ("ts",              "INTEGER"),
]

_AI_DECISIONS_COLUMNS: List[Tuple[str, str]] = [
    ("market_category",  "TEXT    NOT NULL DEFAULT 'OTHER'"),
    ("target_asset",     "TEXT    NOT NULL DEFAULT 'NONE'"),
    ("parent_id",        "INTEGER DEFAULT NULL"),
    ("child_count",      "INTEGER DEFAULT 0"),
    ("aggregation_key",  "TEXT    DEFAULT ''"),
    ("cluster_size",     "INTEGER DEFAULT 1"),
    ("reasoning_path",   "TEXT    DEFAULT ''"),
    ("vip_tag",          "TEXT    DEFAULT ''"),
    ("doubao_action",    "TEXT    DEFAULT 'HOLD'"),
    ("doubao_reasoning", "TEXT    DEFAULT ''"),
    ("extra_models_consensus", "TEXT    DEFAULT ''"),
    ("entry_price",      "REAL    DEFAULT NULL"),
    ("exit_price",       "REAL    DEFAULT NULL"),
    ("max_price",        "REAL    DEFAULT NULL"),
    ("min_price",        "REAL    DEFAULT NULL"),
    ("max_price_time",   "INTEGER DEFAULT 0"),
    ("min_price_time",   "INTEGER DEFAULT 0"),
    ("is_correct",       "TEXT    DEFAULT ''"),
    ("settled",          "INTEGER DEFAULT 0"),
    ("entry_time",       "TEXT    DEFAULT ''"),
    ("prediction_type",        "TEXT    DEFAULT 'continuation'"),
    ("event_phase",            "TEXT    DEFAULT 'mid'"),
    ("market_confirmation",    "TEXT    DEFAULT 'unknown'"),
    ("expected_horizon",       "TEXT    DEFAULT '1-3d'"),
    ("invalidation_condition", "TEXT    DEFAULT ''"),
    ("decision_context",       "TEXT    DEFAULT '{}'"),
    ("mfe_pct",          "REAL    DEFAULT NULL"),
    ("mae_pct",          "REAL    DEFAULT NULL"),
    ("forward_pnl",      "REAL    DEFAULT NULL"),
    ("mfe_time_mins",    "REAL    DEFAULT NULL"),
    ("event_strength",   "TEXT    DEFAULT 'medium'"),
    ("direct_catalyst",  "INTEGER DEFAULT 0"),
    ("timeframe_match",  "TEXT    DEFAULT 'intraday'"),
    ("strategy_id",      "INTEGER DEFAULT NULL"),
    ("strategy_version_id", "INTEGER DEFAULT NULL"),
    ("paper_trading_run_id", "INTEGER DEFAULT NULL"),
    ("agent_model_id", "TEXT DEFAULT ''"),
    ("evidence_confidence", "REAL DEFAULT NULL"),
    ("evidence_action", "TEXT DEFAULT 'HOLD'"),
    ("trade_gate_reason", "TEXT DEFAULT ''"),
    ("ts", "INTEGER"),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ai_decisions_status ON ai_decisions(status);",
    "CREATE INDEX IF NOT EXISTS idx_raw_news_status ON raw_news(status);",
    "CREATE INDEX IF NOT EXISTS idx_ai_parent ON ai_decisions(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_ai_agg_key ON ai_decisions(aggregation_key);",
    "CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_ts ON market_ticks(symbol, ts);",
    "CREATE INDEX IF NOT EXISTS idx_factor_snapshots_asset_ts ON factor_snapshots(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_factor_snapshots_news ON factor_snapshots(news_id);",
    "CREATE INDEX IF NOT EXISTS idx_signal_perf_asset_ts ON signal_performance(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_strategy_versions_strategy ON strategy_versions(strategy_id, version);",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_ai_strategy_version ON ai_decisions(strategy_version_id);",
    "CREATE INDEX IF NOT EXISTS idx_ai_paper_run ON ai_decisions(paper_trading_run_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_runs_status ON paper_trading_runs(status, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_quick_sim_settled ON quick_sim_trades(settled, entry_ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_ts ON hermes_observations(ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_asset ON hermes_observations(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_news ON hermes_observations(news_id);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_skills_asset ON hermes_skills(asset, action);",
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_ts ON macro_snapshots(ts);",
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_key ON macro_snapshots(category, metric_key, ts);",
]


# ---------------------------------------------------------------------------
# Time-series tables — local persistent history
# ---------------------------------------------------------------------------
# All three use an INTEGER epoch-second `ts` as the time axis plus a symbol/asset
# partition key, so range scans and downsampling run off one composite index.

_CREATE_MARKET_TICKS = """
    CREATE TABLE IF NOT EXISTS market_ticks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol       TEXT    NOT NULL,
        ts           INTEGER NOT NULL,
        price        REAL    NOT NULL,
        change24h    REAL    NOT NULL DEFAULT 0.0,
        source       TEXT    NOT NULL DEFAULT 'median',
        source_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE (symbol, ts)
    );
"""

_CREATE_FACTOR_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS factor_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id       INTEGER NOT NULL,
        asset         TEXT    NOT NULL DEFAULT 'NONE',
        ts            INTEGER NOT NULL,
        final_score   REAL    NOT NULL DEFAULT 0.0,
        confidence    REAL    NOT NULL DEFAULT 0.0,
        raw_action    TEXT    NOT NULL DEFAULT 'HOLD',
        gated_action  TEXT    NOT NULL DEFAULT 'HOLD',
        p_value       REAL    NOT NULL DEFAULT 1.0,
        chi_square    REAL    NOT NULL DEFAULT 0.0,
        sample_size   INTEGER NOT NULL DEFAULT 0,
        contradictions INTEGER NOT NULL DEFAULT 0,
        factors       TEXT    NOT NULL DEFAULT '{}',
        evidence      TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_NEWS_EVENTS = """
    CREATE TABLE IF NOT EXISTS news_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id     INTEGER NOT NULL,
        decision_id INTEGER,
        ts          INTEGER NOT NULL,
        source      TEXT    NOT NULL DEFAULT '',
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        score       REAL    NOT NULL DEFAULT 0.0,
        is_noise    INTEGER NOT NULL DEFAULT 0,
        status      TEXT    NOT NULL DEFAULT 'PENDING',
        UNIQUE (news_id, ts)
    );
"""

_CREATE_SIGNAL_PERFORMANCE = """
    CREATE TABLE IF NOT EXISTS signal_performance (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL,
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        ts          INTEGER NOT NULL,
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        is_correct  TEXT    NOT NULL DEFAULT '',
        forward_pnl REAL,
        mfe_pct     REAL,
        mae_pct     REAL,
        UNIQUE (decision_id, ts)
    );
"""

_CREATE_STRATEGIES = """
    CREATE TABLE IF NOT EXISTS strategies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slug        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
        is_current  INTEGER NOT NULL DEFAULT 0,
        current_version_id INTEGER DEFAULT NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_STRATEGIES_COLUMNS: List[Tuple[str, str]] = [
    ("is_current", "INTEGER NOT NULL DEFAULT 0"),
    ("current_version_id", "INTEGER DEFAULT NULL"),
]

_CREATE_STRATEGY_VERSIONS = """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        version     INTEGER NOT NULL,
        params      TEXT    NOT NULL DEFAULT '{}',
        source      TEXT    NOT NULL DEFAULT 'manual'
            CHECK (source IN ('manual', 'seed', 'llm', 'backtest')),
        note        TEXT    NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (strategy_id, version),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
    );
"""

_CREATE_PAPER_TRADING_SETTINGS = """
    CREATE TABLE IF NOT EXISTS paper_trading_settings (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        is_running  INTEGER NOT NULL DEFAULT 0,
        tracks      TEXT    NOT NULL DEFAULT '[]',
        started_at  TEXT,
        active_run_id INTEGER DEFAULT NULL,
        gate_enabled INTEGER NOT NULL DEFAULT 1,
        news_enabled INTEGER NOT NULL DEFAULT 1,
        news_daily_target INTEGER NOT NULL DEFAULT 300,
        news_sources TEXT NOT NULL DEFAULT '{"financialjuice":true,"tree_news":true,"techflow":true,"eastmoney":true,"blockbeats":true}',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_PAPER_SETTINGS_COLUMNS: List[Tuple[str, str]] = [
    ("active_run_id", "INTEGER DEFAULT NULL"),
    ("gate_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("news_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("news_daily_target", "INTEGER NOT NULL DEFAULT 300"),
    ("news_sources", "TEXT NOT NULL DEFAULT '{\"financialjuice\":true,\"tree_news\":true,\"techflow\":true,\"eastmoney\":true,\"blockbeats\":true}'"),
]

_CREATE_PAPER_TRADING_RUNS = """
    CREATE TABLE IF NOT EXISTS paper_trading_runs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        tracks              TEXT    NOT NULL DEFAULT '[]',
        strategy_id         INTEGER NOT NULL,
        strategy_version_id INTEGER NOT NULL,
        agent_version       TEXT    NOT NULL,
        model_id            TEXT    NOT NULL,
        spec_version        TEXT    NOT NULL,
        activation_reason   TEXT    NOT NULL DEFAULT '',
        status              TEXT    NOT NULL DEFAULT 'RUNNING'
            CHECK (status IN ('RUNNING', 'STOPPED')),
        started_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        stopped_at          TEXT,
        FOREIGN KEY (strategy_id) REFERENCES strategies(id),
        FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions(id)
    );
"""

_CREATE_QUICK_SIM_TRADES = """
    CREATE TABLE IF NOT EXISTS quick_sim_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id         INTEGER NOT NULL UNIQUE,
        news_id             INTEGER,
        asset               TEXT    NOT NULL,
        action              TEXT    NOT NULL CHECK (action IN ('BUY', 'SELL')),
        score               REAL,
        evidence_confidence REAL,
        gate_passed         INTEGER NOT NULL DEFAULT 0,
        gate_reason         TEXT    NOT NULL DEFAULT '',
        strategy_id         INTEGER,
        strategy_version_id INTEGER,
        paper_trading_run_id INTEGER,
        notional_usdt       REAL    NOT NULL DEFAULT 0.0,
        entry_price         REAL    NOT NULL,
        entry_ts            INTEGER NOT NULL,
        horizon_minutes     INTEGER NOT NULL,
        exit_price          REAL,
        exit_ts             INTEGER,
        pnl_pct             REAL,
        pnl_usdt            REAL,
        verdict             TEXT    NOT NULL DEFAULT '',
        settled             INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (decision_id) REFERENCES ai_decisions(id) ON DELETE CASCADE
    );
"""

_CREATE_HERMES_OBSERVATIONS = """
    CREATE TABLE IF NOT EXISTS hermes_observations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id          INTEGER,
        decision_id      INTEGER,
        ts               INTEGER NOT NULL,
        source           TEXT    NOT NULL DEFAULT '',
        asset            TEXT    NOT NULL DEFAULT 'NONE',
        observation_type TEXT    NOT NULL DEFAULT 'news',
        content          TEXT    NOT NULL DEFAULT '',
        payload          TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_HERMES_SKILLS = """
    CREATE TABLE IF NOT EXISTS hermes_skills (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_key        TEXT    NOT NULL UNIQUE,
        asset            TEXT    NOT NULL,
        action           TEXT    NOT NULL,
        prediction_type  TEXT    NOT NULL DEFAULT '',
        sample_size      INTEGER NOT NULL DEFAULT 0,
        wins             INTEGER NOT NULL DEFAULT 0,
        losses           INTEGER NOT NULL DEFAULT 0,
        win_rate         REAL,
        avg_pnl          REAL,
        note             TEXT    NOT NULL DEFAULT '',
        updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_MACRO_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS macro_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         INTEGER NOT NULL,
        category   TEXT    NOT NULL,
        source     TEXT    NOT NULL DEFAULT '',
        metric_key TEXT    NOT NULL,
        value      REAL,
        unit       TEXT    NOT NULL DEFAULT '',
        status     TEXT    NOT NULL DEFAULT 'unavailable',
        payload    TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_BACKTEST_RUNS = """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id    INTEGER NOT NULL,
        version_id     INTEGER NOT NULL,
        params         TEXT    NOT NULL DEFAULT '{}',
        result         TEXT    NOT NULL DEFAULT '{}',
        sample_size    INTEGER NOT NULL DEFAULT 0,
        taken          INTEGER NOT NULL DEFAULT 0,
        winrate        REAL,
        total_pnl_usdt REAL,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
        FOREIGN KEY (version_id) REFERENCES strategy_versions(id) ON DELETE CASCADE
    );
"""


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    columns: List[Tuple[str, str]]) -> None:
    """ALTER TABLE ADD COLUMN for each missing column. Idempotent by design."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, col_def in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the full schema to `conn`. Safe to call on every startup."""
    conn.execute(_CREATE_RAW_NEWS)
    conn.execute(_CREATE_AI_DECISIONS)
    conn.execute(_CREATE_MARKET_TICKS)
    conn.execute(_CREATE_FACTOR_SNAPSHOTS)
    conn.execute(_CREATE_NEWS_EVENTS)
    conn.execute(_CREATE_SIGNAL_PERFORMANCE)
    conn.execute(_CREATE_STRATEGIES)
    conn.execute(_CREATE_STRATEGY_VERSIONS)
    conn.execute(_CREATE_PAPER_TRADING_SETTINGS)
    conn.execute(_CREATE_PAPER_TRADING_RUNS)
    conn.execute(_CREATE_QUICK_SIM_TRADES)
    conn.execute(_CREATE_HERMES_OBSERVATIONS)
    conn.execute(_CREATE_HERMES_SKILLS)
    conn.execute(_CREATE_MACRO_SNAPSHOTS)
    conn.execute(_CREATE_BACKTEST_RUNS)
    conn.execute("INSERT OR IGNORE INTO paper_trading_settings (id) VALUES (1)")

    # Backfill columns on databases created by older schema versions
    _ensure_columns(conn, "raw_news", _RAW_NEWS_COLUMNS)
    _ensure_columns(conn, "ai_decisions", _AI_DECISIONS_COLUMNS)
    _ensure_columns(conn, "strategies", _STRATEGIES_COLUMNS)
    _ensure_columns(conn, "paper_trading_settings", _PAPER_SETTINGS_COLUMNS)

    for stmt in _INDEXES:
        conn.execute(stmt)

    conn.execute(
        """UPDATE raw_news
           SET ts = CAST(strftime('%s', REPLACE(substr(timestamp, 1, 19), 'T', ' ')) AS INTEGER)
           WHERE ts IS NULL AND timestamp IS NOT NULL AND length(timestamp) >= 19"""
    )
    conn.execute(
        """UPDATE ai_decisions
           SET ts = CAST(strftime('%s', REPLACE(substr(created_at, 1, 19), 'T', ' ')) AS INTEGER)
           WHERE ts IS NULL AND created_at IS NOT NULL AND length(created_at) >= 19"""
    )
    conn.commit()


# ---------------------------------------------------------------------------
# SQL safety gate (recovered from hermes/repositories/base.py)
# ---------------------------------------------------------------------------

def assert_readonly_sql(sql: str) -> str:
    """Raise ValueError if `sql` contains any write-side-effect keyword.

    Called by every Repository read method as a last-resort safety belt.
    Repository write methods (insert/update/delete) are deliberately NOT
    decorated — they are the *only* places writes may occur.
    """
    upper = sql.upper().strip()
    # Block DDL
    for kw in ("DROP", "ALTER", "CREATE", "TRUNCATE", "ATTACH", "DETACH", "PRAGMA"):
        if upper.startswith(kw) or f" {kw} " in f" {upper} ":
            raise ValueError(f"DDL keyword '{kw}' not allowed in read query: {sql[:120]}")
    # Block DML writes
    for kw in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE "):
        if kw in upper:
            raise ValueError(f"Write keyword '{kw.strip()}' not allowed in read query: {sql[:120]}")
    return sql
