#!/usr/bin/env python3
"""
Trident Agent MVP — Market Snapshot Module
===========================================

A lightweight, self-contained module that pulls real-time market data
from OKX swap markets via ccxt.  Designed to be called once per
AI batch (NOT once per news item) — the returned dict is injected into
the LLM prompt so Kimi K3 sees market context alongside each headline.

Core assets monitored:
  BTC/USDT  — Bitcoin U本位永续合约
  XAU/USDT  — 黄金 U本位永续合约

Usage:
    from market_snapshot import get_snapshot

    # Async (preferred — call from engine's async batch loop)
    snap = await get_snapshot()

    # Sync (for debugging or non-async contexts)
    snap = get_snapshot_sync()

Returns:
    Dict with per-asset price, 24h change, funding rate, and a
    human-readable summary string ready for prompt injection.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import data_quality
from market_structure import analyze_market_structure
from providers.jin10 import get_jin10_provider

# ---------------------------------------------------------------------------
# ccxt — must be installed (pip install ccxt)
# ---------------------------------------------------------------------------
try:
    import ccxt
    import ccxt.async_support as ccxt_async
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False
    ccxt = None  # type: ignore[assignment]
    ccxt_async = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 核心监控资产 (OKX 永续 swap — 黄金只有永续)
_CORE_SYMBOLS: List[Tuple[str, str]] = [
    ("BTC/USDT:USDT",  "BTC"),
    ("XAU/USDT:USDT",  "XAU"),   # 黄金 U本位永续合约
]

# 没有资金费率的标的 (现货 / 特殊品种)
_SKIP_FUNDING: set = set()

# Debug 日志开关 — 稳定后设为 False, 避免刷屏
_LOUD = False

def _debug(msg: str) -> None:
    if _LOUD:
        print(msg)


def _fetch_jin10_xau_sync() -> Optional[Dict[str, Any]]:
    """Return the authorized Jin10 XAU quote, or None without changing fallback behavior."""
    try:
        provider = get_jin10_provider()
        if not provider.configured:
            return None
        for quote in provider.fetch_quotes(
            asset_type=config.JIN10_MARKET_TYPE,
            codes=config.JIN10_MARKET_CODES,
        ):
            if quote.asset == "XAU":
                return quote.to_dict()
    except Exception as exc:
        _debug(f"  [SNAPSHOT:DEBUG] Jin10 XAU unavailable: {type(exc).__name__}")
    return None


def _apply_jin10_xau(
    assets: Dict[str, Dict[str, Any]],
    quote: Optional[Dict[str, Any]],
    *,
    as_of_ms: Optional[int] = None,
) -> None:
    """Overlay Jin10's spot quote without relabelling OKX-derived evidence.

    The asset's top-level identity always describes the price currently shown.
    Structure and funding retain separate, exact OKX provenance fields and
    their existing field-level eligibility gates.
    """
    if not quote or "XAU" not in assets:
        return
    price = _safe_float(quote.get("price"))
    if price <= 0:
        return
    entry = assets["XAU"]
    quote_source = str(quote.get("source") or "金十")
    quote_symbol = str(quote.get("symbol") or "XAUUSD")
    previous_instrument_id = str(entry.get("instrument_id") or "XAU-USDT-SWAP")
    structure_payload = entry.get("market_structure")
    structure_source = (
        str(structure_payload.get("source") or "OKX")
        if isinstance(structure_payload, dict) else "OKX"
    )
    entry.setdefault("structure_source", structure_source)
    entry.setdefault("structure_venue", "OKX")
    entry.setdefault("structure_instrument_id", previous_instrument_id)
    entry.setdefault("structure_instrument_type", "perpetual_swap")
    entry.setdefault("funding_source", "OKX")
    entry.setdefault("funding_venue", "OKX")
    entry.setdefault("funding_instrument_id", previous_instrument_id)
    entry.setdefault("funding_instrument_type", "perpetual_swap")
    observed_at = _normalize_source_timestamp_ms(quote.get("event_ts"))
    now_ms = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
    is_fresh, freshness_reason = _source_freshness(
        observed_at,
        now_ms,
        max_age_ms=_QUOTE_MAX_AGE_MS,
    )
    quality = data_quality.assess(
        source=quote_source,
        kind="market",
        event_id=f"XAU:{observed_at or 'missing-time'}",
        published_at=observed_at,
        payload=quote,
    )
    if not quality.decision_eligible or not is_fresh:
        entry.setdefault("ignored_sources", []).append({
            "source": "金十",
            "quality_status": quality.quality_status,
            "reason": quality.reason if not quality.decision_eligible else freshness_reason,
        })
        return
    change = quote.get("change_pct")
    change_value = _safe_float(change, float("nan"))
    change_is_finite = math.isfinite(change_value)
    structure = structure_payload if isinstance(structure_payload, dict) else {}
    if structure.get("decision_eligible") is True:
        structure_note = "盘面结构来自 OKX，已通过质量门"
    elif structure.get("status") in {"ok", "partial"}:
        structure_note = "OKX 盘面结构仅供观察，不参与决策"
    else:
        structure_note = "OKX 盘面结构不可用，不使用代币/模拟行情替代"
    entry.update({
        "symbol": quote_symbol,
        "price": round(price, 4),
        "price_str": _format_price(price, 2),
        "change_24h_pct": round(change_value, 2) if change_is_finite else entry.get("change_24h_pct", 0.0),
        "change_24h_str": _format_pct(change_value) if change_is_finite else entry.get("change_24h_str", "N/A"),
        "change_24h_decision_eligible": change_is_finite,
        "source": quote_source,
        "venue": "Jin10",
        "instrument_id": quote_symbol,
        "instrument_type": "spot_quote",
        "quote_source": quote_source,
        "quote_venue": "Jin10",
        "quote_instrument_id": quote_symbol,
        "quote_instrument_type": "spot_quote",
        "quote_at": observed_at,
        "quote_freshness": freshness_reason,
        "quality_status": quality.quality_status,
        "quality_reason": quality.reason,
        "decision_eligible": True,
        "status": "ok",
        "status_note": f"金十现货报价；{structure_note}",
    })

# 代理 — 供受限网络环境下连接 OKX
# 懒加载: exchange 创建时才读取环境变量, 避免模块导入时 env 未就绪
def _get_proxy_kwargs() -> Dict[str, Any]:
    """Build proxy kwargs dict if HTTP_PROXY/HTTPS_PROXY env vars are set."""
    proxy_http = os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip()
    proxy_https = os.getenv("HTTPS_PROXY", "").strip() or os.getenv("https_proxy", "").strip()
    if proxy_http or proxy_https:
        return {
            "proxies": {
                "http": proxy_http,
                "https": proxy_https,
            }
        }
    return {}
_FETCH_TIMEOUT_MS = 8000       # 单次请求超时 (毫秒)
_FETCH_OHLCV_TIMEOUT_MS = 15000  # OHLCV 拉取超时（数据量大，需要更长时间）
_RETRY_DELAY_S = 1.0           # 网络失败后重试间隔 (秒)
_MAX_RETRIES = 1               # 额外重试次数 (总共 1 + 1 = 2 次尝试)

# 盘面结构使用真实的 OKX 闭合 K 线。多取一些是为了剔除当前未闭合 K 线后
# 仍能给指标层提供不少于 220 根的计算窗口。
_STRUCTURE_TIMEFRAMES: Tuple[str, ...] = ("15m", "1h", "4h", "1d")
_STRUCTURE_TIMEFRAME_MS: Dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}
_STRUCTURE_FETCH_LIMIT = 240
_STRUCTURE_CACHE_RETRY_MS = 30_000
_STRUCTURE_CACHE_GRACE_MS = 3_000

# Upstream observation timestamps are part of the decision contract.  Local
# request time is never a substitute: a successfully returned but stale quote
# must remain visible-only rather than silently entering an AI decision.
_QUOTE_MAX_AGE_MS = 2 * 60 * 1000
_FUNDING_MAX_AGE_MS = 10 * 60 * 1000
_SOURCE_MAX_FUTURE_SKEW_MS = 30 * 1000

# (symbol, timeframe) -> {candles, expires_at_ms}. 缓存截止下一根 K 线闭合，
# 因此同一批新闻不会重复拉取不会变化的结构数据。
_STRUCTURE_OHLCV_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_STRUCTURE_OHLCV_RETRY_AFTER: Dict[Tuple[str, str], int] = {}

# 趋势判定阈值
_TREND_BULL_THRESHOLD = 3.0    # 7日涨幅 > 3% 判定为 Bull
_TREND_BEAR_THRESHOLD = -3.0   # 7日涨幅 < -3% 判定为 Bear
_ATR_DAYS = 7                   # 旧统计 ATR 计算窗口 (K 线数)
_TREND_CONSISTENCY_BARS = 5     # 窗口内上涨/下跌 K 线数阈值 (非连续天数)

# ccxt 交易所缓存 (避免每次请求重建 session)
_EXCHANGE: Any = None          # type: ccxt.okx (sync)
_EXCHANGE_ASYNC: Any = None    # type: ccxt.async_support.okx (async)


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------

def _get_exchange() -> Any:
    """Return a configured synchronous ccxt OKX swap exchange (reuse across calls)."""
    global _EXCHANGE
    if _EXCHANGE is None:
        if not HAS_CCXT:
            raise RuntimeError("ccxt 未安装，无法获取行情快照。 pip install ccxt")
        kwargs: Dict[str, Any] = {
            "options": {"defaultType": "swap"},
            "timeout": _FETCH_TIMEOUT_MS,
            "enableRateLimit": True,
        }
        proxy = _get_proxy_kwargs()
        kwargs.update(proxy)
        proxy_note = f" proxy={proxy['proxies']['http']}" if proxy else " (直连)"
        _debug(f"  [SNAPSHOT:DEBUG] 创建 sync OKX exchange...{proxy_note}")
        _EXCHANGE = ccxt.okx(kwargs)
        # Warm up: load markets metadata (cached after first call)
        try:
            start = time.time()
            _EXCHANGE.load_markets()
            elapsed = round((time.time() - start) * 1000)
            _debug(f"  [SNAPSHOT:DEBUG] load_markets() OK in {elapsed}ms")
        except Exception as e:
            _debug(f"  [SNAPSHOT:DEBUG] load_markets() 失败: {type(e).__name__}: {str(e)[:120]}")
            import traceback
            traceback.print_exc()
            # 预热失败不影响后续单次请求
    return _EXCHANGE


def _get_exchange_async() -> Any:
    """Return a configured async ccxt OKX swap exchange."""
    global _EXCHANGE_ASYNC
    if _EXCHANGE_ASYNC is None:
        if not HAS_CCXT:
            raise RuntimeError("ccxt 未安装，无法获取行情快照。 pip install ccxt")
        kwargs: Dict[str, Any] = {
            "options": {"defaultType": "swap"},
            "timeout": _FETCH_TIMEOUT_MS,
            "enableRateLimit": True,
        }
        proxy = _get_proxy_kwargs()
        kwargs.update(proxy)
        proxy_note = f" proxy={proxy['proxies']['http']}" if proxy else " (直连)"
        _debug(f"  [SNAPSHOT:DEBUG] 创建 async OKX exchange...{proxy_note}")
        _EXCHANGE_ASYNC = ccxt_async.okx(kwargs)
        # load_markets 在首次 fetch_ticker 时隐式调用, 这里仅打日志
        _debug(f"  [SNAPSHOT:DEBUG] async exchange 已创建 (load_markets 将在首次 fetch 隐式执行)")
    return _EXCHANGE_ASYNC


# ---------------------------------------------------------------------------
# Single-ticker fetchers
# ---------------------------------------------------------------------------

def _fetch_ticker_sync(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch a ticker from OKX for one exact symbol. Returns None on failure."""
    ex = _get_exchange()
    for attempt in range(1 + _MAX_RETRIES):
        try:
            ticker = ex.fetch_ticker(symbol)
            # Verify a finite positive price, not merely a present key.
            price = _extract_price(ticker)
            if price is None or price <= 0:
                return None
            return ticker
        except Exception as e:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S)
            else:
                print(f"  [SNAPSHOT] fetch_ticker({symbol}) 失败: {type(e).__name__}: {str(e)[:80]}")
    return None


async def _fetch_ticker_async(symbol: str) -> Optional[Dict[str, Any]]:
    """Async fetch one exact ticker from OKX."""
    ex = _get_exchange_async()
    for attempt in range(1 + _MAX_RETRIES):
        try:
            t0 = time.time()
            ticker = await ex.fetch_ticker(symbol)
            elapsed_ms = round((time.time() - t0) * 1000)
            _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) attempt={attempt+1} "
                  f"HTTP_OK elapsed={elapsed_ms}ms")
            # ── response 验证 ──
            if ticker is None:
                _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) → response 为 None/空")
                return None
            last = _extract_price(ticker)
            if last is None or last <= 0:
                _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) → 所有价格字段缺失: "
                      f"keys={list(ticker.keys())[:12]}")
                # Print raw info for diagnostics
                raw_info = ticker.get("info", {})
                if isinstance(raw_info, dict):
                    _debug(f"  [SNAPSHOT:DEBUG]   raw info keys: {list(raw_info.keys())[:10]}")
                    _debug(f"  [SNAPSHOT:DEBUG]   raw info sample: {json.dumps(raw_info, ensure_ascii=False)[:300]}")
                return None
            _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) → last={last}, "
                  f"bid={ticker.get('bid')}, ask={ticker.get('ask')}, "
                  f"change_24h={ticker.get('percentage')}")
            return ticker
        except Exception as e:
            exc_type = type(e).__name__
            exc_msg = str(e)[:200]
            _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) attempt={attempt+1} 异常: "
                  f"{exc_type}: {exc_msg}")
            # ── 分类错误类型 ──
            if "ConnectionError" in exc_type or "ConnectTimeout" in exc_type or "ProxyError" in exc_type:
                _debug(f"  [SNAPSHOT:DEBUG] → 网络连接失败 (DNS/代理/防火墙?)")
            elif "Timeout" in exc_type or "timed" in exc_msg.lower():
                _debug(f"  [SNAPSHOT:DEBUG] → 请求超时 (OKX API 响应慢/网络延迟)")
            elif "RateLimit" in exc_type or "DDoS" in exc_msg:
                _debug(f"  [SNAPSHOT:DEBUG] → 被 OKX 限流")
            elif "BadSymbol" in exc_msg or "not found" in exc_msg.lower():
                _debug(f"  [SNAPSHOT:DEBUG] → 交易对不存在 (需检查 defaultType=future)")
            elif "ExchangeNotAvailable" in exc_type:
                _debug(f"  [SNAPSHOT:DEBUG] → OKX 服务不可用")
            else:
                import traceback
                _debug(f"  [SNAPSHOT:DEBUG] → 未分类异常, 完整 traceback:")
                traceback.print_exc()
            if attempt < _MAX_RETRIES:
                _debug(f"  [SNAPSHOT:DEBUG] → 等待 {_RETRY_DELAY_S}s 后重试...")
                await asyncio.sleep(_RETRY_DELAY_S)
            else:
                _debug(f"  [SNAPSHOT:DEBUG] fetch_ticker({symbol}) 所有重试已耗尽, 返回 None")
    return None


def _fetch_funding_rate_sync(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch a funding observation without discarding its source timestamp."""
    ex = _get_exchange()
    try:
        info = ex.fetch_funding_rate(symbol)
        rate = info.get("fundingRate")
        if rate is None:
            rate = info.get("info", {}).get("lastFundingRate")
        if rate is not None:
            parsed = float(rate)
            if math.isfinite(parsed):
                return {
                    "rate": parsed,
                    "source_ts_ms": _extract_source_timestamp_ms(info),
                }
    except Exception:
        pass
    return None


async def _fetch_funding_rate_async(symbol: str) -> Optional[Dict[str, Any]]:
    """Async fetch a funding observation with its exchange timestamp."""
    ex = _get_exchange_async()
    try:
        info = await ex.fetch_funding_rate(symbol)
        rate = info.get("fundingRate")
        if rate is None:
            rate = info.get("info", {}).get("lastFundingRate")
        if rate is not None:
            parsed = float(rate)
            if math.isfinite(parsed):
                return {
                    "rate": parsed,
                    "source_ts_ms": _extract_source_timestamp_ms(info),
                }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Safe field parsers
# ---------------------------------------------------------------------------

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Coerce a finite value to float or return default."""
    if val is None:
        return default
    try:
        parsed = float(val)
        return parsed if math.isfinite(parsed) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _normalize_source_timestamp_ms(value: Any) -> Optional[int]:
    """Normalize an authoritative epoch/ISO timestamp to epoch milliseconds.

    Naive ISO strings and implausible epochs are rejected: guessing a timezone
    here would turn an ambiguous observation into apparently fresh data.
    """
    if value is None or isinstance(value, bool):
        return None
    parsed: Optional[float] = None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            try:
                from datetime import datetime

                candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if candidate.tzinfo is None:
                    return None
                parsed = candidate.timestamp() * 1000.0
            except (ValueError, OverflowError):
                return None
    if parsed is None or not math.isfinite(parsed):
        return None
    # Seconds are currently ~1e9; milliseconds ~1e12.  Reject micro/nano and
    # dates before 2000 so malformed provider values cannot look fresh.
    if 946_684_800 <= parsed < 10_000_000_000:
        parsed *= 1000.0
    if not 946_684_800_000 <= parsed < 10_000_000_000_000:
        return None
    return int(parsed)


def _extract_source_timestamp_ms(payload: Any) -> Optional[int]:
    """Read the observation time emitted by ccxt/OKX, never local receipt time."""
    if not isinstance(payload, dict):
        return None
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    for value in (
        payload.get("timestamp"),
        payload.get("datetime"),
        info.get("ts"),
        info.get("timestamp"),
        info.get("uTime"),
    ):
        normalized = _normalize_source_timestamp_ms(value)
        if normalized is not None:
            return normalized
    return None


def _source_freshness(
    source_ts_ms: Optional[int],
    as_of_ms: int,
    *,
    max_age_ms: int,
) -> Tuple[bool, str]:
    """Return strict source-time eligibility and an auditable reason."""
    if source_ts_ms is None:
        return False, "source_timestamp_missing"
    age_ms = int(as_of_ms) - int(source_ts_ms)
    if age_ms < -_SOURCE_MAX_FUTURE_SKEW_MS:
        return False, "source_timestamp_in_future"
    if age_ms > max_age_ms:
        return False, "source_timestamp_stale"
    return True, "source_timestamp_fresh"


def _extract_price(ticker: Dict[str, Any]) -> Optional[float]:
    """Extract current price from a ccxt ticker dict with multi-key fallback.

    ccxt normalizes spot and swap tickers differently across exchanges.
    This function tries every known key path.
    """
    # 1. ccxt normalized keys
    for key in ("last", "close"):
        val = ticker.get(key)
        if val is not None:
            price = _safe_float(val, float("nan"))
            if math.isfinite(price) and price > 0:
                return price
    # 2. Raw exchange response
    info = ticker.get("info", {})
    if isinstance(info, dict):
        for key in ("lastPrice", "last", "price"):
            val = info.get(key)
            if val is not None:
                price = _safe_float(val, float("nan"))
                if math.isfinite(price) and price > 0:
                    return price
    # 3. Bid/ask midpoint (last resort)
    bid = _safe_float(ticker.get("bid"), float("nan"))
    ask = _safe_float(ticker.get("ask"), float("nan"))
    if (
        math.isfinite(bid) and math.isfinite(ask)
        and bid > 0 and ask > 0 and bid <= ask
    ):
        # Stable midpoint avoids overflowing ``bid + ask`` for large but
        # individually finite provider values.
        midpoint = bid + (ask - bid) / 2.0
        return round(midpoint, 4) if math.isfinite(midpoint) else None
    return None


# ---------------------------------------------------------------------------
# OHLCV fetchers — closed multi-timeframe candles with per-timeframe cache
# ---------------------------------------------------------------------------

_OHLCV_FALLBACK_LIMIT = 43  # 43 根 4h K 线包含 42 个间隔，恰好约 7 天


def _filter_closed_candles(
    candles: Any,
    timeframe: str,
    as_of_ms: int,
) -> List[List[float]]:
    """Order rows and retain only obvious candle shapes closed by ``as_of_ms``.

    ccxt normally includes the in-progress last candle. Letting that candle
    enter structure analysis would make signals repaint within the bar. OHLCV
    validity and conflicting duplicates remain for the analysis quality gate.
    """
    duration_ms = _STRUCTURE_TIMEFRAME_MS[timeframe]
    filtered: List[Tuple[int, List[float]]] = []
    if not isinstance(candles, (list, tuple)):
        return []
    for raw in candles:
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            continue
        if isinstance(raw[0], bool):
            continue
        try:
            timestamp_number = float(raw[0])
            opened_at = int(timestamp_number)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            opened_at < 0
            or opened_at > 10**16
            or abs(timestamp_number - opened_at) > 1e-6
            or opened_at + duration_ms > as_of_ms
        ):
            continue
        # Preserve duplicate timestamps, including conflicting duplicates.
        # The analysis layer must see and reject that evidence explicitly so
        # its quality report remains auditable.
        filtered.append((opened_at, list(raw[:6])))
    filtered.sort(key=lambda item: item[0])
    return [row for _, row in filtered[-_STRUCTURE_FETCH_LIMIT:]]


def _cache_expiry_ms(candles: List[List[float]], timeframe: str, as_of_ms: int) -> int:
    """Expire shortly after the next expected candle close."""
    duration_ms = _STRUCTURE_TIMEFRAME_MS[timeframe]
    next_close_ms = int(candles[-1][0]) + (2 * duration_ms) + _STRUCTURE_CACHE_GRACE_MS
    return max(as_of_ms + _STRUCTURE_CACHE_RETRY_MS, next_close_ms)


def _cached_closed_candles(symbol: str, timeframe: str, as_of_ms: int) -> Optional[List[List[float]]]:
    cached = _STRUCTURE_OHLCV_CACHE.get((symbol, timeframe))
    if not cached or int(cached.get("expires_at_ms") or 0) <= as_of_ms:
        return None
    return [list(row) for row in (cached.get("candles") or [])]


def _store_closed_candles(
    symbol: str,
    timeframe: str,
    candles: List[List[float]],
    as_of_ms: int,
) -> None:
    _STRUCTURE_OHLCV_CACHE[(symbol, timeframe)] = {
        "candles": [list(row) for row in candles],
        "expires_at_ms": _cache_expiry_ms(candles, timeframe, as_of_ms),
        "fetched_at_ms": as_of_ms,
        "source": "OKX",
    }
    _STRUCTURE_OHLCV_RETRY_AFTER.pop((symbol, timeframe), None)


def _mark_timeframe_fetch_failed(symbol: str, timeframe: str, as_of_ms: int) -> None:
    _STRUCTURE_OHLCV_RETRY_AFTER[(symbol, timeframe)] = as_of_ms + _STRUCTURE_CACHE_RETRY_MS


def _timeframe_retry_blocked(symbol: str, timeframe: str, as_of_ms: int) -> bool:
    return _STRUCTURE_OHLCV_RETRY_AFTER.get((symbol, timeframe), 0) > as_of_ms


def _fetch_closed_timeframe_sync(
    symbol: str,
    timeframe: str,
    as_of_ms: int,
) -> Optional[List[List[float]]]:
    cached = _cached_closed_candles(symbol, timeframe, as_of_ms)
    if cached is not None:
        return cached
    if _timeframe_retry_blocked(symbol, timeframe, as_of_ms):
        return None
    try:
        rows = _get_exchange().fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=_STRUCTURE_FETCH_LIMIT,
            params={"timeout": _FETCH_OHLCV_TIMEOUT_MS},
        )
        closed = _filter_closed_candles(rows, timeframe, as_of_ms)
        if closed:
            _store_closed_candles(symbol, timeframe, closed, as_of_ms)
            return closed
    except Exception as exc:
        _debug(
            f"  [SNAPSHOT:DEBUG] fetch_ohlcv({symbol} {timeframe}) 失败: "
            f"{type(exc).__name__}: {str(exc)[:80]}"
        )
    _mark_timeframe_fetch_failed(symbol, timeframe, as_of_ms)
    # Fail closed after cache expiry. Reusing an expired bar here would also
    # leak stale values into the legacy stats_7d consumer.
    return None


def _okx_instrument_id(symbol: str) -> str:
    """Convert a ccxt swap symbol to the exact public OKX instrument id."""
    market = str(symbol or "").split(":", 1)[0].replace("/", "-")
    return f"{market}-SWAP" if market else ""


def _okx_provenance(symbol: str) -> Dict[str, str]:
    """Return explicit quote/structure/funding identities for an OKX swap."""
    instrument_id = _okx_instrument_id(symbol)
    return {
        "source": "OKX",
        "venue": "OKX",
        "instrument_id": instrument_id,
        "instrument_type": "perpetual_swap",
        "quote_source": "OKX",
        "quote_venue": "OKX",
        "quote_instrument_id": instrument_id,
        "quote_instrument_type": "perpetual_swap",
        "structure_source": "OKX",
        "structure_venue": "OKX",
        "structure_instrument_id": instrument_id,
        "structure_instrument_type": "perpetual_swap",
        "funding_source": "OKX",
        "funding_venue": "OKX",
        "funding_instrument_id": instrument_id,
        "funding_instrument_type": "perpetual_swap",
    }


async def _fetch_closed_timeframe_async(
    symbol: str,
    timeframe: str,
    as_of_ms: int,
) -> Optional[List[List[float]]]:
    cached = _cached_closed_candles(symbol, timeframe, as_of_ms)
    if cached is not None:
        return cached
    if _timeframe_retry_blocked(symbol, timeframe, as_of_ms):
        return None
    try:
        rows = await _get_exchange_async().fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=_STRUCTURE_FETCH_LIMIT,
            params={"timeout": _FETCH_OHLCV_TIMEOUT_MS},
        )
        closed = _filter_closed_candles(rows, timeframe, as_of_ms)
        if closed:
            _store_closed_candles(symbol, timeframe, closed, as_of_ms)
            return closed
    except Exception as exc:
        _debug(
            f"  [SNAPSHOT:DEBUG] fetch_ohlcv({symbol} {timeframe}) 失败: "
            f"{type(exc).__name__}: {str(exc)[:80]}"
        )
    _mark_timeframe_fetch_failed(symbol, timeframe, as_of_ms)
    return None


def _fetch_structure_timeframes_sync(
    symbol: str,
    as_of_ms: Optional[int] = None,
) -> Dict[str, List[List[float]]]:
    """Fetch each timeframe independently so one failed request cannot poison the rest."""
    cutoff_ms = int(time.time() * 1000 if as_of_ms is None else as_of_ms)
    result: Dict[str, List[List[float]]] = {}
    for timeframe in _STRUCTURE_TIMEFRAMES:
        rows = _fetch_closed_timeframe_sync(symbol, timeframe, cutoff_ms)
        if rows:
            result[timeframe] = rows
    return result


async def _fetch_structure_timeframes_async(
    symbol: str,
    as_of_ms: Optional[int] = None,
) -> Dict[str, List[List[float]]]:
    """Async equivalent; all four independent OKX requests run concurrently."""
    cutoff_ms = int(time.time() * 1000 if as_of_ms is None else as_of_ms)
    tasks = {
        timeframe: asyncio.create_task(
            _fetch_closed_timeframe_async(symbol, timeframe, cutoff_ms)
        )
        for timeframe in _STRUCTURE_TIMEFRAMES
    }
    result: Dict[str, List[List[float]]] = {}
    for timeframe, task in tasks.items():
        rows = await task
        if rows:
            result[timeframe] = rows
    return result


def _select_7d_candles(
    timeframes: Dict[str, List[List[float]]],
) -> Optional[Tuple[List[List[float]], str]]:
    """Reuse structure candles for legacy 7d fields, with an explicit source timeframe."""
    daily = timeframes.get("1d") or []
    if len(daily) >= 8:
        return daily[-8:], "1d"
    four_hour = timeframes.get("4h") or []
    if len(four_hour) >= _OHLCV_FALLBACK_LIMIT:
        return four_hour[-_OHLCV_FALLBACK_LIMIT:], "4h"
    return None


def _fetch_ohlcv_with_fallback_sync(symbol: str) -> Optional[Tuple[List[List[float]], str]]:
    """Backward-compatible legacy helper backed by the shared closed-bar cache."""
    return _select_7d_candles(_fetch_structure_timeframes_sync(symbol))


async def _fetch_ohlcv_with_fallback_async(symbol: str) -> Optional[Tuple[List[List[float]], str]]:
    """Backward-compatible async legacy helper backed by the shared closed-bar cache."""
    return _select_7d_candles(await _fetch_structure_timeframes_async(symbol))


# ---------------------------------------------------------------------------
# Trend & regime computation
# ---------------------------------------------------------------------------

def _validated_legacy_candles(candles: Any) -> Optional[List[List[float]]]:
    """Return unambiguous finite rows for the legacy 7-day summary.

    The structure engine already validates its own input, but ``stats_7d`` is
    a separate compatibility path. Validate again here so malformed or
    conflicting provider rows cannot leak NaN into prompts or cast a legacy
    directional vote.
    """
    if not isinstance(candles, (list, tuple)):
        return None
    by_timestamp: Dict[int, List[float]] = {}
    for raw in candles:
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            return None
        try:
            timestamp_value = float(raw[0])
            values = [float(raw[index]) for index in range(1, 6)]
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isfinite(timestamp_value)
            or timestamp_value < 0
            or timestamp_value != int(timestamp_value)
            or any(not math.isfinite(value) for value in values)
        ):
            return None
        open_, high, low, close, volume = values
        if (
            open_ <= 0
            or high <= 0
            or low <= 0
            or close <= 0
            or volume < 0
            or high < low
            or not low <= open_ <= high
            or not low <= close <= high
        ):
            return None
        timestamp = int(timestamp_value)
        row = [timestamp, open_, high, low, close, volume]
        existing = by_timestamp.get(timestamp)
        if existing is not None and existing != row:
            return None
        by_timestamp[timestamp] = row
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _compute_7d_stats(
    candles: List[List[float]],
    source_tf: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute legacy 7d fields while exposing their actual bar semantics.

    Args:
        candles: List of [timestamp_ms, open, high, low, close, volume].

    Returns dict with:
        price_7d_ago, return_7d_pct, return_7d_str,
        atr_pct, atr_value, atr_str,
        trend, trend_strength, up_bars, down_bars,
        highest_7d, lowest_7d, range_7d_pct.

    ``n_up_days`` / ``n_down_days`` remain as deprecated aliases so existing
    consumers do not break. They count bars and never mean consecutive days.
    """
    validated = _validated_legacy_candles(candles)
    if not validated or len(validated) < 2:
        return _empty_7d_stats(source_tf)

    candles = validated

    closes = [c[4] for c in candles]   # index 4 = close
    highs = [c[2] for c in candles]     # index 2 = high
    lows = [c[3] for c in candles]      # index 3 = low

    current = closes[-1]
    prior = closes[0]
    if prior <= 0 or current <= 0:
        return _empty_7d_stats(source_tf)

    ret_7d = (current - prior) / prior * 100.0
    highest_7d = max(highs)
    lowest_7d = min(lows)
    range_7d = (highest_7d - lowest_7d) / current * 100.0 if current > 0 else 0.0

    # ── ATR (Average True Range) ──
    tr_values: List[float] = []
    for i in range(1, len(candles)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_values.append(tr)
    atr_window = tr_values[-_ATR_DAYS:]
    atr_value = sum(atr_window) / len(atr_window) if atr_window else 0.0
    atr_pct = (atr_value / current * 100.0) if current > 0 else 0.0

    # ── Trend classification ──
    n_up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    n_down = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])

    # Preserve the old 5-of-7 daily intent across the 4h fallback instead of
    # treating five positive 4h bars out of 42 as a strong weekly trend.
    strong_bar_threshold = max(
        _TREND_CONSISTENCY_BARS,
        math.ceil((len(closes) - 1) * 0.70),
    )
    if n_up >= strong_bar_threshold and ret_7d > _TREND_BULL_THRESHOLD:
        trend = "Strong Bull"
        trend_strength = "high"
    elif n_down >= strong_bar_threshold and ret_7d < _TREND_BEAR_THRESHOLD:
        trend = "Strong Bear"
        trend_strength = "high"
    elif ret_7d > _TREND_BULL_THRESHOLD:
        trend = "Bull"
        trend_strength = "medium"
    elif ret_7d < _TREND_BEAR_THRESHOLD:
        trend = "Bear"
        trend_strength = "medium"
    elif abs(ret_7d) < 1.0:
        trend = "Ranging"
        trend_strength = "low"
    elif ret_7d > 0:
        trend = "Mild Bull"
        trend_strength = "low"
    else:
        trend = "Mild Bear"
        trend_strength = "low"

    return {
        "price_7d_ago": round(prior, 4),
        "return_7d_pct": round(ret_7d, 2),
        "return_7d_str": _format_pct(ret_7d),
        "atr_pct": round(atr_pct, 3),
        "atr_value": round(atr_value, 4),
        "atr_str": f"{atr_pct:.3f}%",
        "trend": trend,
        "trend_strength": trend_strength,
        "up_bars": n_up,
        "down_bars": n_down,
        "bar_count": len(candles),
        "source_tf": source_tf or "unknown",
        "atr_period_bars": len(atr_window),
        "atr_source_tf": source_tf or "unknown",
        # Backwards-compatible aliases. These are bar counts, not streaks.
        "n_up_days": n_up,
        "n_down_days": n_down,
        "highest_7d": round(highest_7d, 4),
        "lowest_7d": round(lowest_7d, 4),
        "range_7d_pct": round(range_7d, 2),
    }


def _empty_7d_stats(source_tf: Optional[str] = None) -> Dict[str, Any]:
    """Return empty 7d stats for error/degraded paths."""
    return {
        "price_7d_ago": 0.0,
        "return_7d_pct": 0.0,
        "return_7d_str": "N/A",
        "atr_pct": 0.0,
        "atr_value": 0.0,
        "atr_str": "N/A",
        "trend": "Unknown",
        "trend_strength": "none",
        "up_bars": 0,
        "down_bars": 0,
        "bar_count": 0,
        "source_tf": source_tf or "unavailable",
        "atr_period_bars": 0,
        "atr_source_tf": source_tf or "unavailable",
        "n_up_days": 0,
        "n_down_days": 0,
        "highest_7d": 0.0,
        "lowest_7d": 0.0,
        "range_7d_pct": 0.0,
    }


def _unavailable_market_structure(reason: str, source: str = "OKX") -> Dict[str, Any]:
    """Return a stable, explicitly non-tradable structure envelope."""
    return {
        "status": "unavailable",
        "source": source,
        "decision_eligible": False,
        "trend_score": 0.0,
        "trend": "unknown",
        "confidence": 0.0,
        "alignment": "unavailable",
        "alignment_score": 0.0,
        "aggregate": {
            "trend_score": 0.0,
            "trend": "unknown",
            "confidence": 0.0,
            "alignment": "unavailable",
            "alignment_score": 0.0,
            "available_timeframes": [],
        },
        "timeframes": {},
        "quality": {
            "source": source,
            "source_decision_eligible": False,
            "reason": reason,
        },
        "research": {
            "smc": {
                "status": "research_only",
                "execution_eligible": False,
            },
        },
        "warnings": [reason],
        "reason": reason,
    }


def _analyze_structure_safe(
    timeframes: Dict[str, List[List[float]]],
    *,
    as_of_ms: int,
    source_decision_eligible: bool,
) -> Dict[str, Any]:
    """Run structure analysis without allowing it to break the ticker snapshot."""
    if not timeframes:
        return _unavailable_market_structure("OKX 未返回真实 OHLCV")
    try:
        raw = analyze_market_structure(
            timeframes,
            as_of_ms=as_of_ms,
            source="OKX",
            source_decision_eligible=bool(source_decision_eligible),
        )
    except Exception as exc:
        return _unavailable_market_structure(
            f"盘面结构计算失败: {type(exc).__name__}"
        )
    if not isinstance(raw, dict):
        return _unavailable_market_structure("盘面结构返回格式无效")

    result = dict(raw)
    result.setdefault("source", "OKX")
    result.setdefault("status", "partial")
    result.setdefault("timeframes", {})
    result.setdefault("warnings", [])
    if not isinstance(result.get("aggregate"), dict):
        result["aggregate"] = {
            key: result.get(key)
            for key in (
                "trend_score", "trend", "confidence", "alignment",
                "alignment_score", "available_timeframes",
            )
            if key in result
        }
    quality = result.get("quality")
    if not isinstance(quality, dict):
        quality = {}
    else:
        quality = dict(quality)
    quality.setdefault("source", "OKX")
    quality["source_decision_eligible"] = bool(source_decision_eligible)
    result["quality"] = quality
    # The analyzer may never override the repository's source quality gate.
    if not source_decision_eligible:
        result["decision_eligible"] = False
    else:
        result["decision_eligible"] = bool(result.get("decision_eligible"))
    return result


def _structure_timeframe_decision_eligible(
    structure: Dict[str, Any],
    timeframe: Optional[str],
    stats: Dict[str, Any],
) -> bool:
    """Bind legacy stats eligibility to the exact timeframe they use."""
    if not timeframe or stats.get("return_7d_str") in {None, "N/A"}:
        return False
    frames = structure.get("timeframes")
    frame = frames.get(timeframe) if isinstance(frames, dict) else None
    return bool(
        isinstance(frame, dict)
        and frame.get("decision_eligible") is True
        and frame.get("status") in {"ok", "partial"}
    )


def _format_pct(value: float, decimals: int = 2) -> str:
    """Format a float as a signed percentage string, e.g. '+2.35%'."""
    return f"{value:+.{decimals}f}%"


def _format_price(value: float, decimals: int = 2) -> str:
    """Format a price with appropriate decimal places."""
    if value >= 1000:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"


def _format_optional_price(value: Any, decimals: int = 2) -> str:
    """Format a structure level without turning missing data into 0.00."""
    number = _safe_float(value, float("nan"))
    if not isinstance(number, (float, int)) or not math.isfinite(float(number)) or float(number) <= 0:
        return "N/A"
    return _format_price(float(number), decimals)


def _format_optional_number(value: Any, decimals: int = 2, suffix: str = "") -> str:
    """Format a finite indicator without fabricating zero for missing data."""
    number = _safe_float(value, float("nan"))
    if not math.isfinite(number):
        return "N/A"
    return f"{number:.{decimals}f}{suffix}"


# ---------------------------------------------------------------------------
# Main snapshot API
# ---------------------------------------------------------------------------

async def get_snapshot() -> Dict[str, Any]:
    """
    Async — pull a multi-asset market snapshot from exact OKX swap symbols.

    Returns a dict:
      {
        "timestamp": "2026-07-23 14:30:01 CST",
        "epoch_ms": 1753285801000,
        "assets": {
          "BTC": {
            "symbol": "BTC/USDT",
            "price": 67200.50,
            "price_str": "67,200.50",
            "change_24h_pct": 2.35,
            "change_24h_str": "+2.35%",
            "funding_rate_pct": 0.0100,
            "funding_rate_str": "+0.0100%",
            "status": "ok",
          },
          "XAU": {
            "symbol": "XAU/USDT",
            "price": 2680.00,
            ...
            "status": "ok",            # 或 "unavailable" / "degraded"
            "status_note": "现货代理, 无资金费率",
          },
          ...
        },
        "summary": "...",              # 人类可读的摘要, 可直接注入 Prompt
        "status": "ok",                # 整体状态: "ok" | "partial" | "down"
      }

    On graceful degradation:
      - 单个标的拉不到 → status="unavailable", 不影响其他标的
      - 全部拉不到 → status="down", summary 返回占位字符串
      - 网络/ccxt 异常 → 永不抛异常, 最差情况返回空快照
    """
    if not HAS_CCXT:
        _debug(f"  [SNAPSHOT:DEBUG] ❌ HAS_CCXT=False — ccxt 未安装!")
        return _empty_snapshot("ccxt 未安装 (pip install ccxt)")

    _debug(f"  [SNAPSHOT:DEBUG] ccxt version={ccxt.__version__}, "
          f"async_ccxt version={ccxt_async.__version__ if ccxt_async else 'None'}")
    jin10_task = (
        asyncio.create_task(asyncio.to_thread(_fetch_jin10_xau_sync))
        if getattr(config, "JIN10_ENABLED", False) else None
    )
    ex = _get_exchange_async()
    _debug(f"  [SNAPSHOT:DEBUG] exchange={type(ex).__name__}, "
          f"urls.api={ex.urls.get('api', 'N/A') if hasattr(ex, 'urls') else 'N/A'}")
    t0 = time.time()
    ts = int(t0 * 1000)

    assets: Dict[str, Dict[str, Any]] = {}
    ok_count = 0
    fail_count = 0

    # ── 并行拉取 ticker + 多周期 OHLCV + 资金费率 ──
    _debug(f"  [SNAPSHOT:DEBUG] 启动并行 fetch: ticker × {len(_CORE_SYMBOLS)}, "
          f"structure(4tf) × {len(_CORE_SYMBOLS)}, funding × {len(_CORE_SYMBOLS)}")
    ticker_tasks = {
        asset_id: asyncio.create_task(_fetch_ticker_async(symbol))
        for symbol, asset_id in _CORE_SYMBOLS
    }
    structure_tasks = {
        asset_id: asyncio.create_task(_fetch_structure_timeframes_async(symbol, ts))
        for symbol, asset_id in _CORE_SYMBOLS
    }
    funding_tasks: Dict[str, asyncio.Task] = {}
    for symbol, asset_id in _CORE_SYMBOLS:
        if asset_id in _SKIP_FUNDING:
            continue
        funding_tasks[asset_id] = asyncio.create_task(_fetch_funding_rate_async(symbol))

    # 等待全部完成
    ticker_results: Dict[str, Optional[Dict]] = {}
    for asset_id, task in ticker_tasks.items():
        ticker_results[asset_id] = await task
    structure_results: Dict[str, Dict[str, List[List[float]]]] = {}
    for asset_id, task in structure_tasks.items():
        structure_results[asset_id] = await task
    funding_results: Dict[str, Optional[Dict[str, Any]]] = {}
    for asset_id, task in funding_tasks.items():
        funding_results[asset_id] = await task

    # ── DEBUG: 打印原始 task 结果 ──
    for asset_id in ticker_results:
        t = ticker_results.get(asset_id)
        _debug(f"  [SNAPSHOT:DEBUG] ticker_result[{asset_id}] = "
              f"{'None' if t is None else f'OK(keys={list(t.keys())[:5]})'}")
    for asset_id, timeframes in structure_results.items():
        counts = {timeframe: len(rows) for timeframe, rows in timeframes.items()}
        _debug(f"  [SNAPSHOT:DEBUG] structure_ohlcv[{asset_id}] = {counts or 'unavailable'}")
    for asset_id in funding_results:
        f = funding_results.get(asset_id)
        rate = f.get("rate") if isinstance(f, dict) else None
        _debug(f"  [SNAPSHOT:DEBUG] funding_result[{asset_id}] = "
              f"{'None' if rate is None else f'{float(rate):.6f}'}")

    # ── 组装结果 ──
    for symbol, asset_id in _CORE_SYMBOLS:
        ticker = ticker_results.get(asset_id)
        funding_observation = funding_results.get(asset_id)
        funding_rate_value = (
            _safe_float(funding_observation.get("rate"), float("nan"))
            if isinstance(funding_observation, dict) else float("nan")
        )
        funding_rate = funding_rate_value if math.isfinite(funding_rate_value) else None
        funding_at = (
            _normalize_source_timestamp_ms(funding_observation.get("source_ts_ms"))
            if isinstance(funding_observation, dict) else None
        )
        structure_timeframes = structure_results.get(asset_id) or {}
        ohlcv_pair = _select_7d_candles(structure_timeframes)
        ohlcv, ohlcv_tf = ohlcv_pair if ohlcv_pair else (None, None)

        # 计算 7d 趋势统计
        stats_7d = (
            _compute_7d_stats(ohlcv, ohlcv_tf)
            if ohlcv else _empty_7d_stats(ohlcv_tf)
        )

        if ticker is None:
            market_structure = _analyze_structure_safe(
                structure_timeframes,
                as_of_ms=ts,
                source_decision_eligible=False,
            )
            assets[asset_id] = {
                "symbol": symbol,
                **_okx_provenance(symbol),
                "price": 0.0,
                "price_str": "获取失败",
                "change_24h_pct": 0.0,
                "change_24h_str": "N/A",
                "funding_rate_pct": 0.0,
                "funding_rate_str": "N/A",
                "funding_at": funding_at,
                "funding_freshness": "ticker_unavailable",
                "funding_decision_eligible": False,
                "status": "unavailable",
                "status_note": "网络请求失败",
                "quote_at": _extract_source_timestamp_ms(ticker),
                "quote_freshness": "ticker_unavailable",
                "change_24h_decision_eligible": False,
                "quality_status": "unavailable",
                "quality_reason": "ticker 网络请求失败",
                "decision_eligible": False,
                "stats_7d": stats_7d,
                "stats_7d_decision_eligible": False,
                "market_structure": market_structure,
            }
            fail_count += 1
            continue

        price = _extract_price(ticker) or 0.0
        change_value = _safe_float(ticker.get("percentage"), float("nan"))
        change_is_finite = math.isfinite(change_value)
        change_pct = change_value if change_is_finite else 0.0
        quote_at = _extract_source_timestamp_ms(ticker)
        quote_is_fresh, quote_freshness = _source_freshness(
            quote_at,
            ts,
            max_age_ms=_QUOTE_MAX_AGE_MS,
        )
        funding_is_fresh, funding_freshness = _source_freshness(
            funding_at,
            ts,
            max_age_ms=_FUNDING_MAX_AGE_MS,
        )

        # 资金费率 — 转为 % (ccxt 返回小数, 如 0.0001 = 0.01%)
        fr_pct = 0.0
        fr_str = "N/A"
        if funding_rate is not None:
            fr_pct = funding_rate * 100.0  # 小数 → %
            fr_str = f"{fr_pct:+.4f}%"
        elif asset_id in _SKIP_FUNDING:
            fr_str = "N/A (现货)"

        quality = data_quality.assess(
            source="OKX", kind="market", event_id=f"{asset_id}:{quote_at or 'missing-time'}",
            published_at=quote_at,
            payload={"price": price, "symbol": symbol, "source_ts_ms": quote_at},
        )
        quote_decision_eligible = bool(quality.decision_eligible and quote_is_fresh)
        market_structure = _analyze_structure_safe(
            structure_timeframes,
            as_of_ms=ts,
            source_decision_eligible=quote_decision_eligible,
        )

        asset_entry = {
            "symbol": symbol,
            **_okx_provenance(symbol),
            "price": round(price, 4),
            "price_str": _format_price(price, 2),
            "change_24h_pct": round(change_pct, 2),
            "change_24h_str": _format_pct(change_pct),
            "funding_rate_pct": round(fr_pct, 4),
            "funding_rate_str": fr_str,
            "funding_at": funding_at,
            "funding_freshness": funding_freshness,
            "funding_decision_eligible": bool(
                quality.decision_eligible and funding_rate is not None and funding_is_fresh
            ),
            "status": "ok",
            "quote_at": quote_at,
            "quote_freshness": quote_freshness,
            "change_24h_decision_eligible": bool(quote_decision_eligible and change_is_finite),
            "quality_status": quality.quality_status,
            "quality_reason": quality.reason if quality.decision_eligible and quote_is_fresh else (
                quality.reason if not quality.decision_eligible else quote_freshness
            ),
            "decision_eligible": quote_decision_eligible,
            "stats_7d": stats_7d,
            "stats_7d_decision_eligible": _structure_timeframe_decision_eligible(
                market_structure, ohlcv_tf, stats_7d
            ),
            "market_structure": market_structure,
        }

        if not quote_decision_eligible:
            asset_entry["status"] = "unavailable"
            asset_entry["status_note"] = (
                f"行情质量门禁未通过: "
                f"{quality.reason if not quality.decision_eligible else quote_freshness}"
            )

        assets[asset_id] = asset_entry
        if quote_decision_eligible:
            ok_count += 1
        else:
            fail_count += 1

    jin10_quote = await jin10_task if jin10_task is not None else None
    _apply_jin10_xau(assets, jin10_quote, as_of_ms=ts)

    # ── 整体状态 ──
    total = len(_CORE_SYMBOLS)
    ok_count = sum(
        item.get("status") == "ok" and item.get("decision_eligible") is True
        for item in assets.values()
    )
    if ok_count == total:
        overall_status = "ok"
    elif ok_count == 0:
        overall_status = "down"
    else:
        overall_status = "partial"

# ── 宏观指标 (OKX 行情适配器不提供 DXY/US10Y/VIX) ──
    macro: Dict[str, Any] = {
        "dxy":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供 DXY — 需接入权威宏观源"},
        "us10y": {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供美债收益率 — 需接入权威宏观源"},
        "oil":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 当前行情适配器未接入原油数据"},
        "vix":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供 VIX — 需接入 CBOE 等权威源"},
    }

    # ── 生成人类可读摘要 (直接注入 Prompt) ──
    summary = _build_summary(assets, macro, overall_status)

    elapsed = round(time.time() - t0, 3)
    print(f"  [SNAPSHOT] {overall_status.upper()} ({ok_count}/{total} OK) "
          f"in {elapsed}s — {summary.split(chr(10))[0]}")

    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": ts,
        "assets": assets,
        "macro": macro,
        "summary": summary,
        "status": overall_status,
    }


def get_snapshot_sync() -> Dict[str, Any]:
    """
    Synchronous wrapper — for debugging, REPL, or non-async scripts.

    usage:
        snap = get_snapshot_sync()
        print(snap["summary"])
    """
    if not HAS_CCXT:
        return _empty_snapshot("ccxt 未安装 (pip install ccxt)")

    ex = _get_exchange()
    t0 = time.time()
    ts = int(t0 * 1000)

    assets: Dict[str, Dict[str, Any]] = {}
    ok_count = 0
    fail_count = 0

    for symbol, asset_id in _CORE_SYMBOLS:
        ticker = _fetch_ticker_sync(symbol)
        structure_timeframes = _fetch_structure_timeframes_sync(symbol, ts)
        ohlcv_pair = _select_7d_candles(structure_timeframes)
        ohlcv, ohlcv_tf = ohlcv_pair if ohlcv_pair else (None, None)
        stats_7d = (
            _compute_7d_stats(ohlcv, ohlcv_tf)
            if ohlcv else _empty_7d_stats(ohlcv_tf)
        )

        funding_observation = None
        if asset_id not in _SKIP_FUNDING:
            funding_observation = _fetch_funding_rate_sync(symbol)
        funding_rate_value = (
            _safe_float(funding_observation.get("rate"), float("nan"))
            if isinstance(funding_observation, dict) else float("nan")
        )
        funding_rate = funding_rate_value if math.isfinite(funding_rate_value) else None
        funding_at = (
            _normalize_source_timestamp_ms(funding_observation.get("source_ts_ms"))
            if isinstance(funding_observation, dict) else None
        )

        if ticker is None:
            market_structure = _analyze_structure_safe(
                structure_timeframes,
                as_of_ms=ts,
                source_decision_eligible=False,
            )
            assets[asset_id] = {
                "symbol": symbol,
                **_okx_provenance(symbol),
                "price": 0.0, "price_str": "获取失败",
                "change_24h_pct": 0.0, "change_24h_str": "N/A",
                "funding_rate_pct": 0.0, "funding_rate_str": "N/A",
                "funding_at": funding_at,
                "funding_freshness": "ticker_unavailable", "funding_decision_eligible": False,
                "status": "unavailable", "status_note": "网络请求失败",
                "quote_at": None,
                "quote_freshness": "ticker_unavailable",
                "change_24h_decision_eligible": False,
                "quality_status": "unavailable",
                "quality_reason": "ticker 网络请求失败",
                "decision_eligible": False,
                "stats_7d": stats_7d,
                "stats_7d_decision_eligible": False,
                "market_structure": market_structure,
            }
            fail_count += 1
            continue

        price = _extract_price(ticker) or 0.0
        change_value = _safe_float(ticker.get("percentage"), float("nan"))
        change_is_finite = math.isfinite(change_value)
        change_pct = change_value if change_is_finite else 0.0
        quote_at = _extract_source_timestamp_ms(ticker)
        quote_is_fresh, quote_freshness = _source_freshness(
            quote_at,
            ts,
            max_age_ms=_QUOTE_MAX_AGE_MS,
        )
        funding_is_fresh, funding_freshness = _source_freshness(
            funding_at,
            ts,
            max_age_ms=_FUNDING_MAX_AGE_MS,
        )
        fr_pct = 0.0
        fr_str = "N/A"
        if funding_rate is not None:
            fr_pct = funding_rate * 100.0
            fr_str = f"{fr_pct:+.4f}%"
        elif asset_id in _SKIP_FUNDING:
            fr_str = "N/A (现货)"

        quality = data_quality.assess(
            source="OKX", kind="market", event_id=f"{asset_id}:{quote_at or 'missing-time'}",
            published_at=quote_at,
            payload={"price": price, "symbol": symbol, "source_ts_ms": quote_at},
        )
        quote_decision_eligible = bool(quality.decision_eligible and quote_is_fresh)
        market_structure = _analyze_structure_safe(
            structure_timeframes,
            as_of_ms=ts,
            source_decision_eligible=quote_decision_eligible,
        )

        asset_entry = {
            "symbol": symbol,
            **_okx_provenance(symbol),
            "price": round(price, 4),
            "price_str": _format_price(price, 2),
            "change_24h_pct": round(change_pct, 2),
            "change_24h_str": _format_pct(change_pct),
            "funding_rate_pct": round(fr_pct, 4),
            "funding_rate_str": fr_str,
            "funding_at": funding_at,
            "funding_freshness": funding_freshness,
            "funding_decision_eligible": bool(
                quality.decision_eligible and funding_rate is not None and funding_is_fresh
            ),
            "status": "ok",
            "quote_at": quote_at,
            "quote_freshness": quote_freshness,
            "change_24h_decision_eligible": bool(quote_decision_eligible and change_is_finite),
            "quality_status": quality.quality_status,
            "quality_reason": quality.reason if quality.decision_eligible and quote_is_fresh else (
                quality.reason if not quality.decision_eligible else quote_freshness
            ),
            "decision_eligible": quote_decision_eligible,
            "stats_7d": stats_7d,
            "stats_7d_decision_eligible": _structure_timeframe_decision_eligible(
                market_structure, ohlcv_tf, stats_7d
            ),
            "market_structure": market_structure,
        }
        if not quote_decision_eligible:
            asset_entry["status"] = "unavailable"
            asset_entry["status_note"] = (
                f"行情质量门禁未通过: "
                f"{quality.reason if not quality.decision_eligible else quote_freshness}"
            )
        assets[asset_id] = asset_entry
        if quote_decision_eligible:
            ok_count += 1
        else:
            fail_count += 1

    _apply_jin10_xau(
        assets,
        _fetch_jin10_xau_sync() if getattr(config, "JIN10_ENABLED", False) else None,
        as_of_ms=ts,
    )

    macro = {
        "dxy":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供 DXY"},
        "us10y": {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供美债收益率"},
        "oil":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 当前行情适配器未接入原油数据"},
        "vix":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable",
                   "note": "OKX 行情适配器不提供 VIX"},
    }

    total = len(_CORE_SYMBOLS)
    ok_count = sum(
        item.get("status") == "ok" and item.get("decision_eligible") is True
        for item in assets.values()
    )
    overall_status = "ok" if ok_count == total else ("down" if ok_count == 0 else "partial")
    summary = _build_summary(assets, macro, overall_status)
    elapsed = round(time.time() - t0, 3)
    print(f"  [SNAPSHOT] {overall_status.upper()} ({ok_count}/{total} OK) "
          f"in {elapsed}s")

    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": ts,
        "assets": assets,
        "macro": macro,
        "summary": summary,
        "status": overall_status,
    }


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────

def _fmt_ts() -> str:
    """格式化当前上海时间戳为可读字符串"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S CST")


def _latest_confirmed_structure_event(structure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the chronologically latest confirmed BOS/CHoCH event."""
    candidates = [
        item
        for item in (structure.get("latest_bos"), structure.get("latest_choch"))
        if isinstance(item, dict) and item.get("provisional") is not True
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: _safe_float(item.get("confirmed_at"), 0.0))


def _build_summary(assets: Dict[str, Dict], macro: Dict[str, Any], overall_status: str) -> str:
    """
    将快照数据组装为一段紧凑的文本摘要, 可直接嵌入 LLM system/user prompt。

    Returns compact multi-line string with price, 24h & 7d returns,
    trend/regime, ATR, and macro indicators.
    """
    lines = ["── 市场快照 ──"]
    if overall_status == "down":
        lines.append("⚠️ 市场数据不可用；仅保留可审计的降级状态，不得据此交易。")

    for asset_id in ["BTC", "XAU"]:
        a = assets.get(asset_id)
        if a is None:
            continue

        structure = a.get("market_structure") or _unavailable_market_structure("结构字段缺失")
        unavailable = a.get("status") == "unavailable" or a.get("decision_eligible") is not True
        if unavailable:
            lines.append(f"{asset_id}: 数据不可用")
        else:
            stats = a.get("stats_7d", {})
            stats_eligible = (
                a.get("stats_7d_decision_eligible") is True
                if "stats_7d_decision_eligible" in a
                else True
            )
            funding_eligible = (
                a.get("funding_decision_eligible") is True
                if "funding_decision_eligible" in a
                else True
            )
            line = (
                f"{asset_id} ${a['price']:,.2f} | "
                f"24h {a['change_24h_str']} | "
                f"7d {stats.get('return_7d_str', 'N/A') if stats_eligible else 'N/A'} | "
                f"资金费率 {a['funding_rate_str'] if funding_eligible else 'N/A'}"
            )
            lines.append(line)

            if stats_eligible:
                # 旧字段仍保留，但摘要明确它们是 K 线数而非连续天数。
                trend = stats.get("trend", "Unknown")
                atr = stats.get("atr_str", "N/A")
                strength = stats.get("trend_strength", "none")
                source_tf = stats.get("source_tf", "unknown")
                atr_period = stats.get("atr_period_bars", 0)
                lines.append(
                    f"  趋势: {trend} (强度={strength}, 周期={source_tf}, "
                    f"上涨K线{stats.get('up_bars', stats.get('n_up_days', 0))}/"
                    f"下跌K线{stats.get('down_bars', stats.get('n_down_days', 0))}) | "
                    f"ATR({atr_period}×{source_tf})={atr} | "
                    f"7d区间: {stats.get('lowest_7d', 0):,.2f}–{stats.get('highest_7d', 0):,.2f}"
                )
            else:
                lines.append("  旧7d趋势/ATR未通过字段级质量门，不参与判断。")

        lines.append(
            f"  盘面结构[{structure.get('source', 'unknown')}]: "
            f"status={structure.get('status', 'unavailable')} | "
            f"trend={structure.get('trend', 'unknown')} | "
            f"score={_safe_float(structure.get('trend_score')):+.3f} | "
            f"alignment={structure.get('alignment', 'unavailable')}"
            f"({_safe_float(structure.get('alignment_score')):.3f}) | "
            f"confidence={_safe_float(structure.get('confidence')):.3f} | "
            f"decision_eligible={str(structure.get('decision_eligible') is True).lower()}"
        )
        structure_timeframes = structure.get("timeframes")
        if isinstance(structure_timeframes, dict):
            for timeframe in _STRUCTURE_TIMEFRAMES:
                frame = structure_timeframes.get(timeframe)
                if not isinstance(frame, dict) or frame.get("decision_eligible") is not True:
                    continue
                indicators = frame.get("indicators") if isinstance(frame.get("indicators"), dict) else {}
                frame_structure = frame.get("structure") if isinstance(frame.get("structure"), dict) else {}
                support = frame_structure.get("support") if isinstance(frame_structure.get("support"), dict) else {}
                resistance = frame_structure.get("resistance") if isinstance(frame_structure.get("resistance"), dict) else {}
                event = _latest_confirmed_structure_event(frame_structure)
                event_text = "无已确认BOS/CHoCH"
                if isinstance(event, dict):
                    event_text = f"{event.get('kind', '结构')}:{event.get('direction', 'unknown')}"
                lines.append(
                    f"    {timeframe}: {frame.get('trend', 'unknown')} "
                    f"score={_safe_float(frame.get('trend_score')):+.3f} "
                    f"EMA200={_format_optional_price(indicators.get('ema200'))} "
                    f"RSI={_format_optional_number(indicators.get('rsi14'), 1)} "
                    f"ATR={_format_optional_number(indicators.get('atr_pct'), 2, '%')} "
                    f"S/R={_format_optional_price(support.get('price'))}/"
                    f"{_format_optional_price(resistance.get('price'))} "
                    f"{event_text}"
                )

    lines.append("盘面结构中的摆点/BOS/CHoCH仅作因果研究参考；交易闸门只读取通过质量门的综合结构分。")

    # ── 宏观指标 ──
    lines.append("── 宏观指标 ──")
    macro_line_parts = []
    for key, label in [("dxy", "DXY"), ("us10y", "US10Y"), ("oil", "Oil"), ("vix", "VIX")]:
        m = macro.get(key, {})
        if m.get("status") == "unavailable":
            macro_line_parts.append(f"{label}: N/A")
        else:
            macro_line_parts.append(f"{label}: ${m.get('value', 0):.2f} ({m.get('change_24h_str', 'N/A')})")
    lines.append(" | ".join(macro_line_parts))
    lines.append("(DXY/US10Y/Oil/VIX 需接入外部宏观数据源 — 当前不可用)")

    lines.append(f"数据状态: {overall_status}")
    return "\n".join(lines)


def _empty_snapshot(reason: str = "ccxt 未安装") -> Dict[str, Any]:
    """返回一个安全的空快照 — 用于所有降级场景"""
    assets = {}
    for symbol, asset_id in _CORE_SYMBOLS:
        assets[asset_id] = {
            "symbol": symbol,
            **_okx_provenance(symbol),
            "price": 0.0, "price_str": "N/A",
            "change_24h_pct": 0.0, "change_24h_str": "N/A",
            "funding_rate_pct": 0.0, "funding_rate_str": "N/A",
            "funding_at": None,
            "funding_freshness": "source_timestamp_missing",
            "funding_decision_eligible": False,
            "status": "unavailable", "status_note": reason,
            "quote_at": None,
            "quote_freshness": "source_timestamp_missing",
            "change_24h_decision_eligible": False,
            "quality_status": "unavailable",
            "quality_reason": reason,
            "decision_eligible": False,
            "stats_7d": _empty_7d_stats(),
            "stats_7d_decision_eligible": False,
            "market_structure": _unavailable_market_structure(reason),
        }
    macro = {
        "dxy":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable", "note": reason},
        "us10y": {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable", "note": reason},
        "oil":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable", "note": reason},
        "vix":   {"value": 0.0, "change_24h_str": "N/A", "status": "unavailable", "note": reason},
    }
    return {
        "timestamp": _fmt_ts(),
        "epoch_ms": int(time.time() * 1000),
        "assets": assets,
        "macro": macro,
        "summary": _build_summary(assets, macro, "down"),
        "status": "down",
    }


async def close() -> None:
    """释放 ccxt async session (engine shutdown 时调用)"""
    global _EXCHANGE_ASYNC
    if _EXCHANGE_ASYNC is not None:
        try:
            await _EXCHANGE_ASYNC.close()
        except Exception:
            pass
        _EXCHANGE_ASYNC = None


# ──────────────────────────────────────────────────────────────────
# 独立运行：python3 market_snapshot.py
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═" * 60)
    print("[SNAPSHOT] Trident Market Snapshot — 独立测试")
    print("═" * 60)

    if not HAS_CCXT:
        print("❌ ccxt 未安装. 请运行: pip install ccxt --break-system-packages")
        sys.exit(1)

    # Try async first; fall back to sync if no event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside a running loop (e.g. Jupyter) — use sync
            snap = get_snapshot_sync()
        else:
            snap = asyncio.run(get_snapshot())
    except RuntimeError:
        snap = get_snapshot_sync()

    print()
    print("── 市场摘要 (可直接注入 LLM Prompt) ──")
    print(snap["summary"])
    print()
    print("── 完整 JSON ──")
    import json
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print()
    print(f"整体状态: {snap['status']}")
