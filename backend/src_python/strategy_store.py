#!/usr/bin/env python3
"""
策略库 — 本地持久化交易策略与版本。

schema 归 db.py，本模块只做读写。内置 3 条与本平台交易参数对齐的种子策略：
  news-threshold   新闻信号阈值过滤
  conservative     高阈值低仓位
  aggressive       低阈值高仓位 + 紧追踪止损
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Optional

import config
import db

PARAM_BOUNDS = {
    "signal_threshold": (0.3, 0.9),
    "notional_usdt": (5.0, 500.0),
    "leverage": (1, 20),
    "trailing_callback_rate": (0.1, 5.0),
    "holding_horizon_minutes": (15, 10080),
    "short_horizon_minutes": (5, 240),
    "medium_horizon_minutes": (240, 4320),
    "long_horizon_minutes": (4320, 43200),
    "take_profit_pct": (0.0, 20.0),
    "stop_loss_pct": (0.0, 20.0),
    "uncertainty_threshold": (0.4, 0.8),
    "min_event_strength": ("", "strong"),  # informational; validated separately
}

ALLOWED_STRENGTH = {"", "weak", "medium", "strong"}
ALLOWED_ASSETS = {"", "BTC", "ETH", "SOL", "XAU", "GOLD", "WTI"}
STRENGTH_RANK = {"weak": 1, "medium": 2, "strong": 3}


def default_params() -> Dict[str, Any]:
    return {
        "signal_threshold": float(config.BINANCE_SIGNAL_THRESHOLD),
        "notional_usdt": float(config.BINANCE_NOTIONAL_USDT),
        "leverage": int(config.BINANCE_LEVERAGE),
        "trailing_callback_rate": float(config.BINANCE_TRAILING_CALLBACK_RATE),
        "holding_horizon_minutes": 120,
        "short_horizon_minutes": 30,
        "medium_horizon_minutes": 720,
        "long_horizon_minutes": 10080,
        "take_profit_pct": 0.0,
        "stop_loss_pct": 0.0,
        "uncertainty_threshold": 0.55,
        "dual_side_mode": "paper_only",
        "close_on_take_profit": True,
        "min_event_strength": "",
        "asset_filter": "",
        "require_direct_catalyst": False,
    }


def clamp_params(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = default_params()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    out["signal_threshold"] = _clamp_float(raw.get("signal_threshold"), *PARAM_BOUNDS["signal_threshold"], base["signal_threshold"])
    out["notional_usdt"] = _clamp_float(raw.get("notional_usdt"), *PARAM_BOUNDS["notional_usdt"], base["notional_usdt"])
    out["leverage"] = int(_clamp_float(raw.get("leverage"), *PARAM_BOUNDS["leverage"], base["leverage"]))
    out["trailing_callback_rate"] = _clamp_float(raw.get("trailing_callback_rate"), *PARAM_BOUNDS["trailing_callback_rate"], base["trailing_callback_rate"])
    out["holding_horizon_minutes"] = int(_clamp_float(raw.get("holding_horizon_minutes"), *PARAM_BOUNDS["holding_horizon_minutes"], base["holding_horizon_minutes"]))
    out["short_horizon_minutes"] = int(_clamp_float(raw.get("short_horizon_minutes"), *PARAM_BOUNDS["short_horizon_minutes"], base["short_horizon_minutes"]))
    out["medium_horizon_minutes"] = int(_clamp_float(raw.get("medium_horizon_minutes"), *PARAM_BOUNDS["medium_horizon_minutes"], base["medium_horizon_minutes"]))
    out["long_horizon_minutes"] = int(_clamp_float(raw.get("long_horizon_minutes"), *PARAM_BOUNDS["long_horizon_minutes"], base["long_horizon_minutes"]))
    out["take_profit_pct"] = _clamp_float(raw.get("take_profit_pct"), *PARAM_BOUNDS["take_profit_pct"], base["take_profit_pct"])
    out["stop_loss_pct"] = _clamp_float(raw.get("stop_loss_pct"), *PARAM_BOUNDS["stop_loss_pct"], base["stop_loss_pct"])
    out["uncertainty_threshold"] = _clamp_float(raw.get("uncertainty_threshold"), *PARAM_BOUNDS["uncertainty_threshold"], base["uncertainty_threshold"])
    strength = str(raw.get("min_event_strength") or "").lower()
    out["min_event_strength"] = strength if strength in ALLOWED_STRENGTH else ""
    asset = str(raw.get("asset_filter") or "").upper()
    out["asset_filter"] = asset if asset in ALLOWED_ASSETS else ""
    out["require_direct_catalyst"] = bool(raw.get("require_direct_catalyst"))
    mode = str(raw.get("dual_side_mode") or base["dual_side_mode"]).lower()
    out["dual_side_mode"] = mode if mode in {"off", "paper_only", "review"} else base["dual_side_mode"]
    out["close_on_take_profit"] = bool(raw.get("close_on_take_profit", base["close_on_take_profit"]))
    return out


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def signal_matches(params: Dict[str, Any], *, action: str, score: float,
                   asset: str, event_strength: str = "medium",
                   direct_catalyst: int = 0) -> bool:
    """策略入场过滤。回测与实时模拟盘共用同一口径。"""
    normalized_action = str(action or "").upper()
    if normalized_action not in {"BUY", "SELL"}:
        return False
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(numeric_score):
        return False
    if (normalized_action == "BUY" and numeric_score <= 0) or (normalized_action == "SELL" and numeric_score >= 0):
        return False
    if abs(numeric_score) < float(params["signal_threshold"]):
        return False
    asset_filter = str(params.get("asset_filter") or "").upper()
    if asset_filter and str(asset or "").upper() != asset_filter:
        return False
    minimum = str(params.get("min_event_strength") or "").lower()
    actual = str(event_strength or "medium").lower()
    if minimum and STRENGTH_RANK.get(actual, 2) < STRENGTH_RANK.get(minimum, 0):
        return False
    if params.get("require_direct_catalyst") and not int(direct_catalyst or 0):
        return False
    return True


def _connect() -> sqlite3.Connection:
    connection = db.get_connection()
    connection.row_factory = sqlite3.Row
    return connection


def _parse_params(blob: Any) -> Dict[str, Any]:
    if isinstance(blob, dict):
        return clamp_params(blob)
    if isinstance(blob, str) and blob:
        try:
            return clamp_params(json.loads(blob))
        except json.JSONDecodeError:
            return default_params()
    return default_params()


def _parse_structure(blob: Any) -> Dict[str, Any]:
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str) and blob.strip():
        try:
            value = json.loads(blob)
            return value if isinstance(value, dict) else {"raw": value}
        except json.JSONDecodeError:
            return {"raw": blob}
    return {}


def _row_strategy(row: sqlite3.Row, latest: Optional[sqlite3.Row] = None) -> Dict[str, Any]:
    payload = dict(row)
    if latest:
        payload["latest_version"] = {
            "id": latest["id"],
            "version": latest["version"],
            "params": _parse_params(latest["params"]),
            "ai_prompt": latest["ai_prompt"] if "ai_prompt" in latest.keys() else "",
            "analysis_structure": _parse_structure(latest["analysis_structure"] if "analysis_structure" in latest.keys() else "{}"),
            "source": latest["source"],
            "note": latest["note"],
            "created_at": latest["created_at"],
        }
    else:
        payload["latest_version"] = None
    return payload


SEED_STRATEGIES = (
    {
        "slug": "news-threshold",
        "name": "新闻阈值策略",
        "description": "对齐当前实盘默认参数：|sentiment| 超过信号阈值才开仓，名义本金与追踪止损取 config。",
        "params": default_params(),
        "note": "种子：与 BINANCE_* 默认值对齐",
    },
    {
        "slug": "conservative",
        "name": "保守过滤策略",
        "description": "更高阈值、更低名义本金，只吃 strong 事件，降低假突破。",
        "params": {
            **default_params(),
            "signal_threshold": 0.7,
            "notional_usdt": max(10.0, float(config.BINANCE_NOTIONAL_USDT) * 0.5),
            "leverage": max(1, int(config.BINANCE_LEVERAGE) - 2),
            "trailing_callback_rate": 0.8,
            "min_event_strength": "strong",
            "require_direct_catalyst": True,
        },
        "note": "种子：保守过滤",
    },
    {
        "slug": "aggressive",
        "name": "进攻追踪策略",
        "description": "更低阈值、更高仓位、更紧追踪止损，适合高置信样本。",
        "params": {
            **default_params(),
            "signal_threshold": 0.35,
            "notional_usdt": min(200.0, float(config.BINANCE_NOTIONAL_USDT) * 1.5),
            "trailing_callback_rate": 0.3,
            "holding_horizon_minutes": 60,
        },
        "note": "种子：进攻追踪",
    },
)


def seed_if_empty(connection: Optional[sqlite3.Connection] = None) -> int:
    owned = connection is None
    connection = connection or _connect()
    try:
        count = connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
        if count:
            return 0
        created = 0
        for item in SEED_STRATEGIES:
            create_strategy(
                item["name"],
                item["description"],
                item["params"],
                slug=item["slug"],
                source="seed",
                note=item["note"],
                connection=connection,
            )
            created += 1
        first = connection.execute(
            "SELECT id FROM strategies ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if first:
            latest = connection.execute(
                "SELECT id FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC LIMIT 1",
                (first[0],),
            ).fetchone()
            connection.execute(
                "UPDATE strategies SET is_current=1, current_version_id=? WHERE id=?",
                (latest[0], first[0]),
            )
        connection.commit()
        return created
    finally:
        if owned:
            connection.close()


def list_strategies(include_archived: bool = False) -> List[Dict[str, Any]]:
    seed_if_empty()
    connection = _connect()
    try:
        sql = "SELECT * FROM strategies"
        if not include_archived:
            sql += " WHERE status='active'"
        sql += " ORDER BY id ASC"
        rows = connection.execute(sql).fetchall()
        out = []
        for row in rows:
            latest = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            out.append(_row_strategy(row, latest))
        return out
    finally:
        connection.close()


def get_strategy(strategy_id: int) -> Dict[str, Any]:
    seed_if_empty()
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if row is None:
            raise KeyError(f"strategy {strategy_id} not found")
        latest = connection.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        versions = [
            {
                "id": item["id"],
                "version": item["version"],
                "params": _parse_params(item["params"]),
                "ai_prompt": item["ai_prompt"] if "ai_prompt" in item.keys() else "",
                "analysis_structure": _parse_structure(item["analysis_structure"] if "analysis_structure" in item.keys() else "{}"),
                "source": item["source"],
                "note": item["note"],
                "created_at": item["created_at"],
            }
            for item in connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC",
                (strategy_id,),
            )
        ]
        payload = _row_strategy(row, latest)
        payload["versions"] = versions
        return payload
    finally:
        connection.close()


def create_strategy(
    name: str,
    description: str,
    params: Dict[str, Any],
    *,
    slug: str = "",
    source: str = "manual",
    note: str = "",
    ai_prompt: str = "",
    analysis_structure: Optional[Dict[str, Any]] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("strategy name required")
    slug = (slug or _slugify(name)).strip()
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO strategies(slug, name, description) VALUES(?,?,?)",
            (slug, name, description or ""),
        )
        strategy_id = int(cursor.lastrowid)
        add_version(
            strategy_id, params, source=source, note=note,
            ai_prompt=ai_prompt, analysis_structure=analysis_structure,
            connection=connection,
        )
        if owned:
            connection.commit()
        return get_strategy(strategy_id) if owned else {"id": strategy_id, "slug": slug}
    finally:
        if owned:
            connection.close()


def add_version(
    strategy_id: int,
    params: Dict[str, Any],
    *,
    source: str = "manual",
    note: str = "",
    ai_prompt: str = "",
    analysis_structure: Optional[Dict[str, Any]] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    if source not in {"manual", "seed", "llm", "backtest"}:
        source = "manual"
    owned = connection is None
    connection = connection or _connect()
    try:
        exists = connection.execute("SELECT id FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if exists is None:
            raise KeyError(f"strategy {strategy_id} not found")
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM strategy_versions WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()[0]
        version = int(current) + 1
        clamped = clamp_params(params)
        structure = analysis_structure if isinstance(analysis_structure, dict) else {}
        prompt = str(ai_prompt or "").strip()[:12000]
        cursor = connection.execute(
            """INSERT INTO strategy_versions
               (strategy_id, version, params, ai_prompt, analysis_structure, source, note)
               VALUES(?,?,?,?,?,?,?)""",
            (
                strategy_id, version, json.dumps(clamped, ensure_ascii=False),
                prompt, json.dumps(structure, ensure_ascii=False), source, note or "",
            ),
        )
        connection.execute(
            "UPDATE strategies SET updated_at=datetime('now') WHERE id=?",
            (strategy_id,),
        )
        if owned:
            connection.commit()
        return {
            "id": int(cursor.lastrowid),
            "strategy_id": strategy_id,
            "version": version,
            "params": clamped,
            "ai_prompt": prompt,
            "analysis_structure": structure,
            "source": source,
            "note": note or "",
        }
    finally:
        if owned:
            connection.close()


def get_version(version_id: int) -> Dict[str, Any]:
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(f"strategy version {version_id} not found")
        payload = dict(row)
        payload["params"] = _parse_params(row["params"])
        payload["analysis_structure"] = _parse_structure(row["analysis_structure"] if "analysis_structure" in row.keys() else "{}")
        payload["ai_prompt"] = row["ai_prompt"] if "ai_prompt" in row.keys() else ""
        return payload
    finally:
        connection.close()


def get_current_strategy(connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    owned = connection is None
    connection = connection or _connect()
    try:
        seed_if_empty(connection)
        row = connection.execute(
            "SELECT * FROM strategies WHERE is_current=1 AND status='active' LIMIT 1"
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM strategies WHERE status='active' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is not None:
                connection.execute("UPDATE strategies SET is_current=0, current_version_id=NULL")
        if row is None:
            raise KeyError("no active strategy")
        version = None
        if row["current_version_id"]:
            version = connection.execute(
                "SELECT * FROM strategy_versions WHERE id=? AND strategy_id=?",
                (row["current_version_id"], row["id"]),
            ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
        payload = _row_strategy(row, version)
        payload["active_version"] = payload.pop("latest_version")
        return payload
    finally:
        if owned:
            connection.close()


def set_current_strategy(strategy_id: int, version_id: Optional[int] = None,
                         connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    owned = connection is None
    connection = connection or _connect()
    try:
        row = connection.execute(
            "SELECT * FROM strategies WHERE id=? AND status='active'", (strategy_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"active strategy {strategy_id} not found")
        if version_id is None:
            version = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC LIMIT 1",
                (strategy_id,),
            ).fetchone()
        else:
            version = connection.execute(
                "SELECT * FROM strategy_versions WHERE id=? AND strategy_id=?",
                (version_id, strategy_id),
            ).fetchone()
        if version is None:
            raise KeyError("strategy version not found")
        connection.execute("UPDATE strategies SET is_current=0, current_version_id=NULL")
        connection.execute(
            "UPDATE strategies SET is_current=1, current_version_id=?, updated_at=datetime('now') WHERE id=?",
            (version["id"], strategy_id),
        )
        if owned:
            connection.commit()
        return get_current_strategy(connection)
    finally:
        if owned:
            connection.close()


def archive_strategy(strategy_id: int) -> None:
    connection = _connect()
    try:
        cursor = connection.execute(
            "UPDATE strategies SET status='archived', updated_at=datetime('now') WHERE id=?",
            (strategy_id,),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"strategy {strategy_id} not found")
        connection.commit()
    finally:
        connection.close()


def save_backtest_run(
    strategy_id: int,
    version_id: int,
    params: Dict[str, Any],
    result: Dict[str, Any],
) -> int:
    connection = _connect()
    try:
        cursor = connection.execute(
            """INSERT INTO backtest_runs(strategy_id, version_id, params, result, sample_size, taken, winrate, total_pnl_usdt)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                strategy_id,
                version_id,
                json.dumps(params, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                int(result.get("sample_size") or 0),
                int(result.get("taken") or 0),
                result.get("winrate"),
                result.get("total_pnl_usdt"),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def list_backtest_runs(strategy_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    connection = _connect()
    try:
        if strategy_id is None:
            rows = connection.execute(
                "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM backtest_runs WHERE strategy_id=? ORDER BY id DESC LIMIT ?",
                (strategy_id, max(1, min(limit, 100))),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["params"] = json.loads(row["params"] or "{}")
            except json.JSONDecodeError:
                item["params"] = {}
            try:
                item["result"] = json.loads(row["result"] or "{}")
            except json.JSONDecodeError:
                item["result"] = {}
            out.append(item)
        return out
    finally:
        connection.close()


def _slugify(name: str) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw[:48] or "strategy"
