#!/usr/bin/env python3
"""内置 Hermes 驱动：多 agent 写作 + 有价值数据沉淀。

对齐 NousResearch/hermes-agent 的学习闭环（观察 → 技能 → 回灌），
以及 TradingAgents 的分席写作（新闻/宏观/风险/交易员），但不改主模型 14 字段 prompt。

铁律：
  - L0 只存原文、行情、已结算统计，不把未核验的 LLM 结论当事实
  - 技能只从 settled WIN/LOSS 刷新，禁止伪造胜负
  - schema 在 db.py，本模块只读写
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence

import config
import db

_SKILL_MIN_SAMPLE = int(getattr(config, "HERMES_SKILL_MIN_SAMPLE", 8))
_PASSED_TRADE_GATE_REASON = "证据充分，允许输出方向性结论"
_OBS_CONTENT_LIMIT = 400
_BRIEF_LIMIT = 160
_DESKS = ("news", "macro", "risk", "trader")

_GEOPOLITICS = ("战争", "开战", "空袭", "制裁", "导弹", "冲突", "地缘", "war", "sanction", "missile")
_RATES = ("降息", "加息", "利率", "央行", "cpi", "fomc", "鲍威尔", "流动性", "rate", "fed")
_ENERGY = ("原油", "opec", "库存", "产油", "wti", "brent", "石油")
_CRYPTO = ("比特币", "btc", "eth", "etf", "交易所", "稳定币", "hack", "监管")
_RISK_OFF = ("避险", "恐慌", "暴跌", "挤兑", "违约", "破产", "risk-off", "crash")


def _now() -> int:
    return int(time.time())


def _clip(text: Any, limit: int = _OBS_CONTENT_LIMIT) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _owned_connection(connection: Optional[sqlite3.Connection]):
    if connection is not None:
        return connection, False
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn, True


def classify_lane(text: str) -> str:
    blob = str(text or "").lower()
    if any(token in blob for token in _GEOPOLITICS):
        return "gold"
    if any(token in blob for token in _ENERGY):
        return "oil"
    if any(token in blob for token in _RATES + _CRYPTO):
        return "crypto"
    return "macro"


def _hits(text: str, tokens: Sequence[str]) -> List[str]:
    blob = str(text or "").lower()
    return [token for token in tokens if token in blob]


def write_desks(news_text: str, market_context: str = "") -> List[Dict[str, Any]]:
    """分席写作：确定性简报，不另打 LLM，不改主决策语义。"""
    text = _clip(news_text, 800)
    lane = classify_lane(text)
    geo = _hits(text, _GEOPOLITICS)
    rates = _hits(text, _RATES)
    energy = _hits(text, _ENERGY)
    crypto = _hits(text, _CRYPTO)
    stress = _hits(text, _RISK_OFF)
    market = _clip(market_context, 180)
    return [
        {
            "desk": "news",
            "title": "新闻席",
            "brief": _clip(
                f"原文要点：{text or '空新闻'}。"
                f"{'命中地缘词 ' + '/'.join(geo) + '。' if geo else ''}"
                f"{'命中利率词 ' + '/'.join(rates) + '。' if rates else ''}"
                f"{'命中能源词 ' + '/'.join(energy) + '。' if energy else ''}"
                f"{'命中加密词 ' + '/'.join(crypto) + '。' if crypto else ''}",
                _BRIEF_LIMIT,
            ),
        },
        {
            "desk": "macro",
            "title": "宏观席",
            "brief": _clip(
                f"赛道归类 {lane}。"
                f"{'利率/流动性叙事占主导。' if rates else ''}"
                f"{'地缘冲击优先映射黄金水位。' if geo else ''}"
                f"{'能源供给扰动优先映射原油。' if energy else ''}"
                f"{'加密行业事件按高 Beta 风险资产处理。' if crypto and not rates else ''}"
                f"{'未命中强主题，保持宏观观察。' if not (geo or rates or energy or crypto) else ''}",
                _BRIEF_LIMIT,
            ),
        },
        {
            "desk": "risk",
            "title": "风险席",
            "brief": _clip(
                f"{'风险偏好承压：' + '/'.join(stress) + '。' if stress else '未见明确风险挤兑词。'}"
                f"{'战争/制裁场景下 BTC 按风险资产而非避险处理。' if geo else ''}"
                f"{'行情校验：' + market if market else '本轮无可用行情快照。'}",
                _BRIEF_LIMIT,
            ),
        },
        {
            "desk": "trader",
            "title": "交易席",
            "brief": _clip(
                "写作席只提供上下文，不下最终单。"
                "最终 BUY/SELL/HOLD 仍由主模型 14 字段 + 校验层决定。",
                _BRIEF_LIMIT,
            ),
        },
    ]


def render_writing_context(desks: Sequence[Dict[str, Any]], skills: Sequence[Dict[str, Any]] = ()) -> str:
    lines = ["[Hermes Multi-Agent Writing]"]
    for desk in desks:
        title = desk.get("title") or desk.get("desk") or "desk"
        brief = _clip(desk.get("brief"), _BRIEF_LIMIT)
        if brief:
            lines.append(f"{title}: {brief}")
    if skills:
        lines.append("[Hermes Settled Skills]")
        for skill in skills[:6]:
            wr = skill.get("win_rate")
            wr_text = f"{wr:.0%}" if isinstance(wr, (int, float)) else "n/a"
            pnl = skill.get("avg_pnl")
            pnl_text = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "N/A"
            lines.append(
                f"{skill.get('asset')} {skill.get('prediction_type') or '*'} {skill.get('action')}: "
                f"sample {skill.get('sample_size')} win {wr_text} avg pnl {pnl_text}"
            )
    return "\n".join(lines)


def record_observation(
    news_id: Optional[int],
    source: str,
    content: str,
    *,
    asset: str = "NONE",
    observation_type: str = "news",
    decision_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    ts: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    conn, owned = _owned_connection(connection)
    try:
        if not _table_exists(conn, "hermes_observations"):
            return 0
        clipped = _clip(content)
        if not clipped:
            return 0
        cur = conn.execute(
            """INSERT INTO hermes_observations
                   (news_id, decision_id, ts, source, asset, observation_type, content, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                news_id,
                decision_id,
                int(ts or _now()),
                str(source or ""),
                str(asset or "NONE").upper(),
                observation_type,
                clipped,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        if owned:
            conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        if owned:
            conn.close()


def persist_news_writing(
    news_id: int,
    news_text: str,
    *,
    source: str = "",
    market_context: str = "",
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    desks = write_desks(news_text, market_context)
    conn, owned = _owned_connection(connection)
    try:
        record_observation(
            news_id,
            source or "news",
            news_text,
            observation_type="news",
            payload={"lane": classify_lane(news_text)},
            connection=conn,
        )
        if market_context.strip():
            record_observation(
                news_id,
                "market_snapshot",
                market_context,
                observation_type="market",
                connection=conn,
            )
        for desk in desks:
            record_observation(
                news_id,
                f"desk:{desk['desk']}",
                desk["brief"],
                observation_type="desk",
                payload={"desk": desk["desk"], "title": desk["title"]},
                connection=conn,
            )
        if owned:
            conn.commit()
        return {"desks": desks, "writing_context": render_writing_context(desks)}
    finally:
        if owned:
            conn.close()


def persist_decision_observation(
    news_id: int,
    decision_id: int,
    *,
    asset: str,
    action: str,
    score: float,
    source: str = "",
    extras: Optional[Dict[str, Any]] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    payload = {
        "action": action,
        "score": score,
        **(extras or {}),
    }
    return record_observation(
        news_id,
        source or "ai_decision",
        f"{asset} {action} score={score}",
        asset=asset,
        observation_type="decision",
        decision_id=decision_id,
        payload=payload,
        connection=connection,
    )


def refresh_skills(connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """仅从正式模拟盘已结算 WIN/LOSS 刷新技能。"""
    conn, owned = _owned_connection(connection)
    try:
        if not _table_exists(conn, "hermes_skills"):
            return []
        if not _table_exists(conn, "ai_decisions") or not _table_exists(conn, "raw_news"):
            conn.execute("DELETE FROM hermes_skills")
            if owned:
                conn.commit()
            return []
        rows = conn.execute(
            """SELECT UPPER(ad.target_asset) AS asset,
                      UPPER(ad.suggested_action) AS action,
                      COALESCE(ad.prediction_type, '') AS prediction_type,
                      COUNT(*) AS sample_size,
                      SUM(CASE WHEN UPPER(ad.is_correct)='WIN' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN UPPER(ad.is_correct)='LOSS' THEN 1 ELSE 0 END) AS losses,
                      AVG(ad.forward_pnl) AS avg_pnl
               FROM ai_decisions ad
               INNER JOIN raw_news rn ON rn.id = ad.news_id
               WHERE ad.settled=1
                 AND UPPER(ad.is_correct) IN ('WIN','LOSS')
                 AND ad.forward_pnl IS NOT NULL
                 AND UPPER(ad.suggested_action) IN ('BUY','SELL')
                 AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
                 AND LOWER(COALESCE(rn.quality_status, '')) = 'verified'
                 AND UPPER(COALESCE(ad.evidence_action, 'HOLD')) = UPPER(ad.suggested_action)
                 AND COALESCE(ad.trade_gate_reason, '') = ?
                 AND ad.paper_trading_run_id IS NOT NULL
               GROUP BY 1, 2, 3""",
            (_PASSED_TRADE_GATE_REASON,),
        ).fetchall()
        written: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            sample = int(payload.get("sample_size") or 0)
            if sample < _SKILL_MIN_SAMPLE:
                continue
            wins = int(payload.get("wins") or 0)
            losses = int(payload.get("losses") or 0)
            decided = wins + losses
            if decided < _SKILL_MIN_SAMPLE:
                continue
            win_rate = wins / decided
            avg_pnl = payload.get("avg_pnl")
            asset = str(payload.get("asset") or "")
            action = str(payload.get("action") or "")
            pred = str(payload.get("prediction_type") or "")
            skill_key = f"{asset}|{action}|{pred or '*'}"
            note = f"已结算 {decided} 条，胜 {wins} 条，仅作研究参考"
            conn.execute(
                """INSERT INTO hermes_skills
                       (skill_key, asset, action, prediction_type, sample_size, wins, losses,
                        win_rate, avg_pnl, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(skill_key) DO UPDATE SET
                       sample_size=excluded.sample_size,
                       wins=excluded.wins,
                       losses=excluded.losses,
                       win_rate=excluded.win_rate,
                       avg_pnl=excluded.avg_pnl,
                       note=excluded.note,
                       updated_at=excluded.updated_at""",
                (
                    skill_key, asset, action, pred, decided, wins, losses,
                    win_rate, avg_pnl, note,
                ),
            )
            written.append({
                "skill_key": skill_key,
                "asset": asset,
                "action": action,
                "prediction_type": pred,
                "sample_size": decided,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl,
                "note": note,
            })
        # hermes_skills has no per-sample provenance.  Remove groups that no
        # longer meet the strict production gate so legacy research-derived
        # skills cannot survive a refresh and leak back into prompts.
        skill_keys = [item["skill_key"] for item in written]
        if skill_keys:
            placeholders = ",".join("?" for _ in skill_keys)
            conn.execute(
                f"DELETE FROM hermes_skills WHERE skill_key NOT IN ({placeholders})",
                skill_keys,
            )
        else:
            conn.execute("DELETE FROM hermes_skills")
        if owned:
            conn.commit()
        return written
    finally:
        if owned:
            conn.close()


def load_skills(connection: Optional[sqlite3.Connection] = None,
                limit: int = 8) -> List[Dict[str, Any]]:
    conn, owned = _owned_connection(connection)
    try:
        if not _table_exists(conn, "hermes_skills"):
            return []
        rows = conn.execute(
            """SELECT skill_key, asset, action, prediction_type, sample_size,
                      wins, losses, win_rate, avg_pnl, note
               FROM hermes_skills
               ORDER BY sample_size DESC, win_rate DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            conn.close()


def build_prompt_context(news_text: str, market_context: str = "",
                         connection: Optional[sqlite3.Connection] = None) -> str:
    desks = write_desks(news_text, market_context)
    skills = load_skills(connection=connection)
    return render_writing_context(desks, skills)


def recent_observations(limit: int = 20,
                        connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    conn, owned = _owned_connection(connection)
    try:
        if not _table_exists(conn, "hermes_observations"):
            return []
        rows = conn.execute(
            """SELECT id, news_id, decision_id, ts, source, asset, observation_type, content
               FROM hermes_observations
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            conn.close()
