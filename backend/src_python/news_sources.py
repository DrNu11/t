"""新闻源开关与每日入库配额。

默认开启全部新闻源，每日目标 300 条（上海时区）。
关闭总开关或单个源后，对应入库会被拦截。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional

import config
import db

SOURCE_KEYS = ("financialjuice", "tree_news", "techflow", "eastmoney", "blockbeats", "jin10")
DEFAULT_DAILY_TARGET = 300

_DEFAULT_SOURCES = {key: True for key in SOURCE_KEYS}


def _today() -> str:
    from datetime import datetime
    return datetime.now(config.TZ_SHANGHAI).strftime("%Y-%m-%d")


def _normalize_source(source: str) -> Optional[str]:
    value = (source or "").lower()
    if "jin10" in value or "金十" in (source or ""):
        return "jin10"
    if "techflow" in value or "深潮" in (source or ""):
        return "techflow"
    if "eastmoney" in value or "东方财富" in (source or ""):
        return "eastmoney"
    if "blockbeats" in value or "律动" in (source or ""):
        return "blockbeats"
    if "tree" in value or "telegram" in value or value.startswith("web:"):
        return "tree_news"
    if "financialjuice" in value or value.startswith("ws:") or "fj" in value:
        return "financialjuice"
    return None


def get_settings(connection=None) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                "SELECT news_enabled, news_daily_target, news_sources FROM paper_trading_settings WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            # 旧库尚未补 news_* 列时按默认全开，避免 watcher 整轮停采。
            row = None
        enabled = True
        daily_target = DEFAULT_DAILY_TARGET
        sources = dict(_DEFAULT_SOURCES)
        if row is not None:
            if row[0] is not None:
                enabled = bool(row[0])
            if row[1]:
                daily_target = max(1, int(row[1]))
            try:
                stored = json.loads(row[2] or "{}")
                if isinstance(stored, dict):
                    for key in SOURCE_KEYS:
                        if key in stored:
                            sources[key] = bool(stored[key])
            except (TypeError, json.JSONDecodeError):
                pass
        today = _today()
        today_count = int(conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=?",
            (today,),
        ).fetchone()[0])
        remaining = max(0, daily_target - today_count)
        return {
            "enabled": enabled,
            "daily_target": daily_target,
            "today_count": today_count,
            "remaining": remaining,
            "sources": sources,
            "today": today,
        }
    finally:
        if own:
            conn.close()


def set_settings(
    *,
    enabled: Optional[bool] = None,
    daily_target: Optional[int] = None,
    sources: Optional[Dict[str, Any]] = None,
    connection=None,
) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        current = get_settings(conn)
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        next_target = current["daily_target"] if daily_target is None else max(1, int(daily_target))
        next_sources = dict(current["sources"])
        if isinstance(sources, dict):
            for key in SOURCE_KEYS:
                if key in sources:
                    next_sources[key] = bool(sources[key])
        conn.execute(
            """UPDATE paper_trading_settings
               SET news_enabled=?, news_daily_target=?, news_sources=?
               WHERE id=1""",
            (int(next_enabled), next_target, json.dumps(next_sources, ensure_ascii=False)),
        )
        if own:
            conn.commit()
        return get_settings(conn)
    finally:
        if own:
            conn.close()


def allow_ingest(source: str, connection=None) -> bool:
    """入库前检查：总开关、单源开关、当日配额。"""
    own = connection is None
    conn = connection or db.get_connection()
    try:
        settings = get_settings(conn)
        if not settings["enabled"]:
            return False
        if settings["remaining"] <= 0:
            return False
        key = _normalize_source(source)
        if key is None:
            return True
        return bool(settings["sources"].get(key, True))
    finally:
        if own:
            conn.close()
