"""快速模拟交易评测通道。

把 ai_decisions 里通过来源、证据和策略闸门的方向性信号按真实行情建仓，经过
QUICK_SIM_HORIZON_MINUTES 后按真实行情结算，用于盈利率评测。

与正式模拟盘的区别：
- 只评测已进入正式严格模拟盘的信号，宽松/研究样本不污染主统计；
- 持仓周期短（默认 5 分钟），只服务于统计评测，不写 ai_decisions。

盈亏一律来自真实行情，取不到价格就不建仓、不结算，绝不补造数据。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional

import config
import db
from engine.forward import classify_directional_outcome

_PASSED_REASON = "证据充分，允许输出方向性结论"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = previous


def pending_signals(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """尚未进入快速模拟的方向性信号。"""
    return _rows(
        conn,
        """SELECT ad.id, ad.news_id, UPPER(ad.target_asset) AS asset,
                  UPPER(ad.suggested_action) AS action, ad.sentiment_score AS score,
                  ad.evidence_confidence, ad.trade_gate_reason, ad.strategy_id,
                  ad.strategy_version_id, ad.paper_trading_run_id
           FROM ai_decisions ad
           INNER JOIN raw_news rn ON rn.id = ad.news_id
           INNER JOIN paper_trading_runs pr
                   ON pr.id = ad.paper_trading_run_id AND pr.status = 'RUNNING'
           INNER JOIN paper_trading_settings pts
                   ON pts.id = 1 AND pts.is_running = 1
                  AND pts.active_run_id = pr.id
           LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
           WHERE qs.id IS NULL
             AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
             AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
             AND LOWER(COALESCE(rn.quality_status, '')) = 'verified'
             AND UPPER(COALESCE(ad.evidence_action, 'HOLD')) = UPPER(ad.suggested_action)
             AND COALESCE(ad.trade_gate_reason, '') = ?
             AND ad.entry_price IS NOT NULL AND ad.entry_price > 0
           ORDER BY ad.id DESC LIMIT ?""",
        (_PASSED_REASON, limit),
    )


def open_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """给未建仓的方向性信号按真实行情建仓，返回新建仓明细。"""
    horizon = config.QUICK_SIM_HORIZON_MINUTES
    notional = config.QUICK_SIM_NOTIONAL_USDT
    created: List[Dict[str, Any]] = []
    for signal in pending_signals(conn, limit):
        price = price_fn(signal["asset"])
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        reason = signal["trade_gate_reason"] or ""
        cursor = conn.execute(
            """INSERT OR IGNORE INTO quick_sim_trades
               (decision_id, news_id, asset, action, score, evidence_confidence,
                gate_passed, gate_reason, strategy_id, strategy_version_id,
                paper_trading_run_id, notional_usdt, entry_price, entry_ts, horizon_minutes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["id"], signal["news_id"], signal["asset"], signal["action"],
                signal["score"], signal["evidence_confidence"],
                int(reason == _PASSED_REASON), reason,
                signal["strategy_id"], signal["strategy_version_id"],
                signal["paper_trading_run_id"], notional,
                float(price), int(time.time()), horizon,
            ),
        )
        if cursor.rowcount:
            created.append({
                "decision_id": signal["id"], "asset": signal["asset"],
                "action": signal["action"], "entry_price": float(price),
            })
    conn.commit()
    return created


def settle_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> List[Dict[str, Any]]:
    """到期的快速模拟仓位按真实行情结算，返回结算明细。"""
    now = int(time.time())
    due = _rows(
        conn,
        """SELECT id, asset, action, entry_price, entry_ts, horizon_minutes, notional_usdt
           FROM quick_sim_trades
           WHERE settled = 0 AND entry_ts + horizon_minutes * 60 <= ?""",
        (now,),
    )
    settled: List[Dict[str, Any]] = []
    for trade in due:
        price = price_fn(trade["asset"])
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        entry = float(trade["entry_price"])
        exit_price = float(price)
        pnl_pct = (exit_price - entry) / entry * 100
        if trade["action"] == "SELL":
            pnl_pct = -pnl_pct
        pnl_usdt = float(trade["notional_usdt"]) * pnl_pct / 100
        verdict = classify_directional_outcome(
            trade["action"], pnl_pct, trade["asset"]
        )
        conn.execute(
            """UPDATE quick_sim_trades
               SET exit_price=?, exit_ts=?, pnl_pct=?, pnl_usdt=?, verdict=?, settled=1
               WHERE id=?""",
            (round(exit_price, 6), now, round(pnl_pct, 6), round(pnl_usdt, 6),
             verdict, trade["id"]),
        )
        settled.append({
            "id": trade["id"], "asset": trade["asset"], "action": trade["action"],
            "entry_price": entry, "exit_price": exit_price,
            "pnl_pct": round(pnl_pct, 4), "verdict": verdict,
        })
    conn.commit()
    return settled


def _profitability(rows: List[Dict[str, Any]], notional: float) -> Dict[str, Any]:
    wins = sum(row["verdict"] == "WIN" for row in rows)
    losses = sum(row["verdict"] == "LOSS" for row in rows)
    decided = wins + losses
    total_pnl_pct = sum(float(row["pnl_pct"] or 0.0) for row in rows)
    total_pnl_usdt = sum(float(row["pnl_usdt"] or 0.0) for row in rows)
    invested = sum(float(row["notional_usdt"] or 0.0) for row in rows) or notional * len(rows)
    return {
        "settled": len(rows),
        "wins": wins,
        "losses": losses,
        "breakeven": len(rows) - decided,
        "winrate_pct": round(wins / decided * 100, 2) if decided else None,
        "avg_pnl_pct": round(total_pnl_pct / len(rows), 4) if rows else None,
        "total_pnl_pct": round(total_pnl_pct, 4),
        "total_pnl_usdt": round(total_pnl_usdt, 4),
        "return_on_notional_pct": round(total_pnl_usdt / invested * 100, 4) if invested else None,
        "profitable": total_pnl_usdt > 0 if rows else None,
    }


def evaluation(conn: sqlite3.Connection, recent_limit: int = 20) -> Dict[str, Any]:
    """快速模拟交易的盈利率评测，全部来自真实结算记录。"""
    settled_rows = _rows(
        conn,
        """SELECT asset, action, gate_passed, verdict, pnl_pct, pnl_usdt, notional_usdt
           FROM quick_sim_trades WHERE settled = 1""",
    )
    production_rows = [row for row in settled_rows if row["gate_passed"]]
    research_rows = [row for row in settled_rows if not row["gate_passed"]]
    open_rows = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, entry_ts, horizon_minutes,
                  gate_passed, gate_reason
           FROM quick_sim_trades
           WHERE settled = 0 AND gate_passed = 1 ORDER BY entry_ts DESC""",
    )
    recent = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, exit_price, pnl_pct,
                  pnl_usdt, verdict, gate_passed, gate_reason, entry_ts, exit_ts, settled
           FROM quick_sim_trades WHERE gate_passed = 1 ORDER BY id DESC LIMIT ?""",
        (recent_limit,),
    )
    notional = config.QUICK_SIM_NOTIONAL_USDT
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for row in production_rows:
        by_asset.setdefault(row["asset"], []).append(row)
    return {
        "enabled": config.QUICK_SIM_ENABLED,
        "horizon_minutes": config.QUICK_SIM_HORIZON_MINUTES,
        "notional_usdt": notional,
        "open_trades": len(open_rows),
        "overall": _profitability(production_rows, notional),
        "gate_passed": _profitability(production_rows, notional),
        # Backward-compatible diagnostic only. These legacy research samples
        # are intentionally excluded from overall/by_asset/recent.
        "gate_rejected": _profitability(research_rows, notional),
        "research_excluded": len(research_rows),
        "by_asset": [
            {"asset": asset, **_profitability(rows, notional)}
            for asset, rows in sorted(by_asset.items())
        ],
        "open": open_rows[:recent_limit],
        "recent": recent,
        "method": "仅对来源可决策且证据/策略闸门通过的 BUY/SELL 信号按真实行情建仓结算",
    }


def run_cycle(price_fn: Callable[[str], Optional[float]]) -> Dict[str, Any]:
    """一次完整周期：建仓 + 结算，返回本轮变化。"""
    if not config.QUICK_SIM_ENABLED:
        return {"opened": [], "settled": []}
    conn = db.get_connection()
    try:
        opened = open_trades(conn, price_fn)
        settled = settle_trades(conn, price_fn)
        return {"opened": opened, "settled": settled}
    finally:
        conn.close()


def snapshot() -> Dict[str, Any]:
    """给 API 用的评测快照。"""
    conn = db.get_connection()
    try:
        return evaluation(conn)
    finally:
        conn.close()
