import asyncio
import json

import api_server
import market_feeds


def _reset_structure_cache():
    api_server._MARKET_STRUCTURE_CACHE.update({"expires": 0.0, "value": None})
    api_server._MARKET_STRUCTURE_INFLIGHT = None


def test_market_feed_mapping_and_median(monkeypatch):
    values = {
        "Binance": (100.0, 1.0), "OKX": (102.0, 2.0),
        "Bitget": (104.0, 3.0), "Gate.io": (106.0, 4.0),
    }
    monkeypatch.setattr(market_feeds, "_FETCHERS", {
        source: (lambda asset, value=value: {"price": value[0] + {"BTC": 0, "ETH": 100, "SOL": 200}[asset], "change24h": value[1]})
        for source, value in values.items()
    })
    result = market_feeds.fetch_market_prices()
    assert result["status"] == "ok"
    assert result["items"][0]["price"] == 103.0
    assert result["items"][0]["change24h"] == 2.5
    assert result["items"][0]["sourceCount"] == 4
    assert all(source["status"] == "ok" for source in result["sources"].values())


def test_market_feed_partial_failure_isolated(monkeypatch):
    def failed(asset):
        raise RuntimeError("offline")
    monkeypatch.setattr(market_feeds, "_FETCHERS", {
        "Binance": lambda asset: {"price": 10, "change24h": 1},
        "OKX": lambda asset: {"price": 12, "change24h": 3},
        "Bitget": failed,
        "Gate.io": failed,
    })
    result = market_feeds.fetch_market_prices()
    assert [item["price"] for item in result["items"]] == [11, 11, 11]
    assert result["sources"]["Bitget"]["status"] == "unavailable"


def test_market_api_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(api_server.market_feeds, "fetch_market_prices", lambda: calls.append(1) or {"items": []})
    api_server._MARKET_CACHE.update({"expires": 0, "value": None})
    assert asyncio.run(api_server.get_market_prices()) == {"items": []}
    assert asyncio.run(api_server.get_market_prices()) == {"items": []}
    assert len(calls) == 1


def test_market_structure_api_returns_only_matching_verified_asset(monkeypatch):
    _reset_structure_cache()

    async def snapshot():
        return {
            "timestamp": "2026-08-22T12:00:00Z",
            "assets": {
                "BTC": {
                    "source": "OKX",
                    "decision_eligible": True,
                    "market_structure": {
                        "status": "ok",
                        "decision_eligible": True,
                        "aggregate": {"trend": "bullish", "trend_score": 0.7},
                    },
                },
                "XAU": {
                    "source": "OKX",
                    "decision_eligible": False,
                    "status_note": "真实交易对不可用",
                },
            },
        }

    monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
    result = asyncio.run(api_server.get_market_structure("btc"))
    assert result["asset"] == "BTC"
    assert result["status"] == "ok"
    assert result["decision_eligible"] is True
    assert result["structure"]["aggregate"]["trend"] == "bullish"

    unavailable = asyncio.run(api_server.get_market_structure("xau"))
    assert unavailable["asset"] == "XAU"
    assert unavailable["status"] == "unavailable"
    assert unavailable["decision_eligible"] is False
    assert unavailable["structure"] is None


def test_market_structure_api_fails_closed_on_snapshot_error(monkeypatch):
    _reset_structure_cache()

    async def snapshot():
        raise RuntimeError("offline")

    monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
    result = asyncio.run(api_server.get_market_structure("BTC"))
    assert result["status"] == "unavailable"
    assert result["decision_eligible"] is False
    assert result["reason"] == "snapshot_unavailable:RuntimeError"


def test_market_structure_api_preserves_structure_provenance_and_reason(monkeypatch):
    _reset_structure_cache()

    async def snapshot():
        return {
            "epoch_ms": 1_800_000_000_000,
            "assets": {
                "XAU": {
                    "source": "金十",
                    "venue": "Jin10",
                    "instrument_id": "XAUUSD",
                    "instrument_type": "spot_quote",
                    "quote_source": "金十",
                    "quote_venue": "Jin10",
                    "quote_instrument_id": "XAUUSD",
                    "quote_instrument_type": "spot_quote",
                    "structure_source": "OKX",
                    "structure_venue": "OKX",
                    "structure_instrument_id": "XAU-USDT-SWAP",
                    "structure_instrument_type": "perpetual_swap",
                    "decision_eligible": True,
                    "quality_reason": "verified_source",
                    "market_structure": {
                        "source": "OKX",
                        "status": "unavailable",
                        "decision_eligible": False,
                        "quality": {"reason": "OKX 未返回真实 OHLCV"},
                        "warnings": ["OKX 未返回真实 OHLCV"],
                    },
                }
            },
        }

    monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
    result = asyncio.run(api_server.get_market_structure("XAU"))

    assert result["source"] == "OKX"
    assert result["structure_source"] == "OKX"
    assert result["structure_venue"] == "OKX"
    assert result["structure_instrument_id"] == "XAU-USDT-SWAP"
    assert result["structure_instrument_type"] == "perpetual_swap"
    assert result["quote_source"] == "金十"
    assert result["venue"] == "OKX"
    assert result["instrument_id"] == "XAU-USDT-SWAP"
    assert result["instrument_type"] == "perpetual_swap"
    assert result["quote_venue"] == "Jin10"
    assert result["quote_instrument_id"] == "XAUUSD"
    assert result["reason"] == "OKX 未返回真实 OHLCV"
    assert result["decision_eligible"] is False
    assert result["updated_at"] == 1_800_000_000_000


def test_market_structure_api_rejects_unsupported_asset_without_upstream_call(monkeypatch):
    _reset_structure_cache()
    calls = []

    async def snapshot():
        calls.append(1)
        return {"assets": {}}

    monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
    result = asyncio.run(api_server.get_market_structure("WTI"))

    assert result["status"] == "unavailable"
    assert result["reason"] == "unsupported_asset"
    assert result["supported_assets"] == ["BTC", "XAU"]
    assert calls == []


def test_market_structure_api_singleflights_concurrent_requests(monkeypatch):
    _reset_structure_cache()
    calls = []

    async def snapshot():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {
            "epoch_ms": 1_800_000_000_000,
            "assets": {
                asset: {
                    "source": "OKX",
                    "decision_eligible": True,
                    "market_structure": {
                        "source": "OKX",
                        "status": "ok",
                        "decision_eligible": True,
                    },
                }
                for asset in ("BTC", "XAU")
            },
        }

    async def run_concurrently():
        return await asyncio.gather(
            *(api_server.get_market_structure("BTC" if index % 2 == 0 else "XAU") for index in range(8))
        )

    monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
    results = asyncio.run(run_concurrently())

    assert len(calls) == 1
    assert all(item["decision_eligible"] is True for item in results)


def test_market_structure_cache_survives_last_waiter_cancellation(monkeypatch):
    _reset_structure_cache()
    calls = []

    async def run_scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def snapshot():
            calls.append(1)
            started.set()
            await release.wait()
            return {"epoch_ms": 1_800_000_000_000, "assets": {}}

        monkeypatch.setattr(api_server.market_snapshot, "get_snapshot", snapshot)
        waiter = asyncio.create_task(api_server._cached_market_structure_snapshot())
        await started.wait()
        producer = api_server._MARKET_STRUCTURE_INFLIGHT
        assert producer is not None

        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        release.set()
        await producer
        cached = await api_server._cached_market_structure_snapshot()
        return cached

    result = asyncio.run(run_scenario())

    assert result["epoch_ms"] == 1_800_000_000_000
    assert calls == [1]
    assert api_server._MARKET_STRUCTURE_CACHE["value"] is result
    assert api_server._MARKET_STRUCTURE_INFLIGHT is None


def test_techflow_api_maps_json_and_handles_challenge(monkeypatch):
    class Response:
        headers = {"content-type": "application/json"}
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"list": [{"id": 7, "title": "快讯", "summary": "摘要", "url": "/news/7", "published_at": "now"}]}}
    monkeypatch.setattr(api_server.requests, "get", lambda *args, **kwargs: Response())
    api_server._TECHFLOW_CACHE.update({"expires": 0, "value": None})
    result = asyncio.run(api_server.get_techflow_news())
    assert result["items"][0] == {"id": "7", "title": "快讯", "summary": "摘要", "url": "https://www.techflowpost.com/news/7", "published_at": "now", "source": "TechFlow 深潮"}

    Response.headers = {"content-type": "text/html"}
    api_server._TECHFLOW_CACHE.update({"expires": 0, "value": None})
    assert asyncio.run(api_server.get_techflow_news())["status"] == "unavailable"


def test_blockbeats_api_maps_json(monkeypatch):
    class Response:
        headers = {"content-type": "application/json"}
        def raise_for_status(self): pass
        def json(self):
            return {"status": 0, "data": {"data": [{"id": 9, "title": "律动快讯", "content": "摘要", "link": "https://example.com/9", "create_time": "now"}]}}
    monkeypatch.setattr(api_server.requests, "get", lambda *args, **kwargs: Response())
    api_server._BLOCKBEATS_CACHE.update({"expires": 0, "value": None})
    result = asyncio.run(api_server.get_blockbeats_news())
    assert result["items"][0] == {
        "id": "9",
        "title": "律动快讯",
        "summary": "摘要",
        "url": "https://example.com/9",
        "published_at": "now",
        "source": "律动 BlockBeats",
    }


def test_news_report_tracks_signal_and_trade_pipeline(monkeypatch):
    async def techflow():
        return {"status": "ok", "items": [{"id": "1"}], "error": ""}

    async def eastmoney():
        return {"status": "ok", "items": [{"id": "2"}], "error": ""}

    async def events(limit=None):
        return [
            {"news_id": 1, "source": "TechFlow 深潮", "timestamp": "now", "news_text": "报告", "analysis_status": "DONE", "decision_id": 3, "target_asset": "BTC", "action": "BUY", "score": 0.8, "entry_price": 100.0, "settled": 0, "reason": "利好", "evidence_action": "BUY", "trade_gate_reason": "证据充分，允许输出方向性结论"},
            {"news_id": 2, "source": "东方财富", "timestamp": "now", "news_text": "报告2", "analysis_status": "DONE", "decision_id": 4, "target_asset": "XAU", "action": "HOLD", "score": 0.1, "entry_price": None, "settled": 1, "is_correct": "HOLD", "forward_pnl": 0.0, "reason": "观望"},
            {"news_id": 3, "source": "东方财富", "timestamp": "now", "news_text": "报告3", "analysis_status": "DONE", "decision_id": 5, "target_asset": "ETH", "action": "SELL", "score": -0.4, "entry_price": None, "settled": 0, "reason": "利空", "evidence_action": "HOLD", "trade_gate_reason": "统一置信度 40 低于闸门 60，仅输出观望结论"},
        ]

    async def blockbeats():
        return {"status": "ok", "items": [{"id": "3"}], "error": ""}

    monkeypatch.setattr(api_server, "get_techflow_news", techflow)
    monkeypatch.setattr(api_server, "get_eastmoney_news", eastmoney)
    monkeypatch.setattr(api_server, "get_blockbeats_news", blockbeats)
    monkeypatch.setattr(api_server, "_fetch_event_rows", events)
    monkeypatch.setattr(api_server.quick_sim, "snapshot", lambda: {"enabled": True, "overall": {"settled": 0}})
    result = asyncio.run(api_server.get_news_report())
    assert result["pipeline"] == {"source_items": 3, "stored_reports": 3, "analyzed": 3, "signals": 2, "evidence_passed": 1, "evidence_rejected": 1, "executed": 1, "open_trades": 1, "settled_trades": 1}
    assert result["gate_reasons"] == {"统一置信度 40 低于闸门 60，仅输出观望结论": 1}
    assert result["quick_sim"]["enabled"] is True
    assert result["reports"][0]["action"] == "BUY"


def test_external_news_ingestion_is_deduplicated(temp_db, monkeypatch):
    monkeypatch.setenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", "techflow")
    monkeypatch.setattr(api_server.db, "get_connection", lambda: __import__("sqlite3").connect(
        temp_db.execute("PRAGMA database_list").fetchone()[2]
    ))
    monkeypatch.setattr(api_server, "evaluate_news", lambda *args: {"is_noise": 0, "relevance_score": 0.9})
    items = [{
        "id": "7", "title": "美联储宣布降息", "summary": "黄金与比特币波动",
        "source": "TechFlow 深潮", "published_at": "2026-08-21T10:00:00+08:00",
    }]
    assert api_server._ingest_external_news_sync(items) == 1
    assert api_server._ingest_external_news_sync(items) == 0
    row = temp_db.execute("SELECT source, content, status FROM raw_news").fetchone()
    assert row[0] == "TechFlow 深潮"
    assert "美联储宣布降息" in row[1]
    assert row[2] == "PENDING"


def _insert_analysis_data(temp_db):
    news_id = temp_db.execute("INSERT INTO raw_news(source,content,timestamp,status) VALUES('Internal','完整原文内容','2026-08-15','DONE')").lastrowid
    context = {"assets": {"BTC": {"change_24h_pct": 2, "funding_rate_pct": 0.01, "stats_7d": {"trend": "Bull", "atr_pct": 2}}}}
    temp_db.execute("""INSERT INTO ai_decisions(news_id,sentiment_score,suggested_action,reasoning,target_asset,market_confirmation,decision_context,cluster_size,settled,is_correct)
                       VALUES(?,0.8,'BUY','利好','BTC','confirmed',?,3,1,'WIN')""", (news_id, json.dumps(context)))
    temp_db.commit()
    return news_id


def test_news_analysis_api_complete_and_weighted(temp_db, monkeypatch):
    news_id = _insert_analysis_data(temp_db)
    monkeypatch.setattr(api_server, "DB_PATH", temp_db.execute("PRAGMA database_list").fetchone()[2])
    result = asyncio.run(api_server.get_news_analysis(news_id))
    assert result["news"]["content"] == "完整原文内容"
    assert set(result["analysis"]["factors"]) == {"news_sentiment", "market_confirmation", "trend", "volatility", "funding", "cluster_heat", "historical_confidence"}
    assert sum(result["analysis"]["weights"].values()) == 1
    assert set(result["analysis"]["evidence"]) == set(result["analysis"]["factors"])
    assert all(0 <= item["evidence_score"] <= 10 for item in result["analysis"]["evidence"].values())
    assert 0 <= result["analysis"]["confidence"] <= 100
    assert result["analysis"]["significance"]["sufficient_sample"] is False
    assert result["strategy"]["passed_gate"] == result["analysis"]["confidence_detail"]["passed_gate"]
    assert result["strategy"]["note"].startswith("仅供")


def test_strategy_advice_llm_bounds_and_rules_fallback(temp_db, monkeypatch):
    news_id = _insert_analysis_data(temp_db)
    monkeypatch.setattr(api_server, "DB_PATH", temp_db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(api_server, "_settled_stats_sync", lambda asset: {"total": 4, "wins": 3, "avg_pnl": 1.2})

    class Completions:
        def create(self, **kwargs):
            body = {"signal_threshold": 9, "notional_multiplier": 9, "trailing_callback_rate": -2, "holding_horizon_minutes": 999, "reason": "r", "risk_notes": "risk", "rollback_condition": "rollback"}
            message = type("Message", (), {"content": json.dumps(body)})()
            return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    monkeypatch.setattr(api_server, "_agent_llm_client", lambda: client)
    llm = asyncio.run(api_server.get_strategy_advice(news_id))
    assert llm["mode"] == "llm"
    assert llm["advice"]["signal_threshold"] == 0.9
    assert llm["advice"]["notional_multiplier"] == 1.5
    assert llm["advice"]["trailing_callback_rate"] == 0.1
    assert llm["advice"]["holding_horizon_minutes"] == 240

    monkeypatch.setattr(api_server, "_llm_strategy_advice_sync", lambda *args: (_ for _ in ()).throw(RuntimeError("secret")))
    rules = asyncio.run(api_server.get_strategy_advice(news_id))
    assert rules["mode"] == "rules"
    assert rules["applied"] is False
    assert 0 <= rules["validation"]["confidence"] <= 100
    assert rules["validation"]["gated_action"] in {"BUY", "SELL", "HOLD"}
    for key, bounds in rules["bounds"].items():
        assert bounds["min"] <= rules["advice"][key] <= bounds["max"]
