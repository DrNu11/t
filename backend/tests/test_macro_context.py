"""离线测试：非新闻数据层（盘面 / 情绪 / 加息预期 / 资金流）。"""

import json
import urllib.error

import macro_context


def _fixture_get(url: str, timeout: int = 8):
    if "premiumIndex" in url:
        return {"markPrice": "101", "indexPrice": "100", "lastFundingRate": "0.0001"}
    if "openInterest" in url:
        return {"openInterest": "12345"}
    if "globalLongShortAccountRatio" in url:
        return [{"longShortRatio": "1.2", "longAccount": "0.545", "shortAccount": "0.455"}]
    if "takerlongshortRatio" in url:
        return [{"buyVol": "200", "sellVol": "100", "buySellRatio": "2.0"}]
    if "alternative.me" in url:
        return {"data": [{"value": "28", "value_classification": "Fear"}]}
    if "newyorkfed" in url:
        return {"refRates": [{"percentRate": 3.64, "effectiveDate": "2026-08-15", "type": "EFFR"}]}
    if "yahoo" in url:
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 96.2}}]}}
    raise AssertionError(url)


def test_layers_parse_and_do_not_invent_missing_etf(monkeypatch):
    monkeypatch.setattr("config.SOSOVALUE_ETF_URL", "")
    layers = macro_context.collect_layers(_fixture_get)
    assert layers["sentiment"][0]["value"] == 28
    assert layers["fed"][-1]["payload"]["next_move"] == "hike"
    etf = [item for item in layers["flow"] if item["metric_key"] == "btc_etf_net"][0]
    assert etf["status"] == "unavailable"
    assert etf["value"] is None
    text = macro_context.render_prompt_block(layers)
    assert "盘面结构" in text
    assert "Fear&Greed 28" not in text  # candidate sentiment is stored but cannot enter AI
    assert layers["sentiment"][0]["decision_eligible"] is False
    assert "不得臆造" not in text or "btc_etf" in json.dumps(layers, ensure_ascii=False)


def test_all_down_does_not_fabricate():
    def boom(url, timeout=8):
        raise TimeoutError("offline")

    layers = macro_context.collect_layers(boom)
    text = macro_context.render_prompt_block(layers)
    assert "禁止补造" in text
    assert all(item["status"] == "unavailable" for item in macro_context.flatten(layers))


def test_persist_macro_snapshots(temp_db, monkeypatch):
    monkeypatch.setattr("config.SOSOVALUE_ETF_URL", "")
    layers = {
        "structure": [macro_context._metric("structure", "binance_fapi", "BTC.funding", 0.01, unit="%")],
        "sentiment": [macro_context._metric("sentiment", "alternative.me", "crypto_fng", 28)],
        "fed": [macro_context._fail("fed", "yahoo_zq", "implied_ff", "offline")],
        "flow": [],
    }
    written = macro_context.persist_layers(layers, ts=1_700_000_000, connection=temp_db)
    temp_db.commit()
    assert written == 3
    rows = temp_db.execute("SELECT metric_key, status FROM macro_snapshots ORDER BY id").fetchall()
    assert {row["metric_key"] for row in rows} == {"BTC.funding", "crypto_fng", "implied_ff"}
    assert any(row["status"] == "unavailable" for row in rows)
    assert temp_db.execute("SELECT COUNT(*) FROM data_quality_audit").fetchone()[0] == 2


def test_persist_is_idempotent_within_bucket_and_latest_is_one_per_metric(temp_db):
    layers = {
        "structure": [
            macro_context._metric("structure", "binance_fapi", "BTC.funding", 0.01, unit="%")
        ],
        "sentiment": [],
        "fed": [],
        "flow": [],
    }
    stamp = 1_700_000_000
    assert macro_context.persist_layers(layers, ts=stamp, connection=temp_db) == 1
    assert macro_context.persist_layers(layers, ts=stamp + 5, connection=temp_db) == 0

    changed = {
        **layers,
        "structure": [
            macro_context._metric("structure", "binance_fapi", "BTC.funding", 0.02, unit="%")
        ],
    }
    assert macro_context.persist_layers(changed, ts=stamp + 6, connection=temp_db) == 1
    assert macro_context.persist_layers(changed, ts=stamp + 7, connection=temp_db) == 0

    rows = macro_context.latest_snapshot(limit=80, connection=temp_db)
    funding = [item for item in rows if item["metric_key"] == "BTC.funding"]
    assert len(funding) == 1
    assert funding[0]["value"] == 0.02
    assert temp_db.execute(
        "SELECT COUNT(*) FROM macro_snapshots WHERE metric_key='BTC.funding'"
    ).fetchone()[0] == 2

    failed = {
        "structure": [], "sentiment": [], "flow": [],
        "fed": [macro_context._fail("fed", "yahoo_zq", "implied_ff", "offline")],
    }
    assert macro_context.persist_layers(failed, ts=stamp, connection=temp_db) == 1
    assert macro_context.persist_layers(failed, ts=stamp + 30, connection=temp_db) == 0


def test_upstream_timestamp_makes_observation_id_stable_across_poll_buckets():
    item = macro_context._metric(
        "structure", "binance_fapi", "BTC.oi", 123.0,
        extra={"upstream_time": 1_700_000_000_000},
    )
    first = macro_context.apply_quality({"structure": [item]}, observed_at=1_700_000_010)
    later = macro_context.apply_quality({"structure": [item]}, observed_at=1_700_003_600)
    assert first["structure"][0]["quality_event_id"] == later["structure"][0]["quality_event_id"]


def test_binance_structure_skips_xau_and_preserves_http_status():
    requested = []

    def get(url: str, timeout: int = 8):
        requested.append(url)
        if "premiumIndex" in url:
            raise urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", {}, None)
        if "openInterest" in url:
            return {"openInterest": "12", "time": 1_700_000_000_000}
        if "globalLongShortAccountRatio" in url:
            return [{"longShortRatio": "1.1", "timestamp": 1_700_000_000_000}]
        raise AssertionError(url)

    rows = macro_context.fetch_structure(get)
    assert all("XAUUSDT" not in url for url in requested)
    assert {"BTC", "ETH"} == {
        item["payload"].get("label") for item in rows if item["payload"].get("label")
    }
    errors = [item for item in rows if item["metric_key"].endswith(".premium")]
    assert len(errors) == 2
    assert all(item["payload"]["http_status"] == 451 for item in errors)
    assert all("status=451" in item["payload"]["note"] for item in errors)
