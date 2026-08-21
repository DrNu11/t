from providers.contracts import NewsEvent
from providers.jin10 import Jin10Provider


def test_news_event_projects_to_existing_watcher_shape():
    event = NewsEvent(
        event_id="101",
        published_at="2026-08-21 10:18:00",
        source="金十",
        title="黄金上涨",
        body="现货黄金突破关键价位",
        category="商品/外汇",
        impact_assets=["XAU"],
    )
    legacy = event.to_legacy()
    assert legacy["id"] == "101"
    assert legacy["summary"] == "现货黄金突破关键价位"
    assert legacy["impact_assets"] == ["XAU"]


def test_jin10_flash_and_quote_mapping(monkeypatch):
    responses = {
        "flash": {"data": [{
            "id": 77,
            "time": "2026-08-21 10:18:00",
            "important": 1,
            "classify": ["黄金"],
            "qh_tags": ["XAUUSD"],
            "data": {"content": "<b>黄金</b>突破 2400 美元。"},
        }]},
        "quotes": {"data": [{"c": "XAUUSD", "p": "2401.5", "hc": "2390", "b": "2401.4", "a": "2401.6", "t": 1724206680}]},
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        assert kwargs["headers"]["secret-key"] == "test-key"
        assert "secret-key" not in str(kwargs.get("params", {}))
        return Response(responses["flash" if url.endswith("flash") else "quotes"])

    monkeypatch.setattr("providers.jin10.requests.get", fake_get)
    provider = Jin10Provider(enabled=True, api_key="test-key")
    events = provider.fetch_news()
    assert events[0].title == "黄金 突破 2400 美元"
    assert events[0].body == "黄金 突破 2400 美元。"
    assert "XAU" in events[0].impact_assets
    quotes = provider.fetch_quotes()
    assert quotes[0].asset == "XAU"
    assert quotes[0].price == 2401.5
    assert round(quotes[0].change_pct, 2) == 0.48


def test_jin10_is_disabled_without_authorization(monkeypatch):
    def fail_get(*args, **kwargs):
        raise AssertionError("disabled provider must not make a request")

    monkeypatch.setattr("providers.jin10.requests.get", fail_get)
    provider = Jin10Provider(enabled=False, api_key="")
    assert provider.fetch_news() == []
    assert provider.fetch_quotes() == []
