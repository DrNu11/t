"""Tests for strict AI analysis normalisation and deterministic fallbacks."""

import math

from decision_guard import normalize_ai_analysis


def _assert_bounded_features(features):
    assert math.isclose(
        features["bullish_probability"] + features["bearish_probability"],
        1.0,
        abs_tol=1e-6,
    )
    for key in (
        "bullish_probability",
        "bearish_probability",
        "uncertainty",
        "bullish_force",
        "bearish_force",
    ):
        assert 0.0 <= features[key] <= 1.0


def test_legacy_high_direct_event_becomes_conflict_with_short_impact():
    features = normalize_ai_analysis({
        "sentiment_score": 0.72,
        "suggested_action": "BUY",
        "event_strength": "high",
        "prediction_type": "breakout",
        "event_phase": "early",
        "direct_catalyst": True,
    })

    _assert_bounded_features(features)
    assert features["analysis_type"] == "conflict"
    assert features["bullish_probability"] > features["bearish_probability"]
    assert features["bullish_force"] > features["bearish_force"] > 0.0
    assert features["impact_horizon"] == "short"
    assert features["expected_horizon"] == "intraday"
    assert features["analysis_basis"]["mode"] == "deterministic_fallback"
    assert "probabilities" in features["analysis_basis"]["fallback_fields"]


def test_legacy_continuation_event_becomes_directional_trend():
    features = normalize_ai_analysis({
        "sentiment_score": -0.40,
        "suggested_action": "SELL",
        "event_strength": "medium",
        "prediction_type": "continuation",
        "event_phase": "mid",
        "direct_catalyst": False,
    })

    _assert_bounded_features(features)
    assert features["analysis_type"] == "trend"
    assert features["bearish_probability"] > features["bullish_probability"]
    assert features["bearish_force"] > features["bullish_force"] > 0.0
    assert features["impact_horizon"] == "medium"
    assert features["expected_horizon"] == "1-3d"
    assert features["timeframe_match"] == "swing"


def test_explicit_probabilities_are_parsed_and_normalized():
    features = normalize_ai_analysis({
        "sentiment_score": 0.35,
        "suggested_action": "BUY",
        "event_strength": "medium",
        "prediction_type": "continuation",
        "event_phase": "mid",
        "analysis_type": "trend",
        "bullish_probability": "60%",
        "bearish_probability": "20%",
        "uncertainty": 0.30,
        "bullish_force": 9,  # invalid: must use deterministic fallback
        "bearish_force": 0.20,
        "impact_horizon": "intraday",
    })

    _assert_bounded_features(features)
    assert features["bullish_probability"] == 0.75
    assert features["bearish_probability"] == 0.25
    assert features["uncertainty"] == 0.30
    assert features["bullish_force"] < 1.0
    assert features["bearish_force"] == 0.20
    assert features["impact_horizon"] == "short"
    assert "forces" in features["analysis_basis"]["fallback_fields"]


def test_expected_macro_horizon_is_preserved():
    features = normalize_ai_analysis({
        "sentiment_score": 0.25,
        "suggested_action": "BUY",
        "event_strength": "medium",
        "prediction_type": "continuation",
        "event_phase": "mid",
        "expected_horizon": "1w+",
        "timeframe_match": "macro",
    })

    _assert_bounded_features(features)
    assert features["analysis_type"] == "trend"
    assert features["impact_horizon"] == "long"
    assert features["expected_horizon"] == "1w+"
    assert features["timeframe_match"] == "macro"


def test_non_finite_values_and_unsafe_windows_are_rejected():
    features = normalize_ai_analysis({
        "sentiment_score": float("nan"),
        "suggested_action": "BUY",
        "bullish_probability": "nan",
        "bearish_probability": None,
        "uncertainty": float("inf"),
        "impact_window": {
            "short": {"min_minutes": 100, "max_minutes": 10},
            "medium": {"min_minutes": 120, "max_minutes": 600},
            "long": {"min_minutes": 1, "max_minutes": 9_999_999},
        },
    })

    _assert_bounded_features(features)
    assert features["bullish_probability"] > 0.5
    assert features["impact_window"]["short"] == {
        "min_minutes": 0,
        "max_minutes": 120,
    }
    assert features["impact_window"]["medium"] == {
        "min_minutes": 120,
        "max_minutes": 600,
    }
    assert features["impact_window"]["long"] == {
        "min_minutes": 4320,
        "max_minutes": 43200,
    }


def test_stale_production_default_signature_is_recomputed():
    features = normalize_ai_analysis({
        "sentiment_score": -0.30,
        "suggested_action": "SELL",
        "event_strength": "high",
        "direct_catalyst": True,
        "prediction_type": "breakout",
        "event_phase": "early",
        "analysis_type": "trend",
        "bullish_probability": 0.5,
        "bearish_probability": 0.5,
        "bullish_force": 0.0,
        "bearish_force": 0.0,
        "impact_horizon": "medium",
    })

    _assert_bounded_features(features)
    assert features["analysis_type"] == "conflict"
    assert features["bearish_probability"] > features["bullish_probability"]
    assert features["bearish_force"] > features["bullish_force"] > 0.0
    assert features["impact_horizon"] == "short"
    assert features["analysis_basis"]["mode"] == "deterministic_fallback"
