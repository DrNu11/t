"""Decision-guard coverage for verified multi-timeframe market structure."""

import json

import pytest

import decision_guard


def _row(asset_context, *, asset="BTC", sentiment=0.0, macro_layers=None):
    context = {"assets": {asset: asset_context}}
    if macro_layers is not None:
        context["macro_context"] = {"layers": macro_layers}
    return {
        "sentiment_score": sentiment,
        "target_asset": asset,
        "market_confirmation": "unknown",
        "decision_context": json.dumps(context),
        "cluster_size": 1,
    }


def _asset(*, atr=2.0, structure=None):
    result = {
        "status": "ok",
        "decision_eligible": True,
        "change_24h_pct": 0.0,
        "funding_rate_pct": 0.0,
        "stats_7d": {"trend": "Range", "atr_pct": atr},
    }
    if structure is not None:
        result["market_structure"] = structure
    return result


def _structure(score, *, status="ok", eligible=True, trend=None, alignment=None):
    return {
        "status": status,
        "decision_eligible": eligible,
        "trend_score": score,
        "trend": trend or ("bullish" if score > 0 else "bearish"),
        "alignment": alignment or ("bullish" if score > 0 else "bearish"),
        "alignment_score": 0.75,
    }


@pytest.mark.parametrize(
    ("score", "trend", "alignment"),
    [(0.8, "bullish", "bullish"), (-0.7, "bearish", "bearish")],
)
def test_verified_price_structure_preserves_bullish_and_bearish_direction(
    score, trend, alignment,
):
    result = decision_guard.evaluate_decision(
        _row(_asset(structure=_structure(score, trend=trend, alignment=alignment)))
    )

    factor = result["factors"]["price_structure"]
    assert factor["score"] == score
    assert f"趋势={trend}" in factor["explanation"]
    assert f"多周期一致性={alignment}" in factor["explanation"]
    assert 0.10 <= result["weights"]["price_structure"] <= 0.15
    # Structure corroborates other evidence; by itself it cannot cross the
    # directional action threshold or bypass the evidence gate.
    assert result["raw_action"] == "HOLD"
    assert result["action"] == "HOLD"


@pytest.mark.parametrize(
    "structure",
    [
        _structure(0.9, status="unavailable"),
        _structure(0.9, eligible=False),
        _structure(2.0),
        {"status": "ok", "decision_eligible": True},
    ],
)
def test_unavailable_or_invalid_price_structure_is_ignored(structure):
    result = decision_guard.evaluate_decision(_row(_asset(structure=structure)))

    assert "price_structure" not in result["factors"]
    assert "price_structure" not in result["weights"]


def test_partial_but_eligible_price_structure_is_consumed():
    result = decision_guard.evaluate_decision(
        _row(_asset(structure=_structure(0.4, status="partial")))
    )

    assert result["factors"]["price_structure"]["score"] == 0.4
    assert "状态=partial" in result["factors"]["price_structure"]["explanation"]


def test_unknown_asset_never_falls_back_to_btc_market_data():
    btc = _asset(
        atr=9.0,
        structure=_structure(1.0),
    )
    context = {"assets": {"BTC": btc}}
    result = decision_guard.evaluate_decision({
        "sentiment_score": 0.0,
        "target_asset": "XAU",
        "market_confirmation": "confirmed",
        "decision_context": json.dumps(context),
        "cluster_size": 1,
    })

    for name in ("market_confirmation", "trend", "volatility", "funding"):
        assert result["factors"][name]["score"] == 0.0
        assert "质量门禁" in result["factors"][name]["explanation"]
    assert "price_structure" not in result["factors"]


def test_atr_changes_observation_but_never_direction_or_score():
    calm = decision_guard.evaluate_decision(_row(_asset(atr=0.2), sentiment=0.4))
    volatile = decision_guard.evaluate_decision(_row(_asset(atr=25.0), sentiment=0.4))

    assert calm["factors"]["volatility"]["score"] == 0.0
    assert volatile["factors"]["volatility"]["score"] == 0.0
    assert calm["weights"]["volatility"] == 0.0
    assert volatile["weights"]["volatility"] == 0.0
    assert calm["factors"]["volatility"]["explanation"] != volatile["factors"]["volatility"]["explanation"]
    assert calm["final_score"] == volatile["final_score"]
    assert calm["raw_action"] == volatile["raw_action"]


def test_cluster_and_history_are_direction_neutral_for_bearish_and_bullish_cases():
    bearish_dense = _row(_asset(), sentiment=-1.0)
    bearish_dense.update({
        "market_confirmation": "rejected",
        "cluster_size": 20,
        "history_sample": {"scope": "asset", "total": 100, "wins": 100},
    })
    bearish_sparse = _row(_asset(), sentiment=-1.0)
    bearish_sparse.update({
        "market_confirmation": "rejected",
        "cluster_size": 1,
        "history_sample": {"scope": "asset", "total": 100, "wins": 0},
    })
    bullish_dense = _row(_asset(), sentiment=1.0)
    bullish_dense.update({
        "market_confirmation": "confirmed",
        "cluster_size": 20,
        "history_sample": {"scope": "asset", "total": 100, "wins": 100},
    })

    bearish = decision_guard.evaluate_decision(bearish_dense)
    sparse = decision_guard.evaluate_decision(bearish_sparse)
    bullish = decision_guard.evaluate_decision(bullish_dense)

    for result in (bearish, sparse, bullish):
        assert result["factors"]["cluster_heat"]["score"] == 0.0
        assert result["weights"]["cluster_heat"] == 0.0
        assert result["factors"]["historical_confidence"]["score"] == 0.0
        assert result["weights"]["historical_confidence"] == 0.0
        assert result["factors"]["cluster_heat"]["role"] == "confidence_only"
        assert result["factors"]["historical_confidence"]["role"] == "confidence_only"
    assert bearish["final_score"] == sparse["final_score"]
    assert bearish["raw_action"] == sparse["raw_action"] == "SELL"
    assert bearish["action"] == "SELL"
    # A 0%-win formal history must veto execution; its small two-sided
    # chi-square p-value proves failure, not confidence.
    assert sparse["action"] == "HOLD"
    assert sparse["confidence_detail"]["significance_weight"] == 0.0
    assert bullish["raw_action"] == bullish["action"] == "BUY"
    assert bullish["final_score"] == pytest.approx(-bearish["final_score"])


def test_weights_are_normalised_with_structure_and_optional_factors():
    context = _row(
        _asset(structure=_structure(0.6)),
        sentiment=0.5,
        macro_layers={
            "structure": [
                {
                    "metric_key": "BTC.ls_ratio",
                    "status": "ok",
                    "decision_eligible": True,
                    "value": 1.2,
                },
            ],
        },
    )
    context["decision_features"] = {
        "bullish_probability": 0.7,
        "bearish_probability": 0.3,
        "bullish_force": 0.6,
        "bearish_force": 0.2,
    }
    result = decision_guard.evaluate_decision(context)

    assert sum(result["weights"].values()) == 1.0
    assert set(result["weights"]) == set(result["factors"])


def test_macro_funding_is_not_counted_twice_and_ratio_is_positioning():
    macro_layers = {
        "structure": [
            {
                "metric_key": "BTC.funding",
                "status": "ok",
                "decision_eligible": True,
                "value": 0.5,
            },
            {
                "metric_key": "BTC.ls_ratio",
                "status": "ok",
                "decision_eligible": True,
                "value": 1.3,
            },
        ],
    }
    result = decision_guard.evaluate_decision(
        _row(_asset(structure=_structure(0.2)), macro_layers=macro_layers)
    )

    assert result["factors"]["funding"]["score"] == 0.0
    assert "structure" not in result["factors"]
    assert result["factors"]["positioning"]["score"] < 0.0


def test_legacy_snapshot_without_market_structure_remains_compatible():
    result = decision_guard.evaluate_decision(_row(_asset(atr=3.0)))

    assert set(result["factors"]) == {
        "news_sentiment",
        "market_confirmation",
        "trend",
        "volatility",
        "funding",
        "cluster_heat",
        "historical_confidence",
    }
    assert "price_structure" not in result["weights"]
    assert sum(result["weights"].values()) == pytest.approx(1.0)


def test_outer_asset_gate_cannot_be_bypassed_by_nested_structure():
    asset = _asset(structure=_structure(0.9))
    asset["status"] = "unavailable"
    asset["decision_eligible"] = False

    result = decision_guard.evaluate_decision(_row(asset, sentiment=0.2))

    assert "price_structure" not in result["factors"]
    assert "price_structure" not in result["weights"]


def test_new_snapshot_field_gates_block_cross_source_stats_and_funding():
    asset = _asset(atr=8.0, structure=_structure(0.6, eligible=False))
    asset["stats_7d"] = {"trend": "Strong Bear", "atr_pct": 8.0}
    asset["funding_rate_pct"] = 0.5
    asset["stats_7d_decision_eligible"] = False
    asset["funding_decision_eligible"] = False

    result = decision_guard.evaluate_decision(_row(asset, sentiment=0.2))

    assert result["factors"]["trend"]["score"] == 0.0
    assert result["factors"]["funding"]["score"] == 0.0
    assert "quality" in result["factors"]["trend"]["explanation"].lower() or "质量门禁" in result["factors"]["trend"]["explanation"]
    assert "质量门禁" in result["factors"]["funding"]["explanation"]


def test_new_snapshot_change_gate_blocks_market_confirmation():
    asset = _asset(structure=_structure(0.3))
    asset["change_24h_pct"] = 4.0
    asset["change_24h_decision_eligible"] = False
    row = _row(asset)
    row["market_confirmation"] = "confirmed"

    result = decision_guard.evaluate_decision(row)

    assert result["factors"]["market_confirmation"]["score"] == 0.0
    assert "24h" in result["factors"]["market_confirmation"]["explanation"]


def test_verified_structure_replaces_legacy_trend_instead_of_double_counting():
    asset = _asset(structure=_structure(0.7))
    asset["stats_7d"] = {"trend": "Strong Bull", "atr_pct": 2.0}
    asset["stats_7d_decision_eligible"] = True
    asset["funding_decision_eligible"] = True

    result = decision_guard.evaluate_decision(_row(asset))

    assert result["factors"]["price_structure"]["score"] == 0.7
    assert result["factors"]["trend"]["score"] == 0.0
    assert result["weights"]["trend"] == 0.0
    assert "避免重复计权" in result["factors"]["trend"]["explanation"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_directional_values_are_neutral(value):
    result = decision_guard.evaluate_decision({
        "sentiment_score": value,
        "target_asset": "BTC",
        "market_confirmation": "unknown",
        "decision_context": json.dumps({
            "assets": {
                "BTC": {
                    "status": "ok",
                    "decision_eligible": True,
                    "change_24h_pct": value,
                }
            }
        }),
        "cluster_size": 1,
    })

    assert result["factors"]["news_sentiment"]["score"] == 0.0
    assert result["factors"]["market_confirmation"]["score"] == 0.0
    assert result["final_score"] == 0.0
