"""Smoke tests for src_python/config.py — single config entry point."""

import os
import subprocess
import sys

import conftest  # tests/conftest.py (sys.path bootstrap lives there)

import config


def test_config_importable():
    assert config.BASE_DIR
    assert config.TZ_SHANGHAI is not None


def test_db_path_default():
    expected = os.path.join(config.BASE_DIR, "trident_event_bus.db")
    # Only meaningful when TRIDENT_DB_PATH is not set in the environment
    if not os.getenv("TRIDENT_DB_PATH"):
        assert config.DB_PATH == expected
        assert config.DB_PATH.endswith(os.path.join("backend", "trident_event_bus.db"))


def test_db_path_env_override(tmp_path):
    """TRIDENT_DB_PATH overrides the default — verified in a subprocess so
    the reload cannot pollute config state for other test modules."""
    override = str(tmp_path / "override_event_bus.db")
    env = os.environ.copy()
    env["TRIDENT_DB_PATH"] = override
    out = subprocess.run(
        [sys.executable, "-c", "import config; print(config.DB_PATH)"],
        capture_output=True, text=True, env=env, cwd=conftest.SRC_PYTHON,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == override


def test_thresholds():
    assert config.VIP_SCORE_BOOST == 1.25
    assert config.BATCH_SIZE == 10
    assert config.IMPACT_THRESHOLD["BTC"] == 2.0
    assert "[VIP:TRUMP]" in config.VIP_KOLS.values()
    assert 20 <= config.NEWS_WATCH_INTERVAL_MS <= 1000


def test_cors_default():
    if not os.getenv("CORS_ALLOW_ORIGINS"):
        assert "http://localhost:3000" in config.CORS_ALLOW_ORIGINS
        assert "http://127.0.0.1:3000" in config.CORS_ALLOW_ORIGINS


def test_ai_model_selection_defaults_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL_STATE_PATH", str(tmp_path / "runtime" / "ai_model.json"))
    assert config.get_selected_ai_model_id() == config.DEFAULT_AI_MODEL_ID


def test_ai_model_roster_contains_only_public_aiping_model():
    assert config.AIPING_BASE_URL == "https://www.aiping.cn/api/v1"
    assert config.AIPING_MODEL == "DeepSeek-V4-Flash-0731"
    assert config.DEFAULT_AI_MODEL_ID == config.AIPING_MODEL
    assert config.AI_MODEL_ROSTER == (
        {"id": config.AIPING_MODEL, "label": "DeepSeek V4 Flash 0731 (Aiping)"},
    )
    assert "api_key" not in config.AI_MODEL_ROSTER[0]


def test_ai_model_selection_atomic_write(tmp_path, monkeypatch):
    state_path = tmp_path / "runtime" / "ai_model.json"
    monkeypatch.setattr(config, "AI_MODEL_STATE_PATH", str(state_path))
    selected = config.AIPING_MODEL

    assert config.write_selected_ai_model_id(selected) == selected
    assert config.get_selected_ai_model_id() == selected
    assert list(state_path.parent.glob("ai_model_*.json")) == []


def test_ai_model_selection_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL_STATE_PATH", str(tmp_path / "ai_model.json"))
    try:
        config.write_selected_ai_model_id("unknown/model")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown model must be rejected")
