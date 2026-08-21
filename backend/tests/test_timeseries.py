"""Smoke tests for src_python/timeseries.py — 本地时序持久化层."""

import timeseries


def _seed_ticks(temp_db, rows):
    temp_db.executemany(
        "INSERT INTO market_ticks(symbol, ts, price, change24h, source, source_count) VALUES(?,?,?,?,'median',4)",
        rows,
    )
    temp_db.commit()


def test_record_market_snapshot_persists_and_is_idempotent(temp_db, monkeypatch):
    monkeypatch.setattr(timeseries, "_now", lambda: 1_700_000_000)
    payload = {"items": [
        {"asset": "BTC", "price": 90000.0, "change24h": 1.5, "source": "median", "sourceCount": 4},
        {"asset": "ETH", "price": 3000.0, "change24h": -0.5, "source": "median", "sourceCount": 3},
    ]}
    assert timeseries.record_market_snapshot(payload, connection=temp_db) == 2
    assert timeseries.record_market_snapshot(payload, connection=temp_db) == 0
    assert temp_db.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0] == 2
    assert temp_db.execute("SELECT COUNT(*) FROM data_quality_audit").fetchone()[0] == 2


def test_record_market_snapshot_empty_items_noop(temp_db):
    assert timeseries.record_market_snapshot({"items": []}, connection=temp_db) == 0


def test_query_ticks_range_and_ascending_order(temp_db):
    _seed_ticks(temp_db, [("BTC", 100, 10.0, 0.0), ("BTC", 200, 20.0, 0.0),
                          ("BTC", 300, 30.0, 0.0), ("ETH", 200, 5.0, 0.0)])
    items = timeseries.query_ticks("btc", start=150, end=350, connection=temp_db)
    assert [item["ts"] for item in items] == [200, 300]
    assert items[0]["price"] == 20.0
    assert all(item["symbol"] == "BTC" for item in items)


def test_query_ticks_limit_keeps_newest(temp_db):
    _seed_ticks(temp_db, [("BTC", ts, float(ts), 0.0) for ts in (100, 200, 300, 400)])
    items = timeseries.query_ticks("BTC", limit=2, connection=temp_db)
    assert [item["ts"] for item in items] == [300, 400]


def test_resample_ohlc_buckets_correctly(temp_db):
    # 两个 1 分钟桶：0-59 与 60-119
    _seed_ticks(temp_db, [("BTC", 0, 100.0, 0.0), ("BTC", 30, 120.0, 0.0), ("BTC", 59, 110.0, 0.0),
                          ("BTC", 60, 90.0, 0.0), ("BTC", 119, 95.0, 0.0)])
    candles = timeseries.resample_ohlc("BTC", interval="1m", connection=temp_db)
    assert [candle["ts"] for candle in candles] == [0, 60]
    assert candles[0] == {"ts": 0, "open": 100.0, "high": 120.0, "low": 100.0, "close": 110.0, "ticks": 3}
    assert candles[1] == {"ts": 60, "open": 90.0, "high": 95.0, "low": 90.0, "close": 95.0, "ticks": 2}


def test_resample_ohlc_rejects_unknown_interval(temp_db):
    try:
        timeseries.resample_ohlc("BTC", interval="7s", connection=temp_db)
    except ValueError as exc:
        assert "7s" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_record_and_query_factor_snapshot(temp_db, monkeypatch):
    monkeypatch.setattr(timeseries, "_now", lambda: 1_700_000_000)
    analysis = {
        "final_score": 0.42, "confidence": 61.5, "raw_action": "BUY", "action": "BUY",
        "significance": {"p_value": 0.03, "chi_square": 4.8, "sample_size": 20},
        "contradictions": [{"type": "a", "detail": "d"}],
        "factors": {"news_sentiment": {"score": 0.8}},
        "evidence": {"news_sentiment": {"evidence_score": 9.0}},
    }
    snapshot_id = timeseries.record_factor_snapshot(11, "btc", analysis, connection=temp_db)
    assert snapshot_id > 0
    items = timeseries.query_factor_history("BTC", connection=temp_db)
    assert len(items) == 1
    assert items[0]["asset"] == "BTC"
    assert items[0]["confidence"] == 61.5
    assert items[0]["contradictions"] == 1
    assert items[0]["factors"] == {"news_sentiment": 0.8}
    assert items[0]["evidence"] == {"news_sentiment": 9.0}


def test_query_factor_history_filters_by_asset(temp_db):
    for asset in ("BTC", "ETH"):
        timeseries.record_factor_snapshot(1, asset, {"final_score": 0.1}, connection=temp_db)
    assert len(timeseries.query_factor_history(connection=temp_db)) == 2
    assert len(timeseries.query_factor_history("ETH", connection=temp_db)) == 1


def test_record_signal_performance_and_rolling_winrate(temp_db):
    rows = [
        {"decision_id": 1, "asset": "BTC", "action": "BUY", "is_correct": "WIN", "forward_pnl": 2.0, "mfe_pct": 3.0, "mae_pct": 1.0},
        {"decision_id": 2, "asset": "BTC", "action": "SELL", "is_correct": "LOSS", "forward_pnl": -1.0, "mfe_pct": 0.5, "mae_pct": 2.0},
        {"decision_id": 3, "asset": "ETH", "action": "BUY", "is_correct": "WIN", "forward_pnl": 5.0, "mfe_pct": 6.0, "mae_pct": 0.2},
    ]
    assert timeseries.record_signal_performance(rows, connection=temp_db) == 3
    btc = timeseries.rolling_winrate("BTC", connection=temp_db)
    assert btc == {"asset": "BTC", "window": 2, "wins": 1, "winrate": 0.5, "cumulative_pnl": 1.0}
    every = timeseries.rolling_winrate(connection=temp_db)
    assert every["window"] == 3 and every["wins"] == 2


def test_rolling_winrate_empty_sample(temp_db):
    assert timeseries.rolling_winrate("BTC", connection=temp_db)["winrate"] == 0.0


def test_prune_market_ticks_respects_retention(temp_db, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(timeseries, "_now", lambda: now)
    _seed_ticks(temp_db, [("BTC", now - 100 * 86400, 1.0, 0.0), ("BTC", now - 1 * 86400, 2.0, 0.0)])
    assert timeseries.prune_market_ticks(retention_days=90, connection=temp_db) == 1
    assert temp_db.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0] == 1
