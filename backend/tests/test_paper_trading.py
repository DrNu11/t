import pytest

import paper_trading


def test_paper_trading_requires_track(temp_db):
    with pytest.raises(ValueError):
        paper_trading.set_settings(True, [], temp_db)


def test_paper_trading_tracks_and_asset_filter(temp_db):
    settings = paper_trading.set_settings(True, ["crypto", "gold", "invalid"], temp_db)
    assert settings["is_running"] is True
    assert settings["tracks"] == ["crypto", "gold"]
    assert settings["active_run_id"] is not None
    assert settings["active_run"]["agent_version"] == paper_trading.AGENT_VERSION
    assert settings["active_run"]["strategy_version_id"] is not None
    assert settings["active_run"]["spec_version"]
    assert paper_trading.asset_allowed("BTC", settings)
    assert paper_trading.asset_allowed("SOL", settings)
    assert paper_trading.asset_allowed("XAU", settings)
    assert not paper_trading.asset_allowed("WTI", settings)

    stopped = paper_trading.set_settings(False, settings["tracks"], temp_db)
    assert stopped["is_running"] is False
    assert stopped["active_run_id"] is None
    run = temp_db.execute(
        "SELECT status, stopped_at FROM paper_trading_runs WHERE id=?",
        (settings["active_run_id"],),
    ).fetchone()
    assert run[0] == "STOPPED"
    assert run[1]


def test_paper_trading_gate_toggle(temp_db):
    started = paper_trading.set_settings(True, ["crypto"], temp_db, gate_enabled=False)
    assert started["gate_enabled"] is False
    assert "宽松" in started["active_run"]["activation_reason"]
    tightened = paper_trading.set_settings(True, ["crypto"], temp_db, gate_enabled=True)
    assert tightened["gate_enabled"] is True
    loosened = paper_trading.set_settings(True, ["crypto", "oil"], temp_db, gate_enabled=False)
    assert loosened["is_running"] is True
    assert loosened["tracks"] == ["crypto", "oil"]
    assert loosened["gate_enabled"] is False
