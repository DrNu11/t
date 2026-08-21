"""快速模拟评测通道冒烟测试（全离线，行情用假函数注入）。"""

import time

import config
import quick_sim


def _insert_signal(conn, asset, action, reason, score=1.0, confidence=9.0):
    news_id = conn.execute(
        "INSERT INTO raw_news (source, content, timestamp, status) VALUES ('test', ?, '2026-08-15 10:00:00', 'DONE')",
        (f"{asset} {action} news",),
    ).lastrowid
    decision_id = conn.execute(
        """INSERT INTO ai_decisions
               (news_id, sentiment_score, suggested_action, reasoning, market_category,
                target_asset, evidence_confidence, trade_gate_reason)
           VALUES (?, ?, ?, 'test', 'CRYPTO', ?, ?, ?)""",
        (news_id, score, action, asset, confidence, reason),
    ).lastrowid
    conn.commit()
    return decision_id


def test_open_trades_uses_real_price_and_marks_gate(temp_db, monkeypatch):
    monkeypatch.setattr(config, "QUICK_SIM_HORIZON_MINUTES", 5)
    monkeypatch.setattr(config, "QUICK_SIM_NOTIONAL_USDT", 100.0)
    passed = _insert_signal(temp_db, "BTC", "BUY", "证据充分，允许输出方向性结论")
    rejected = _insert_signal(temp_db, "ETH", "SELL", "统一置信度 9.0 低于闸门 55.0，仅输出观望结论")
    _insert_signal(temp_db, "SOL", "BUY", "证据充分，允许输出方向性结论")

    prices = {"BTC": 60000.0, "ETH": 3000.0}
    created = quick_sim.open_trades(temp_db, lambda asset: prices.get(asset))

    assert {item["decision_id"] for item in created} == {passed, rejected}
    rows = {
        row["decision_id"]: row
        for row in temp_db.execute(
            "SELECT decision_id, gate_passed, entry_price, horizon_minutes, notional_usdt FROM quick_sim_trades"
        ).fetchall()
    }
    assert rows[passed]["gate_passed"] == 1
    assert rows[rejected]["gate_passed"] == 0
    assert rows[passed]["entry_price"] == 60000.0
    assert rows[passed]["horizon_minutes"] == 5
    assert rows[passed]["notional_usdt"] == 100.0
    # 无真实行情的 SOL 不建仓
    assert len(rows) == 2

    # 幂等：重复调用不会重复建仓
    quick_sim.open_trades(temp_db, lambda asset: prices.get(asset))
    assert temp_db.execute("SELECT count(*) FROM quick_sim_trades").fetchone()[0] == 2


def test_settle_trades_only_after_horizon(temp_db, monkeypatch):
    monkeypatch.setattr(config, "QUICK_SIM_HORIZON_MINUTES", 5)
    monkeypatch.setattr(config, "QUICK_SIM_NOTIONAL_USDT", 100.0)
    decision_id = _insert_signal(temp_db, "BTC", "BUY", "证据充分，允许输出方向性结论")
    quick_sim.open_trades(temp_db, lambda asset: 100.0)

    # 未到期不结算
    assert quick_sim.settle_trades(temp_db, lambda asset: 110.0) == []

    temp_db.execute(
        "UPDATE quick_sim_trades SET entry_ts = ? WHERE decision_id = ?",
        (int(time.time()) - 6 * 60, decision_id),
    )
    temp_db.commit()

    settled = quick_sim.settle_trades(temp_db, lambda asset: 110.0)
    assert len(settled) == 1
    assert settled[0]["verdict"] == "WIN"
    assert settled[0]["pnl_pct"] == 10.0
    row = temp_db.execute(
        "SELECT settled, pnl_pct, pnl_usdt, exit_price FROM quick_sim_trades WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    assert row["settled"] == 1
    assert row["exit_price"] == 110.0
    assert row["pnl_usdt"] == 10.0


def test_settle_sell_direction_and_missing_price(temp_db, monkeypatch):
    monkeypatch.setattr(config, "QUICK_SIM_HORIZON_MINUTES", 5)
    sell_id = _insert_signal(temp_db, "ETH", "SELL", "证据充分，允许输出方向性结论")
    quick_sim.open_trades(temp_db, lambda asset: 100.0)
    temp_db.execute("UPDATE quick_sim_trades SET entry_ts = ?", (int(time.time()) - 600,))
    temp_db.commit()

    # 取不到真实行情就不结算
    assert quick_sim.settle_trades(temp_db, lambda asset: None) == []
    assert temp_db.execute("SELECT settled FROM quick_sim_trades WHERE decision_id = ?", (sell_id,)).fetchone()["settled"] == 0

    settled = quick_sim.settle_trades(temp_db, lambda asset: 90.0)
    assert settled[0]["pnl_pct"] == 10.0
    assert settled[0]["verdict"] == "WIN"


def test_evaluation_groups_by_gate_and_asset(temp_db, monkeypatch):
    monkeypatch.setattr(config, "QUICK_SIM_HORIZON_MINUTES", 5)
    monkeypatch.setattr(config, "QUICK_SIM_NOTIONAL_USDT", 100.0)
    passed = _insert_signal(temp_db, "BTC", "BUY", "证据充分，允许输出方向性结论")
    rejected = _insert_signal(temp_db, "ETH", "BUY", "统一置信度 9.0 低于闸门 55.0，仅输出观望结论")
    quick_sim.open_trades(temp_db, lambda asset: 100.0)
    temp_db.execute("UPDATE quick_sim_trades SET entry_ts = ?", (int(time.time()) - 600,))
    temp_db.commit()
    quick_sim.settle_trades(temp_db, lambda asset: 110.0 if asset == "BTC" else 95.0)

    report = quick_sim.evaluation(temp_db)
    assert report["horizon_minutes"] == 5
    assert report["notional_usdt"] == 100.0
    assert report["open_trades"] == 0
    assert report["overall"]["settled"] == 2
    assert report["overall"]["wins"] == 1
    assert report["overall"]["losses"] == 1
    assert report["overall"]["winrate_pct"] == 50.0
    assert report["overall"]["total_pnl_pct"] == 5.0
    assert report["overall"]["total_pnl_usdt"] == 5.0
    assert report["overall"]["return_on_notional_pct"] == 2.5
    assert report["overall"]["profitable"] is True
    assert report["gate_passed"]["settled"] == 1
    assert report["gate_passed"]["wins"] == 1
    assert report["gate_rejected"]["settled"] == 1
    assert report["gate_rejected"]["losses"] == 1
    assert {item["asset"] for item in report["by_asset"]} == {"BTC", "ETH"}
    assert {row["decision_id"] for row in report["recent"]} == {passed, rejected}


def test_evaluation_empty_returns_none_instead_of_fake_numbers(temp_db):
    report = quick_sim.evaluation(temp_db)
    assert report["overall"]["settled"] == 0
    assert report["overall"]["winrate_pct"] is None
    assert report["overall"]["avg_pnl_pct"] is None
    assert report["overall"]["profitable"] is None
    assert report["open"] == []
    assert report["recent"] == []


def test_run_cycle_disabled_is_noop(temp_db, monkeypatch):
    monkeypatch.setattr(config, "QUICK_SIM_ENABLED", False)
    _insert_signal(temp_db, "BTC", "BUY", "证据充分，允许输出方向性结论")
    assert quick_sim.run_cycle(lambda asset: 100.0) == {"opened": [], "settled": []}
    assert temp_db.execute("SELECT count(*) FROM quick_sim_trades").fetchone()[0] == 0
