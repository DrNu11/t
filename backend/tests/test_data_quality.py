"""Offline source-governance and macro-calendar regression tests."""

from providers.contracts import MacroEvent

import config
import data_quality
import macro_context


def test_direct_exchange_is_verified_and_unknown_source_is_blocked():
    verified = data_quality.assess(
        source="Binance", kind="market", event_id="BTC:1",
        published_at=1_700_000_000,
        observed_at=1_700_000_001,
        payload={"price": 100.0, "symbol": "BTCUSDT"},
    )
    assert verified.accepted is True
    assert verified.decision_eligible is True
    assert verified.quality_status == "verified"

    blocked = data_quality.assess(
        source="random-blog", kind="news", event_id="x",
        payload={"title": "headline"},
    )
    assert blocked.accepted is False
    assert blocked.reason == "source_not_allowlisted"


def test_tokenized_gold_proxies_are_retained_but_not_decision_eligible():
    for source in ("binance_paxg", "coingecko_paxg"):
        decision = data_quality.assess(
            source=source,
            kind="market",
            event_id=f"{source}:1",
            published_at=1_700_000_000,
            observed_at=1_700_000_001,
            payload={"price": 2400.0, "symbol": "PAXGUSDT"},
        )
        assert decision.accepted is True
        assert decision.decision_eligible is False
        assert decision.quality_status == "candidate"


def test_candidate_requires_explicit_source_promotion(monkeypatch):
    payload = {"title": "美联储公布声明"}
    candidate = data_quality.assess(
        source="TechFlow 深潮", kind="news", event_id="n1",
        published_at="2026-08-21T10:00:00+08:00", payload=payload,
    )
    assert candidate.accepted is True
    assert candidate.decision_eligible is False

    monkeypatch.setenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", "techflow")
    promoted = data_quality.assess(
        source="TechFlow 深潮", kind="news", event_id="n1",
        published_at="2026-08-21T10:00:00+08:00", payload=payload,
    )
    assert promoted.decision_eligible is True
    assert promoted.quality_status == "verified"


def test_jin10_requires_authorisation_before_promotion(monkeypatch):
    monkeypatch.setenv("TRIDENT_JIN10_DECISION_ENABLED", "1")
    monkeypatch.setattr(config, "JIN10_ENABLED", False)
    monkeypatch.setattr(config, "JIN10_API_KEY", "")
    denied = data_quality.assess(
        source="金十", kind="news", event_id="j1",
        published_at="2026-08-21T10:00:00+08:00", payload={"title": "CPI"},
    )
    assert denied.accepted is False
    assert denied.reason == "jin10_not_authorized_or_disabled"


def test_allowlisted_news_without_a_real_timestamp_is_blocked(monkeypatch):
    monkeypatch.setenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", "techflow")
    decision = data_quality.assess(
        source="TechFlow 深潮", kind="news", event_id="missing-clock",
        published_at="now", payload={"title": "美联储快讯"},
    )
    assert decision.accepted is False
    assert decision.decision_eligible is False
    assert decision.reason == "missing_or_invalid_timestamp"


def test_identical_candidate_observation_is_detected_without_hiding_changes(temp_db):
    payload = {"title": "候选快讯"}
    decision = data_quality.assess(
        source="TechFlow 深潮", kind="news", event_id="dedup-1",
        published_at="2026-08-21T10:00:00+08:00", payload=payload,
    )
    data_quality.record(
        temp_db, decision, published_at="2026-08-21T10:00:00+08:00",
        payload=payload, quarantine=True,
    )
    temp_db.commit()
    assert data_quality.observation_already_recorded(temp_db, decision, payload) is True
    assert data_quality.observation_already_recorded(
        temp_db, decision, {"title": "候选快讯（更新）"},
    ) is False


def test_macro_calendar_upsert_keeps_quality_state(temp_db, monkeypatch):
    monkeypatch.setattr(config, "JIN10_ENABLED", True)
    monkeypatch.setattr(config, "JIN10_API_KEY", "test-authorized-key")
    monkeypatch.delenv("TRIDENT_JIN10_DECISION_ENABLED", raising=False)
    event = MacroEvent(
        event_id="cpi-2026-08",
        indicator="美国 CPI",
        source="金十",
        published_at="2026-08-21T20:30:00+08:00",
        consensus=2.8,
        previous=2.7,
        country="US",
        impact=5,
    )
    assert macro_context.persist_macro_events([event], connection=temp_db) == 1
    temp_db.commit()
    row = temp_db.execute(
        "SELECT quality_status, decision_eligible, status FROM macro_events"
    ).fetchone()
    assert tuple(row) == ("candidate", 0, "scheduled")
    listed = macro_context.list_macro_events(
        start="2026-08-21T00:00:00+08:00",
        end="2026-08-22T00:00:00+08:00",
        decision_eligible=False,
    )
    assert [item["event_id"] for item in listed] == ["jin10:cpi-2026-08"]

    monkeypatch.setenv("TRIDENT_JIN10_DECISION_ENABLED", "1")
    released = MacroEvent(
        **{**event.to_dict(), "actual": 2.6},
    )
    assert macro_context.persist_macro_events([released], connection=temp_db) == 1
    temp_db.commit()
    row = temp_db.execute(
        "SELECT quality_status, decision_eligible, status, actual FROM macro_events"
    ).fetchone()
    assert tuple(row) == ("verified", 1, "released", 2.6)
