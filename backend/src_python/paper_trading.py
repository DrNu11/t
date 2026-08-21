"""模拟操盘启停、赛道、策略与 Agent 运行快照。"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import db
import strategy_store

TRACK_ASSETS = {
    "crypto": {"BTC", "ETH", "SOL", "CRYPTO"},
    "gold": {"XAU", "GOLD"},
    "oil": {"WTI", "OIL"},
}
AGENT_VERSION = "trident-evidence-agent-v1"
_RULE_FILES = (
    "AGENTS.md",
    os.path.join("docs", "约定规范.md"),
    os.path.join("docs", "方案.md"),
    os.path.join("docs", "防守与陷阱.md"),
    os.path.join("docs", "新闻.md"),
)


def _spec_version() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    digest = hashlib.sha256()
    for relative in _RULE_FILES:
        path = os.path.join(root, relative)
        with open(path, "rb") as stream:
            digest.update(relative.encode("utf-8"))
            digest.update(stream.read())
    return digest.hexdigest()[:16]


def _run_payload(conn: sqlite3.Connection, run_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    row = conn.execute(
        """SELECT pr.*, s.name AS strategy_name, sv.version AS strategy_version
           FROM paper_trading_runs pr
           JOIN strategies s ON s.id=pr.strategy_id
           JOIN strategy_versions sv ON sv.id=pr.strategy_version_id
           WHERE pr.id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    columns = (
        "id", "tracks", "strategy_id", "strategy_version_id", "agent_version",
        "model_id", "spec_version", "activation_reason", "status", "started_at",
        "stopped_at", "strategy_name", "strategy_version",
    )
    payload = dict(zip(columns, row))
    payload["tracks"] = json.loads(payload["tracks"] or "[]")
    return payload


def get_settings(connection=None) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT is_running, tracks, started_at, active_run_id, updated_at, gate_enabled FROM paper_trading_settings WHERE id=1"
        ).fetchone()
        if row is None:
            conn.execute("INSERT INTO paper_trading_settings (id) VALUES (1)")
            if own:
                conn.commit()
            return {
                "is_running": False, "tracks": [], "started_at": None,
                "active_run_id": None, "active_run": None, "gate_enabled": True,
                "updated_at": None,
            }
        return {
            "is_running": bool(row[0]),
            "tracks": json.loads(row[1] or "[]"),
            "started_at": row[2],
            "active_run_id": row[3],
            "active_run": _run_payload(conn, row[3]),
            "updated_at": row[4],
            "gate_enabled": bool(row[5] if row[5] is not None else 1),
        }
    finally:
        if own:
            conn.close()


def set_settings(
    is_running: bool,
    tracks: List[str],
    connection=None,
    *,
    gate_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    normalized = sorted({track for track in tracks if track in TRACK_ASSETS})
    if is_running and not normalized:
        raise ValueError("开始模拟操盘前必须至少选择一个投资赛道")
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        previous = get_settings(conn)
        active_run_id = previous["active_run_id"]
        started_at: Optional[str] = previous["started_at"]
        next_gate = previous.get("gate_enabled", True) if gate_enabled is None else bool(gate_enabled)
        if is_running and not previous["is_running"]:
            current = strategy_store.get_current_strategy(conn)
            activated = strategy_store.set_current_strategy(
                int(current["id"]), int(current["active_version"]["id"]), conn
            )
            version = activated["active_version"]
            started_at = now
            cursor = conn.execute(
                """INSERT INTO paper_trading_runs
                   (tracks, strategy_id, strategy_version_id, agent_version,
                    model_id, spec_version, activation_reason, started_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    json.dumps(normalized), activated["id"], version["id"],
                    AGENT_VERSION, config.get_selected_ai_model_id(), _spec_version(),
                    "开始模拟操盘：自动激活当前策略版本；"
                    + ("宽松模式关闭证据/策略闸门" if not next_gate else "启用多证据、卡方、反幻觉和赛道闸门"),
                    now,
                ),
            )
            active_run_id = int(cursor.lastrowid)
        elif not is_running and previous["is_running"] and active_run_id:
            conn.execute(
                "UPDATE paper_trading_runs SET status='STOPPED', stopped_at=? WHERE id=? AND status='RUNNING'",
                (now, active_run_id),
            )
            active_run_id = None
        conn.execute(
            """UPDATE paper_trading_settings
               SET is_running=?, tracks=?, started_at=?, active_run_id=?, gate_enabled=?, updated_at=? WHERE id=1""",
            (int(is_running), json.dumps(normalized), started_at, active_run_id, int(next_gate), now),
        )
        if own:
            conn.commit()
        return get_settings(conn)
    finally:
        if own:
            conn.close()


def asset_allowed(asset: str, settings: Dict[str, Any]) -> bool:
    value = asset.upper()
    return any(value in TRACK_ASSETS.get(track, set()) for track in settings.get("tracks", []))
