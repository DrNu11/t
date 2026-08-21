#!/usr/bin/env python3
"""
Trident Agent MVP — Local Time-Series Store
============================================

本地时序持久化层，建在 db.py 的三张时序表之上：

  - market_ticks       行情 tick（多交易所中位数价 + 24h 涨跌）
  - factor_snapshots   多因子分与统一置信度快照（含卡方检验结果）
  - signal_performance 已结算信号的前向表现（pnl / mfe / mae）

时间轴统一用 INTEGER epoch 秒 `ts`，配合 (symbol|asset, ts) 复合索引做区间扫描。
OHLC 重采样直接用 SQL 按桶分组完成，不把原始 tick 拉进 Python。

schema 归 db.py 管，本模块只做读写，不含任何 CREATE/ALTER。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

import db
import data_quality

# 支持的重采样粒度（秒）
BUCKET_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# 默认保留期：超过该天数的 tick 会被 prune_market_ticks 清掉
DEFAULT_RETENTION_DAYS = 90


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    connection = db.get_connection()
    connection.row_factory = sqlite3.Row
    return connection


def _bucket_seconds(interval: str) -> int:
    if interval not in BUCKET_SECONDS:
        raise ValueError(f"unsupported interval: {interval}")
    return BUCKET_SECONDS[interval]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def record_market_snapshot(payload: Dict[str, Any],
                           connection: Optional[sqlite3.Connection] = None) -> int:
    """把一次 /api/market/prices 聚合结果落成 tick。

    同一 (symbol, ts) 重复写入会被 UNIQUE 约束吞掉（幂等），返回实际写入条数。
    """
    items = payload.get("items") or []
    if not items:
        return 0
    ts = _now()
    owned = connection is None
    connection = connection or _connect()
    try:
        data_quality.ensure_schema(connection)
        rows = []
        for index, item in enumerate(items):
            try:
                if not isinstance(item, dict):
                    raise ValueError("quote_not_object")
                asset = str(item.get("asset") or item.get("symbol") or "").upper().strip()
                price = float(item.get("price"))
                change = float(item.get("change24h") or 0.0)
                source = str(item.get("source") or "median")
                source_count = int(item.get("sourceCount") or 0)
                event_ts = int(item.get("event_ts") or ts)
                if event_ts > 10_000_000_000:
                    event_ts //= 1000
                if not asset or not math.isfinite(price) or price <= 0 or not math.isfinite(change):
                    raise ValueError("invalid_quote_shape")
                if abs(change) > 10000:
                    raise ValueError("implausible_change24h")
                if source_count < 1:
                    raise ValueError("missing_source_count")
            except (TypeError, ValueError, OverflowError) as exc:
                raw_source = item.get("source") if isinstance(item, dict) else "unknown"
                decision = data_quality.assess(
                    source=str(raw_source or "unknown"), kind="market",
                    event_id=f"invalid:{ts}:{index}", payload=item,
                    observed_at=ts,
                )
                decision = data_quality.QualityDecision(
                    decision.source, decision.kind, decision.event_id, False, False,
                    decision.authority_tier, str(exc), "blocked", decision.latency_ms,
                )
                if not data_quality.observation_already_recorded(connection, decision, item):
                    data_quality.record(connection, decision, payload=item, quarantine=True, observed_at=ts)
                continue
            decision = data_quality.assess(
                source=source,
                kind="market",
                event_id=f"{asset}:{event_ts}",
                published_at=event_ts,
                payload=item,
                observed_at=ts,
            )
            if not data_quality.observation_already_recorded(connection, decision, item):
                data_quality.record(connection, decision, published_at=event_ts, payload=item,
                                    metadata={"source_count": source_count}, observed_at=ts)
            if not decision.accepted:
                continue
            rows.append((asset, event_ts, price, change, source, source_count))
        if not rows:
            if owned:
                connection.commit()
            return 0
        cursor = connection.executemany(
            """INSERT OR IGNORE INTO market_ticks(symbol, ts, price, change24h, source, source_count)
               VALUES(?,?,?,?,?,?)""",
            rows,
        )
        connection.commit()
        return cursor.rowcount
    finally:
        if owned:
            connection.close()


def record_factor_snapshot(news_id: int, asset: str, analysis: Dict[str, Any],
                           connection: Optional[sqlite3.Connection] = None) -> int:
    """把一次多因子 + 证据校验结果落成时序快照，返回新行 id。"""
    significance = analysis.get("significance") or {}
    row = (
        int(news_id), str(asset or "NONE").upper(), _now(),
        float(analysis.get("final_score") or 0.0),
        float(analysis.get("confidence") or 0.0),
        str(analysis.get("raw_action") or "HOLD"),
        str(analysis.get("action") or "HOLD"),
        float(significance.get("p_value") or 1.0),
        float(significance.get("chi_square") or 0.0),
        int(significance.get("sample_size") or 0),
        len(analysis.get("contradictions") or []),
        json.dumps({name: item.get("score") for name, item in (analysis.get("factors") or {}).items()}, ensure_ascii=False),
        json.dumps({name: item.get("evidence_score") for name, item in (analysis.get("evidence") or {}).items()}, ensure_ascii=False),
    )
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute(
            """INSERT INTO factor_snapshots(news_id, asset, ts, final_score, confidence, raw_action,
                                            gated_action, p_value, chi_square, sample_size,
                                            contradictions, factors, evidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        if owned:
            connection.commit()
        return int(cursor.lastrowid)
    finally:
        if owned:
            connection.close()


def record_news_event(
    news_id: int,
    source: str = "",
    is_noise: int = 0,
    status: str = "PENDING",
    decision_id: Optional[int] = None,
    asset: str = "NONE",
    action: str = "HOLD",
    score: float = 0.0,
    ts: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """把一条新闻/信号按时间戳写入 SQLite 时序表 news_events。"""
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute(
            """INSERT OR REPLACE INTO news_events(
                   news_id, decision_id, ts, source, asset, action, score, is_noise, status
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                int(news_id),
                decision_id,
                int(ts or _now()),
                str(source or ""),
                str(asset or "NONE").upper(),
                str(action or "HOLD").upper(),
                float(score or 0.0),
                int(is_noise or 0),
                str(status or "PENDING"),
            ),
        )
        if owned:
            connection.commit()
        return int(cursor.lastrowid)
    finally:
        if owned:
            connection.close()


def query_news_events(
    start: Optional[int] = None,
    end: Optional[int] = None,
    asset: Optional[str] = None,
    limit: int = 2000,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """按时间区间读取新闻信号时序。"""
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT news_id, decision_id, ts, source, asset, action, score, is_noise, status
                 FROM news_events WHERE ts BETWEEN ? AND ?"""
        params: List[Any] = [start, end]
        if asset:
            sql += " AND asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 20000)))
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        if owned:
            connection.close()


def record_signal_performance(rows: Iterable[Dict[str, Any]],
                              connection: Optional[sqlite3.Connection] = None) -> int:
    """把已结算信号的前向表现落成时序点，(decision_id, ts) 幂等。"""
    ts = _now()
    values = [
        (int(row["decision_id"]), str(row.get("asset") or "NONE").upper(), ts,
         str(row.get("action") or "HOLD").upper(), str(row.get("is_correct") or ""),
         row.get("forward_pnl"), row.get("mfe_pct"), row.get("mae_pct"))
        for row in rows
    ]
    if not values:
        return 0
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.executemany(
            """INSERT OR IGNORE INTO signal_performance(decision_id, asset, ts, action, is_correct,
                                                        forward_pnl, mfe_pct, mae_pct)
               VALUES(?,?,?,?,?,?,?,?)""",
            values,
        )
        connection.commit()
        return cursor.rowcount
    finally:
        if owned:
            connection.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_ticks(symbol: str, start: Optional[int] = None, end: Optional[int] = None,
                limit: int = 500, connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """按时间区间取原始 tick，返回按时间升序排列的列表。"""
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT symbol, ts, price, change24h, source, source_count
               FROM market_ticks WHERE symbol=? AND ts BETWEEN ? AND ?
               ORDER BY ts DESC LIMIT ?""",
            (symbol.upper(), start, end, max(1, min(limit, 5000))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        if owned:
            connection.close()


def resample_ohlc(symbol: str, interval: str = "1m", limit: int = 200,
                  start: Optional[int] = None, end: Optional[int] = None,
                  connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """把 tick 按时间桶下采样成 OHLC，聚合完全在 SQL 内完成。

    open/close 用桶内首末 tick 的价格（按 ts 排序），high/low 用极值。
    """
    bucket = _bucket_seconds(interval)
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT (ts / ?) * ? AS bucket_ts,
                      MIN(price) AS low, MAX(price) AS high,
                      COUNT(*) AS ticks,
                      (SELECT price FROM market_ticks o
                        WHERE o.symbol=m.symbol AND (o.ts / ?) * ? = (m.ts / ?) * ?
                        ORDER BY o.ts ASC LIMIT 1) AS open,
                      (SELECT price FROM market_ticks c
                        WHERE c.symbol=m.symbol AND (c.ts / ?) * ? = (m.ts / ?) * ?
                        ORDER BY c.ts DESC LIMIT 1) AS close
               FROM market_ticks m
               WHERE m.symbol=? AND m.ts BETWEEN ? AND ?
               GROUP BY bucket_ts ORDER BY bucket_ts DESC LIMIT ?""",
            (bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket,
             symbol.upper(), start, end, max(1, min(limit, 2000))),
        ).fetchall()
        return [
            {"ts": row["bucket_ts"], "open": row["open"], "high": row["high"],
             "low": row["low"], "close": row["close"], "ticks": row["ticks"]}
            for row in reversed(rows)
        ]
    finally:
        if owned:
            connection.close()


def query_factor_history(asset: Optional[str] = None, limit: int = 100,
                         connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """取多因子置信度时序，用于回溯因子有效性衰减。"""
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT news_id, asset, ts, final_score, confidence, raw_action, gated_action,
                        p_value, chi_square, sample_size, contradictions, factors, evidence
                 FROM factor_snapshots"""
        params: List[Any] = []
        if asset:
            sql += " WHERE asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        rows = connection.execute(sql, params).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["factors"] = json.loads(item["factors"] or "{}")
            item["evidence"] = json.loads(item["evidence"] or "{}")
            result.append(item)
        return result
    finally:
        if owned:
            connection.close()


def rolling_winrate(asset: Optional[str] = None, window: int = 20,
                    connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """按最近 `window` 条已结算信号算滚动胜率与累计盈亏。"""
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT is_correct, forward_pnl FROM signal_performance"""
        params: List[Any] = []
        if asset:
            sql += " WHERE asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(window, 1000)))
        rows = connection.execute(sql, params).fetchall()
        total = len(rows)
        wins = sum(1 for row in rows if row["is_correct"] == "WIN")
        pnl = sum(row["forward_pnl"] or 0.0 for row in rows)
        return {
            "asset": (asset or "ALL").upper(),
            "window": total,
            "wins": wins,
            "winrate": round(wins / total, 4) if total else 0.0,
            "cumulative_pnl": round(pnl, 4),
        }
    finally:
        if owned:
            connection.close()


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------

def prune_market_ticks(retention_days: int = DEFAULT_RETENTION_DAYS,
                       connection: Optional[sqlite3.Connection] = None) -> int:
    """删除超过保留期的 tick，返回删除条数。因子与信号表不清理（样本量本身就是资产）。"""
    cutoff = _now() - max(1, retention_days) * 86400
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute("DELETE FROM market_ticks WHERE ts < ?", (cutoff,))
        connection.commit()
        return cursor.rowcount
    finally:
        if owned:
            connection.close()
