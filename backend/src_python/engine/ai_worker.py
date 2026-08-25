"""Task B — Concurrent Batch AI Worker
+ stdlib-only OpenAI-compatible LLM client + model roster + performance feedback.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from config import (
    AI_MODEL_ROSTER,
    DEFAULT_AI_MODEL_ID,
    AIPING_API_KEY,
    AIPING_BASE_URL,
    AIPING_EXTRA_BODY,
    AIPING_JSON_MODE,
    OPENROUTER_AI_API_KEY,
    OPENROUTER_AI_BASE_URL,
    OPENROUTER_AI_JSON_MODE,
    OPENROUTER_AI_BATCH_SIZE,
    OPENROUTER_AI_MAX_CONCURRENCY,
    AI_TRANSIENT_MAX_RETRIES,
    AI_TRANSIENT_RETRY_BASE_SECONDS,
    AI_TRANSIENT_RETRY_MAX_SECONDS,
    get_selected_ai_model_id,
    VIP_SCORE_BOOST,
    BATCH_SIZE,
    HERMES_AGENT_ENABLED,
    MACRO_CONTEXT_ENABLED,
    ALLOW_LEGACY_DECISIONS,
    CANDIDATE_AI_ANALYSIS_ENABLED,
    _AGG_WINDOW_HOURS,
    _AGG_MIN_SCORE,
)

# 市场快照 — 每轮 AI batch 前拉取一次 BTC/XAU 行情
from market_snapshot import get_snapshot
import strategy_store
import paper_trading
import decision_guard
import evidence
import hermes_agent
import macro_context
import timeseries
from decision_features import merge_into_context

from .alerts import send_feishu_alert
from .prices import _get_current_price
from .utils import _detect_vip, _now, _open_db, _ts

# HOLD 弱分阈值：|score| 不超过此值时视为中性观望，不伪造符号。
_NEUTRAL_SCORE_EPS = 0.001
# Candidate rows may be analysed by the selected model when explicitly
# enabled, but the later ``quality_allows_paper_position`` gate remains
# verified-only.  This lets Ox Alpha provide an observation without turning an
# unverified feed into a trade or a settled training sample.
_ANALYSIS_QUALITY_STATUSES = (
    ("verified", "candidate")
    if CANDIDATE_AI_ANALYSIS_ENABLED
    else ("verified",)
)
_ALLOWED_QUALITY_STATUSES = (
    _ANALYSIS_QUALITY_STATUSES + ("legacy",)
    if ALLOW_LEGACY_DECISIONS
    else _ANALYSIS_QUALITY_STATUSES
)

# Current market/macro state is valid only for genuinely live news.  Five
# minutes is deliberately strict: when timestamps are ambiguous, stale or in
# the future, the worker would rather omit context than leak future data into
# research, replay or backtest decisions.
_MARKET_CONTEXT_REALTIME_WINDOW_S = 5 * 60
_PROCESSING_LEASE_SECONDS = 15 * 60
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)
_NUMERIC_TIMESTAMP_RE = re.compile(r"^[+]?(?:\d+(?:\.\d+)?|\.\d+)$")


def _epoch_seconds(value: Any) -> Optional[float]:
    """Normalize Unix seconds/milliseconds; reject other numeric magnitudes."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if 1_000_000_000 <= number < 100_000_000_000:
        return number
    if 1_000_000_000_000 <= number < 100_000_000_000_000:
        return number / 1000.0
    return None


def _news_timestamp_epoch(
    value: Any,
    *,
    snapshot_epoch: float,
) -> Optional[float]:
    """Parse a dated news timestamp relative to a trusted snapshot epoch.

    Accepted contracts are full ISO datetimes and Unix seconds/milliseconds.
    Naive ISO values are interpreted in Asia/Shanghai.  Clock-only values are
    deliberately rejected because binding them to today's date could make a
    historical/replayed item appear live.
    """

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_seconds(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _NUMERIC_TIMESTAMP_RE.fullmatch(text):
        return _epoch_seconds(text)

    try:
        # Date-only and minute-only values are intentionally not accepted.
        if not _ISO_DATETIME_RE.fullmatch(text):
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def evaluate_news_context_eligibility(
    news_timestamp: Any,
    snapshot_epoch: Any,
    *,
    realtime_window_seconds: int = _MARKET_CONTEXT_REALTIME_WINDOW_S,
) -> Dict[str, Any]:
    """Pure, fail-closed timestamp gate for current market/macro context."""

    snapshot_s = _epoch_seconds(snapshot_epoch)
    raw_timestamp = "" if news_timestamp is None else str(news_timestamp).strip()
    base: Dict[str, Any] = {
        "market_context_eligible": False,
        "timestamp_mismatch": True,
        "market_context_reason": "snapshot_epoch_invalid",
        "news_timestamp_raw": raw_timestamp,
        "news_epoch_ms": None,
        "snapshot_epoch_ms": (
            int(round(snapshot_s * 1000)) if snapshot_s is not None else None
        ),
        "context_age_seconds": None,
        "realtime_window_seconds": int(realtime_window_seconds),
    }
    if snapshot_s is None:
        return base
    if not raw_timestamp:
        base["market_context_reason"] = "news_timestamp_missing"
        return base

    news_s = _news_timestamp_epoch(news_timestamp, snapshot_epoch=snapshot_s)
    if news_s is None:
        base["market_context_reason"] = "news_timestamp_unparseable"
        return base

    age_s = snapshot_s - news_s
    base["news_epoch_ms"] = int(round(news_s * 1000))
    base["context_age_seconds"] = round(age_s, 3)
    if age_s < 0:
        base["market_context_reason"] = "news_timestamp_after_snapshot"
        return base
    if age_s > max(0, int(realtime_window_seconds)):
        base["market_context_reason"] = "news_timestamp_outside_realtime_window"
        return base

    base.update({
        "market_context_eligible": True,
        "timestamp_mismatch": False,
        "market_context_reason": "within_realtime_window",
    })
    return base


def build_news_context_package(
    news_timestamp: Any,
    snapshot: Any,
    macro_payload: Any = None,
) -> Dict[str, Any]:
    """Build one news item's prompt/context without leaking current state.

    The returned package is deterministic for its arguments.  Ineligible
    events receive an audit-only decision context containing timestamp gate
    metadata; current assets, summaries and macro layers are omitted entirely.
    """

    safe_snapshot = snapshot if isinstance(snapshot, dict) else {}
    eligibility = evaluate_news_context_eligibility(
        news_timestamp,
        safe_snapshot.get("epoch_ms"),
    )
    snapshot_status = str(safe_snapshot.get("status") or "unavailable").lower()
    snapshot_assets = safe_snapshot.get("assets")
    snapshot_ready = (
        snapshot_status in {"ok", "partial"}
        and isinstance(snapshot_assets, dict)
        and any(
            isinstance(item, dict)
            and item.get("status") == "ok"
            and item.get("decision_eligible") is True
            for item in snapshot_assets.values()
        )
    )
    eligibility["snapshot_status"] = snapshot_status
    if eligibility["market_context_eligible"] and not snapshot_ready:
        eligibility.update({
            "market_context_eligible": False,
            "timestamp_mismatch": False,
            "market_context_reason": "snapshot_not_decision_ready",
        })
    if not eligibility["market_context_eligible"]:
        withheld = {
            "status": "context_withheld",
            **eligibility,
        }
        return {
            "market_context": "",
            "decision_context": json.dumps(
                withheld, ensure_ascii=False, separators=(",", ":"),
            ),
            "eligibility": eligibility,
        }

    market_context = str(safe_snapshot.get("summary") or "").strip()
    macro = macro_payload if isinstance(macro_payload, dict) else {}
    macro_summary = str(macro.get("summary") or "").strip()
    if macro_summary:
        market_context = (
            f"{market_context}\n\n{macro_summary}".strip()
            if market_context else macro_summary
        )

    decision_payload = dict(safe_snapshot)
    decision_payload.update(eligibility)
    decision_context = json.dumps(
        decision_payload, ensure_ascii=False, separators=(",", ":"),
    )
    if macro:
        decision_context = merge_into_context(decision_context, {}, macro)
    return {
        "market_context": market_context,
        "decision_context": decision_context,
        "eligibility": eligibility,
    }


def evaluate_target_market_context_eligibility(
    target_asset: Any,
    snapshot: Any,
    context_eligibility: Any,
) -> Dict[str, Any]:
    """Require a verified same-asset quote before aggregation or paper trade."""
    canonical = evidence.normalize_asset(target_asset)
    if not isinstance(context_eligibility, dict) or not context_eligibility.get(
        "market_context_eligible"
    ):
        return {
            "target_market_context_eligible": False,
            "target_market_context_reason": str(
                ((context_eligibility or {}).get("market_context_reason") or "context_unavailable")
                if isinstance(context_eligibility, dict) else "context_unavailable"
            ),
            "target_asset_context_key": canonical,
        }
    assets = snapshot.get("assets") if isinstance(snapshot, dict) else None
    asset_context = assets.get(canonical) if isinstance(assets, dict) else None
    eligible = bool(
        canonical
        and isinstance(asset_context, dict)
        and asset_context.get("status") == "ok"
        and asset_context.get("decision_eligible") is True
    )
    return {
        "target_market_context_eligible": eligible,
        "target_market_context_reason": (
            "verified_same_asset_context" if eligible else "target_asset_context_unavailable"
        ),
        "target_asset_context_key": canonical,
    }


def select_current_learning_context(
    context_eligibility: Any,
    *,
    performance_context: str = "",
    hermes_skills: Any = None,
) -> Dict[str, Any]:
    """Fail closed when selecting present-day learned context for one event."""

    eligible = bool(
        isinstance(context_eligibility, dict)
        and context_eligibility.get("market_context_eligible") is True
        and context_eligibility.get("timestamp_mismatch") is False
    )
    skills = hermes_skills if isinstance(hermes_skills, list) else []
    return {
        "eligible": eligible,
        "performance_context": str(performance_context or "") if eligible else "",
        "hermes_skills": list(skills) if eligible else [],
    }


def _authoritative_news_timestamp(news_row: Any) -> Any:
    """Prefer the canonical event epoch; use dated legacy text only if absent."""

    keys = news_row.keys() if hasattr(news_row, "keys") else ()
    if "ts" in keys and news_row["ts"] is not None:
        return news_row["ts"]
    if "timestamp" in keys:
        return news_row["timestamp"] or ""
    return ""


def _canonical_news_timestamp(value: Any) -> str:
    """Serialize the authoritative event clock for prompts and decision rows."""

    epoch = _epoch_seconds(value)
    if epoch is not None:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    return "" if value is None else str(value)


def _settled_sample_for_context(
    asset: str,
    *,
    target_market_context_eligible: bool,
    connection: sqlite3.Connection,
) -> Dict[str, Any]:
    """Never read present-day settled evidence for a non-live event."""

    if not target_market_context_eligible:
        return {}
    return evidence.collect_settled_sample(asset, connection=connection)


def reconcile_score_action(score: float, action: str) -> tuple[float, str]:
    """对齐 LLM 方向与分数符号，避免把观望误判成矛盾。

    - HOLD + 强分（|score|>0.3）按分数抬成 BUY/SELL
    - HOLD + 弱分保持 HOLD，分数归零，不再写成 +0.05
    - BUY/SELL 与分数符号相反时，以方向为准翻转分数
    - BUY/SELL 但分数接近 0 时，给一个同向弱分，便于聚合
    """
    action = (action or "HOLD").upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    score = max(-1.0, min(1.0, score))

    if action == "HOLD":
        if score > 0.3:
            return score, "BUY"
        if score < -0.3:
            return score, "SELL"
        return 0.0, "HOLD"

    if action == "BUY":
        if score <= 0:
            score = abs(score) if abs(score) >= _NEUTRAL_SCORE_EPS else 0.05
        return score, "BUY"

    if score >= 0:
        score = -abs(score) if abs(score) >= _NEUTRAL_SCORE_EPS else -0.05
    return score, "SELL"


def direction_consistent(action: str, score: float) -> bool:
    """HOLD 始终自洽；BUY 需正分，SELL 需负分。"""
    action = (action or "").upper()
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if action == "HOLD":
        return True
    if action == "BUY":
        return score > 0
    if action == "SELL":
        return score < 0
    return False


def quality_allows_paper_position(quality_status: Any) -> bool:
    """Only verified news may open a tracked paper-trading position.

    ``ALLOW_LEGACY_DECISIONS`` can deliberately admit old rows for research
    analysis, but it must never weaken this final position boundary.  Quick
    simulations remain the isolated place for candidate/unverified samples.
    """

    return str(quality_status or "").strip().lower() == "verified"


def _apply_vip_score_boost(
    score: float, action: str, news_content: str,
) -> tuple[float, str]:
    """Apply the VIP multiplier before dynamic fields are normalised."""

    vip_tag, _ = _detect_vip(news_content or "")
    if vip_tag and action in {"BUY", "SELL"}:
        boosted = round(float(score) * VIP_SCORE_BOOST, 4)
        if abs(boosted) <= 1.0:
            return boosted, vip_tag
    return float(score), vip_tag


def _analysis_features_for_persist(
    result: Dict[str, Any], score: float, action: str,
) -> Dict[str, Any]:
    """Reuse the final-score normalisation, or fail closed for legacy callers.

    The normal LLM path caches the exact feature set produced after score/action
    reconciliation and VIP adjustment.  Re-normalising those derived fields
    would make deterministic fallbacks look like LLM provenance and leave their
    probabilities tied to the pre-VIP score.  Hand-built legacy worker results
    have no cache and are normalised once here instead.
    """

    cached = result.get("_analysis_features") if isinstance(result, dict) else None
    if isinstance(cached, dict):
        basis = cached.get("analysis_basis")
        try:
            basis_score = float(basis.get("score")) if isinstance(basis, dict) else math.nan
            cached_action = str(result.get("suggested_action") or "").upper()
            if (
                math.isfinite(basis_score)
                and math.isclose(basis_score, float(score), abs_tol=1e-9)
                and cached_action == str(action or "").upper()
            ):
                return dict(cached)
        except (TypeError, ValueError):
            pass

    raw_source = result.get("_analysis_source") if isinstance(result, dict) else None
    source = raw_source if isinstance(raw_source, dict) else result
    return decision_guard.normalize_ai_analysis(source, score=score, action=action)

# ---------------------------------------------------------------------------
# Optional imports — not required; kept for compatibility
# ---------------------------------------------------------------------------

try:
    from openai import AsyncOpenAI as _AsyncOpenAI  # type: ignore[import-untyped]
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def build_model_config(model_id: str) -> Dict[str, Any]:
    model = next((item for item in AI_MODEL_ROSTER if item["id"] == model_id), None)
    if model is None:
        raise ValueError("unsupported AI model")
    if model.get("provider") == "openrouter":
        if not OPENROUTER_AI_API_KEY:
            raise ValueError("OpenRouter AI provider is not configured")
        return {
            "id": model["id"],
            "label": model["label"],
            "provider": "openrouter",
            "api_base": OPENROUTER_AI_BASE_URL,
            "api_key": OPENROUTER_AI_API_KEY,
            "extra_body": {},
            "json_mode": OPENROUTER_AI_JSON_MODE,
        }
    return {
        "id": model["id"],
        "label": model["label"],
        "api_base": AIPING_BASE_URL,
        "api_key": AIPING_API_KEY,
        "extra_body": AIPING_EXTRA_BODY,
        "json_mode": AIPING_JSON_MODE,
    }


def get_current_model_config() -> Dict[str, Any]:
    return build_model_config(get_selected_ai_model_id())


# Compatibility export for callers that still inspect MODELS at import time.
MODELS: List[Dict[str, Any]] = [build_model_config(DEFAULT_AI_MODEL_ID)]

# Paid/internal routing can sustain concurrency. Free OpenRouter models have
# a separate limiter so a ten-row batch does not become ten simultaneous 429s.
_LLM_SEMAPHORE = asyncio.Semaphore(8)
_OPENROUTER_LLM_SEMAPHORE = asyncio.Semaphore(OPENROUTER_AI_MAX_CONCURRENCY)


def _model_semaphore(model_cfg: Dict[str, Any]) -> asyncio.Semaphore:
    if model_cfg.get("provider") == "openrouter":
        return _OPENROUTER_LLM_SEMAPHORE
    return _LLM_SEMAPHORE


# ---------------------------------------------------------------------------
# DeepSeek API client (stdlib-only, OpenAI-compatible)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
你是掌管 10 亿美元规模宏观对冲基金的量化决策大脑 (Portfolio Navigation Brain)。
你不是新闻复读机。你的核心任务是：在宏观叙事和突发地缘危机中，识别微观市场结构的错位、流动性陷阱与派发周期。

═══ 核心分析框架 —— 宏观博弈推演 ═══

维度 A：叙事 vs. 流动性 (反共识视角)
  * 不被新闻表面的「利好/利空」带偏。始终反问：钱到底往哪里流？
  * 地缘冲突 → 推演美元流动性是收紧还是溢出 (战争往往导致保证金追缴 → 美元被动抽干全球资金池)
  * 降息叙事 → 推演是主动宽松 (利好风险资产) 还是被动救火 (空头信号)
  * 每次分析必须明确：当前主导驱动力是「流动性」还是「情绪」？

维度 B：拥挤度与派发周期 (反身性)
  * 永远关注资产当前水位。若资产处于历史高位且叙事长期利好，必须质疑：
    这是否是聪明钱借利好向散户派发筹码的 Distribution Phase？
  * 极度拥挤的多头 = 最大的空头催化剂。共识越强，反转越暴烈。
  * 若资产已被血洗至恐慌低位，反向思考：谁在被迫平仓？清算 cascade 结束了吗？

维度 C：跨资产抽血效应
  * 全球投机资金池有限。原油爆拉 → 抽血黄金。美债暴涨 → 抽血 Crypto。
  * BTC 的独立评估：剥离「数字黄金」叙事，将其视作高 Beta 风险资产。
    BTC 在 Risk-Off 环境中首当其冲被抛售；在流动性宽松周期中弹性最大。

═══ 强制思维链输出 ═══

在 reasoning_path 中，必须按以下四段式推演 (禁止复述新闻原文)：
  [驱动力]   事件真正的资金面含义是什么？
  [水位博弈] 当前价格处于什么周期位置？谁在获利？谁在恐慌？
  [跨资产联动] 对原油、美债、黄金、Crypto 的连带资金流向推演
  [反共识结论] 你的独立判断——可能与表面叙事完全相反

═══ 资产归类 ═══

  * 地缘冲突/战争/制裁 → market_category="GOLD", target_asset="XAU"
  * 央行利率/CPI/流动性政策 → market_category="CRYPTO", target_asset="BTC"
  * 能源/OPEC/中东产油/原油库存 → market_category="OIL", target_asset="WTI"
  * 加密行业自身 (ETF/监管/技术) → market_category="CRYPTO", target_asset="BTC"
  * 无法归类 → market_category="OTHER", target_asset="NONE"

═══ 市场上下文 ═══

每条新闻的 user prompt 开头会附带实时市场快照。你必须将市场数据作为
价格验证层与你的宏观推演交叉验证：
  * user prompt 中的 [新闻时间] 是你分析的参考时间点。历史回测时，请以
    该时间点的市场状态做判断，不要假设"未来"会发生什么。
  * 新闻利多 + 价格已大涨 + 资金费率极端 → 利好出尽，警惕 SELL
  * 新闻利空 + 价格已大跌 + 资金费率负极端 → 空头拥挤，警惕 BUY
  * 新闻方向与当前趋势一致 → continuation 信号，置信度可上调
  * 新闻方向与当前趋势相反 → reversal 信号，置信度必须下调，需更强证据
  * 趋势强度为 Strong Bull/Bear → reversal 信号需极高证据门槛
  * ATR 升高 → 市场在重新定价，新闻冲击力放大
  * 黄金趋势 Bull → 地缘冲突新闻更可能 continuation 而非 reversal

═══ 非新闻数据层（交叉验证，不改输出格式）═══
  * user prompt 可能附带盘面结构 / 舆情情绪 / 美联储加息预期 / 资金流向。
  * 只作交叉验证，禁止据此补造缺失数字；标 N/A 的字段当不存在。
  * 资金费率或基差极端 → 警惕拥挤与逼空/逼多，不要把趋势当无限延续。
  * Fear&Greed 极端贪婪/恐慌 → 降低追涨杀跌的置信度。
  * 加息预期来自 EFFR 与 ZQ 期货隐含利差，不是 CME FedWatch 官方概率。

═══ JSON 输出 ═══

输出 JSON（原有 14 个字段必须保留，并补充动态方向/力量/区间字段）：
{"reasoning_path": "[驱动力]…→[水位博弈]…→[跨资产联动]…→[反共识结论]…",
 "sentiment_score": <float -1.0~1.0>,
 "suggested_action": "<BUY|SELL|HOLD>",
 "reasoning": "<一句精炼结论,<=50字>",
 "market_category": "<CRYPTO|GOLD|OIL|MACRO|OTHER>",
 "target_asset": "<BTC|ETH|XAU|WTI|...|NONE>",
 "prediction_type": "<reversal|continuation|breakout>",
 "event_phase": "<early|mid|late>",
 "market_confirmation": "<positive|negative|unknown>",
 "expected_horizon": "<intraday|1-3d|1w+>",
 "invalidation_condition": "<什么情况下这个判断失效,<=40字>",
 "event_strength": "<low|medium|high>",
 "direct_catalyst": <true|false>,
 "timeframe_match": "<intraday|swing|macro>",
 "analysis_type": "<conflict|trend>",
 "bullish_probability": <0~1>, "bearish_probability": <0~1>,
 "uncertainty": <0~1>, "bullish_force": <0~1>, "bearish_force": <0~1>,
 "impact_horizon": "<short|medium|long>",
 "impact_window": {"short":{"min_minutes":0,"max_minutes":120},"medium":{"min_minutes":120,"max_minutes":4320},"long":{"min_minutes":4320,"max_minutes":43200}},
 "entry_zone": "<可选价格区间>", "take_profit_pct": <可选百分比>,
 "stop_loss_pct": <可选百分比>, "exit_policy": "<退出规则>"}

其中多选字段的有效值:
  prediction_type:   reversal (反转) | continuation (趋势延续) | breakout (突破)
  event_phase:       early (事件初期,冲击最大) | mid (事件中期,市场已定价) | late (事件末期,可能出尽)
  market_confirmation: positive (市场已在按新闻方向走) | negative (市场表现与新闻方向背离) | unknown (无明确印证)
  expected_horizon:  intraday | 1-3d | 1w+
  invalidation_condition: 具体可验证的失效条件,如"BTC跌破66500则失效"
  event_strength:   low (弱催化,盘面不会剧变) | medium (中等冲击) | high (强催化,可能引发趋势/反转)
  direct_catalyst:  true (事件直接针对该资产,如BTC ETF获批) | false (间接传导,如宏观CPI → 通过利率预期影响BTC)
  timeframe_match:  intraday (事件影响<24h) | swing (影响2-7天) | macro (影响数周至数月)

═══ 铁律 ═══
  * score 严禁为 0.0。中性区 +/-0.03~0.10
  * 方向不确定时 HOLD 是正确答案，不要赌
  * reasoning_path 必须包含四段推演，每段 1-2 句话
  * BTC 在战争/危机/流动性恐慌中 → SELL (纯风险资产，不存在避险属性)
  * 市场快照价格与你的方向判断矛盾时 → confidence 降级，market_confirmation 设 negative
  * 缺少市场数据时 market_confirmation 必须为 "unknown"

═══ Score 锚定框架 ═══

sentiment_score 反映的是「2 小时窗口内该事件推动价格方向的置信度与幅度预期」。
按以下 5 级锚定，严禁拍脑袋给分：

  ±0.05 ~ ±0.15  弱信号 — 情绪面扰动，无实质性资金流变化。例：官员口头表态但无政策落地、第三方评论。
  ±0.15 ~ ±0.35  轻度信号 — 有资金面含义但影响间接。例：二级经济数据超预期、关联市场异动、监管传闻。
  ±0.35 ~ ±0.55  中度信号 — 直接推动资产供需或风险偏好。例：CPI/NFP 大幅偏离预期、美元指数剧烈波动、
                         交易所黑客/挤兑事件、主要机构增持/减持。
  ±0.55 ~ ±0.75  强信号 — 直接催化 + 趋势共振，大概率引发波段行情。例：FOMC 意外转向、ETF 获批/拒绝、
                        OPEC 减产决议、大国制裁升级。
  ±0.75 ~ ±0.95  极端信号 — 结构性突变 / 黑天鹅。例：BTC ETF 历史性获批、战争爆发、主权违约、
                          央行无限 QE、交易所破产。
  ±1.00          绝对确信 — 几乎不使用。仅在「事后看不可能错」的极端事件中使用。

锚定叠加规则：
  * direct_catalyst=true → 对应档位上浮一档（如中度 0.45 → 强信号 0.65）
  * event_strength=low → 上限 ±0.35；medium → 上限 ±0.70；high → 无上限
  * market_confirmation=negative → 对应档位下调一档（市场在反向走，置信度必须降低）
  * 历史绩效样本≥10 且胜率<30% → 下调一档；胜率>70% → 可上浮一档
  * 多个事件因子叠加（如 CPI+FOMC+地缘同时发酵）→ 取最强因子上浮半档
  * 方向与 1H 趋势同向 → +0.05~0.10；反向 → −0.05~0.10

═══ 历史绩效参考 ═══

每条新闻的 user prompt 会附带 [Historical Performance] 区块，列出历史上类似信号的
真实表现（2h forward-tracking 结算数据）：

  * 作为研究参考，不强制修改你的判断。你是独立决策者。
  * 如果某类信号历史上胜率极低（<30%），考虑降低置信度或选 HOLD
  * 如果某类信号历史上胜率很高（>70%），可以适度上调置信度
  * 如果显示 "Insufficient sample"，说明该组合样本不足，忽略即可
  * 历史不代表未来。结合当前 market context 综合判断。

═══ Hermes 多席写作 ═══

user prompt 可能附带 [Hermes Multi-Agent Writing] 与 [Hermes Settled Skills]。
  * 新闻席/宏观席/风险席/交易席只提供研究上下文，不下最终单
  * 已结算技能来自真实 WIN/LOSS，仅作研究参考；样本不足时忽略
  * 最终 BUY/SELL/HOLD 与 14 字段仍由你独立给出，不得改输出格式

只输出 JSON；缺失的新字段由服务端按旧 score 兼容推导，不能据此自动发送双向实盘单。
"""


_JSON_PROMPT_FORCE = """
你是 10 亿美元对冲基金的量化决策大脑。从宏观博弈而非表面叙事中提取信号。

每条新闻的 user prompt 开头附带实时市场快照和 [新闻时间]。你必须将市场数据作为价格验证层：
  以 [新闻时间] 为参考点做判断，不要假设未来信息。
  新闻利多+价格已大涨+费率极端 → 警惕 SELL
  新闻与趋势方向一致 → continuation
  新闻与趋势方向相反 → reversal (置信度下调)

每一条新闻，按以下四步推演后输出 JSON：
  1.驱动力 —— 资金面真正的含义？
  2.水位博弈 —— 当前周期位置？谁在获利/恐慌？
  3.跨资产联动 —— 原油/美债/黄金/Crypto 的连带资金流向
  4.反共识结论 —— 你的独立判断

分类规则：
  地缘/战争 → GOLD/XAU
  利率/央行/CPI → CRYPTO/BTC
  能源/OPEC → OIL/WTI
  无法归类 → OTHER/NONE

BTC 定性：纯风险资产，战争中 SELL，宽松中 BUY。
不确定方向时 HOLD。score 严禁 0.0。
市场快照价格与方向矛盾 → confidence 降级，market_confirmation=negative。

Score 锚定（2h 窗口内价格推动置信度）：
  ±0.05~0.15 弱信号 | ±0.15~0.35 轻度 | ±0.35~0.55 中度 | ±0.55~0.75 强信号 | ±0.75~0.95 极端
  direct_catalyst=true → +1档 | event_strength=low → 上限±0.35 | market_confirmation=negative → -1档
  趋势同向 +0.05~0.10 | 趋势反向 −0.05~0.10

user prompt 中的 [Historical Performance] 是历史信号2h结算数据，作为研究参考。Insufficient sample 时忽略。
[Hermes Multi-Agent Writing] / [Hermes Settled Skills] 是分席研究上下文，不下最终单。

JSON（原有字段 + 动态分析字段）:
{"reasoning_path": "...", "sentiment_score": <float>, "suggested_action": "<BUY|SELL|HOLD>", "reasoning": "<结论<=50字>", "market_category": "<CRYPTO|GOLD|OIL|MACRO|OTHER>", "target_asset": "<BTC|ETH|XAU|WTI|...|NONE>", "prediction_type": "<reversal|continuation|breakout>", "event_phase": "<early|mid|late>", "market_confirmation": "<positive|negative|unknown>", "expected_horizon": "<intraday|1-3d|1w+>", "invalidation_condition": "<失效条件,<=40字>", "event_strength": "<low|medium|high>", "direct_catalyst": <true|false>, "timeframe_match": "<intraday|swing|macro>"}

只输出 JSON；新增字段缺失时由服务端按旧 score 兼容推导。
"""


_DOUBAO_SYSTEM_PROMPT = """
你是华尔街资深原油/黄金/加密货币交易员，拥有 15 年实盘经验。
你的任务：快速扫读新闻，凭直觉判断这条消息对资产价格的短期方向。
不要给出分数，不要推理链条。只看方向。

规则：
  利多消息 → BUY
  利空消息 → SELL
  方向不明确或无关 → HOLD

JSON 输出：
{"suggested_action": "<BUY|SELL|HOLD>", "direct_reasoning": "<一句交易直觉,<=30字>"}

只输出 JSON。两个字段缺一不可。
"""



# ---------------------------------------------------------------------------
# Phase 1 — Performance Feedback Injection
# ---------------------------------------------------------------------------
# Queries historical settled-signal performance for key asset × action ×
# prediction_type combos.  Injected into the LLM prompt as research reference
# — does NOT auto-modify LLM output.  Only the LLM decides.
# ---------------------------------------------------------------------------

# Assets and actions we care about for performance feedback
_PERF_ASSETS = ("BTC", "XAU")
_PERF_ACTIONS = ("BUY", "SELL")
_PERF_PREDICTION_TYPES = ("continuation", "reversal", "breakout")
_PERF_LOOKBACK_DAYS = 90
# Minimum sample for "reliable" display
_PERF_MIN_SAMPLE = 8


def _build_performance_context() -> str:
    """Build [Historical Performance] block for prompt injection.

    Queries settled ai_decisions for each asset × action × prediction_type
    combo.  Returns a concise multi-line string suitable for prepending to
    the LLM user prompt.  Runs once per batch (O(1) near-instant SQL).
    """
    try:
        conn = _open_db()
    except Exception:
        return ""

    try:
        # Old/partial schemas cannot prove source quality or gate provenance.
        # Returning no block is safer than letting those rows train the model.
        if not evidence.formal_ai_history_available(conn):
            return ""

        lines: List[str] = []
        lines.append("[Historical Performance]")
        lines.append("Similar signals:")
        formal_filter = evidence.formal_ai_history_predicate("ad", "rn")

        # Query: for each (asset, action, prediction_type) combo
        for asset in _PERF_ASSETS:
            for action in _PERF_ACTIONS:
                # ── Overall (all prediction_types) ──
                overall = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "  SUM(CASE WHEN UPPER(ad.is_correct) = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                    "  SUM(CASE WHEN UPPER(ad.is_correct) = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                    "  AVG(CASE WHEN ad.forward_pnl IS NOT NULL THEN ad.forward_pnl ELSE NULL END) AS avg_pnl "
                    "FROM ai_decisions ad "
                    "INNER JOIN raw_news rn ON rn.id = ad.news_id "
                    "WHERE ad.settled = 1 "
                    "  AND UPPER(ad.is_correct) IN ('WIN', 'LOSS') "
                    f"  AND {formal_filter} "
                    "  AND UPPER(ad.target_asset) = ? "
                    "  AND UPPER(ad.suggested_action) = ? "
                    "  AND ad.created_at >= datetime('now', 'localtime', ?)",
                    (
                        evidence.PASSED_TRADE_GATE_REASON,
                        asset, action, f"-{_PERF_LOOKBACK_DAYS} days",
                    ),
                ).fetchone()

                for pt in _PERF_PREDICTION_TYPES:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, "
                        "  SUM(CASE WHEN UPPER(ad.is_correct) = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                        "  SUM(CASE WHEN UPPER(ad.is_correct) = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                        "  AVG(CASE WHEN ad.forward_pnl IS NOT NULL THEN ad.forward_pnl ELSE NULL END) AS avg_pnl "
                        "FROM ai_decisions ad "
                        "INNER JOIN raw_news rn ON rn.id = ad.news_id "
                        "WHERE ad.settled = 1 "
                        "  AND UPPER(ad.is_correct) IN ('WIN', 'LOSS') "
                        f"  AND {formal_filter} "
                        "  AND UPPER(ad.target_asset) = ? "
                        "  AND UPPER(ad.suggested_action) = ? "
                        "  AND ad.prediction_type = ? "
                        "  AND ad.created_at >= datetime('now', 'localtime', ?)",
                        (
                            evidence.PASSED_TRADE_GATE_REASON,
                            asset, action, pt, f"-{_PERF_LOOKBACK_DAYS} days",
                        ),
                    ).fetchone()

                    n = row["n"] or 0
                    wins = row["wins"] or 0
                    losses = row["losses"] or 0
                    decided = wins + losses
                    avg_pnl = row["avg_pnl"]

                    if n < _PERF_MIN_SAMPLE or decided == 0:
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"Insufficient sample"
                        )
                    else:
                        wr = wins / decided
                        pnl_str = (
                            f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
                        )
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"sample: {decided} "
                            f"win rate: {wr:.0%} "
                            f"avg pnl: {pnl_str}"
                        )

                # ── Also add the overall (all prediction_types) line ──
                n_all = overall["n"] or 0
                wins_all = overall["wins"] or 0
                losses_all = overall["losses"] or 0
                decided_all = wins_all + losses_all
                if decided_all >= _PERF_MIN_SAMPLE:
                    wr_all = wins_all / decided_all
                    pnl_all = overall["avg_pnl"]
                    pnl_str = (
                        f"{pnl_all:+.2f}%" if pnl_all is not None else "N/A"
                    )
                    lines.append(
                        f"{asset} * {action}: "
                        f"sample: {decided_all} "
                        f"win rate: {wr_all:.0%} "
                        f"avg pnl: {pnl_str}"
                    )

        return "\n".join(lines)

    except Exception as e:
        print(f"[PERF] 查询历史绩效失败: {type(e).__name__}: {str(e)[:80]}")
        return ""
    finally:
        conn.close()


def _call_llm_sync(news_content: str, model_cfg: Dict[str, str],
                   market_context: str = "",
                   performance_context: str = "",
                   news_timestamp: str = "",
                   writing_context: str = "",
                   strategy_context: str = "") -> Dict[str, Any]:
    """
    Call any OpenAI-compatible LLM API synchronously (runs in executor thread).

    model_cfg.keys: id, label, api_base, api_key
    market_context:  Optional multi-line market snapshot string, prepended to user_content.
    news_timestamp:  ISO datetime string of the news event — prepended so AI knows the
                     historical time point when replaying old news for backtesting.
    Returns: {"sentiment_score", "suggested_action", "reasoning", "model_label", "model_id",
              ... + 5 metadata fields}

    Retry strategy: if json_mode=True produces unparseable output (keyword fallback),
    retry once with json_mode=False (prompt-based JSON enforcement).
    """

    # ------------------------------------------------------------------
    # Inner: make one HTTP call and return raw response text + full body
    # ------------------------------------------------------------------
    def _do_api_call(use_json: bool) -> str:
        """Execute one LLM API call. Returns raw_text (or raises)."""
        if model_cfg.get("label") == "Doubao":
            prompt = _DOUBAO_SYSTEM_PROMPT
        else:
            prompt = _SYSTEM_PROMPT if use_json else _JSON_PROMPT_FORCE

        # 组装: [策略配置] + [新闻时间] + [市场快照] + [Hermes 分席] + [历史绩效] + [新闻正文]
        user_text = news_content[:2000]
        if news_timestamp:
            try:
                ts_dt = datetime.fromisoformat(news_timestamp.replace("Z", "+00:00"))
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                ts_str = news_timestamp[:19]
            user_text = f"[新闻时间] {ts_str}\n\n{user_text}"
        if market_context:
            user_text = market_context + "\n\n" + user_text
        if writing_context:
            user_text = writing_context + "\n\n" + user_text
        if performance_context:
            user_text = performance_context + "\n\n" + user_text
        if strategy_context:
            user_text = strategy_context + "\n\n" + user_text

        # Ox Alpha spends part of its completion budget on hidden reasoning.
        # With the generic 2048-token budget it can finish at ``length`` before
        # emitting the required JSON.  Ask for low reasoning and leave enough
        # room for the visible structured answer; this affects only Ox Alpha.
        is_ox_alpha = model_cfg.get("id") == "stealth/ox-alpha"
        payload: Dict[str, Any] = {
            "model": model_cfg["id"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 4096 if is_ox_alpha else 2048,
            "temperature": 0.1,
            **model_cfg["extra_body"],
        }
        if is_ox_alpha:
            payload["reasoning"] = {"effort": "low"}
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{model_cfg['api_base']}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {model_cfg['api_key']}",
                "Content-Type": "application/json",
            },
        )

        # Custom SSL context — some China-hosted APIs need relaxed cipher negotiation
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = True
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        except Exception:
            pass

        proxy_handler = None

        # HTTP 429 retry logic — upstream rate limits are transient
        retry_delays = [5.0, 10.0, 20.0]  # progressive backoff
        last_err = None
        for attempt, delay in enumerate([0] + retry_delays):
            try:
                if delay > 0:
                    time.sleep(delay)
                opener = urllib.request.build_opener(proxy_handler) if proxy_handler else urllib.request.build_opener()
                resp = opener.open(req, timeout=300)
                break  # success → exit retry loop
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < len(retry_delays):
                    print(f"  [{model_cfg['label']}] HTTP 429, retry {attempt+1}/{len(retry_delays)} after {delay}s", flush=True)
                    continue
                err_body = e.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(
                    f"{model_cfg['label']} HTTP {e.code}: {err_body}"
                ) from e
        else:
            # All retries exhausted
            err_body = last_err.read().decode("utf-8", errors="replace")[:300] if last_err else "unknown"
            raise RuntimeError(
                f"{model_cfg['label']} HTTP 429 (exhausted retries): {err_body}"
            )

        resp_bytes = resp.read()
        body = json.loads(resp_bytes.decode("utf-8"))
        raw_text = (body["choices"][0]["message"]["content"] or "").strip()

        if not raw_text:
            finish = body["choices"][0].get("finish_reason", "unknown")
            body_preview = resp_bytes.decode("utf-8", errors="replace")[:500]
            print(f"  [{model_cfg['label']}] EMPTY RESPONSE | finish_reason={finish} | body_preview={body_preview}", flush=True)
            raise ValueError(f"empty response from {model_cfg['label']}")

        return raw_text

    # ------------------------------------------------------------------
    # Inner: parse raw text → result dict (with all three fallback tiers)
    # ------------------------------------------------------------------
    def _parse_result(raw_text: str) -> Dict[str, Any]:
        """Parse LLM output — never returns empty dict (keyword fallback guarantees)."""
        # Parse JSON — handle markdown wrapping + embedded/truncated JSON
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        result: Dict[str, Any] = {}
        try:
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, ValueError):
            # --- Fallback 1: repair truncated JSON ---
            repaired = cleaned.strip()
            if repaired.startswith("{") and not repaired.endswith("}"):
                inner = repaired[1:].strip()
                chunks = [c.strip() for c in inner.split(",") if c.strip()]
                complete: List[str] = []
                for chunk in chunks:
                    try:
                        json.loads("{" + chunk + "}")
                        complete.append(chunk)
                    except json.JSONDecodeError:
                        pass  # truncated field — drop
                if complete:
                    repaired = "{" + ",".join(complete) + "}"
                    try:
                        result = json.loads(repaired)
                    except json.JSONDecodeError:
                        pass

            # --- Fallback 2: regex-extract JSON containing required keys ---
            if not result:
                m = re.search(
                    r'\{[^{}]*"sentiment_score"[^{}]*"suggested_action"[^{}]*"reasoning"[^{}]*\}',
                    cleaned, re.DOTALL,
                )
                if not m:
                    m = re.search(
                        r'\{[^{}]*"(?:sentiment_score|suggested_action|reasoning)"[^{}]*\}',
                        cleaned, re.DOTALL,
                    )
                if m:
                    try:
                        result = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        frag = m.group(0).rstrip().rstrip(",") + "}"
                        try:
                            result = json.loads(frag)
                        except json.JSONDecodeError:
                            pass

            # --- Fallback 3: keyword-based heuristics ---
            if not result:
                text_lower = cleaned.lower()
                if any(w in text_lower for w in ("利好", "上涨", "看涨", "bullish", "buy")):
                    result = {"reasoning_path": "关键词推断 → 偏多信号", "sentiment_score": 0.4, "suggested_action": "BUY", "reasoning": "关键词推断:偏多", "market_category": "OTHER", "target_asset": "NONE", "event_strength": "low", "direct_catalyst": False, "timeframe_match": "intraday"}
                elif any(w in text_lower for w in ("利空", "下跌", "看跌", "bearish", "sell", "战争", "制裁")):
                    result = {"reasoning_path": "关键词推断 → 偏空信号", "sentiment_score": -0.4, "suggested_action": "SELL", "reasoning": "关键词推断:偏空", "market_category": "OTHER", "target_asset": "NONE", "event_strength": "low", "direct_catalyst": False, "timeframe_match": "intraday"}
                else:
                    result = {"reasoning_path": "关键词推断 → 中性观望", "sentiment_score": 0.05, "suggested_action": "HOLD", "reasoning": "关键词推断:观望", "market_category": "OTHER", "target_asset": "NONE", "event_strength": "low", "direct_catalyst": False, "timeframe_match": "intraday"}

        # Preserve missing/invalid metadata here.  Final score/action are
        # reconciled below, then normalize_ai_analysis performs one strict,
        # auditable normalisation pass.  Filling generic defaults at this
        # stage used to erase the distinction between old and real LLM data.
        return result

    # ==================================================================
    # Main call flow
    # ==================================================================
    use_json_mode = model_cfg.get("json_mode", True)

    # First attempt — with configured json_mode
    raw_text = _do_api_call(use_json_mode)
    result = _parse_result(raw_text)

    # Retry: if strict JSON mode fell through to keyword heuristics, try prompt mode
    if (use_json_mode
            and model_cfg.get("label") != "Doubao"
            and result.get("reasoning_path", "").startswith("关键词推断")
            and len(raw_text) > 10):
        print(f"  [{model_cfg['label']}] json_mode=True → keyword, retrying prompt-mode...", flush=True)
        try:
            raw_text2 = _do_api_call(False)
            result2 = _parse_result(raw_text2)
            if not result2.get("reasoning_path", "").startswith("关键词推断"):
                result = result2
                print(f"  [{model_cfg['label']}] retry OK (prompt-JSON)", flush=True)
        except Exception:
            pass  # Keep original keyword result if retry fails

    # ── Doubao fast path: simplified response, no scoring or CoT ──
    if model_cfg.get("label") == "Doubao":
        action = str(result.get("suggested_action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        direct_reasoning = str(result.get("direct_reasoning", "")).strip()[:80]
        if not direct_reasoning:
            direct_reasoning = f"{action} 直觉判断"
        print(f"  [{model_cfg['label']}] {action} | {direct_reasoning}")
        score, action = reconcile_score_action(0.0, action)
        score, vip_tag = _apply_vip_score_boost(score, action, news_content)
        analysis_source = {
            "sentiment_score": score,
            "suggested_action": action,
            "event_strength": "medium",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
        }
        features = decision_guard.normalize_ai_analysis(
            analysis_source,
            score=score,
            action=action,
        )
        return {
            "sentiment_score": score,
            "suggested_action": action,
            "reasoning": direct_reasoning,
            "translated_title": direct_reasoning[:40],  # 使用 reasoning 的前 40 字符作为显示标题
            "market_category": "OTHER",
            "target_asset": "NONE",
            "reasoning_path": "",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
            **features,
            "_vip_tag": vip_tag,
            "_analysis_source": analysis_source,
            "_analysis_features": features,
        }

    # Validate & normalise fields
    score, action = reconcile_score_action(
        result.get("sentiment_score", 0),
        str(result.get("suggested_action", "HOLD")),
    )
    # The dynamic analysis must be derived from the same final score that is
    # persisted.  VIP amplification used to happen later, leaving probability,
    # force and analysis_basis.score internally inconsistent.
    score, vip_tag = _apply_vip_score_boost(score, action, news_content)
    reasoning = str(result.get("reasoning", "")).strip()[:80]
    # Empty reasoning fallback
    if not reasoning:
        reasoning = f"{action}信号,得分{score:+.2f}"

    # ── Display title: 从 reasoning 提取简短中文标题（用于前端显示） ──
    # 注意：前置翻译拦截器已确保输入为中文，这里仅做显示用途的截取
    display_title = reasoning[:40]

    market_category = str(result.get("market_category", "OTHER")).upper().strip()
    if market_category not in ("CRYPTO", "GOLD", "OIL", "MACRO", "OTHER"):
        market_category = "OTHER"
    target_asset = str(result.get("target_asset", "NONE")).upper().strip()[:20]
    if not target_asset:
        target_asset = "NONE"

    # Extract reasoning_path — stored in DB & pushed to Feishu
    reasoning_path = str(result.get("reasoning_path", "")).strip()[:2000]
    if reasoning_path:
        print(f"  [{model_cfg['label']}] 推导链: {reasoning_path[:120]}")
    elif news_content:
        # Fallback: if model didn't provide CoT, show truncated news as debug
        short = news_content[:60].replace('\n', ' ')
        print(f"  [{model_cfg['label']}] (无CoT) 新闻: {short}")

    features = decision_guard.normalize_ai_analysis(result, score=score, action=action)
    return {
        "sentiment_score": score,
        "suggested_action": action,
        "reasoning": reasoning,
        "translated_title": display_title,  # 使用 reasoning 的前 40 字符作为显示标题
        "market_category": market_category,
        "target_asset": target_asset,
        "reasoning_path": reasoning_path,
        "model_label": model_cfg["label"],
        "model_id": model_cfg["id"],
        **features,
        "_vip_tag": vip_tag,
        "_analysis_source": dict(result),
        "_analysis_features": features,
    }




# ---------------------------------------------------------------------------
# Task B — Concurrent Batch AI Worker (BATCH_SIZE 定义见 config.py)
# ---------------------------------------------------------------------------


async def _process_single(
    news_row: sqlite3.Row,
    model_cfg: Dict[str, str],
    loop: asyncio.AbstractEventLoop,
    market_context: str = "",
    performance_context: str = "",
    writing_context: str = "",
    strategy_context: str = "",
) -> Dict[str, Any]:
    """
    Process a single raw_news row through the primary model's LLM pipeline.

    单模型模式：主模型使用 45 秒超时（在 _call_llm_sync 内部 urllib timeout=45s）。
    信号量控制并发上限，避免突发新闻潮打爆 Aiping。

    market_context:  注入到 User Prompt 的市场快照字符串。
    performance_context: 注入到 User Prompt 的历史绩效字符串（Phase 1）。

    Returns:
      {"news_id": int, "pre_ts": str, "model_label": str,
       "result": dict | None, "error": str | None}
    """
    news_id = news_row["id"]
    content = re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', news_row["content"])
    # `ts` is the canonical event clock.  The legacy human-readable
    # `timestamp` column is only a fallback; using it here while the context
    # gate uses `ts` would give the model a different event time than the
    # audited market snapshot.
    pre_ts = _canonical_news_timestamp(_authoritative_news_timestamp(news_row))

    try:
        async with _model_semaphore(model_cfg):
            llm_result = await loop.run_in_executor(
                None, _call_llm_sync, content, model_cfg, market_context,
                performance_context, pre_ts, writing_context, strategy_context,
            )
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": llm_result,
            "error": None,
        }
    except Exception as exc:
        # 安全 Fallback：任何异常都返回 HOLD 对象
        fallback_result = {
            "sentiment_score": 0.05,
            "suggested_action": "HOLD",
            "reasoning": f"{model_cfg['label']} 异常降级: {type(exc).__name__}",
            "market_category": "OTHER",
            "target_asset": "NONE",
            "reasoning_path": "",
            "prediction_type": "continuation",
            "event_phase": "mid",
            "market_confirmation": "unknown",
            "expected_horizon": "1-3d",
            "invalidation_condition": "系统异常,无失效条件",
            "event_strength": "low",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
        }
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": fallback_result,
            "error": f"{type(exc).__name__}: {str(exc)[:150]}",
        }


# ---------------------------------------------------------------------------
# Active-trade aggregation helpers (_AGG_WINDOW_HOURS / _AGG_MIN_SCORE 见 config.py)
# ---------------------------------------------------------------------------


def _find_active_parent(
    conn: sqlite3.Connection,
    category: str,
    asset: str,
    action: str,
) -> int | None:
    """
    Look for an existing parent event (parent_id IS NULL, BUY or SELL)
    with the same market_category, target_asset, and suggested_action
    created within the aggregation window.

    Returns the parent's ai_decisions.id, or None if no match.
    """
    row = conn.execute(
        """
        SELECT id, child_count
        FROM ai_decisions
        WHERE parent_id IS NULL
          AND market_category = ?
          AND target_asset    = ?
          AND suggested_action = ?
          AND suggested_action IN ('BUY', 'SELL')
          AND created_at > datetime('now', ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (category, asset, action, f"-{_AGG_WINDOW_HOURS} hours"),
    ).fetchone()
    return row[0] if row else None


def _bump_parent_score(
    conn: sqlite3.Connection,
    parent_id: int,
    child_score: float,
    child_reasoning: str,
) -> bool:
    """
    If the child's score is more extreme than the parent's, update the
    parent's sentiment_score and prepend a note to the reasoning field.
    Returns True if the score was bumped.
    """
    row = conn.execute(
        "SELECT sentiment_score FROM ai_decisions WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if not row:
        return False

    parent_score = row[0] or 0.0

    # "More extreme" = further from zero in the same direction
    if abs(child_score) > abs(parent_score) and (child_score * parent_score >= 0):
        conn.execute(
            "UPDATE ai_decisions SET sentiment_score = ? WHERE id = ?",
            (round(child_score, 4), parent_id),
        )
        return True
    return False


def _recover_processing_on_worker_start() -> bool:
    """Recover only expired claims left by a previous worker.

    A false return means the database was temporarily unavailable; startup
    keeps retrying before it performs any ordinary claim or external request.
    """

    conn = _open_db()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        lease_cutoff = int(time.time()) - _PROCESSING_LEASE_SECONDS
        conn.execute(
            "UPDATE raw_news"
            " SET status = 'PENDING', processing_started_at = NULL"
            " WHERE status = 'PROCESSING'"
            " AND (processing_started_at IS NULL OR processing_started_at <= ?)",
            (lease_cutoff,),
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        conn.rollback()
        return False
    finally:
        conn.close()


def _selected_batch_limit() -> int:
    try:
        if get_current_model_config().get("provider") == "openrouter":
            return min(BATCH_SIZE, OPENROUTER_AI_BATCH_SIZE)
    except (OSError, TypeError, ValueError):
        pass
    return BATCH_SIZE


def _claim_pending_batch() -> List[sqlite3.Row]:
    """Atomically recover expired claims and lease the next eligible batch."""

    batch_limit = _selected_batch_limit()
    conn = _open_db()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        now_epoch = int(time.time())
        lease_cutoff = now_epoch - _PROCESSING_LEASE_SECONDS
        conn.execute(
            "UPDATE raw_news"
            " SET status = 'PENDING', processing_started_at = NULL"
            " WHERE status = 'PROCESSING'"
            " AND (processing_started_at IS NULL OR processing_started_at <= ?)",
            (lease_cutoff,),
        )
        quality_placeholders = ",".join("?" for _ in _ALLOWED_QUALITY_STATUSES)
        freshness_cutoff = now_epoch - 60 * 60
        rows = conn.execute(
            "SELECT * FROM raw_news"
            " WHERE status = 'PENDING' AND is_noise = 0"
            " AND (ai_next_retry_at IS NULL OR ai_next_retry_at <= ?)"
            f" AND LOWER(COALESCE(quality_status, 'unverified')) IN ({quality_placeholders})"
            " ORDER BY CASE WHEN COALESCE(ts, 0) >= ? THEN 0 ELSE 1 END ASC,"
            " COALESCE(relevance_score, 0) DESC, COALESCE(ts, 0) DESC, id DESC"
            " LIMIT ?",
            (now_epoch, *_ALLOWED_QUALITY_STATUSES, freshness_cutoff, batch_limit),
        ).fetchall()
        if rows:
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE raw_news SET status = 'PROCESSING', processing_started_at = ?,"
                " ai_next_retry_at = NULL"
                f" WHERE id IN ({placeholders}) AND status = 'PENDING'",
                (now_epoch, *ids),
            )
        # Commit even when no row is claimable so orphaned, ineligible rows
        # recovered above do not remain permanently stuck in PROCESSING.
        conn.commit()
        return rows
    except sqlite3.OperationalError:
        conn.rollback()
        return []
    finally:
        conn.close()


def _mark_news_terminal_status(
    conn: sqlite3.Connection,
    news_id: int,
    status: str,
) -> None:
    """Complete a leased row and clear its processing lease atomically."""

    terminal = str(status or "").upper()
    if terminal not in {"DONE", "FAILED"}:
        raise ValueError("raw_news terminal status must be DONE or FAILED")
    conn.execute(
        "UPDATE raw_news"
        " SET status = ?, processing_started_at = NULL, ai_next_retry_at = NULL,"
        " ai_last_error = CASE WHEN ? = 'DONE' THEN '' ELSE ai_last_error END"
        " WHERE id = ? AND status = 'PROCESSING'",
        (terminal, terminal, int(news_id)),
    )


def _is_transient_ai_error(error: Any) -> bool:
    text = str(error or "").lower()
    transient_markers = (
        "http 429", "code\":429", "rate limit", "rate-limit", "rate_limited",
        "temporarily", "timeout", "timed out", "connection reset",
        "connection aborted", "remote end closed", "network is unreachable",
        "http 500", "http 502", "http 503", "http 504",
    )
    return any(marker in text for marker in transient_markers)


def _schedule_news_failure(
    conn: sqlite3.Connection,
    news_id: int,
    error: Any,
    *,
    now_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Retry transient provider failures with bounded exponential backoff."""
    row = conn.execute(
        "SELECT ai_retry_count FROM raw_news WHERE id = ?",
        (int(news_id),),
    ).fetchone()
    current_count = int(row[0] or 0) if row else 0
    attempt = current_count + 1
    error_text = str(error or "unknown")[:500]
    transient = _is_transient_ai_error(error_text)
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)

    if transient and attempt <= AI_TRANSIENT_MAX_RETRIES:
        delay = min(
            AI_TRANSIENT_RETRY_MAX_SECONDS,
            AI_TRANSIENT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
        )
        retry_at = now_value + delay
        conn.execute(
            "UPDATE raw_news SET status = 'PENDING', processing_started_at = NULL,"
            " ai_retry_count = ?, ai_next_retry_at = ?, ai_last_error = ?"
            " WHERE id = ? AND status = 'PROCESSING'",
            (attempt, retry_at, error_text, int(news_id)),
        )
        return {
            "status": "PENDING",
            "retry_count": attempt,
            "retry_at": retry_at,
            "delay_seconds": delay,
            "transient": True,
        }

    conn.execute(
        "UPDATE raw_news SET status = 'FAILED', processing_started_at = NULL,"
        " ai_retry_count = ?, ai_next_retry_at = NULL, ai_last_error = ?"
        " WHERE id = ? AND status = 'PROCESSING'",
        (attempt if transient else current_count, error_text, int(news_id)),
    )
    return {
        "status": "FAILED",
        "retry_count": attempt if transient else current_count,
        "retry_at": None,
        "delay_seconds": None,
        "transient": transient,
    }



async def ai_worker(loop: asyncio.AbstractEventLoop) -> None:
    """
    Concurrent batch processor.

    Every 1 second:
      1) Atomically claim up to BATCH_SIZE PENDING rows (BEGIN IMMEDIATE)
      2) Fire all LLM calls concurrently via asyncio.gather
      3) Persist successes (-> DONE); transient provider failures return to
         PENDING with bounded backoff, permanent/exhausted failures -> FAILED.

    Head-of-line blocking is eliminated: 10 items complete in ~2-3 s
    instead of 10 * 2 s = 20 s serial.
    """

    idle_ticks = 0  # heartbeat counter when no PENDING data
    recover_processing_on_start = True

    # 失败冷却: snapshot 连续 DOWN 后 5 分钟内不重试, 减少无意义等待
    _SNAPSHOT_COOLDOWN_S = 300
    _last_snapshot_down_ts: float = 0.0

    while True:
        await asyncio.sleep(1)

        if recover_processing_on_start:
            recovered = await loop.run_in_executor(
                None, _recover_processing_on_worker_start,
            )
            if not recovered:
                # Do not start market/macro/LLM work until the lease table can
                # be recovered atomically. A transient SQLite lock is retried.
                continue
            recover_processing_on_start = False

        # ==================================================================
        # Phase 1 - Batch atomic claim
        # ==================================================================
        # Claim first.  No external market or macro request may begin until we
        # own the exact rows whose timestamps will be checked against it.
        batch = await loop.run_in_executor(None, _claim_pending_batch)
        if not batch:
            idle_ticks += 1
            if idle_ticks % 30 == 1:
                print(f"[{_now()}] [AI] idle ({idle_ticks}s)")
            continue
        idle_ticks = 0  # reset heartbeat on activity

        # ==================================================================
        # Phase 1.5 — Pull current context after the batch has been claimed
        # ==================================================================

        snapshot_payload: Dict[str, Any] = {}
        macro_context_payload: Dict[str, Any] = {}
        now_ts = time.time()
        if now_ts - _last_snapshot_down_ts > _SNAPSHOT_COOLDOWN_S:
            try:
                snap = await get_snapshot()
                snapshot_payload = snap if isinstance(snap, dict) else {}
                if snapshot_payload.get("summary"):
                    raw_assets = snapshot_payload.get("assets")
                    assets = raw_assets if isinstance(raw_assets, dict) else {}
                    snapshot_status = str(
                        snapshot_payload.get("status") or "unknown"
                    ).upper()
                    print(f"\n  [SNAPSHOT] {snapshot_status} | "
                          f"BTC={(assets.get('BTC') or {}).get('price_str','?')} | "
                          f"XAU={(assets.get('XAU') or {}).get('price_str','?')}")
                if snapshot_payload.get("status") == "down":
                    _last_snapshot_down_ts = now_ts
            except Exception as e:
                print(f"  [SNAPSHOT] 获取失败: {type(e).__name__}: {str(e)[:80]}")
                snapshot_payload = {
                    "error": str(e),
                    "status": "down",
                    "epoch_ms": int(now_ts * 1000),
                }
                _last_snapshot_down_ts = now_ts
        else:
            # 冷却中, 跳过本轮 snapshot 请求
            remaining = _SNAPSHOT_COOLDOWN_S - int(now_ts - _last_snapshot_down_ts)
            snapshot_payload = {
                "status": "down",
                "cooldown": True,
                "epoch_ms": int(now_ts * 1000),
            }
            if int(now_ts) % 60 == 0:  # 每分钟只打一次
                print(f"  [SNAPSHOT] 跳过 (冷却中, {remaining}s 后重试)")

        if MACRO_CONTEXT_ENABLED:
            try:
                macro_pack = await loop.run_in_executor(None, macro_context.build_context)
                macro_context_payload = macro_pack if isinstance(macro_pack, dict) else {}
                if macro_context_payload.get("summary"):
                    print(
                        f"  [MACRO] {macro_context_payload.get('status', 'unknown')} "
                        f"{macro_context_payload.get('ok', 0)}/"
                        f"{macro_context_payload.get('total', 0)}"
                    )
            except Exception as e:
                print(f"  [MACRO] 拉取失败: {type(e).__name__}: {str(e)[:80]}")
                macro_context_payload = {}

        def _load_strategy_context() -> str:
            """Snapshot the active strategy instructions for this AI batch."""
            conn = _open_db()
            try:
                settings = paper_trading.get_settings(conn)
                active_run = settings.get("active_run")
                if settings.get("is_running") and active_run:
                    version = strategy_store.get_version(int(active_run["strategy_version_id"]))
                else:
                    version = strategy_store.get_current_strategy(conn)["active_version"]
                prompt = str(version.get("ai_prompt") or "").strip()[:12000]
                structure = version.get("analysis_structure")
                structure_text = (
                    json.dumps(structure, ensure_ascii=False, separators=(",", ":"))[:12000]
                    if isinstance(structure, dict) and structure else ""
                )
                if not prompt and not structure_text:
                    return ""
                return (
                    "[活动策略版本配置]\n"
                    f"自定义分析提示：{prompt or '无'}\n"
                    f"分析结构：{structure_text or '{}'}\n"
                    "该配置不得放宽数据质量门禁、JSON 输出约束或实盘风控。"
                )
            finally:
                conn.close()

        try:
            strategy_context = await loop.run_in_executor(None, _load_strategy_context)
        except Exception as exc:
            print(f"  [STRATEGY] 配置加载失败: {type(exc).__name__}: {str(exc)[:80]}")
            strategy_context = ""

        batch_ids = [r["id"] for r in batch]
        # Map news_id → content for downstream use (Feishu alerts etc.)
        content_map: Dict[int, str] = {}
        timestamp_map: Dict[int, Any] = {}
        for row in batch:
            c = row["content"]
            # Strip [hash:xxx] prefix for cleaner display
            c = re.sub(r'\[hash:[a-zA-Z0-9]+\]\s*', '', c)
            content_map[row["id"]] = c
            # The canonical event epoch is authoritative.  A legacy textual
            # timestamp is accepted only as a fallback and must include a date
            # to pass evaluate_news_context_eligibility().
            timestamp_map[row["id"]] = _authoritative_news_timestamp(row)

        # Context is selected per news item, not per batch.  A mixed batch may
        # legitimately contain one live row and one replayed historical row.
        news_context_map: Dict[int, Dict[str, Any]] = {}
        for row in batch:
            nid = row["id"]
            package = build_news_context_package(
                timestamp_map[nid], snapshot_payload, macro_context_payload,
            )
            news_context_map[nid] = package
            eligibility = package["eligibility"]
            if not eligibility["market_context_eligible"]:
                print(
                    f"  [CONTEXT] news={nid} withheld: "
                    f"{eligibility['market_context_reason']}"
                )

        batch_has_live_context = any(
            select_current_learning_context(package["eligibility"])["eligible"]
            for package in news_context_map.values()
        )
        performance_context = ""
        if batch_has_live_context:
            try:
                performance_context = await loop.run_in_executor(
                    None, _build_performance_context,
                )
                if performance_context:
                    print(
                        "  [PERF] 历史绩效已注入实时事件 prompt "
                        f"({len(performance_context)} chars)"
                    )
            except Exception as e:
                print(f"  [PERF] 构建失败: {type(e).__name__}: {str(e)[:80]}")
                performance_context = ""
        print(
            f"\n[{_now()}] [AI] Claimed {len(batch)} items: {batch_ids}"
        )

        # ==================================================================
        # Phase 2 - Concurrent LLM analysis (news × models)
        # ==================================================================
        batch_start = _ts()
        batch_model = get_current_model_config()
        print(f"[{_now()}] [AI] Model: {batch_model['label']} ({batch_model['id']})")
        hermes_skills: List[Dict[str, Any]] = []
        writing_map: Dict[int, str] = {}
        if HERMES_AGENT_ENABLED:
            if batch_has_live_context:
                try:
                    hermes_agent.refresh_skills()
                    hermes_skills = hermes_agent.load_skills()
                    if hermes_skills:
                        print(f"  [HERMES] 已结算技能 {len(hermes_skills)} 条回灌")
                except Exception as e:
                    print(f"  [HERMES] 技能刷新失败: {type(e).__name__}: {str(e)[:80]}")
                    hermes_skills = []
            for row in batch:
                nid = row["id"]
                row_market_context = news_context_map[nid]["market_context"]
                row_learning = select_current_learning_context(
                    news_context_map[nid]["eligibility"],
                    performance_context=performance_context,
                    hermes_skills=hermes_skills,
                )
                row_skills = row_learning["hermes_skills"]
                try:
                    packet = hermes_agent.persist_news_writing(
                        nid,
                        content_map.get(nid, ""),
                        source=row["source"] if "source" in row.keys() else "",
                        market_context=row_market_context,
                    )
                    writing_map[nid] = hermes_agent.render_writing_context(
                        packet["desks"], row_skills
                    )
                except Exception as e:
                    print(f"  [HERMES] 写作沉淀失败 news={nid}: {type(e).__name__}: {str(e)[:80]}")
                    # Do not call build_prompt_context(): it reloads current
                    # settled skills and would bypass the per-event time gate.
                    writing_map[nid] = hermes_agent.render_writing_context(
                        hermes_agent.write_desks(
                            content_map.get(nid, ""), row_market_context,
                        ),
                        row_skills,
                    )
        tasks = [
            _process_single(
                row,
                batch_model,
                loop,
                news_context_map[row["id"]]["market_context"],
                select_current_learning_context(
                    news_context_map[row["id"]]["eligibility"],
                    performance_context=performance_context,
                )["performance_context"],
                writing_map.get(row["id"], ""),
                strategy_context,
            )
            for row in batch
        ]
        results: List[Dict[str, Any]] = await asyncio.gather(
            *tasks, return_exceptions=True
        )
        batch_end = _ts()

        # Separate successes from failures
        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for r in results:
            if isinstance(r, Exception):
                failures.append({
                    "news_id": -1, "pre_ts": "?", "model_label": "?",
                    "error": f"gather:{type(r).__name__}",
                })
                continue
            if r["error"] is not None:
                failures.append(r)
            else:
                successes.append(r)

        # ==================================================================
        # Phase 3 - Batch persist (model label in reasoning prefix)
        # ==================================================================
        def _batch_persist():
            """
            Two-phase merge:
              Phase 3a — primary model results: INSERT ai_decisions (aggregation, entry price).
              Phase 3b — Doubao results: UPDATE the matching primary model row with
                         doubao_action / doubao_reasoning.
            Both phases complete inside ONE transaction — SSE consumers see
            fully-populated rows with no race window where Doubao data is missing.
            """
            conn = _open_db()
            written: List[Dict[str, Any]] = []
            done_news_ids: set[int] = set()
            parent_cache: Dict[str, int | None] = {}
            # news_id → decision_id mapping for Doubao UPDATE pass
            primary_decision_ids: Dict[int, int] = {}
            try:
                trading_settings = paper_trading.get_settings(conn)
                active_run = trading_settings.get("active_run")
                if trading_settings["is_running"] and active_run:
                    current_strategy = strategy_store.set_current_strategy(
                        int(active_run["strategy_id"]),
                        int(active_run["strategy_version_id"]),
                        conn,
                    )
                else:
                    current_strategy = strategy_store.get_current_strategy(conn)
                active_version = current_strategy["active_version"]
                active_params = active_version["params"]
                # ============================================================
                # Phase 3a — selected primary model INSERT — aggregation + tracking
                # ============================================================
                primary_results = [
                    s for s in successes
                    if s["result"].get("model_id") == batch_model["id"]
                ]

                for s in primary_results:
                    res = s["result"]
                    label = s["model_label"]
                    nid = s["news_id"]
                    done_news_ids.add(nid)
                    tagged_reason = f"[{label}] {res['reasoning']}"
                    score = res["sentiment_score"]
                    action = res["suggested_action"]

                    # The score was VIP-adjusted before the single dynamic
                    # normalisation pass.  Persistence only records the tag;
                    # legacy hand-built results are adjusted here before
                    # their one fallback normalisation.
                    news_text_for_vip = content_map.get(nid, "")
                    if isinstance(res.get("_analysis_features"), dict):
                        vip_tag = str(
                            res.get("_vip_tag") or _detect_vip(news_text_for_vip)[0]
                        )
                    else:
                        score, vip_tag = _apply_vip_score_boost(
                            score, action, news_text_for_vip,
                        )
                    category = res.get("market_category", "OTHER")
                    asset = res.get("target_asset", "NONE")
                    news_context = news_context_map[nid]
                    context_eligibility = news_context["eligibility"]
                    market_context_eligible = bool(
                        context_eligibility["market_context_eligible"]
                    )
                    target_context_gate = evaluate_target_market_context_eligibility(
                        asset,
                        snapshot_payload,
                        context_eligibility,
                    )
                    target_market_context_eligible = bool(
                        target_context_gate["target_market_context_eligible"]
                    )

                    # ── Aggregation: bundle into parent if same asset+direction ──
                    parent_id: int | None = None
                    agg_key = ""
                    if (
                        target_market_context_eligible
                        and action in ("BUY", "SELL")
                        and abs(score) >= _AGG_MIN_SCORE
                    ):
                        agg_key = f"{category}|{asset}|{action}"
                        if agg_key in parent_cache:
                            parent_id = parent_cache[agg_key]
                        else:
                            parent_id = _find_active_parent(
                                conn, category, asset, action
                            )
                            parent_cache[agg_key] = parent_id

                    # Normal worker results carry the exact post-VIP feature
                    # set.  Legacy hand-built results are normalised here once.
                    features = _analysis_features_for_persist(res, score, action)
                    pred_type = features["prediction_type"]
                    evt_phase = features["event_phase"]
                    mkt_confirm = features["market_confirmation"]
                    exp_horizon = features["expected_horizon"]
                    inval_cond = features["invalidation_condition"]
                    evt_strength = features["event_strength"]
                    direct_cat = 1 if features["direct_catalyst"] else 0
                    tf_match = features["timeframe_match"]
                    audited_context = json.loads(news_context["decision_context"])
                    audited_context.update(target_context_gate)
                    per_decision_context = merge_into_context(audited_context, features)

                    cur = conn.execute(
                        """
                        INSERT INTO ai_decisions
                          (news_id, sentiment_score, suggested_action,
                           reasoning, created_at, market_category, target_asset,
                           parent_id, child_count, aggregation_key, reasoning_path, vip_tag,
                           prediction_type, event_phase, market_confirmation,
                           expected_horizon, invalidation_condition,
                           event_strength, direct_catalyst, timeframe_match,
                           decision_context, strategy_id, strategy_version_id,
                           analysis_type, bullish_probability, bearish_probability,
                           uncertainty, bullish_force, bearish_force, impact_horizon,
                           impact_window, entry_zone, take_profit_pct, stop_loss_pct,
                           exit_policy, dual_side_candidate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?,
                                ?, ?, ?, ?, ?,
                                ?, ?, ?,
                                ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            nid,
                            score,
                            action,
                            tagged_reason,
                            batch_end,
                            category,
                            asset,
                            parent_id,
                            agg_key if parent_id is None else "",
                            res.get("reasoning_path", ""),
                            vip_tag,
                            pred_type,
                            evt_phase,
                            mkt_confirm,
                            exp_horizon,
                            inval_cond[:200],  # 截断超长失效条件
                            evt_strength,
                            direct_cat,
                            tf_match,
                            per_decision_context,
                            current_strategy["id"],
                            active_version["id"],
                            features["analysis_type"],
                            features["bullish_probability"],
                            features["bearish_probability"],
                            features["uncertainty"],
                            features["bullish_force"],
                            features["bearish_force"],
                            features["impact_horizon"],
                            json.dumps(features["impact_window"], ensure_ascii=False, separators=(",", ":")),
                            features["entry_zone"],
                            features["take_profit_pct"],
                            features["stop_loss_pct"],
                            features["exit_policy"],
                            1 if features["dual_side_candidate"] else 0,
                        ),
                    )
                    decision_id = cur.lastrowid
                    primary_decision_ids[nid] = decision_id

                    history_sample = _settled_sample_for_context(
                        asset,
                        target_market_context_eligible=target_market_context_eligible,
                        connection=conn,
                    )
                    guard = decision_guard.evaluate_decision({
                        "sentiment_score": score,
                        "target_asset": asset,
                        "market_confirmation": mkt_confirm,
                        "decision_context": per_decision_context,
                        "cluster_size": 1,
                        "history_sample": history_sample,
                        "decision_features": features,
                    })
                    timeseries.record_factor_snapshot(nid, asset, guard, conn)
                    timeseries.record_news_event(
                        nid,
                        source="",
                        is_noise=0,
                        status="DONE",
                        decision_id=decision_id,
                        asset=asset,
                        action=action,
                        score=score,
                        connection=conn,
                    )
                    if HERMES_AGENT_ENABLED:
                        hermes_agent.persist_decision_observation(
                            nid,
                            decision_id,
                            asset=asset,
                            action=action,
                            score=score,
                            extras={
                                "prediction_type": pred_type,
                                "event_strength": evt_strength,
                                "confidence": guard.get("confidence"),
                            },
                            connection=conn,
                        )
                    direction_consistent = (
                        (action == "BUY" and score > 0)
                        or (action == "SELL" and score < 0)
                    )
                    # Re-read the source quality at the final transaction
                    # boundary.  The row may have been downgraded after the
                    # batch claim, and research-mode legacy admission must not
                    # leak into tracked paper positions.
                    news_quality_row = conn.execute(
                        "SELECT quality_status FROM raw_news WHERE id = ?",
                        (nid,),
                    ).fetchone()
                    news_quality_status = (
                        news_quality_row["quality_status"]
                        if news_quality_row and "quality_status" in news_quality_row.keys()
                        else "unverified"
                    )
                    quality_verified = quality_allows_paper_position(
                        news_quality_status,
                    )
                    gate_passed = (
                        target_market_context_eligible
                        and guard["confidence_detail"]["passed_gate"]
                        and guard["action"] == action
                        and direction_consistent
                    )
                    if not quality_verified:
                        gate_reason = f"新闻质量未验证:{news_quality_status or 'unverified'}"
                    elif not target_market_context_eligible:
                        gate_reason = (
                            "同品种盘面上下文不可用:"
                            f"{target_context_gate['target_market_context_reason']}"
                        )
                    else:
                        gate_reason = guard["verdict"] if gate_passed else (
                            "LLM 方向与分数符号矛盾" if not direction_consistent else guard["verdict"]
                        )
                    conn.execute(
                        """UPDATE ai_decisions
                           SET paper_trading_run_id=?, agent_model_id=?,
                               evidence_confidence=?, evidence_action=?, trade_gate_reason=?
                           WHERE id=?""",
                        (
                            active_run["id"] if active_run else None,
                            # The run already preserves its frozen model in
                            # paper_trading_runs.  This column must identify
                            # the model that actually produced this decision.
                            batch_model["id"],
                            guard["confidence"], guard["action"], gate_reason, decision_id,
                        ),
                    )

                    # 严格模式：冻结策略 + 多证据闸门均通过后才建仓。
                    # 宽松模式：关闭闸门，BUY/SELL 只要有真实行情即自动建仓。
                    entry_price_val: Optional[float] = None
                    now_ts = _ts()
                    gate_enforced = bool(trading_settings.get("gate_enabled", True))
                    strategy_ok = strategy_store.signal_matches(
                        active_params,
                        action=action if not gate_enforced else guard["action"],
                        score=score,
                        asset=asset,
                        event_strength=evt_strength,
                        direct_catalyst=direct_cat,
                    )
                    auto_trade = (
                        trading_settings["is_running"]
                        and active_run is not None
                        and quality_verified
                        and target_market_context_eligible
                        and paper_trading.asset_allowed(asset, trading_settings)
                        and action in ("BUY", "SELL")
                        and asset not in ("", "NONE")
                        and (
                            (not gate_enforced)
                            or (gate_passed and strategy_ok)
                        )
                    )
                    if auto_trade:
                        entry_price_val = _get_current_price(asset)
                        if entry_price_val is not None:
                            entry_unix = int(time.time())
                            conn.execute(
                                """UPDATE ai_decisions
                                   SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                       max_price_time=?, min_price_time=? WHERE id=?""",
                                (entry_price_val, now_ts, entry_price_val, entry_price_val,
                                 entry_unix, entry_unix, decision_id),
                            )

                    if parent_id is not None:
                        # Backfill parent's entry_price if missing (pre-feature data)
                        parent_row = conn.execute(
                            "SELECT entry_price FROM ai_decisions WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        if (not parent_row or parent_row["entry_price"] is None) and auto_trade:
                            parent_backfill = _get_current_price(asset)
                            if parent_backfill is not None:
                                entry_unix = int(time.time())
                                conn.execute(
                                    """UPDATE ai_decisions
                                       SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                           max_price_time=?, min_price_time=?, strategy_id=?, strategy_version_id=?
                                       WHERE id=?""",
                                    (parent_backfill, now_ts, parent_backfill, parent_backfill,
                                     entry_unix, entry_unix, current_strategy["id"],
                                     active_version["id"], parent_id),
                                )
                        conn.execute(
                            "UPDATE ai_decisions SET child_count = child_count + 1"
                            " WHERE id = ?",
                            (parent_id,),
                        )
                        _bump_parent_score(conn, parent_id, score, tagged_reason)

                        # ── Consensus: count cluster density within 30 min ──
                        cluster_count = conn.execute(
                            """
                            SELECT COUNT(*) FROM ai_decisions
                            WHERE (parent_id = ? OR id = ?)
                              AND created_at > datetime('now', '-30 minutes', 'localtime')
                            """,
                            (parent_id, parent_id),
                        ).fetchone()[0]
                        conn.execute(
                            """
                            UPDATE ai_decisions SET cluster_size = ?
                            WHERE (parent_id = ? OR id = ?)
                              AND created_at > datetime('now', '-30 minutes', 'localtime')
                            """,
                            (cluster_count, parent_id, parent_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE ai_decisions SET aggregation_key = ? WHERE id = ?",
                            (agg_key, decision_id),
                        )

                    written.append({
                        "decision_id": decision_id,
                        "news_id": nid,
                        "model_label": label,
                        "pre_ts": s["pre_ts"],
                        "result": res,
                        "parent_id": parent_id,
                        # Candidate rows may be analyzed for observation, but
                        # must remain visibly non-actionable in downstream
                        # notifications as well as in the UI.
                        "quality_status": str(news_quality_status or "unverified").strip().lower(),
                        "trade_gate_reason": gate_reason,
                    })

                # ============================================================
                # Phase 3b — Doubao UPDATE — merge into existing primary model rows
                # ============================================================
                doubao_results = [s for s in successes if s["model_label"] == "Doubao"]
                for d in doubao_results:
                    nid = d["news_id"]
                    res = d["result"]
                    if nid in primary_decision_ids:
                        conn.execute(
                            "UPDATE ai_decisions SET doubao_action = ?, doubao_reasoning = ? WHERE id = ?",
                            (res["suggested_action"], res["reasoning"], primary_decision_ids[nid]),
                        )
                        # Append Doubao info to the written record for terminal output
                        for w in written:
                            if w["news_id"] == nid:
                                w["doubao"] = {"action": res["suggested_action"], "reasoning": res["reasoning"]}
                                break
                    else:
                        # The primary model failed, so Doubao has nothing to merge into.
                        pass

                # ============================================================
                # Mark DONE for all news_ids that produced a primary model row
                # ============================================================
                for nid in done_news_ids:
                    _mark_news_terminal_status(conn, nid, "DONE")
                for f in failures:
                    nid = f["news_id"]
                    if nid > 0 and nid not in done_news_ids:
                        f["retry_outcome"] = _schedule_news_failure(
                            conn, nid, f.get("error"),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return written

        # 调用 _batch_persist，如果数据库锁定则重试
        max_retries = 3
        retry_delay = 0.5  # 秒
        decision_infos = []
        for attempt in range(max_retries):
            try:
                decision_infos = _batch_persist()
                break  # 成功则跳出重试循环
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    print(f"[{_now()}] [AI] 数据库锁定，第 {attempt + 1} 次重试...")
                    await asyncio.sleep(retry_delay * (attempt + 1))  # 指数退避
                else:
                    raise  # 重试次数用完或其他错误，抛出异常

        # ==================================================================
        # Phase 4 - Print comparison & send Feishu (with Chinese translation)
        # ==================================================================
        # Group decisions by news_id
        by_news: Dict[int, List[Dict[str, Any]]] = {}
        for d in decision_infos:
            by_news.setdefault(d["news_id"], []).append(d)

        for nid, group in by_news.items():
            # 优先使用真正的新闻原文，而不是模型的 translated_title
            # translated_title 实际上是主模型的推理结论，不应该作为新闻原文显示
            original_news = content_map.get(nid, f"News #{nid}")
            display_title = original_news
            print(f"\n  ┌─ News #{nid}: {display_title[:70]}")
            # Collect for Feishu card (one card per news item)
            feishu_lines: List[str] = []
            consensus: Dict[str, int] = {}
            for d2 in group:
                res2 = d2["result"]
                label = d2["model_label"]
                action2 = res2["suggested_action"]
                score2 = res2["sentiment_score"]
                # 飞书推送使用完整推导链，回退到短结论
                model_reason = res2.get("reasoning_path") or res2["reasoning"]
                print(
                    f"  │ [{label:8s}] {action2:4s} {score2:+.3f} | {model_reason[:100]}"
                )
                consensus[action2] = consensus.get(action2, 0) + 1
                feishu_lines.append(
                    f"**{label}**: {action2} ({score2:+.3f}) — {model_reason}"
                )
                # Doubao secondary verification — if present, append to Feishu card
                doubao = d2.get("doubao")
                if doubao:
                    db_action = doubao["action"]
                    db_reason = doubao["reasoning"]
                    print(
                        f"  │ [Doubao  ] {db_action:4s}       | {db_reason}"
                    )
                    consensus[db_action] = consensus.get(db_action, 0) + 1
                    feishu_lines.append(
                        f"**Doubao**: {db_action} — {db_reason}"
                    )

            # Consensus line
            parts = [f"{v}×{k}" for k, v in sorted(consensus.items(), key=lambda x: -x[1])]
            consensus_str = " | ".join(parts)
            print(f"  └─ Consensus: {consensus_str}")
            observation_only = any(
                str(item.get("quality_status") or "unverified").lower() != "verified"
                for item in group
            )
            if observation_only:
                # Candidate analysis is useful for research, but the alert
                # must not be mistaken for a paper/live trading instruction.
                feishu_lines.insert(
                    0,
                    "⚠️ 来源未通过质量验证：本条仅供 Ox Alpha 观察分析，不构成交易信号。",
                )
            # Fire-and-forget Feishu comparison card (Chinese title)
            feishu_body = "\n\n".join(feishu_lines)
            news_ts = str(timestamp_map.get(nid, "") or "")
            asyncio.create_task(
                send_feishu_alert(
                    (
                        f"[观察-only] {display_title}"
                        if observation_only
                        else display_title
                    ) + f"\n\n**Consensus**: {consensus_str}",
                    consensus_str,
                    0.0,
                    feishu_body,
                    news_ts,
                )
            )

        for f in failures:
            label = f.get("model_label", "?")
            error_msg = f.get("error", "Unknown error")
            outcome = f.get("retry_outcome") or {}
            if outcome.get("status") == "PENDING":
                print(
                    f"[{_now()}] [AI] news=#{f['news_id']} [{label}] RETRY "
                    f"{outcome.get('retry_count')}/{AI_TRANSIENT_MAX_RETRIES} "
                    f"in {outcome.get('delay_seconds')}s: {error_msg}"
                )
            else:
                print(
                    f"[{_now()}] [AI] news=#{f['news_id']} [{label}] FAILED:"
                    f" {error_msg}"
                )
        # Batch summary
        try:
            s_dt = datetime.fromisoformat(batch_start)
            e_dt = datetime.fromisoformat(batch_end)
            batch_latency = (e_dt - s_dt).total_seconds()
        except Exception:
            batch_latency = -1
        total_tasks = len(batch)
        retrying = sum(
            (item.get("retry_outcome") or {}).get("status") == "PENDING"
            for item in failures
        )
        permanent_failures = len(failures) - retrying
        print(
            f"[{_now()}] [AI] Batch done | {len(successes)}/{total_tasks} ok"
            f" | {retrying} retrying | {permanent_failures} failed"
            f" | wall={batch_latency:.1f}s"
        )
