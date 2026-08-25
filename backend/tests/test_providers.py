import pytest

from providers.contracts import NewsEvent
from providers.binance_paxg import BinancePaxgProvider
from providers.coingecko_paxg import CoinGeckoPaxgProvider
from providers.jin10 import Jin10Provider, Jin10SchemaError
from providers.oanda import OandaProvider
from providers.official_macro import (
    OfficialMacroCalendarProvider,
    _parse_bea,
    _parse_bls,
    _parse_census,
    _parse_fed,
)
from providers.trading_economics import TradingEconomicsProvider


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


def test_jin10_calendar_validates_and_normalises_to_utc(monkeypatch):
    payload = {"data": {"list": [{
        "id": "us-cpi-2026-08",
        "name": "美国 CPI 年率",
        "pub_time": "2026-08-21 20:30:00",
        "actual": "2.8",
        "previous": "2.7",
        "consensus": "2.8",
        "unit": "%",
        "country": "美国",
        "star": "5",
        "time_period": "2026年7月",
    }]}}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        assert url == "https://licensed.example/calendar"
        assert kwargs["headers"]["secret-key"] == "test-key"
        return Response()

    monkeypatch.setattr("providers.jin10.requests.get", fake_get)
    provider = Jin10Provider(
        enabled=True,
        api_key="test-key",
        calendar_url="https://licensed.example/calendar",
    )
    events = provider.fetch_macro()
    assert len(events) == 1
    assert events[0].event_id == "us-cpi-2026-08"
    assert events[0].published_at == "2026-08-21T12:30:00Z"
    assert events[0].actual == 2.8
    assert events[0].impact == 5


@pytest.mark.parametrize("payload", [
    {},
    {"data": {"unexpected": []}},
    {"data": "not-a-row-list"},
    {"data": ["not-an-object"]},
])
def test_jin10_calendar_rejects_unknown_http_200_shapes(monkeypatch, payload):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr("providers.jin10.requests.get", lambda *args, **kwargs: Response())
    provider = Jin10Provider(
        enabled=True,
        api_key="test-key",
        calendar_url="https://licensed.example/calendar",
    )
    with pytest.raises(Jin10SchemaError):
        provider.fetch_macro()


def test_jin10_calendar_rejects_malformed_rows(monkeypatch):
    payload = {"data": [{
        "id": "bad-cpi",
        "name": "美国 CPI 年率",
        "pub_time": "not-a-date",
        "actual": "not-a-number",
    }]}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr("providers.jin10.requests.get", lambda *args, **kwargs: Response())
    provider = Jin10Provider(
        enabled=True,
        api_key="test-key",
        calendar_url="https://licensed.example/calendar",
    )
    with pytest.raises(Jin10SchemaError):
        provider.fetch_macro()


def test_jin10_calendar_disabled_without_key_makes_no_request(monkeypatch):
    monkeypatch.setattr(
        "providers.jin10.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    provider = Jin10Provider(
        enabled=True,
        api_key="",
        calendar_url="https://licensed.example/calendar",
    )
    assert provider.fetch_macro() == []


def test_oanda_xau_mapping_keeps_bid_ask_and_instrument_identity(monkeypatch):
    payload = {
        "prices": [{
            "instrument": "XAU_USD",
            "time": "2026-08-21T12:30:00.000000000Z",
            "bids": [{"price": "2400.10"}],
            "asks": [{"price": "2400.30"}],
        }]
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        assert url.endswith("/v3/accounts/account-1/pricing")
        assert kwargs["params"] == {"instruments": "XAU_USD"}
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        return Response()

    monkeypatch.setattr("providers.oanda.requests.get", fake_get)
    provider = OandaProvider(
        enabled=True,
        api_token="test-token",
        account_id="account-1",
    )
    quotes = provider.fetch_quotes()
    assert quotes[0].asset == "XAU"
    assert quotes[0].symbol == "XAU_USD"
    assert quotes[0].price == 2400.2
    assert quotes[0].bid == 2400.1
    assert quotes[0].ask == 2400.3


def test_oanda_is_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(
        "providers.oanda.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    provider = OandaProvider(enabled=True, api_token="", account_id="account-1")
    assert provider.fetch_quotes() == []


def test_trading_economics_calendar_mapping_is_explicitly_candidate(monkeypatch):
    payload = [{
        "CalendarId": 123,
        "Date": "2026-08-21T12:30:00Z",
        "Country": "United States",
        "Event": "CPI YoY",
        "Actual": "2.8%",
        "Previous": "2.7%",
        "Forecast": "2.8%",
        "Importance": "High",
        "Unit": "%",
        "Reference": "Jul 2026",
    }]

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        assert url == "https://licensed.example/calendar"
        assert kwargs["params"]["c"] == "client:secret"
        return Response()

    monkeypatch.setattr("providers.trading_economics.requests.get", fake_get)
    provider = TradingEconomicsProvider(
        enabled=True,
        credentials="client:secret",
        calendar_url="https://licensed.example/calendar",
    )
    events = provider.fetch_macro()
    assert len(events) == 1
    assert events[0].event_id == "123"
    assert events[0].indicator == "CPI YoY"
    assert events[0].published_at == "2026-08-21T12:30:00Z"
    assert events[0].actual == 2.8
    assert events[0].consensus == 2.8
    assert events[0].impact == 5


def test_trading_economics_requires_explicit_url_and_credentials(monkeypatch):
    monkeypatch.setattr(
        "providers.trading_economics.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    provider = TradingEconomicsProvider(enabled=True, credentials="", calendar_url="")
    assert provider.fetch_macro() == []


def test_binance_paxg_mapping_is_candidate_proxy(monkeypatch):
    payload = {
        "symbol": "PAXGUSDT", "lastPrice": "2401.5", "priceChangePercent": "1.2",
        "volume": "12.5", "bidPrice": "2401.4", "askPrice": "2401.6",
        "highPrice": "2410", "lowPrice": "2380", "prevClosePrice": "2373",
        "closeTime": 1_724_206_680_000,
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        assert url.endswith("/api/v3/ticker/24hr")
        assert kwargs["params"] == {"symbol": "PAXGUSDT"}
        return Response()

    monkeypatch.setattr("providers.binance_paxg.requests.get", fake_get)
    quotes = BinancePaxgProvider(enabled=True).fetch_quotes()
    assert quotes[0].asset == "XAU"
    assert quotes[0].symbol == "PAXGUSDT"
    assert quotes[0].price == 2401.5
    assert quotes[0].bid == 2401.4


def test_coingecko_paxg_requires_provider_timestamp(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"pax-gold": {"usd": 2400.0, "usd_24h_change": 0.5}}

    monkeypatch.setattr("providers.coingecko_paxg.requests.get", lambda *args, **kwargs: Response())
    assert CoinGeckoPaxgProvider(enabled=True).fetch_quotes() == []


def test_coingecko_paxg_cache_avoids_rate_limit_polling(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"pax-gold": {"usd": 2400.0, "last_updated_at": 1724206680}}

    def fake_get(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr("providers.coingecko_paxg.requests.get", fake_get)
    provider = CoinGeckoPaxgProvider(enabled=True, cache_seconds=60)
    assert provider.fetch_quotes()[0].price == 2400.0
    assert provider.fetch_quotes()[0].price == 2400.0
    assert len(calls) == 1


def test_official_calendar_parsers_keep_original_sources():
    bea = """
    <table><tr><th>Year 2026</th><th>Release</th></tr>
    <tr><td>August 26 8:30 AM</td><td>GDP (Second Estimate), 2nd Quarter 2026</td></tr></table>
    """
    census = """
    <table><tr><th>Indicator</th><th>Release Date</th><th>Time</th><th>Period</th></tr>
    <tr><td>New Residential Sales</td><td>August 25, 2026</td><td>10:00 AM</td><td>July 2026</td></tr></table>
    """
    fed = """
    <div class="panel panel-default"><div class="panel-heading"><h4><a id="1">2026 FOMC Meetings</a></h4></div>
    <div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>September</strong></div>
    <div class="fomc-meeting__date">15-16*</div></div></div>
    """
    bea_events = _parse_bea(bea, "https://www.bea.gov/news/schedule", "", "")
    census_events = _parse_census(census, "https://www.census.gov/economic-indicators/calendar-listview.html", "", "")
    fed_events = _parse_fed(fed, "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "", "")
    assert bea_events[0].source == "bea"
    assert bea_events[0].published_at == "2026-08-26T12:30:00Z"
    assert census_events[0].source == "census"
    assert fed_events[0].time_period == "September 15-16*"


def test_bls_release_list_parser_maps_cpi_and_employment_schedule():
    bls = """
    <table class="release-list">
      <tr><th>Date</th><th>Time</th><th>Release</th></tr>
      <tr><td>Friday, August 7, 2026</td><td>08:30 AM</td><td>Employment Situation for July 2026</td></tr>
      <tr><td>Wednesday, August 12, 2026</td><td>08:30 AM</td><td>Consumer Price Index for July 2026</td></tr>
    </table>
    """
    events = _parse_bls(
        bls,
        "https://www.bls.gov/schedule/2026/08_sched_list.htm",
        "2026-08-01T00:00:00Z",
        "2026-08-31T23:59:59Z",
    )
    assert [event.indicator for event in events] == [
        "Employment Situation for July 2026",
        "Consumer Price Index for July 2026",
    ]
    assert events[0].published_at == "2026-08-07T12:30:00Z"


def test_official_calendar_provider_is_disabled_without_enable_flag(monkeypatch):
    monkeypatch.setattr(
        "providers.official_macro.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    provider = OfficialMacroCalendarProvider(enabled=False)
    assert provider.fetch_macro() == []
    assert provider.health()["configured"] is False
