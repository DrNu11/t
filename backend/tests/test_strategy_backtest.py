"""Strategy library + honest backtest on settled trades only."""

import backtest
import strategy_store

PASSED_REASON = "证据充分，允许输出方向性结论"


def _insert_trade(
    db,
    *,
    action,
    asset,
    score,
    pnl,
    strength="medium",
    catalyst=1,
    settled=1,
    quality_status="verified",
    evidence_action=None,
    gate_reason=PASSED_REASON,
    paper_run_id=1,
):
    news_id = db.execute(
        """INSERT INTO raw_news(source,content,timestamp,status,quality_status)
           VALUES('t','n','2026-01-01','DONE',?)""",
        (quality_status,),
    ).lastrowid
    db.execute(
        """INSERT INTO ai_decisions(news_id,sentiment_score,suggested_action,reasoning,target_asset,
           entry_price,exit_price,forward_pnl,settled,is_correct,event_strength,direct_catalyst,entry_time,
           evidence_action,trade_gate_reason,paper_trading_run_id)
           VALUES(?,?,?, 'r', ?, 100, ?, ?, ?, ?, ?, ?, '2026-01-01 00:00:00', ?, ?, ?)""",
        (
            news_id, score, action, asset, 100 + pnl, pnl, settled,
            "WIN" if pnl > 0 else "LOSS", strength, catalyst,
            evidence_action or action, gate_reason, paper_run_id,
        ),
    )
    db.commit()


def test_seed_and_clamp(temp_db):
    items = strategy_store.list_strategies()
    slugs = {item["slug"] for item in items}
    assert {"news-threshold", "conservative", "aggressive"} <= slugs
    params = strategy_store.clamp_params({
        "signal_threshold": 9, "notional_usdt": -1, "leverage": 99,
        "asset_filter": "DOGE", "min_event_strength": "ultra",
    })
    assert params["signal_threshold"] == 0.9
    assert params["notional_usdt"] == 5.0
    assert params["leverage"] == 20
    assert params["asset_filter"] == ""
    assert params["min_event_strength"] == ""


def test_backtest_uses_real_forward_pnl(temp_db):
    _insert_trade(temp_db, action="BUY", asset="BTC", score=0.8, pnl=2.0)
    _insert_trade(temp_db, action="SELL", asset="ETH", score=0.2, pnl=-1.0)
    _insert_trade(temp_db, action="BUY", asset="BTC", score=0.9, pnl=1.0, strength="weak")
    params = {
        **strategy_store.default_params(),
        "signal_threshold": 0.5,
        "notional_usdt": 100,
        "leverage": 2,
        "min_event_strength": "medium",
    }
    report = backtest.run_backtest(params, connection=temp_db)
    assert report["honest"] is True
    assert report["sample_size"] == 3
    assert report["taken"] == 1
    assert report["skipped"] == 2
    assert report["total_pnl_usdt"] == 4.0  # 100 * 2 * 2%
    assert report["trades"][0]["forward_pnl_pct"] == 2.0


def test_backtest_excludes_every_research_lane_gate_failure(temp_db):
    _insert_trade(temp_db, action="BUY", asset="BTC", score=0.8, pnl=2.0)
    _insert_trade(
        temp_db, action="BUY", asset="BTC", score=0.8, pnl=50.0,
        quality_status="unverified",
    )
    _insert_trade(
        temp_db, action="BUY", asset="BTC", score=0.8, pnl=50.0,
        evidence_action="HOLD",
    )
    _insert_trade(
        temp_db, action="BUY", asset="BTC", score=0.8, pnl=50.0,
        gate_reason="research_only",
    )
    _insert_trade(
        temp_db, action="BUY", asset="BTC", score=0.8, pnl=50.0,
        paper_run_id=None,
    )

    rows = backtest.load_settled_trades(connection=temp_db)
    assert len(rows) == 1
    assert rows[0]["forward_pnl"] == 2.0

    params = strategy_store.default_params()
    report = backtest.run_backtest(params, connection=temp_db)
    assert report["data_source"] == "ai_decisions.settled"
    assert report["sample_size"] == 1
    assert report["total_pnl_usdt"] == round(
        params["notional_usdt"] * params["leverage"] * 0.02, 4
    )


def test_current_strategy_can_be_switched(temp_db):
    items = strategy_store.list_strategies()
    initial = strategy_store.get_current_strategy()
    assert initial["is_current"] == 1
    target = next(item for item in items if item["id"] != initial["id"])
    activated = strategy_store.set_current_strategy(target["id"])
    assert activated["id"] == target["id"]
    assert activated["active_version"]["id"] == target["latest_version"]["id"]
    current_count = temp_db.execute(
        "SELECT COUNT(*) FROM strategies WHERE is_current=1"
    ).fetchone()[0]
    assert current_count == 1


def test_signal_matches_strategy_filters():
    params = {
        **strategy_store.default_params(),
        "signal_threshold": 0.6,
        "asset_filter": "BTC",
        "min_event_strength": "strong",
        "require_direct_catalyst": True,
    }
    assert strategy_store.signal_matches(
        params, action="BUY", score=0.8, asset="BTC",
        event_strength="strong", direct_catalyst=1,
    )
    assert not strategy_store.signal_matches(
        params, action="HOLD", score=0.8, asset="BTC",
        event_strength="strong", direct_catalyst=1,
    )
    assert not strategy_store.signal_matches(
        params, action="SELL", score=0.8, asset="BTC",
        event_strength="strong", direct_catalyst=1,
    )
    assert not strategy_store.signal_matches(
        params, action="BUY", score=-0.8, asset="BTC",
        event_strength="strong", direct_catalyst=1,
    )
    assert not strategy_store.signal_matches(
        params, action="SELL", score=-0.8, asset="XAU",
        event_strength="strong", direct_catalyst=1,
    )
    assert not strategy_store.signal_matches(
        params, action="SELL", score=0.8, asset="BTC",
        event_strength="medium", direct_catalyst=1,
    )


def test_optimize_tightens_on_small_sample(temp_db):
    current = strategy_store.default_params()
    report = {"taken": 2, "winrate": 0.2, "max_drawdown_usdt": -10}
    nxt = backtest.optimize_params(current, report)
    assert nxt["params"]["signal_threshold"] >= current["signal_threshold"]
    assert nxt["params"]["notional_usdt"] <= current["notional_usdt"]
    assert nxt["notes"]
