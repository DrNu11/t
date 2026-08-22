"""Forward paper-settlement regression tests (offline)."""

from datetime import datetime

from engine.forward import (
    _parse_entry_time,
    classify_directional_outcome,
    directional_pnl_pct,
)


def test_directional_pnl_and_verdict_cover_buy_sell_both_directions():
    cases = (
        ("BUY", 100.0, 101.0, 1.0, "WIN"),
        ("BUY", 100.0, 99.0, -1.0, "LOSS"),
        ("SELL", 100.0, 99.0, 1.0, "WIN"),
        ("SELL", 100.0, 101.0, -1.0, "LOSS"),
    )
    for action, entry, exit_price, expected_pnl, expected_verdict in cases:
        pnl = directional_pnl_pct(action, entry, exit_price)
        assert pnl == expected_pnl
        assert classify_directional_outcome(action, pnl, "XAU") == expected_verdict


def test_directional_verdict_has_explicit_small_neutral_band():
    # XAU impact threshold is 1%, so settlement's 1%-of-threshold band is 0.01%.
    assert classify_directional_outcome("BUY", 0.005, "XAU") == "HOLD"
    assert classify_directional_outcome("BUY", 0.02, "XAU") == "WIN"
    assert classify_directional_outcome("SELL", -0.02, "XAU") == "LOSS"
    assert classify_directional_outcome("HOLD", 10.0, "XAU") == "HOLD"


def test_entry_time_normalizes_naive_and_aware_timestamps():
    parsed = _parse_entry_time("2026-08-15T10:00:00")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 8
    assert parsed.day == 15
    assert parsed.hour == 10
    assert parsed.tzinfo is not None

    aware = _parse_entry_time("2026-08-15T02:00:00Z")
    assert aware is not None
    assert aware.hour == 10
    assert aware.utcoffset() == datetime.fromisoformat(
        "2026-08-15T10:00:00+08:00"
    ).utcoffset()


def test_missing_or_invalid_entry_time_stays_untrackable():
    # created_at/news timestamps are intentionally not accepted as fallback
    # arguments: settlement has exactly one authoritative persisted clock.
    assert _parse_entry_time("") is None
    assert _parse_entry_time(None) is None
    assert _parse_entry_time("not-a-time") is None
