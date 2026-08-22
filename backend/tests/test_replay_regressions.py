"""Replay statistics/reflection regression tests (offline SQLite)."""

import asyncio

import api_server
import paper_trading


PASSED = "证据充分，允许输出方向性结论"


def _start_run(conn):
    return paper_trading.set_settings(
        True, ["crypto", "gold", "oil"], conn, gate_enabled=True
    )["active_run"]


def _insert_trade(
    conn,
    run,
    *,
    asset="BTC",
    action="BUY",
    pnl=None,
    settled=1,
    recorded="HOLD",
    quality="verified",
    entry_time="2026-08-15T10:00:00+08:00",
):
    news_id = conn.execute(
        """INSERT INTO raw_news
               (source, content, timestamp, status, quality_status)
           VALUES ('test', ?, '2026-08-15T09:59:00+08:00', 'DONE', ?)""",
        (f"{asset} {action}", quality),
    ).lastrowid
    decision_id = conn.execute(
        """INSERT INTO ai_decisions
               (news_id, sentiment_score, suggested_action, reasoning,
                market_category, target_asset, entry_price, entry_time,
                settled, is_correct, forward_pnl, mfe_pct, mae_pct,
                paper_trading_run_id, strategy_id, strategy_version_id,
                evidence_action, evidence_confidence, trade_gate_reason)
           VALUES (?, ?, ?, 'test', 'CRYPTO', ?, 100.0, ?, ?, ?, ?, 0.8, 0.4,
                   ?, ?, ?, ?, 90.0, ?)""",
        (
            news_id,
            0.8 if action == "BUY" else -0.8,
            action,
            asset,
            entry_time,
            settled,
            recorded,
            pnl,
            run["id"],
            run["strategy_id"],
            run["strategy_version_id"],
            action,
            PASSED,
        ),
    ).lastrowid
    conn.commit()
    return decision_id


def test_stats_and_reflection_derive_legacy_verdicts_without_research_pollution(
    temp_db, monkeypatch
):
    monkeypatch.setattr(api_server, "DB_PATH", api_server.config.DB_PATH)
    run = _start_run(temp_db)
    win_id = _insert_trade(temp_db, run, action="BUY", pnl=0.5)
    loss_id = _insert_trade(temp_db, run, asset="ETH", action="SELL", pnl=-0.4)
    _insert_trade(temp_db, run, asset="SOL", action="BUY", pnl=0.6, quality="unverified")
    _insert_trade(temp_db, run, asset="XAU", action="BUY", settled=0, recorded="", pnl=None)
    _insert_trade(
        temp_db, run, asset="WTI", action="SELL", settled=0,
        recorded="", pnl=None, entry_time="",
    )
    _insert_trade(
        temp_db, run, asset="DOGE", action="BUY", settled=0,
        recorded="", pnl=None, entry_time="", quality="unverified",
    )

    stats = asyncio.run(api_server.get_replay_stats())
    assert stats["overall"]["settled"] == 2
    assert stats["overall"]["wins"] == 1
    assert stats["overall"]["losses"] == 1
    assert stats["overall"]["holds"] == 0
    assert stats["overall"]["tracking"] == 1
    assert stats["overall"]["legacy_untrackable"] == 1
    assert stats["research_excluded"]["settled"] == 1
    assert stats["research_excluded"]["wins"] == 1
    assert stats["research_excluded"]["legacy_untrackable"] == 1

    signals = asyncio.run(api_server.get_replay_signals(settled=1, limit=20))
    by_id = {item["id"]: item for item in signals}
    assert by_id[win_id]["recorded_is_correct"] == "HOLD"
    assert by_id[win_id]["is_correct"] == "WIN"
    assert by_id[loss_id]["is_correct"] == "LOSS"

    reflection = asyncio.run(api_server.get_replay_reflection(limit=20))
    assert reflection["sample"] == 2
    assert reflection["wins"] == 1
    assert reflection["losses"] == 1
    assert reflection["research_excluded"] == {
        "sample": 1, "wins": 1, "losses": 0,
    }


def test_legacy_untrackable_position_has_no_live_pnl_or_active_status():
    position = api_server._paper_metrics(
        {
            "entry_price": 100.0,
            "action": "BUY",
            "settled": 0,
            "entry_time": "",
            "tracking_quality": "LEGACY_UNTRACKABLE",
            "strategy_params": {"notional_usdt": 100.0, "leverage": 2.0},
        },
        150.0,
    )
    assert position["paper_status"] == "LEGACY_UNTRACKABLE"
    assert position["current_price"] is None
    assert position["current_pnl_pct"] is None
    assert position["current_pnl_usdt"] is None


def test_research_position_has_no_live_pnl_or_active_status():
    position = api_server._paper_metrics(
        {
            "entry_price": 100.0,
            "action": "BUY",
            "settled": 0,
            "entry_time": "2026-08-15T10:00:00+08:00",
            "tracking_quality": "RESEARCH_EXCLUDED",
            "strategy_params": {"notional_usdt": 100.0, "leverage": 2.0},
        },
        150.0,
    )
    assert position["paper_status"] == "RESEARCH_EXCLUDED"
    assert position["current_price"] is None
    assert position["current_pnl_pct"] is None
    assert position["current_pnl_usdt"] is None
