#!/usr/bin/env python3
"""
Trident Agent MVP — Central Configuration
==========================================

Single entry point for environment loading, API credentials, DB path and
tuning thresholds. Both engine.py and api_server.py import this module
(plain `import config` — backend/src_python is on sys.path at runtime).

.env is loaded exactly once, here, from backend/.env (parent of src_python/).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timezone, timedelta
from typing import Any, Dict

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & .env
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/

load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.getenv("TRIDENT_DB_PATH") or os.path.join(BASE_DIR, "trident_event_bus.db")
PROJECT_DIR = os.path.dirname(BASE_DIR)
TZ_SHANGHAI = timezone(timedelta(hours=8))

# ── File upload ────────────────────────────────────────────────
UPLOAD_DIR = os.path.abspath(os.getenv(
    "UPLOAD_DIR", os.path.join(PROJECT_DIR, "backend", "uploads")
))
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf",
    "text/plain", "text/csv", "text/html",
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Binance Futures trading.  A second explicit opt-in is required even when
# API keys exist; this keeps the new analysis/paper-trading work non-destructive.
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_USE_TESTNET = _env_bool("BINANCE_USE_TESTNET", False)
BINANCE_LIVE_TRADING_ENABLED = _env_bool("BINANCE_LIVE_TRADING_ENABLED", False)
BINANCE_HEDGE_MODE_ENABLED = _env_bool("BINANCE_HEDGE_MODE_ENABLED", False)
BINANCE_NOTIONAL_USDT = float(os.getenv("BINANCE_NOTIONAL_USDT", "30"))
BINANCE_LEVERAGE = int(os.getenv("BINANCE_LEVERAGE", "5"))
BINANCE_SIGNAL_THRESHOLD = float(os.getenv("BINANCE_SIGNAL_THRESHOLD", "0.5"))
BINANCE_TRAILING_CALLBACK_RATE = float(os.getenv("BINANCE_TRAILING_CALLBACK_RATE", "0.5"))
BINANCE_NEWS_API_URL = os.getenv(
    "BINANCE_NEWS_API_URL", "http://127.0.0.1:8000/api/trading/latest"
).strip()
BINANCE_POLL_INTERVAL = float(os.getenv("BINANCE_POLL_INTERVAL", "2"))
BINANCE_REQUEST_TIMEOUT = float(os.getenv("BINANCE_REQUEST_TIMEOUT", "5"))
BINANCE_STATE_PATH = os.path.abspath(os.getenv(
    "BINANCE_STATE_PATH", os.path.join(PROJECT_DIR, "latest_news.json")
))
BINANCE_LOG_PATH = os.path.abspath(os.getenv(
    "BINANCE_LOG_PATH", os.path.join(PROJECT_DIR, "news_trading.log")
))
TRADING_MANAGER_TOKEN = os.getenv("TRADING_MANAGER_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# LLM API credentials
# ---------------------------------------------------------------------------

# DeepSeek API (OpenAI-compatible endpoint)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Aiping API — primary news analysis and Data Copilot endpoint
AIPING_API_KEY = os.getenv("AIPING_API_KEY", "").strip()
AIPING_BASE_URL = os.getenv(
    "AIPING_BASE_URL", "https://www.aiping.cn/api/v1"
).strip().rstrip("/")
AIPING_MODEL = os.getenv("AIPING_MODEL", "DeepSeek-V4-Flash-0731").strip()
# json_mode: whether to send response_format={"type":"json_object"}.
# Some gateways/models don't support it (hang/timeout) — set AIPING_JSON_MODE=false.
AIPING_JSON_MODE = _env_bool("AIPING_JSON_MODE", True)
# Aiping-specific provider-routing body — only sent when actually talking to
# Aiping. Other OpenAI-compatible endpoints (e.g. custom relay) hang on it.
if "aiping.cn" in AIPING_BASE_URL:
    AIPING_EXTRA_BODY: Dict[str, Any] = {
        "enable_thinking": False,
        "provider": {
            "only": [],
            "order": [],
            "input_price_range": [],
            "output_price_range": [],
            "input_length_range": [],
            "output_length_range": [],
            "throughput_range": [],
            "latency_range": [],
            "sort": None,
        },
    }
else:
    AIPING_EXTRA_BODY: Dict[str, Any] = {}

# OpenRouter remains available for webhook translation and realtime_filter only.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")

# OpenRouter AI-analysis channel.  Keep this separate from the legacy
# OPENROUTER_* variables used by webhook translation/realtime_filter so a new
# official key or endpoint cannot silently change those integrations.
OPENROUTER_AI_API_KEY = os.getenv("OPENROUTER_AI_API_KEY", "").strip()
OPENROUTER_AI_BASE_URL = os.getenv(
    "OPENROUTER_AI_BASE_URL", "https://openrouter.ai/api/v1"
).strip().rstrip("/")
OPENROUTER_AI_JSON_MODE = _env_bool("OPENROUTER_AI_JSON_MODE", True)
OPENROUTER_AI_MAX_CONCURRENCY = max(
    1, int(os.getenv("OPENROUTER_AI_MAX_CONCURRENCY", "1"))
)
OPENROUTER_AI_BATCH_SIZE = max(
    1, int(os.getenv("OPENROUTER_AI_BATCH_SIZE", "2"))
)
AI_TRANSIENT_MAX_RETRIES = max(
    0, int(os.getenv("AI_TRANSIENT_MAX_RETRIES", "5"))
)
AI_TRANSIENT_RETRY_BASE_SECONDS = max(
    15, int(os.getenv("AI_TRANSIENT_RETRY_BASE_SECONDS", "60"))
)
AI_TRANSIENT_RETRY_MAX_SECONDS = max(
    AI_TRANSIENT_RETRY_BASE_SECONDS,
    int(os.getenv("AI_TRANSIENT_RETRY_MAX_SECONDS", "900")),
)
OPENROUTER_AI_MODEL_IDS = tuple(
    item.strip()
    for item in os.getenv(
        "OPENROUTER_AI_MODEL_IDS",
        "stealth/ox-alpha,z-ai/glm-5.2:free,nvidia/nemotron-3.5-lightning:free",
    ).split(",")
    if item.strip()
)

_OPENROUTER_AI_MODEL_LABELS = {
    "stealth/ox-alpha": "Ox Alpha (OpenRouter 免费)",
    "z-ai/glm-5.2:free": "GLM 5.2 (OpenRouter 免费)",
    "nvidia/nemotron-3.5-lightning:free": "Nemotron 3.5 Lightning (OpenRouter 免费)",
}

_AI_MODEL_ROSTER = [{"id": AIPING_MODEL, "label": "DeepSeek V4 Flash 0731 (Aiping)"}]
if OPENROUTER_AI_API_KEY:
    _AI_MODEL_ROSTER.extend(
        {
            "id": model_id,
            "label": _OPENROUTER_AI_MODEL_LABELS.get(
                model_id, f"{model_id} (OpenRouter)"
            ),
            "provider": "openrouter",
        }
        for model_id in OPENROUTER_AI_MODEL_IDS
    )
AI_MODEL_ROSTER = tuple(_AI_MODEL_ROSTER)
DEFAULT_AI_MODEL_ID = AIPING_MODEL
AI_MODEL_STATE_PATH = os.path.abspath(os.path.join(BASE_DIR, "runtime", "ai_model.json"))


def get_selected_ai_model_id() -> str:
    try:
        with open(AI_MODEL_STATE_PATH, "r", encoding="utf-8") as stream:
            model_id = json.load(stream).get("model_id")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return DEFAULT_AI_MODEL_ID
    valid_ids = {model["id"] for model in AI_MODEL_ROSTER}
    return model_id if model_id in valid_ids else DEFAULT_AI_MODEL_ID


def write_selected_ai_model_id(model_id: str) -> str:
    valid_ids = {model["id"] for model in AI_MODEL_ROSTER}
    if model_id not in valid_ids:
        raise ValueError("unsupported AI model")

    state_dir = os.path.dirname(AI_MODEL_STATE_PATH)
    os.makedirs(state_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="ai_model_", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"model_id": model_id}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, AI_MODEL_STATE_PATH)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    return model_id

# xAI (Grok) API — OpenAI-compatible direct endpoint
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"

# Doubao (火山引擎) API — OpenAI-compatible endpoint
# NOTE: DOUBAO_MODEL must be an Endpoint ID (ep-xxxxx), NOT a model name string.
# Create an Inference Endpoint at https://console.volces.com/ark before setting this.
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_BASE_URL = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "")

# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

TECHFLOW_NEWS_URL = os.getenv(
    "TECHFLOW_NEWS_URL",
    "https://www.techflowpost.com/api/client/newsflashes?page=1&page_size=50",
).strip()
EASTMONEY_NEWS_URL = os.getenv(
    "EASTMONEY_NEWS_URL",
    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=345&order=1&needInteractData=0&page_index=1&page_size=50",
).strip()
BLOCKBEATS_NEWS_URL = os.getenv(
    "BLOCKBEATS_NEWS_URL",
    "https://api.theblockbeats.news/v1/open-api/open-flash?size=50&page=1&type=push&lang=cn",
).strip()
# Jin10 Open Data (requires an authorized secret-key; disabled by default).
JIN10_ENABLED = _env_bool("JIN10_ENABLED", False)
JIN10_API_KEY = os.getenv("JIN10_API_KEY", "").strip()
JIN10_FLASH_URL = os.getenv(
    "JIN10_FLASH_URL", "https://open-data-api.jin10.com/data-api/flash"
).strip()
JIN10_QUOTE_URL = os.getenv(
    "JIN10_QUOTE_URL", "https://open-data-api.jin10.com/data-api/quotes"
).strip()
JIN10_SYMBOLS_URL = os.getenv(
    "JIN10_SYMBOLS_URL", "https://open-data-api.jin10.com/data-api/symbols"
).strip()
# The calendar route varies by Jin10 entitlement/version, so require an
# explicit URL instead of guessing and silently querying the wrong endpoint.
JIN10_CALENDAR_URL = os.getenv("JIN10_CALENDAR_URL", "").strip()
JIN10_CALENDAR_CATEGORY = os.getenv("JIN10_CALENDAR_CATEGORY", "cj").strip()
JIN10_FLASH_CATEGORIES = os.getenv("JIN10_FLASH_CATEGORIES", "1,2,3,4,5").strip()
JIN10_MARKET_TYPE = os.getenv("JIN10_MARKET_TYPE", "GOODS").strip()
JIN10_MARKET_CODES = os.getenv("JIN10_MARKET_CODES", "XAUUSD").strip()
JIN10_REQUEST_TIMEOUT = max(1.0, float(os.getenv("JIN10_REQUEST_TIMEOUT", "5")))
JIN10_POLL_SECONDS = max(10, int(os.getenv("JIN10_POLL_SECONDS", "15")))
# Optional real XAU/USD broker quote.  It is intentionally separate from the
# OKX XAU swap used by market-structure analysis and is disabled by default.
OANDA_ENABLED = _env_bool("OANDA_ENABLED", False)
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxtrade.oanda.com").strip().rstrip("/")
OANDA_INSTRUMENTS = os.getenv("OANDA_INSTRUMENTS", "XAU_USD").strip()
OANDA_REQUEST_TIMEOUT = max(1.0, float(os.getenv("OANDA_REQUEST_TIMEOUT", "5")))
# Free keyless gold cross-checks. Both are candidate-only and never replace
# the canonical OKX XAU-USDT-SWAP structure/settlement instrument.
BINANCE_PAXG_ENABLED = _env_bool("BINANCE_PAXG_ENABLED", False)
BINANCE_PAXG_BASE_URL = os.getenv(
    "BINANCE_PAXG_BASE_URL", "https://data-api.binance.vision"
).strip().rstrip("/")
BINANCE_PAXG_SYMBOL = os.getenv("BINANCE_PAXG_SYMBOL", "PAXGUSDT").strip().upper()
BINANCE_PAXG_REQUEST_TIMEOUT = max(1.0, float(os.getenv("BINANCE_PAXG_REQUEST_TIMEOUT", "3")))
COINGECKO_PAXG_ENABLED = _env_bool("COINGECKO_PAXG_ENABLED", False)
COINGECKO_PAXG_BASE_URL = os.getenv("COINGECKO_PAXG_BASE_URL", "https://api.coingecko.com/api/v3").strip().rstrip("/")
COINGECKO_PAXG_COIN_ID = os.getenv("COINGECKO_PAXG_COIN_ID", "pax-gold").strip()
COINGECKO_PAXG_REQUEST_TIMEOUT = max(1.0, float(os.getenv("COINGECKO_PAXG_REQUEST_TIMEOUT", "5")))
COINGECKO_PAXG_CACHE_SECONDS = max(1.0, float(os.getenv("COINGECKO_PAXG_CACHE_SECONDS", "60")))
# Optional licensed calendar aggregator.  An explicit URL and credentials are
# required; no guest/demo credentials are embedded in the project.
TRADING_ECONOMICS_ENABLED = _env_bool("TRADING_ECONOMICS_ENABLED", False)
TRADING_ECONOMICS_CREDENTIALS = os.getenv("TRADING_ECONOMICS_CREDENTIALS", "").strip()
TRADING_ECONOMICS_CALENDAR_URL = os.getenv("TRADING_ECONOMICS_CALENDAR_URL", "").strip()
TRADING_ECONOMICS_COUNTRIES = os.getenv("TRADING_ECONOMICS_COUNTRIES", "United States").strip()
TRADING_ECONOMICS_REQUEST_TIMEOUT = max(1.0, float(os.getenv("TRADING_ECONOMICS_REQUEST_TIMEOUT", "8")))
# Free official publisher schedules; no commercial credential is required.
# The adapter combines original BLS/BEA/Fed/Census pages and fails closed if
# one publisher blocks a request or changes its HTML shape.
OFFICIAL_MACRO_CALENDAR_ENABLED = _env_bool("OFFICIAL_MACRO_CALENDAR_ENABLED", False)
OFFICIAL_BLS_SCHEDULE_URL = os.getenv(
    "OFFICIAL_BLS_SCHEDULE_URL",
    "https://www.bls.gov/schedule/{year}/{month:02d}_sched_list.htm",
).strip()
OFFICIAL_BEA_SCHEDULE_URL = os.getenv(
    "OFFICIAL_BEA_SCHEDULE_URL", "https://www.bea.gov/news/schedule"
).strip()
OFFICIAL_FED_FOMC_URL = os.getenv(
    "OFFICIAL_FED_FOMC_URL",
    "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
).strip()
OFFICIAL_CENSUS_SCHEDULE_URL = os.getenv(
    "OFFICIAL_CENSUS_SCHEDULE_URL",
    "https://www.census.gov/economic-indicators/calendar-listview.html",
).strip()
OFFICIAL_MACRO_REQUEST_TIMEOUT = max(1.0, float(os.getenv("OFFICIAL_MACRO_REQUEST_TIMEOUT", "8")))
ALLOW_LEGACY_DECISIONS = _env_bool("TRIDENT_ALLOW_LEGACY_DECISIONS", False)
ALLOW_LEGACY_PRICE_FALLBACKS = _env_bool("TRIDENT_ALLOW_LEGACY_PRICE_FALLBACKS", False)
# A disabled evidence gate is useful only for explicitly isolated research.
# Production/paper decision streams fail closed unless an operator opts in.
PAPER_ALLOW_GATE_BYPASS = _env_bool("TRIDENT_PAPER_ALLOW_GATE_BYPASS", False)
MACRO_CALENDAR_ENABLED = _env_bool("MACRO_CALENDAR_ENABLED", False)
MACRO_CALENDAR_POLL_SECONDS = max(15, int(os.getenv("MACRO_CALENDAR_POLL_SECONDS", "60")))
NEWS_SOURCE_POLL_SECONDS = max(10, int(os.getenv("NEWS_SOURCE_POLL_SECONDS", "15")))
NEWS_SOURCE_PAGES = max(1, int(os.getenv("NEWS_SOURCE_PAGES", "6")))
NEWS_DEFAULT_DAILY_TARGET = max(300, int(os.getenv("NEWS_DEFAULT_DAILY_TARGET", "300")))
# FinancialJuice can rate-limit its public homepage/token bootstrap.  Back off
# instead of hammering it every five seconds while the independent candidate
# source watcher continues serving the live board.
FJ_RECONNECT_MIN_SECONDS = max(5, int(os.getenv("FJ_RECONNECT_MIN_SECONDS", "30")))
FJ_RECONNECT_MAX_SECONDS = max(
    FJ_RECONNECT_MIN_SECONDS,
    int(os.getenv("FJ_RECONNECT_MAX_SECONDS", "300")),
)
# Candidate feeds may be stored for the live news board.  They can optionally
# be sent to the selected LLM for observation-only analysis; the separate
# quality gate in the worker still forbids candidate rows from paper/live
# positions and settled performance history.
NEWS_CANDIDATE_DISPLAY_ENABLED = _env_bool("NEWS_CANDIDATE_DISPLAY_ENABLED", False)
CANDIDATE_AI_ANALYSIS_ENABLED = _env_bool(
    "TRIDENT_CANDIDATE_AI_ANALYSIS_ENABLED", False
)
EVENTS_LIST_MAX = max(200, int(os.getenv("EVENTS_LIST_MAX", "10000")))
PAPER_INITIAL_EQUITY_USDT = max(0.0, float(os.getenv("PAPER_INITIAL_EQUITY_USDT", "10000")))

# 快速模拟评测：所有 BUY/SELL 信号按真实行情建仓，短周期结算，用于盈利率评测。
# 与正式模拟盘（多证据闸门 + 策略闸门）互不干扰，闸门结论只作为分组标记保存。
QUICK_SIM_ENABLED = os.getenv("QUICK_SIM_ENABLED", "1").strip() not in ("0", "false", "False")
QUICK_SIM_HORIZON_MINUTES = max(1, int(os.getenv("QUICK_SIM_HORIZON_MINUTES", "5")))
QUICK_SIM_NOTIONAL_USDT = max(1.0, float(os.getenv("QUICK_SIM_NOTIONAL_USDT", "100")))
QUICK_SIM_POLL_SECONDS = max(5, int(os.getenv("QUICK_SIM_POLL_SECONDS", "15")))

# 飞书告警机器人 webhook — 为空时 engine 只打印一次警告并跳过发送
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")

# 新闻写库到 SSE 的内部增量检测周期（毫秒）。第三方上游延迟不受此项控制。
NEWS_WATCH_INTERVAL_MS = max(20, min(1000, int(os.getenv("NEWS_WATCH_INTERVAL_MS", "50"))))

# CORS 白名单 — 逗号分隔，默认本地 Next.js 开发端口
_cors_raw = os.getenv("CORS_ALLOW_ORIGINS", "")
CORS_ALLOW_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:3002",
    "http://127.0.0.1:3002",
    "http://localhost:3030",
    "http://127.0.0.1:3030",
]

# ---------------------------------------------------------------------------
# Tuning thresholds (数值与 engine.py 原定义一致，勿改)
# ---------------------------------------------------------------------------

# VIP KOL monitoring
VIP_KOLS: Dict[str, str] = {
    "Trump":   "[VIP:TRUMP]", "特朗普": "[VIP:TRUMP]",
    "Musk":    "[VIP:MUSK]",  "马斯克": "[VIP:MUSK]", "Elon": "[VIP:MUSK]",
    "Powell":  "[VIP:FED]",   "鲍威尔": "[VIP:FED]", "FOMC": "[VIP:FED]", "美联储": "[VIP:FED]",
    "Vance":   "[VIP:OTHER]", "万斯": "[VIP:OTHER]", "Bessent": "[VIP:OTHER]",
}
VIP_SCORE_BOOST = 1.25

# Active-trade aggregation
_AGG_WINDOW_HOURS = 1          # how long a parent stays "active" (tight window to avoid over-clustering)
_AGG_MIN_SCORE    = 0.25       # only aggregate signals with |score| >= this

# AI worker batch size
BATCH_SIZE = 10

# 内置 Hermes 多 agent 写作驱动：分席简报 + 已结算技能回灌（不改主 prompt 语义）
HERMES_AGENT_ENABLED = os.getenv("HERMES_AGENT_ENABLED", "1").strip() not in ("0", "false", "False")
HERMES_SKILL_MIN_SAMPLE = max(1, int(os.getenv("HERMES_SKILL_MIN_SAMPLE", "8")))

# 新闻之外的盘面/舆情/加息预期/资金流（失败标 unavailable，不造数）
MACRO_CONTEXT_ENABLED = os.getenv("MACRO_CONTEXT_ENABLED", "1").strip() not in ("0", "false", "False")
OKX_PUBLIC_API_BASE = os.getenv("OKX_PUBLIC_API_BASE", "https://www.okx.com").strip().rstrip("/")
SOSOVALUE_ETF_URL = os.getenv("SOSOVALUE_ETF_URL", "").strip()

# Asset-specific impact thresholds for forward-tracker verdict ruling
IMPACT_THRESHOLD = {"BTC": 2.0, "ETH": 2.0, "SOL": 2.0,
                    "XAU": 1.0, "GOLD": 1.0,
                    "WTI": 1.5}
