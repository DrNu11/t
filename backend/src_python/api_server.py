#!/usr/bin/env python3
"""
Trident Agent MVP — FastAPI Backend Server (SSE Edition)
=========================================================

Usage:
  cd backend/src_python
  python api_server.py
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import os
import sqlite3
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Literal, Optional

import aiosqlite
import requests
from pydantic import BaseModel, Field
from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

import config
import db
import evidence
import macro_context
import data_quality
import decision_guard
import market_feeds
import market_snapshot
import timeseries
import strategy_store
import paper_trading
import news_sources
import quick_sim
import backtest
from providers.jin10 import get_jin10_provider
from providers.binance_paxg import get_binance_paxg_provider
from providers.coingecko_paxg import get_coingecko_paxg_provider
from providers.oanda import get_oanda_provider
from providers.official_macro import get_official_macro_calendar_provider
from providers.trading_economics import get_trading_economics_provider
from realtime_filter import evaluate_news
from engine.forward import classify_directional_outcome
from engine.prices import _fetch_eastmoney_xau, _fetch_sina_xau, _get_current_price

BASE_DIR = config.BASE_DIR
DB_PATH = config.DB_PATH

TZ_SHANGHAI = config.TZ_SHANGHAI


class TradingProcessManager:
    def __init__(self, script_path: str, cwd: str, log_path: str, popen_factory=subprocess.Popen):
        self.script_path = script_path
        self.cwd = cwd
        self.log_path = log_path
        self.popen_factory = popen_factory
        self._process = None
        self._log_stream = None
        self._lock = threading.Lock()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {
                "running": running,
                "pid": self._process.pid if running else None,
                "mode": "testnet" if config.BINANCE_USE_TESTNET else "live",
            }

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"started": False, "reason": "already_running", "pid": self._process.pid}
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            self._process = self.popen_factory(
                [sys.executable, self.script_path], cwd=self.cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return {"started": True, "pid": self._process.pid}

    def stop(self, timeout: float = 8.0) -> Dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._close_log()
                return {"stopped": False, "reason": "not_running"}
            process.terminate()
        killed = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            killed = True
        with self._lock:
            self._process = None
            self._close_log()
        return {"stopped": True, "killed": killed}

    def logs(self, tail: int = 200) -> list[str]:
        count = min(max(int(tail), 1), 2000)
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as stream:
                return stream.readlines()[-count:]
        except FileNotFoundError:
            return []

    def _close_log(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None


_TRADING_MANAGER = TradingProcessManager(
    os.path.join(config.PROJECT_DIR, "NewsTrading.py"),
    config.PROJECT_DIR,
    config.BINANCE_LOG_PATH,
)

_SSE_QUEUES: List[asyncio.Queue] = []

def _now() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")


def _external_timestamp(value: Any) -> tuple[str, int] | None:
    """Normalize provider timestamps while preserving Beijing-time feeds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        dt = datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(TZ_SHANGHAI)
        return dt.isoformat(timespec="seconds"), int(raw)
    text = str(value).strip()
    if text.isdigit() and len(text) in (10, 13):
        return _external_timestamp(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    dt = dt.astimezone(TZ_SHANGHAI)
    return dt.isoformat(timespec="seconds"), int(dt.timestamp())

def _format_time(iso_ts):
    if not iso_ts:
        return "——"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return (iso_ts or "")[:19] or "——"

def _safe(row, key, default=None):
    """sqlite3.Row safe access — no .get() method."""
    return row[key] if key in row.keys() else default

def _normalize_analysis_status(status: Any, has_decision: bool) -> str:
    if has_decision:
        return "DONE"
    normalized = str(status or "PENDING").upper()
    if normalized == "DONE":
        return "PENDING"
    return normalized if normalized in ("PENDING", "PROCESSING", "FAILED") else "PENDING"


def _row_to_event(row) -> Dict[str, Any]:
    """Map a raw-news row with an optional decision to the frontend ApiEvent shape."""
    decision_id = _safe(row, "decision_id")
    analysis_status = _normalize_analysis_status(_safe(row, "analysis_status"), decision_id is not None)
    reason = _safe(row, "reason") or ""
    reason = re.sub(r"^\[.*?\]\s*", "", reason)
    quality_status = str(_safe(row, "quality_status") or "unverified").strip().lower()
    if decision_id is None:
        if analysis_status == "FAILED":
            reason = "AI 分析失败"
        elif quality_status == "candidate":
            reason = "待 AI 观察分析（来源未验证）"
        else:
            reason = "待 AI 分析"
    market = (_safe(row, "market_category") or "OTHER").upper()
    if market not in ("CRYPTO", "GOLD", "OIL", "MACRO", "OTHER"):
        market = "OTHER"
    # ── Parse extra_models_consensus JSON → individual model fields ──
    raw_consensus = _safe(row, "extra_models_consensus") or ""
    if isinstance(raw_consensus, str):
        raw_consensus = raw_consensus.strip()
    try:
        consensus = json.loads(raw_consensus) if raw_consensus else {}
    except (json.JSONDecodeError, TypeError):
        consensus = {}
    if not isinstance(consensus, dict):
        consensus = {}

    news_id = _safe(row, "news_id", _safe(row, "id"))
    return {
        "id": news_id,
        "news_id": news_id,
        "decision_id": decision_id,
        "analysis_status": analysis_status,
        "paper_trading_run_id": _safe(row, "paper_trading_run_id"),
        "strategy_id": _safe(row, "strategy_id"),
        "strategy_version_id": _safe(row, "strategy_version_id"),
        "agent_model_id": _safe(row, "agent_model_id"),
        "evidence_confidence": _safe(row, "evidence_confidence"),
        "evidence_action": (_safe(row, "evidence_action") or "HOLD").upper(),
        "trade_gate_reason": _safe(row, "trade_gate_reason") or "",
        "timestamp": _format_time(_safe(row, "timestamp")),
        "ai_time": _format_time(_safe(row, "created_at")) if decision_id is not None else "——",
        "source": _safe(row, "source") or "FinancialJuice",
        "quality_status": quality_status,
        "quality_reason": _safe(row, "quality_reason") or "",
        "news_text": re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', (_safe(row, "news_text") or "")[:200]),
        "action": (_safe(row, "action") or "HOLD").upper(),
        "score": round(_safe(row, "score"), 2) if _safe(row, "score") is not None else 0.0,
        "reason": reason[:80],
        "market_category": market,
        "target_asset": (_safe(row, "target_asset") or "NONE").upper(),
        "parent_id": _safe(row, "parent_id"),
        "child_count": _safe(row, "child_count") or 0,
        "reasoning_path": (_safe(row, "reasoning_path") or "")[:200],
        "vip_tag": _safe(row, "vip_tag") or "",
        "entry_price": _safe(row, "entry_price"),
        "exit_price": _safe(row, "exit_price"),
        "max_price": _safe(row, "max_price"),
        "min_price": _safe(row, "min_price"),
        "max_price_time": _safe(row, "max_price_time") or 0,
        "min_price_time": _safe(row, "min_price_time") or 0,
        "entry_time": _safe(row, "entry_time") or "",
        "exit_time": _safe(row, "exit_time") or "",
        "exit_reason": _safe(row, "exit_reason") or "",
        "is_correct": _safe(row, "is_correct") or "",
        "settled": _safe(row, "settled") or 0,
        "doubao_action": _safe(row, "doubao_action") or "HOLD",
        "doubao_reasoning": _safe(row, "doubao_reasoning") or "",
        "deepseek_action": (consensus.get("DeepSeek", {}) if isinstance(consensus.get("DeepSeek"), dict) else {}).get("action") or _safe(row, "deepseek_action") or "HOLD",
        "deepseek_reasoning": (consensus.get("DeepSeek", {}) if isinstance(consensus.get("DeepSeek"), dict) else {}).get("reasoning") or _safe(row, "deepseek_reasoning") or "",
        "gemini_action": (consensus.get("Gemini", {}) if isinstance(consensus.get("Gemini"), dict) else {}).get("action") or _safe(row, "gemini_action") or "HOLD",
        "gemini_reasoning": (consensus.get("Gemini", {}) if isinstance(consensus.get("Gemini"), dict) else {}).get("reasoning") or _safe(row, "gemini_reasoning") or "",
        "grok_action": (consensus.get("Grok", {}) if isinstance(consensus.get("Grok"), dict) else {}).get("action") or _safe(row, "grok_action") or "HOLD",
        "grok_reasoning": (consensus.get("Grok", {}) if isinstance(consensus.get("Grok"), dict) else {}).get("reasoning") or _safe(row, "grok_reasoning") or "",
        # ── Phase 0: 元数据字段 ──
        "prediction_type": _safe(row, "prediction_type") or "continuation",
        "event_phase": _safe(row, "event_phase") or "mid",
        "market_confirmation": _safe(row, "market_confirmation") or "unknown",
        "expected_horizon": _safe(row, "expected_horizon") or "1-3d",
        "invalidation_condition": _safe(row, "invalidation_condition") or "",
        "decision_context": _safe(row, "decision_context") or "{}",
        "analysis_type": _safe(row, "analysis_type") or "trend",
        "bullish_probability": _safe(row, "bullish_probability"),
        "bearish_probability": _safe(row, "bearish_probability"),
        "uncertainty": _safe(row, "uncertainty"),
        "bullish_force": _safe(row, "bullish_force"),
        "bearish_force": _safe(row, "bearish_force"),
        "impact_horizon": _safe(row, "impact_horizon") or "medium",
        "impact_window": _safe(row, "impact_window") or "{}",
        "entry_zone": _safe(row, "entry_zone") or "",
        "take_profit_pct": _safe(row, "take_profit_pct"),
        "stop_loss_pct": _safe(row, "stop_loss_pct"),
        "exit_policy": _safe(row, "exit_policy") or "horizon_or_signal_flip",
        "dual_side_candidate": _safe(row, "dual_side_candidate") or 0,
        # ── Phase 0.5: 结果追踪指标 ──
        "mfe_pct": _safe(row, "mfe_pct"),
        "mae_pct": _safe(row, "mae_pct"),
        "forward_pnl": _safe(row, "forward_pnl"),
        "mfe_time_mins": _safe(row, "mfe_time_mins"),
        "chatgpt_action": (consensus.get("ChatGPT", {}) if isinstance(consensus.get("ChatGPT"), dict) else {}).get("action") or _safe(row, "chatgpt_action") or "HOLD",
        "chatgpt_reasoning": (consensus.get("ChatGPT", {}) if isinstance(consensus.get("ChatGPT"), dict) else {}).get("reasoning") or _safe(row, "chatgpt_reasoning") or "",
        "cluster_size": _safe(row, "cluster_size") or 1,
    }

# -- SSE helpers -----------------------------------------------------------

async def _broadcast_sse(data: Dict[str, Any]) -> None:
    payload = {**data, "server_emitted_at_ms": int(time.time() * 1000)}
    text = json.dumps(payload, ensure_ascii=False)
    dead = []
    for q in _SSE_QUEUES:
        try:
            q.put_nowait(text)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _SSE_QUEUES.remove(q)
        except ValueError:
            pass

_EVENT_SELECT = """
    SELECT
        rn.id AS news_id,
        rn.timestamp,
        rn.source,
        rn.quality_status,
        rn.quality_reason,
        rn.content AS news_text,
        rn.status AS analysis_status,
        ad.id AS decision_id,
        ad.paper_trading_run_id,
        ad.strategy_id,
        ad.strategy_version_id,
        ad.agent_model_id,
        ad.evidence_confidence,
        ad.evidence_action,
        ad.trade_gate_reason,
        UPPER(ad.suggested_action) AS action,
        ad.sentiment_score AS score,
        ad.reasoning AS reason,
        ad.market_category,
        ad.target_asset,
        ad.created_at,
        ad.parent_id,
        ad.child_count,
        ad.reasoning_path,
        ad.vip_tag,
        ad.entry_price,
        ad.exit_price,
        ad.max_price,
        ad.min_price,
        ad.max_price_time,
        ad.min_price_time,
        ad.is_correct,
        ad.settled,
        ad.doubao_action,
        ad.doubao_reasoning,
        ad.extra_models_consensus,
        ad.entry_time,
        ad.cluster_size,
        ad.prediction_type,
        ad.event_phase,
        ad.market_confirmation,
        ad.expected_horizon,
        ad.invalidation_condition,
        ad.decision_context,
        ad.mfe_pct,
        ad.mae_pct,
        ad.forward_pnl,
        ad.mfe_time_mins,
        ad.analysis_type,
        ad.bullish_probability,
        ad.bearish_probability,
        ad.uncertainty,
        ad.bullish_force,
        ad.bearish_force,
        ad.impact_horizon,
        ad.impact_window,
        ad.entry_zone,
        ad.take_profit_pct,
        ad.stop_loss_pct,
        ad.exit_policy,
        ad.dual_side_candidate
    FROM raw_news rn
    LEFT JOIN ai_decisions ad ON ad.id = (
        SELECT MAX(latest.id) FROM ai_decisions latest WHERE latest.news_id = rn.id
    )
"""

_EVENT_SELECT_COMPACT = _EVENT_SELECT.replace(
    "ad.decision_context,",
    "NULL AS decision_context,",
)


async def _fetch_event_rows(
    news_ids: List[int] | None = None,
    limit: int | None = None,
    *,
    compact: bool = False,
) -> List[Dict[str, Any]]:
    params: List[Any] = []
    where = "WHERE rn.is_noise = 0"
    if news_ids is not None:
        if not news_ids:
            return []
        where += f" AND rn.id IN ({','.join('?' for _ in news_ids)})"
        params.extend(news_ids)
    # The ingest watcher may backfill a provider's older pages after the
    # newest item.  Sort by the provider timestamp first so replay/backfill
    # order cannot make stale headlines appear as the live feed.
    select_clause = _EVENT_SELECT_COMPACT if compact else _EVENT_SELECT
    sql = f"{select_clause} {where} ORDER BY datetime(rn.timestamp) DESC, rn.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    async with aiosqlite.connect(DB_PATH) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
    events = [_row_to_event(row) for row in rows]
    if compact:
        # decision_context may contain a full market snapshot and can be tens
        # of kilobytes per row.  The live ledger does not render it; replay
        # endpoints remain the authoritative source for the complete context.
        for event in events:
            event["decision_context"] = "{}"
    return events


async def _fetch_change_cursors() -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as connection:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM raw_news"
        )
        raw_id = int((await cursor.fetchone())[0])
        await cursor.close()
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM ai_decisions"
        )
        decision_id = int((await cursor.fetchone())[0])
        await cursor.close()
    return raw_id, decision_id


async def _fetch_incremental_news_ids(raw_after: int, decision_after: int) -> List[int]:
    async with aiosqlite.connect(DB_PATH) as connection:
        cursor = await connection.execute(
            """
            SELECT id AS news_id FROM raw_news WHERE id > ? AND is_noise = 0
            UNION
            SELECT news_id FROM ai_decisions WHERE id > ?
            """,
            (raw_after, decision_after),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [int(row[0]) for row in rows]


async def _fetch_event_snapshot() -> Dict[int, tuple[str, int | None]]:
    async with aiosqlite.connect(DB_PATH) as connection:
        cursor = await connection.execute(
            """
            SELECT rn.id, rn.status, MAX(ad.id)
            FROM raw_news rn
            LEFT JOIN ai_decisions ad ON ad.news_id = rn.id
            WHERE rn.is_noise = 0
            GROUP BY rn.id, rn.status
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return {row[0]: (str(row[1] or "PENDING").upper(), row[2]) for row in rows}


# -- Gold price helpers ----------------------------------------------------

_last_gold_price: float | None = None

class _MT5State:
    def __init__(self):
        self.ok = False
        self.init_done = False

_mt5 = _MT5State()

def _mt5_init() -> None:
    if _mt5.init_done:
        return
    _mt5.init_done = True
    try:
        import MetaTrader5 as mt5_mod
        if not mt5_mod.initialize():
            print("[MT5] initialize() returned False")
            return
        _mt5.ok = True
        print("[MT5] connected — XAUUSD + WTIUSD — tick streaming active")
    except ImportError:
        print("[MT5] MetaTrader5 package not installed")
    except Exception as e:
        print(f"[MT5] init error: {type(e).__name__}: {e}")

def _mt5_read_tick() -> float | None:
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("XAUUSD")
        if tick and tick.bid and 500 < tick.bid < 10000:
            return tick.bid
        return None
    except Exception:
        return None

def _mt5_recheck() -> None:
    _mt5.ok = False
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("XAUUSD")
        if tick and tick.bid:
            _mt5.ok = True
    except Exception:
        pass

# _fetch_sina_xau / _fetch_eastmoney_xau 统一由 engine.prices 提供（见文件头 import）。
# 注意：engine.prices 版本超时 5s 且内部吞异常返回 None，与本模块 _http_fetch_gold 的
# try/except + 500<p<10000 过滤组合后行为等价。
def _http_fetch_gold() -> tuple[float | None, str]:
    for name, fn in [("Sina", _fetch_sina_xau), ("EastMoney", _fetch_eastmoney_xau)]:
        try:
            p = fn()
            if p is not None and 500 < p < 10000:
                return p, name
        except Exception:
            pass
    return None, ""

async def _broadcast_gold(price: float, src: str):
    global _last_gold_price
    if _last_gold_price is None or abs(price - _last_gold_price) >= 0.01:
        _last_gold_price = price
        await _broadcast_sse({
            "type": "price_update",
            "asset": "XAU",
            "price": round(price, 2),
            "ts": time.time(),
        })

async def gold_http_watcher():
    loop = asyncio.get_running_loop()
    while True:
        try:
            price, src = await loop.run_in_executor(None, _http_fetch_gold)
            if price is not None:
                await _broadcast_gold(price, src)
        except Exception:
            pass
        await asyncio.sleep(1.0)

async def gold_mt5_watcher():
    print("[GOLD] MT5 watcher started")
    while True:
        try:
            if _mt5.ok:
                price = _mt5_read_tick()
                if price is not None and 500 < price < 10000:
                    await _broadcast_gold(price, "MT5")
                    await asyncio.sleep(0.1)
                    continue
                _mt5_recheck()
                if not _mt5.ok:
                    print("[GOLD] MT5 terminal unreachable — HTTP only")
                await asyncio.sleep(1.0)
                continue
            if not _mt5.init_done:
                _mt5_init()
            if not _mt5.ok:
                _mt5.init_done = False
                await asyncio.sleep(30)
                continue
        except Exception as e:
            print(f"[GOLD] MT5 error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


# -- WTI price helpers ----------------------------------------------------

_last_wti_price: float | None = None

def _mt5_read_wti_tick() -> float | None:
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("WTIUSD")
        if tick and tick.bid and 30 < tick.bid < 200:
            return tick.bid
        return None
    except Exception:
        return None

# 注意：WTI 抓取与 engine.prices._fetch_wti_price 不同源（EastMoney secid=113.USDWTI、
# 有效区间 30–200、超时 8s、异常向上抛由 _http_fetch_wti 捕获）——为保持行为不变，
# 这两个函数保留在本模块，不做去重。
def _fetch_sina_wti() -> float | None:
    req = urllib.request.Request(
        "https://hq.sinajs.cn/list=hf_CL",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
    )
    resp = urllib.request.urlopen(req, timeout=8)
    text = resp.read().decode("gbk", errors="replace")
    if '="' in text:
        return float(text.split('="')[1].split(",")[0])
    return None

def _fetch_eastmoney_wti() -> float | None:
    req = urllib.request.Request(
        "https://push2.eastmoney.com/api/qt/stock/get?secid=113.USDWTI&fields=f43",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    resp = urllib.request.urlopen(req, timeout=8)
    body = json.loads(resp.read().decode("utf-8"))
    return float(body["data"]["f43"]) / 100.0

def _http_fetch_wti() -> tuple[float | None, str]:
    for name, fn in [("Sina", _fetch_sina_wti), ("EastMoney", _fetch_eastmoney_wti)]:
        try:
            p = fn()
            if p is not None and 30 < p < 200:
                return p, name
        except Exception:
            pass
    return None, ""

async def _broadcast_wti(price: float, src: str):
    global _last_wti_price
    if _last_wti_price is None or abs(price - _last_wti_price) >= 0.01:
        _last_wti_price = price
        await _broadcast_sse({
            "type": "price_update",
            "asset": "WTI",
            "price": round(price, 2),
            "ts": time.time(),
        })

async def wti_http_watcher():
    print("[WTI] HTTP watcher started (Sina -> EastMoney)")
    loop = asyncio.get_running_loop()
    while True:
        try:
            price, src = await loop.run_in_executor(None, _http_fetch_wti)
            if price is not None:
                await _broadcast_wti(price, src)
        except Exception:
            pass
        await asyncio.sleep(1.0)

async def wti_mt5_watcher():
    print("[WTI] MT5 watcher started")
    while True:
        try:
            if _mt5.ok:
                price = _mt5_read_wti_tick()
                if price is not None and 30 < price < 200:
                    await _broadcast_wti(price, "MT5")
                    await asyncio.sleep(0.1)
                    continue
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[WTI] MT5 error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


# -- DB watcher ------------------------------------------------------------

async def db_watcher() -> None:
    snapshot = await _fetch_event_snapshot()
    raw_cursor, decision_cursor = await _fetch_change_cursors()
    interval = config.NEWS_WATCH_INTERVAL_MS / 1000.0
    next_reconcile = time.monotonic() + 5.0
    while True:
        try:
            current_raw, current_decision = await _fetch_change_cursors()
            changed_ids = await _fetch_incremental_news_ids(raw_cursor, decision_cursor)
            if changed_ids:
                events = await _fetch_event_rows(changed_ids, compact=True)
                for event in reversed(events):
                    await _broadcast_sse(event)
                    snapshot[event["news_id"]] = (
                        str(event["analysis_status"]).upper(),
                        event["decision_id"],
                    )
            raw_cursor = max(raw_cursor, current_raw)
            decision_cursor = max(decision_cursor, current_decision)

            # 状态更新没有独立递增 ID；低频全量对账用于恢复丢失通知并保持最终一致。
            if time.monotonic() >= next_reconcile:
                current = await _fetch_event_snapshot()
                reconciled_ids = [
                    news_id for news_id, state in current.items()
                    if snapshot.get(news_id) != state
                ]
                if reconciled_ids:
                    events = await _fetch_event_rows(reconciled_ids, compact=True)
                    for event in reversed(events):
                        await _broadcast_sse(event)
                snapshot = current
                next_reconcile = time.monotonic() + 5.0
        except Exception:
            pass
        await asyncio.sleep(interval)


# -- Kline helpers ---------------------------------------------------------

def _mock_klines(limit: int, *, base: float, seed: int):
    import random
    result = []
    now = datetime.now(TZ_SHANGHAI)
    rng = random.Random(seed)
    jitter = base * 0.015
    for i in range(limit):
        t = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=limit - i)
        ts = int(t.timestamp())
        o = base + rng.uniform(-jitter, jitter)
        h = o + rng.uniform(0, jitter * 0.5)
        l = o - rng.uniform(0, jitter * 0.5)
        c = l + rng.uniform(0, h - l)
        result.append({
            "time": ts,
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": int(rng.uniform(5000, 30000)),
        })
    return result


# -- Schema migration ------------------------------------------------------

def _migrate_schema() -> None:
    """Add any missing columns to ai_decisions. Safe to call repeatedly."""
    conn = db.get_connection()
    try:
        db.migrate(conn)
    finally:
        conn.close()


# -- Lifespan --------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _migrate_schema()
    tasks = [
        asyncio.create_task(db_watcher(), name="db_watcher"),
        asyncio.create_task(gold_http_watcher(), name="gold_http"),
        asyncio.create_task(gold_mt5_watcher(), name="gold_mt5"),
        asyncio.create_task(wti_http_watcher(), name="wti_http"),
        asyncio.create_task(wti_mt5_watcher(), name="wti_mt5"),
        asyncio.create_task(news_source_watcher(), name="news_sources"),
        asyncio.create_task(macro_calendar_watcher(), name="macro_calendar"),
        asyncio.create_task(quick_sim_watcher(), name="quick_sim"),
    ]
    yield
    await asyncio.to_thread(_TRADING_MANAGER.stop)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# -- FastAPI app definition ------------------------------------------------

app = FastAPI(title="Trident Agent API", version="4.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- SSE stream route ------------------------------------------------------

@app.get("/api/events/stream")
async def sse_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _SSE_QUEUES.append(q)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _SSE_QUEUES.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- REST routes -----------------------------------------------------------

class AIModelSelection(BaseModel):
    model_id: str


def _ai_models_response() -> Dict[str, Any]:
    return {
        "models": [dict(model) for model in config.AI_MODEL_ROSTER],
        "selected": config.get_selected_ai_model_id(),
    }


@app.get("/api/ai/models")
async def get_ai_models():
    return _ai_models_response()


@app.put("/api/ai/models")
async def select_ai_model(selection: AIModelSelection):
    try:
        await asyncio.to_thread(config.write_selected_ai_model_id, selection.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported AI model") from exc
    return _ai_models_response()


@app.get("/api/events")
async def get_events(limit: int = 2000, compact: bool = False) -> List[Dict[str, Any]]:
    """Return the latest non-noise news rows with their latest decision."""
    try:
        return await _fetch_event_rows(
            limit=max(1, min(int(limit), config.EVENTS_LIST_MAX)),
            compact=compact,
        )
    except aiosqlite.OperationalError:
        return []


@app.get("/api/events/today")
async def get_today_events() -> Dict[str, Any]:
    """上海日真实计数，避免前端用列表截断条数当「今日信号」。"""
    today = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(DB_PATH) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    COUNT(*) AS news_count,
                    COALESCE(SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END), 0) AS analyzed_count
                FROM raw_news
                WHERE is_noise = 0 AND substr(timestamp, 1, 10) = ?
                """,
                (today,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM raw_news WHERE is_noise = 0"
            )
            total_row = await cursor.fetchone()
            await cursor.close()
    except aiosqlite.OperationalError:
        return {"today": today, "news_count": 0, "analyzed_count": 0, "total_count": 0}
    return {
        "today": today,
        "news_count": int(row[0] or 0),
        "analyzed_count": int(row[1] or 0),
        "total_count": int(total_row[0] or 0),
    }


_MARKET_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_MARKET_STRUCTURE_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_MARKET_STRUCTURE_INFLIGHT: Optional[asyncio.Task] = None
_MARKET_STRUCTURE_CACHE_TTL = 5.0
_MARKET_STRUCTURE_ASSETS = frozenset({"BTC", "XAU"})
_TECHFLOW_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_EASTMONEY_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_BLOCKBEATS_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_JIN10_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_NEWS_CACHE_TTL = 20.0


def _cached_market_prices() -> Dict[str, Any]:
    now = time.monotonic()
    if _MARKET_CACHE["value"] is None or now >= _MARKET_CACHE["expires"]:
        payload = market_feeds.fetch_market_prices()
        _MARKET_CACHE["value"] = payload
        _MARKET_CACHE["expires"] = now + 2.0
        try:
            timeseries.record_market_snapshot(payload)
        except sqlite3.OperationalError as exc:
            # Snapshot persistence is audit telemetry.  A temporary SQLite
            # writer lock must not turn a valid live quote into an HTTP 500.
            if "locked" not in str(exc).lower():
                raise
    return _MARKET_CACHE["value"]


@app.get("/api/market/prices")
async def get_market_prices():
    return await asyncio.to_thread(_cached_market_prices)


async def _produce_market_structure_snapshot() -> Dict[str, Any]:
    """Refresh the structure cache independently of any HTTP waiter.

    Request handlers await this producer through ``asyncio.shield``.  Cache
    publication and in-flight cleanup deliberately live here so cancellation
    of the last waiting client cannot discard a successful upstream result.
    """
    global _MARKET_STRUCTURE_INFLIGHT
    producer = asyncio.current_task()
    try:
        snapshot = await market_snapshot.get_snapshot()
        if not isinstance(snapshot, dict):
            raise TypeError("market snapshot must be an object")
        _MARKET_STRUCTURE_CACHE["value"] = snapshot
        _MARKET_STRUCTURE_CACHE["expires"] = time.monotonic() + _MARKET_STRUCTURE_CACHE_TTL
        return snapshot
    finally:
        if _MARKET_STRUCTURE_INFLIGHT is producer:
            _MARKET_STRUCTURE_INFLIGHT = None


async def _cached_market_structure_snapshot() -> Dict[str, Any]:
    """Share one short-lived snapshot across structure API callers.

    A snapshot fans out to several upstream OKX endpoints.  Keeping a tiny
    cache and a single producer prevents concurrent UI polls from multiplying
    those requests while preserving near-real-time behaviour.
    """
    global _MARKET_STRUCTURE_INFLIGHT
    now = time.monotonic()
    cached = _MARKET_STRUCTURE_CACHE.get("value")
    if isinstance(cached, dict) and now < float(_MARKET_STRUCTURE_CACHE.get("expires") or 0.0):
        return cached

    task = _MARKET_STRUCTURE_INFLIGHT
    if task is None or task.done():
        task = asyncio.create_task(_produce_market_structure_snapshot())
        _MARKET_STRUCTURE_INFLIGHT = task
    return await asyncio.shield(task)


@app.get("/api/market/structure/{asset}")
async def get_market_structure(asset: str):
    """Return the latest verified, closed-candle structure for one asset.

    Structure analysis is deliberately fail-closed: an unsupported asset or
    unavailable upstream returns an observable ``unavailable`` payload rather
    than borrowing another asset's data or fabricating indicator values.
    """
    asset_key = str(asset or "").strip().upper()
    if not asset_key or not re.fullmatch(r"[A-Z0-9._-]{2,16}", asset_key):
        raise HTTPException(status_code=400, detail="invalid asset")

    # Reject unsupported instruments before any network call.  In particular,
    # WTI is displayed as unavailable until a verified oil provider is wired;
    # it must never trigger a BTC/XAU snapshot or borrow another asset.
    if asset_key not in _MARKET_STRUCTURE_ASSETS:
        return {
            "asset": asset_key,
            "status": "unavailable",
            "decision_eligible": False,
            "source": "",
            "structure_source": "",
            "structure_venue": "",
            "structure_instrument_id": "",
            "structure_instrument_type": "",
            "quote_source": "",
            "quote_venue": "",
            "quote_instrument_id": "",
            "quote_instrument_type": "",
            "venue": "",
            "instrument_id": "",
            "instrument_type": "",
            "updated_at": None,
            "reason": "unsupported_asset",
            "supported_assets": sorted(_MARKET_STRUCTURE_ASSETS),
            "structure": None,
        }

    try:
        snapshot = await _cached_market_structure_snapshot()
    except Exception as exc:
        return {
            "asset": asset_key,
            "status": "unavailable",
            "decision_eligible": False,
            "source": "",
            "structure_source": "",
            "structure_venue": "",
            "structure_instrument_id": "",
            "structure_instrument_type": "",
            "quote_source": "",
            "quote_venue": "",
            "quote_instrument_id": "",
            "quote_instrument_type": "",
            "venue": "",
            "instrument_id": "",
            "instrument_type": "",
            "updated_at": None,
            "reason": f"snapshot_unavailable:{type(exc).__name__}",
            "structure": None,
        }

    assets = snapshot.get("assets") if isinstance(snapshot, dict) else None
    asset_row = assets.get(asset_key) if isinstance(assets, dict) else None
    if not isinstance(asset_row, dict):
        asset_row = {}
    structure = asset_row.get("market_structure")
    if not isinstance(structure, dict):
        structure = None
    structure_status = str((structure or {}).get("status") or "unavailable")
    structure_quality = (
        structure.get("quality")
        if isinstance(structure, dict) and isinstance(structure.get("quality"), dict)
        else {}
    )
    structure_warnings = (
        structure.get("warnings")
        if isinstance(structure, dict) and isinstance(structure.get("warnings"), list)
        else []
    )
    eligible = bool(
        asset_row.get("decision_eligible") is True
        and structure is not None
        and structure.get("decision_eligible") is True
        and structure_status in {"ok", "partial"}
    )
    structure_source = str(
        (structure or {}).get("source")
        or asset_row.get("structure_source")
        or asset_row.get("source")
        or ""
    )
    structure_venue = str(
        (structure or {}).get("venue")
        or asset_row.get("structure_venue")
        or (structure or {}).get("source")
        or ""
    )
    structure_instrument_id = str(
        (structure or {}).get("instrument_id")
        or asset_row.get("structure_instrument_id")
        or ""
    )
    structure_instrument_type = str(
        (structure or {}).get("instrument_type")
        or asset_row.get("structure_instrument_type")
        or ""
    )
    return {
        "asset": asset_key,
        "status": structure_status,
        "decision_eligible": eligible,
        # Top-level source/venue/instrument remain backward-compatible aliases
        # for structure identity. Explicit structure_* and quote_* keys remove
        # any ambiguity for consumers comparing Jin10 spot with OKX swaps.
        "source": structure_source,
        "structure_source": structure_source,
        "structure_venue": structure_venue,
        "structure_instrument_id": structure_instrument_id,
        "structure_instrument_type": structure_instrument_type,
        "quote_source": str(asset_row.get("quote_source") or asset_row.get("source") or ""),
        "quote_venue": str(asset_row.get("quote_venue") or asset_row.get("venue") or ""),
        "quote_instrument_id": str(asset_row.get("quote_instrument_id") or asset_row.get("instrument_id") or ""),
        "quote_instrument_type": str(asset_row.get("quote_instrument_type") or asset_row.get("instrument_type") or ""),
        "venue": structure_venue,
        "instrument_id": structure_instrument_id,
        "instrument_type": structure_instrument_type,
        "updated_at": (
            (structure or {}).get("as_of_ms")
            or (snapshot.get("epoch_ms") if isinstance(snapshot, dict) else None)
            or (snapshot.get("timestamp") if isinstance(snapshot, dict) else None)
        ),
        "reason": "" if eligible else str(
            (structure or {}).get("reason")
            or structure_quality.get("reason")
            or (structure_warnings[0] if structure_warnings else "")
            or asset_row.get("quality_reason")
            or asset_row.get("status_note")
            or "verified_structure_unavailable"
        ),
        "structure": structure,
    }


@app.get("/api/timeseries/ticks/{symbol}")
async def get_timeseries_ticks(symbol: str, limit: int = 500):
    items = await asyncio.to_thread(timeseries.query_ticks, symbol, None, None, limit)
    return {"symbol": symbol.upper(), "count": len(items), "items": items}


@app.get("/api/timeseries/ohlc/{symbol}")
async def get_timeseries_ohlc(symbol: str, interval: str = "1m", limit: int = 200):
    if interval not in timeseries.BUCKET_SECONDS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(timeseries.BUCKET_SECONDS)}")
    items = await asyncio.to_thread(timeseries.resample_ohlc, symbol, interval, limit)
    return {"symbol": symbol.upper(), "interval": interval, "count": len(items), "items": items}


@app.get("/api/timeseries/factors")
async def get_timeseries_factors(asset: str | None = None, limit: int = 100):
    items = await asyncio.to_thread(timeseries.query_factor_history, asset, limit)
    return {"asset": (asset or "ALL").upper(), "count": len(items), "items": items}


@app.get("/api/timeseries/news")
async def get_timeseries_news(asset: str | None = None, limit: int = 2000):
    items = await asyncio.to_thread(timeseries.query_news_events, None, None, asset, limit)
    return {"asset": (asset or "ALL").upper(), "count": len(items), "items": items}


@app.get("/api/timeseries/winrate")
async def get_timeseries_winrate(asset: str | None = None, window: int = 20):
    return await asyncio.to_thread(timeseries.rolling_winrate, asset, window)


def _techflow_value(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return ""


def _paged_url(url: str, page: int, page_keys: tuple[str, ...] = ("page", "page_index")) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key in page_keys:
        if key in query:
            query[key] = [str(page)]
    if not any(key in query for key in page_keys):
        query["page"] = [str(page)]
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path,
        urllib.parse.urlencode(query, doseq=True), parsed.fragment,
    ))


def _extract_news_rows(payload: Any, list_keys: tuple[str, ...]) -> list:
    container = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(container, dict):
        nested = container.get("data")
        if isinstance(nested, dict):
            container = nested
        rows = next((container[k] for k in list_keys if isinstance(container.get(k), list)), [])
        return rows if isinstance(rows, list) else []
    return container if isinstance(container, list) else []


def _fetch_paged_json(
    url: str,
    *,
    referer: str,
    timeout: int,
    pages: int,
) -> Any:
    for page in range(1, max(1, pages) + 1):
        response = requests.get(
            _paged_url(url, page),
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TridentNewsReader/1.0)",
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
            },
        )
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if "html" in content_type:
            raise ValueError("non-json response")
        yield response.json()


def _fetch_techflow_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _TECHFLOW_CACHE["value"] is not None and now < _TECHFLOW_CACHE["expires"]:
        return _TECHFLOW_CACHE["value"]
    try:
        items = []
        seen: set[str] = set()
        for payload in _fetch_paged_json(
            config.TECHFLOW_NEWS_URL,
            referer="https://www.techflowpost.com/",
            timeout=4,
            pages=config.NEWS_SOURCE_PAGES,
        ):
            if isinstance(payload, dict) and payload.get("data") is not None:
                content_hint = str(payload.get("message") or "")
                if "html" in content_hint.lower():
                    raise ValueError("non-json response")
            for row in _extract_news_rows(payload, ("list", "items", "data", "newsflashes")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "name", "content") or "").strip()
                if not title:
                    continue
                item_id = str(_techflow_value(row, "id", "newsflash_id", "uuid") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                summary = str(_techflow_value(
                    row, "summary", "abstract", "description", "digest", "content"
                ) or "").strip()
                url = str(_techflow_value(row, "url", "link", "share_url") or "").strip()
                if url.startswith("/"):
                    url = "https://www.techflowpost.com" + url
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "published_at", "publish_time", "created_at", "createdAt") or ""),
                    "source": "TechFlow 深潮",
                })
        value = {
            "status": "ok" if items else "empty",
            "items": items,
            "error": "" if items else "TechFlow returned no usable rows",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"TechFlow unavailable: {type(exc).__name__}"}
    _TECHFLOW_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


@app.get("/api/news/techflow")
async def get_techflow_news():
    return await asyncio.to_thread(_fetch_techflow_sync)


def _fetch_eastmoney_news_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _EASTMONEY_CACHE["value"] is not None and now < _EASTMONEY_CACHE["expires"]:
        return _EASTMONEY_CACHE["value"]
    try:
        items = []
        seen: set[str] = set()
        eastmoney_url = config.EASTMONEY_NEWS_URL
        if "req_trace=" not in eastmoney_url:
            separator = "&" if "?" in eastmoney_url else "?"
            eastmoney_url = f"{eastmoney_url}{separator}req_trace={time.time_ns()}"
        for payload in _fetch_paged_json(
            eastmoney_url,
            referer="https://www.eastmoney.com/",
            timeout=5,
            pages=config.NEWS_SOURCE_PAGES,
        ):
            if isinstance(payload, dict) and payload.get("data") is None:
                message = str(payload.get("message") or "empty response")
                raise ValueError(f"EastMoney response error: {message[:120]}")
            for row in _extract_news_rows(payload, ("list", "items", "data", "result")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "newsTitle", "Art_Title", "content", "digest") or "").strip()
                if not title:
                    continue
                item_id = str(_techflow_value(row, "id", "newsId", "code", "uniqueUrl", "Art_UniqueUrl") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                summary = str(_techflow_value(row, "summary", "digest", "description", "Art_Summary", "content") or "").strip()
                url = str(_techflow_value(row, "url", "uniqueUrl", "link", "newsUrl", "Art_Url") or "").strip()
                if url.startswith("/"):
                    url = "https://www.eastmoney.com" + url
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "published_at", "showTime", "publishTime", "Art_ShowTime", "date") or ""),
                    "source": "东方财富",
                })
        value = {
            "status": "ok" if items else "empty",
            "items": items,
            "error": "" if items else "EastMoney returned no usable rows",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"EastMoney unavailable: {type(exc).__name__}"}
    _EASTMONEY_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


@app.get("/api/news/eastmoney")
async def get_eastmoney_news():
    return await asyncio.to_thread(_fetch_eastmoney_news_sync)


def _fetch_blockbeats_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _BLOCKBEATS_CACHE["value"] is not None and now < _BLOCKBEATS_CACHE["expires"]:
        return _BLOCKBEATS_CACHE["value"]
    try:
        items = []
        seen: set[str] = set()
        for payload in _fetch_paged_json(
            config.BLOCKBEATS_NEWS_URL,
            referer="https://www.theblockbeats.info/",
            timeout=5,
            pages=config.NEWS_SOURCE_PAGES,
        ):
            if isinstance(payload, dict) and payload.get("status") not in (0, "0", None, "ok", 200):
                continue
            for row in _extract_news_rows(payload, ("data", "list", "items")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "name", "content") or "").strip()
                if not title:
                    continue
                item_id = str(_techflow_value(row, "id", "flash_id", "uuid") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                summary = str(_techflow_value(row, "content", "summary", "description", "digest") or "").strip()
                url = str(_techflow_value(row, "link", "url", "share_url") or "").strip()
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "create_time", "published_at", "created_at", "add_time") or ""),
                    "source": "律动 BlockBeats",
                })
        value = {
            "status": "ok" if items else "empty",
            "items": items,
            "error": "" if items else "BlockBeats returned no usable rows",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"BlockBeats unavailable: {type(exc).__name__}"}
    _BLOCKBEATS_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


def _fetch_jin10_sync() -> Dict[str, Any]:
    """Fetch authorized Jin10 flash data and project it to legacy news items."""
    now = time.monotonic()
    if _JIN10_CACHE["value"] is not None and now < _JIN10_CACHE["expires"]:
        return _JIN10_CACHE["value"]
    provider = get_jin10_provider()
    if not provider.configured:
        value = {"status": "disabled", "items": [], "error": "Jin10 provider is disabled or missing API key"}
    else:
        try:
            events = provider.fetch_news(category=config.JIN10_FLASH_CATEGORIES)
            value = {
                "status": "ok",
                "items": [event.to_legacy() for event in events],
                "error": "",
                "provider": provider.health(),
            }
        except Exception as exc:
            value = {
                "status": "unavailable",
                "items": [],
                "error": f"Jin10 unavailable: {type(exc).__name__}",
                "provider": provider.health(),
            }
    _JIN10_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


@app.get("/api/news/jin10")
async def get_jin10_news():
    return await asyncio.to_thread(_fetch_jin10_sync)


@app.get("/api/news/blockbeats")
async def get_blockbeats_news():
    return await asyncio.to_thread(_fetch_blockbeats_sync)


async def _news_report() -> Dict[str, Any]:
    """汇总外部新闻源、入库状态、AI 信号和模拟交易结果。"""
    techflow, eastmoney, blockbeats, jin10, events, quick_evaluation = await asyncio.gather(
        get_techflow_news(),
        get_eastmoney_news(),
        get_blockbeats_news(),
        get_jin10_news(),
        _fetch_event_rows(limit=200),
        asyncio.to_thread(quick_sim.snapshot),
    )
    report_items = []
    for event in events:
        report_items.append({
            "news_id": event.get("news_id"),
            "source": event.get("source", ""),
            "timestamp": event.get("timestamp", ""),
            "content": event.get("news_text", ""),
            "status": event.get("analysis_status", "PENDING"),
            "decision_id": event.get("decision_id"),
            "paper_trading_run_id": event.get("paper_trading_run_id"),
            "strategy_id": event.get("strategy_id"),
            "strategy_version_id": event.get("strategy_version_id"),
            "agent_model_id": event.get("agent_model_id"),
            "asset": event.get("target_asset", "NONE"),
            "action": event.get("action", "HOLD"),
            "score": event.get("score"),
            "evidence_confidence": event.get("evidence_confidence"),
            "evidence_action": event.get("evidence_action", "HOLD"),
            "trade_gate_reason": event.get("trade_gate_reason", ""),
            "entry_price": event.get("entry_price"),
            "settled": bool(event.get("settled")),
            "is_correct": event.get("is_correct", ""),
            "forward_pnl": event.get("forward_pnl"),
            "reason": event.get("reason", ""),
        })
    directional = [item for item in report_items if item["action"] in ("BUY", "SELL")]
    evidence_passed = [
        item for item in directional
        if item["evidence_action"] == item["action"]
        and item["trade_gate_reason"] == "证据充分，允许输出方向性结论"
    ]
    completed_directional = [
        item for item in directional if item["status"] not in ("PENDING", "PROCESSING")
    ]
    gate_reasons: Dict[str, int] = {}
    for item in completed_directional:
        if item in evidence_passed:
            continue
        reason_key = item["trade_gate_reason"] or "尚未写入闸门结论"
        gate_reasons[reason_key] = gate_reasons.get(reason_key, 0) + 1
    return {
        "generated_at_ms": int(time.time() * 1000),
        "sources": {
            "techflow": techflow,
            "eastmoney": eastmoney,
            "blockbeats": blockbeats,
            "jin10": jin10,
        },
        "pipeline": {
            "source_items": sum(
                len(payload.get("items", []))
                for payload in (techflow, eastmoney, blockbeats, jin10)
                if isinstance(payload, dict)
            ),
            "stored_reports": len(report_items),
            "analyzed": sum(item["decision_id"] is not None for item in report_items),
            "signals": len(directional),
            "evidence_passed": len(evidence_passed),
            "evidence_rejected": len(completed_directional) - len(evidence_passed),
            "executed": sum(item["entry_price"] is not None for item in report_items),
            "open_trades": sum(item["entry_price"] is not None and not item["settled"] for item in report_items),
            "settled_trades": sum(item["settled"] for item in report_items),
        },
        "gate_reasons": gate_reasons,
        "quick_sim": quick_evaluation,
        "reports": report_items,
    }


@app.get("/api/news/report")
async def get_news_report():
    return await _news_report()


def _news_item_sort_key(item: Dict[str, Any]) -> int:
    """Sort provider rows by their own event clock, newest first."""
    value = item.get("published_at") or item.get("time")
    normalized = _external_timestamp(value)
    return int(normalized[1]) if normalized else 0


def _ingest_external_news_sync(
    items: List[Dict[str, Any]], *, max_inserted: int | None = None
) -> int:
    """Persist fresh provider rows until the *actual* insert quota is reached.

    The watcher previously sliced the merged provider list before deduplication.
    Once the first page was already stored, it retried the same duplicates and
    never reached newer rows from later providers.  The limit now counts only
    successful inserts.
    """
    inserted = 0
    insert_limit = None if max_inserted is None else max(0, int(max_inserted))
    conn = db.get_connection()
    try:
        data_quality.ensure_schema(conn)
        for item in items:
            if insert_limit is not None and inserted >= insert_limit:
                break
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or item.get("body") or "").strip()
            if not title:
                continue
            source = str(item.get("source") or "External")
            published_value = item.get("published_at") or item.get("time")
            external_key = f"{source}:{item.get('id') or title}"
            marker = hashlib.sha256(external_key.encode("utf-8")).hexdigest()[:24]
            if conn.execute("SELECT 1 FROM raw_news WHERE content LIKE ? LIMIT 1", (f"[hash:{marker}]%",)).fetchone():
                continue
            quality = data_quality.assess(
                source=source,
                kind="news",
                event_id=item.get("id") or title,
                published_at=published_value,
                payload=item,
            )
            if (
                not quality.decision_eligible
                and not getattr(config, "NEWS_CANDIDATE_DISPLAY_ENABLED", False)
                and data_quality.observation_already_recorded(conn, quality, item)
            ):
                continue
            display_candidate = bool(
                quality.decision_eligible
                or getattr(config, "NEWS_CANDIDATE_DISPLAY_ENABLED", False)
            )
            can_ingest = bool(
                quality.accepted
                and display_candidate
                and news_sources.allow_ingest(source, conn)
            )
            # L2 classification may call a remote model and take seconds.  It
            # must run before the first write in this transaction; otherwise a
            # whole provider batch holds SQLite's single writer lock while the
            # model responds, blocking settings, quotes and the AI worker.
            filtered = evaluate_news(title, summary) if can_ingest else None
            data_quality.record(
                conn,
                quality,
                published_at=published_value,
                payload=item,
                metadata={"provider_id": str(item.get("id") or "")[:200]},
                quarantine=not quality.decision_eligible,
            )
            if not can_ingest:
                conn.commit()
                continue
            content = f"[hash:{marker}] {title}"
            if summary and summary not in title:
                content += f"\n{summary}"
            normalized_ts = _external_timestamp(published_value)
            timestamp, ts_epoch = normalized_ts or (_now(), int(time.time()))
            news_id = db.insert_raw_news(
                conn,
                source=source,
                content=content[:1000],
                timestamp=timestamp,
                status=("DONE" if int(filtered["is_noise"]) else "PENDING"),
                is_noise=int(filtered["is_noise"]),
                relevance_score=float(filtered["relevance_score"]),
                ts=ts_epoch,
                quality_status=quality.quality_status,
                quality_reason=quality.reason,
            )
            try:
                timeseries.record_news_event(
                    news_id,
                    source=source,
                    is_noise=int(filtered["is_noise"]),
                    status=("DONE" if int(filtered["is_noise"]) else "PENDING"),
                    ts=ts_epoch,
                    connection=conn,
                )
            except sqlite3.OperationalError as exc:
                print(f"[NEWS-SOURCES] timeseries skip: {exc}")
            inserted += 1
            # Release the writer lock before classifying the next provider row.
            conn.commit()
    finally:
        conn.close()
    return inserted


async def news_source_watcher() -> None:
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            sources = settings.get("sources") or {}
            remaining = int(settings.get("remaining") or 0)
            enabled = bool(settings.get("enabled", True))
            jobs = []
            if enabled and remaining > 0:
                if sources.get("techflow", True):
                    jobs.append(asyncio.to_thread(_fetch_techflow_sync))
                if sources.get("eastmoney", True):
                    jobs.append(asyncio.to_thread(_fetch_eastmoney_news_sync))
                if sources.get("blockbeats", True):
                    jobs.append(asyncio.to_thread(_fetch_blockbeats_sync))
                if config.JIN10_ENABLED and sources.get("jin10", True):
                    jobs.append(asyncio.to_thread(_fetch_jin10_sync))
            payloads = await asyncio.gather(*jobs, return_exceptions=True) if jobs else []
            items: List[Dict[str, Any]] = []
            for payload in payloads:
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    items.extend(payload.get("items") or [])
            if remaining > 0 and items:
                items.sort(key=_news_item_sort_key, reverse=True)
                await asyncio.to_thread(
                    _ingest_external_news_sync,
                    items,
                    max_inserted=remaining,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[NEWS-SOURCES] {type(exc).__name__}: {exc}")
        poll_seconds = config.NEWS_SOURCE_POLL_SECONDS
        if config.JIN10_ENABLED:
            poll_seconds = min(poll_seconds, config.JIN10_POLL_SECONDS)
        await asyncio.sleep(poll_seconds)


def _macro_calendar_effective_window(
    start: str,
    end: str,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Use a bounded live window when the client omits both dates.

    The database intentionally retains historical releases, but a live
    calendar must not start at the oldest retained FOMC record.  Explicit
    client boundaries are never rewritten.
    """
    if start or end:
        return start, end
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    lower = reference - timedelta(hours=6)
    upper = reference + timedelta(days=120)
    return (
        lower.isoformat(timespec="seconds").replace("+00:00", "Z"),
        upper.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


async def macro_calendar_watcher() -> None:
    """Low-frequency sync for explicitly configured calendar providers."""
    while True:
        try:
            if getattr(config, "MACRO_CALENDAR_ENABLED", False):
                start, end = _macro_calendar_effective_window("", "")
                result = await asyncio.to_thread(
                    macro_context.sync_macro_calendars,
                    start=start,
                    end=end,
                )
                if result.get("status") not in {"ok", "disabled"}:
                    print(f"[MACRO-CALENDAR] {result.get('reason') or result.get('status')}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[MACRO-CALENDAR] {type(exc).__name__}: {exc}")
        await asyncio.sleep(getattr(config, "MACRO_CALENDAR_POLL_SECONDS", 60))


def _market_item_quality(item: Dict[str, Any], payload: Dict[str, Any]) -> data_quality.QualityDecision:
    published_at = item.get("event_ts") or item.get("updated_at") or payload.get("updated_at")
    return data_quality.assess(
        source=str(item.get("source") or ""),
        kind="market",
        event_id=f"{str(item.get('asset') or '').upper()}:{published_at}",
        published_at=published_at,
        payload=item,
    )


def _market_price_from_cache(asset: str) -> float | None:
    """只读已通过决策门禁的行情缓存，候选价不可作为盈亏标签。"""
    payload = _MARKET_CACHE.get("value") or {}
    for item in payload.get("items") or []:
        if str(item.get("asset") or "").upper() != (asset or "").upper():
            continue
        price = item.get("price")
        quality = _market_item_quality(item, payload)
        if quality.decision_eligible and isinstance(price, (int, float)) and price > 0:
            return float(price)
    return None


def _quick_sim_price(asset: str) -> float | None:
    """快速模拟与前端看板共用行情：优先多源缓存，再回退到引擎价格源。"""
    return _market_price_from_cache(asset) or _get_current_price(asset)


async def quick_sim_watcher() -> None:
    """快速模拟评测循环：新方向性信号按真实行情建仓，到期按真实行情结算。"""
    while True:
        try:
            await asyncio.to_thread(_cached_market_prices)
            result = await asyncio.to_thread(quick_sim.run_cycle, _quick_sim_price)
            for trade in result["opened"]:
                print(f"[QUICK-SIM] OPEN #{trade['decision_id']} {trade['action']} {trade['asset']} @ {trade['entry_price']}")
            for trade in result["settled"]:
                print(f"[QUICK-SIM] SETTLE #{trade['id']} {trade['action']} {trade['asset']} {trade['pnl_pct']:+.4f}% -> {trade['verdict']}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[QUICK-SIM] {type(exc).__name__}: {exc}")
        await asyncio.sleep(config.QUICK_SIM_POLL_SECONDS)


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _context_asset(context: Dict[str, Any], asset: str) -> Dict[str, Any]:
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    value = assets.get(asset) or assets.get("BTC") or {}
    return value if isinstance(value, dict) else {}


def _factor_result(row: Dict[str, Any]) -> Dict[str, Any]:
    return decision_guard.evaluate_decision(row)


async def _news_analysis(news_id: int) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            WITH eligible_history AS (
                SELECT h.target_asset, h.is_correct
                FROM ai_decisions h
                INNER JOIN raw_news hr ON hr.id=h.news_id
                WHERE h.settled=1
                  AND LOWER(COALESCE(hr.quality_status, ''))='verified'
                  AND UPPER(h.suggested_action) IN ('BUY','SELL')
                  AND UPPER(COALESCE(h.evidence_action, 'HOLD'))=UPPER(h.suggested_action)
                  AND COALESCE(h.trade_gate_reason, '')=?
                  AND h.paper_trading_run_id IS NOT NULL
            )
            SELECT rn.id AS news_id, rn.source, rn.content, rn.timestamp,
                   ad.id AS decision_id, ad.created_at, ad.sentiment_score,
                   UPPER(ad.suggested_action) AS suggested_action, ad.reasoning,
                   ad.reasoning_path, ad.market_category, ad.target_asset,
                   ad.market_confirmation, ad.decision_context, ad.cluster_size,
                   (SELECT COUNT(*) FROM eligible_history h WHERE UPPER(h.target_asset)=UPPER(ad.target_asset)) AS history_total,
                   (SELECT COUNT(*) FROM eligible_history h WHERE UPPER(h.target_asset)=UPPER(ad.target_asset) AND UPPER(h.is_correct)='WIN') AS history_wins
            FROM raw_news rn
            LEFT JOIN ai_decisions ad ON ad.id=(SELECT MAX(x.id) FROM ai_decisions x WHERE x.news_id=rn.id)
            WHERE rn.id=?
            """,
            (_PASSED_TRADE_GATE_REASON, news_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
    if row is None:
        raise HTTPException(status_code=404, detail="news not found")
    data = dict(row)
    analysis = _factor_result(data)
    await asyncio.to_thread(timeseries.record_factor_snapshot, news_id,
                            str(data.get("target_asset") or "NONE"), analysis)
    return {
        "news": {"id": data["news_id"], "source": data["source"], "content": re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', data["content"] or ""), "published_at": data["timestamp"]},
        "decision": None if data["decision_id"] is None else {key: data[key] for key in ("decision_id", "created_at", "sentiment_score", "suggested_action", "reasoning", "reasoning_path", "market_category", "target_asset", "market_confirmation")},
        "analysis": analysis,
        "strategy": {
            "action": analysis["action"],
            "raw_action": analysis["raw_action"],
            "confidence": analysis["confidence"],
            "passed_gate": analysis["confidence_detail"]["passed_gate"],
            "verdict": analysis["verdict"],
            "note": "仅供策略研究，不执行交易",
        },
    }


@app.get("/api/news/{news_id}/analysis")
async def get_news_analysis(news_id: int):
    return await _news_analysis(news_id)


def _settled_stats_sync(asset: str) -> Dict[str, Any]:
    connection = db.get_connection()
    connection.row_factory = __import__("sqlite3").Row
    try:
        sample = evidence.collect_settled_sample(asset, connection=connection)
        tokens = evidence.query_assets(sample["assets"]) if sample["assets"] else ()
        eligibility_sql = """
            ad.settled=1
            AND UPPER(ad.is_correct) IN ('WIN','LOSS')
            AND LOWER(COALESCE(rn.quality_status, ''))='verified'
            AND UPPER(ad.suggested_action) IN ('BUY','SELL')
            AND UPPER(COALESCE(ad.evidence_action, 'HOLD'))=UPPER(ad.suggested_action)
            AND COALESCE(ad.trade_gate_reason, '')=?
            AND ad.paper_trading_run_id IS NOT NULL
        """
        if tokens:
            placeholders = ",".join("?" * len(tokens))
            row = connection.execute(
                f"""SELECT AVG(ad.forward_pnl) avg_pnl
                    FROM ai_decisions ad
                    INNER JOIN raw_news rn ON rn.id=ad.news_id
                    WHERE {eligibility_sql}
                      AND UPPER(ad.target_asset) IN ({placeholders})""",
                (_PASSED_TRADE_GATE_REASON, *tokens),
            ).fetchone()
        else:
            row = connection.execute(
                f"""SELECT AVG(ad.forward_pnl) avg_pnl
                    FROM ai_decisions ad
                    INNER JOIN raw_news rn ON rn.id=ad.news_id
                    WHERE {eligibility_sql}""",
                (_PASSED_TRADE_GATE_REASON,),
            ).fetchone()
        return {
            "total": sample["total"],
            "wins": sample["wins"],
            "avg_pnl": None if row is None else row["avg_pnl"],
            "scope": sample["scope"],
            "supplemented": sample["supplemented"],
        }
    finally:
        connection.close()


_ADVICE_BOUNDS = {
    "signal_threshold": (0.3, 0.9),
    "notional_multiplier": (0.25, 1.5),
    "trailing_callback_rate": (0.1, 5.0),
    "holding_horizon_minutes": (15, 240),
}


def _rule_advice(analysis: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    confidence = analysis["analysis"]["confidence"] / 100.0
    winrate = (stats.get("wins") or 0) / max(stats.get("total") or 1, 1)
    contradictions = len(analysis["analysis"]["contradictions"])
    significance = analysis["analysis"]["significance"]
    return {
        "signal_threshold": round(_clamp(0.65 - confidence * 0.2 + contradictions * 0.05, 0.3, 0.9), 2),
        "notional_multiplier": round(_clamp(0.5 + confidence * 0.6 + max(winrate - 0.5, 0) - contradictions * 0.15, 0.25, 1.5), 2),
        "trailing_callback_rate": round(_clamp(0.5 + (1 - confidence) * 0.5, 0.1, 5), 2),
        "holding_horizon_minutes": int(_clamp(60 + confidence * 120, 15, 240)),
        "reason": f"统一置信度 {analysis['analysis']['confidence']}/100，卡方 p={significance['p_value']}，矛盾项 {contradictions} 个",
        "risk_notes": "样本不足、p 值过大或因子冲突时维持较高阈值和较低仓位倍率",
        "rollback_condition": "连续 3 条已结算信号亏损或滚动胜率低于 45% 时回滚",
    }


def _llm_strategy_advice_sync(analysis: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    response = _agent_llm_client().chat.completions.create(
        model=_agent_llm_model(), temperature=0.0, max_tokens=700,
        extra_body=config.AIPING_EXTRA_BODY, response_format=({"type": "json_object"} if config.AIPING_JSON_MODE else None),
        messages=[
            {"role": "system", "content": "你是策略参数顾问。只输出严格 JSON，不交易、不修改配置。只能引用输入 JSON 中已有的证据（evidence 为 0-10 归一分、significance 为卡方检验结果、contradictions 为矛盾项），禁止凭空假设任何未给出的数据。p_value 越大或 contradictions 越多，参数越保守。字段只能是 signal_threshold, notional_multiplier, trailing_callback_rate, holding_horizon_minutes, reason, risk_notes, rollback_condition。"},
            {"role": "user", "content": json.dumps({"bounds": _ADVICE_BOUNDS, "analysis": analysis["analysis"], "settled_performance": stats}, ensure_ascii=False)},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    allowed = set(_ADVICE_BOUNDS) | {"reason", "risk_notes", "rollback_condition"}
    if not isinstance(result, dict) or set(result) - allowed or not all(key in result for key in allowed):
        raise ValueError("invalid advice JSON")
    for key, (low, high) in _ADVICE_BOUNDS.items():
        result[key] = _clamp(result[key], low, high)
    result["holding_horizon_minutes"] = int(result["holding_horizon_minutes"])
    return result


@app.post("/api/news/{news_id}/strategy-advice")
async def get_strategy_advice(news_id: int):
    analysis = await _news_analysis(news_id)
    asset = (analysis.get("decision") or {}).get("target_asset") or "NONE"
    stats = await asyncio.to_thread(_settled_stats_sync, asset)
    try:
        advice = await asyncio.to_thread(_llm_strategy_advice_sync, analysis, stats)
        mode = "llm"
    except Exception:
        advice = _rule_advice(analysis, stats)
        mode = "rules"
    current_values = {
        "signal_threshold": config.BINANCE_SIGNAL_THRESHOLD,
        "notional_multiplier": 1.0,
        "trailing_callback_rate": config.BINANCE_TRAILING_CALLBACK_RATE,
        "holding_horizon_minutes": 60,
    }
    return {"mode": mode, "current_values": current_values, "advice": advice, "bounds": {key: {"min": value[0], "max": value[1]} for key, value in _ADVICE_BOUNDS.items()}, "settled_performance": stats, "validation": {"confidence": analysis["analysis"]["confidence"], "significance": analysis["analysis"]["significance"], "contradictions": analysis["analysis"]["contradictions"], "gated_action": analysis["analysis"]["action"], "verdict": analysis["analysis"]["verdict"]}, "applied": False}


@app.get("/api/klines/{symbol}")
async def get_klines(symbol: str, limit: int = 72):
    ticker_map = {
        "BTCUSDT": ("BTC-USD", 80000),
        "XAUUSD":  ("GC=F",    2500),
        "WTIUSD":  ("CL=F",      70),
    }
    yf_sym, mock_base = ticker_map.get(symbol.upper(), (None, None))
    if yf_sym is None:
        return _mock_klines(limit, base=80000, seed=1)
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="3d", interval="1h")
        if hist.empty:
            return _mock_klines(limit, base=mock_base, seed=1)
        result = []
        for idx, row in hist.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=TZ_SHANGHAI)
            result.append({
                "time": int(ts.timestamp()),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]) if not math.isnan(float(row["Volume"])) else 0,
            })
        if result:
            return result[:limit]
    except Exception:
        pass
    return _mock_klines(limit, base=mock_base, seed=1)


@app.get("/api/export/signals")
async def export_paper_signals():
    """导出全部信号分析记录（含 BUY/SELL/HOLD 观望）。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                ad.id,
                rn.timestamp AS news_time,
                rn.content AS news_text,
                UPPER(ad.target_asset) AS asset,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.entry_price,
                ad.reasoning_path,
                ad.reasoning,
                ad.prediction_type,
                ad.event_strength,
                ad.expected_horizon
            FROM ai_decisions ad
            LEFT JOIN raw_news rn ON rn.id = ad.news_id
            ORDER BY ad.id DESC
            LIMIT 2000
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

    import xlsxwriter
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet("信号分析")
    headers = ["时间", "新闻内容", "品种", "方向", "评分", "入场价", "预测类型", "影响强度", "时间维度", "大模型归因"]
    for column, header in enumerate(headers):
        worksheet.write(0, column, header)
    for index, row in enumerate(rows, start=1):
        action = row["action"]
        direction = "多" if action == "BUY" else ("空" if action == "SELL" else "观望")
        worksheet.write(index, 0, row["news_time"] or "")
        worksheet.write(index, 1, row["news_text"] or "")
        asset = "XAU" if row["asset"] == "GOLD" else row["asset"]
        worksheet.write(index, 2, asset or "")
        worksheet.write(index, 3, direction)
        worksheet.write_number(index, 4, round(float(row["score"] or 0), 4))
        if row["entry_price"] is not None and row["entry_price"] > 0:
            worksheet.write_number(index, 5, float(row["entry_price"]))
        worksheet.write(index, 6, row["prediction_type"] or "")
        worksheet.write(index, 7, row["event_strength"] or "")
        worksheet.write(index, 8, row["expected_horizon"] or "")
        worksheet.write(index, 9, row["reasoning_path"] or row["reasoning"] or "")
    worksheet.set_column(0, 0, 20)
    worksheet.set_column(1, 1, 64)
    worksheet.set_column(2, 8, 12)
    worksheet.set_column(9, 9, 60)
    workbook.close()
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=paper_signals.xlsx"},
    )


@app.get("/api/export/excel")
async def export_excel():
    """Export today's buy/sell signals as Excel."""
    try:
        return await _do_export_excel()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return StreamingResponse(
            io.BytesIO(f"Export error: {exc}".encode("utf-8")),
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )


async def _do_export_excel():
    """Core Excel export logic, separated for clean error handling."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                ad.id,
                rn.timestamp,
                rn.source,
                rn.content AS news_text,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.reasoning_path,
                ad.reasoning,
                ad.extra_models_consensus,
                ad.target_asset,
                ad.vip_tag,
                ad.entry_price, ad.exit_price, ad.max_price, ad.min_price,
                ad.max_price_time, ad.min_price_time,
                ad.entry_time,
                ad.exit_time,
                ad.exit_reason,
                ad.is_correct,
                ad.cluster_size,
                ad.prediction_type,
                ad.event_phase,
                ad.market_confirmation,
                ad.expected_horizon,
                ad.invalidation_condition,
                ad.decision_context,
                ad.mfe_pct,
                ad.mae_pct,
                ad.forward_pnl,
                ad.mfe_time_mins
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            WHERE date(ad.created_at) >= date('now', 'localtime', '-5 days')
            ORDER BY ad.id DESC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

    import xlsxwriter
    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output)
    ws = wb.add_worksheet("Signals")
    headers = [
        "ID", "时间", "新闻内容", "品种", "方向", "评分", "入场价", "最高价", "最低价", "出场价",
        "最大浮盈%", "最大浮亏%", "到达极值(min)", "强影响", "胜负", "标签", "主模型归因", "extra_models_consensus",
    ]
    for c, h in enumerate(headers):
        ws.write(0, c, h)

    # Impact thresholds: MFE must exceed asset-specific percentage
    IMPACT_THRESHOLDS = {"BTC": 2.0, "XAU": 1.0, "GOLD": 1.0, "WTI": 1.5}

    for r, row in enumerate(rows, start=1):
        asset = (row["target_asset"] or "").upper()
        action_raw = (row["action"] or "").upper()
        entry = row["entry_price"]
        exit_p = row["exit_price"]
        max_p = row["max_price"]
        min_p = row["min_price"]
        max_ptime = row["max_price_time"] or 0
        min_ptime = row["min_price_time"] or 0
        entry_time_str = row["entry_time"] or ""
        raw_verdict = (row["is_correct"] or "").strip().upper()
        if raw_verdict == "WIN":
            verdict = "正确"
        elif raw_verdict == "LOSS":
            verdict = "错误"
        else:
            verdict = raw_verdict or "—"
        # ── Derived impact metrics (defensive against div-by-zero) ──
        mfe_str = "—"
        mae_str = "—"
        time_to_extreme = "—"
        high_impact = "否"

        if entry and entry > 0:
            try:
                if action_raw == "BUY":
                    if max_p is not None and max_p > 0:
                        mfe_val = (max_p - entry) / entry * 100
                        mfe_str = f"{mfe_val:+.2f}%"
                    if min_p is not None and min_p > 0:
                        mae_val = (min_p - entry) / entry * 100
                        mae_str = f"{mae_val:+.2f}%"
                    if max_ptime > 0:
                        try:
                            et_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                            et_unix = int(et_dt.timestamp())
                            minutes = round((max_ptime - et_unix) / 60, 1)
                            if minutes >= 0:
                                time_to_extreme = f"{minutes}"
                        except (ValueError, TypeError, OSError):
                            pass
                elif action_raw == "SELL":
                    if entry > 0:
                        if min_p is not None and min_p > 0:
                            mfe_val = (entry - min_p) / entry * 100
                            mfe_str = f"{mfe_val:+.2f}%"
                        if max_p is not None and max_p > 0:
                            mae_val = (entry - max_p) / entry * 100
                            mae_str = f"{mae_val:+.2f}%"
                    if min_ptime > 0:
                        try:
                            et_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                            et_unix = int(et_dt.timestamp())
                            minutes = round((min_ptime - et_unix) / 60, 1)
                            if minutes >= 0:
                                time_to_extreme = f"{minutes}"
                        except (ValueError, TypeError, OSError):
                            pass

                if mfe_str != "—":
                    mfe_number = float(mfe_str.replace("%", "").replace("+", ""))
                    threshold = IMPACT_THRESHOLDS.get(asset, 2.0)
                    if mfe_number > threshold:
                        high_impact = "是"
            except Exception:
                pass

        ws.write(r, 0, row["id"])
        ws.write(r, 1, _format_time(row["timestamp"]))
        ws.write(r, 2, re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', (row["news_text"] or "")[:200]))
        ws.write(r, 3, asset)
        action_display = "多" if action_raw == "BUY" else ("空" if action_raw == "SELL" else "观望")
        ws.write(r, 4, action_display)
        ws.write(r, 5, round(row["score"], 2) if row["score"] else 0)
        ws.write(r, 6, entry)
        ws.write(r, 7, max_p)
        ws.write(r, 8, min_p)
        ws.write(r, 9, exit_p)
        ws.write(r, 10, mfe_str)
        ws.write(r, 11, mae_str)
        ws.write(r, 12, time_to_extreme)
        ws.write(r, 13, high_impact)
        ws.write(r, 14, verdict)
        ws.write(r, 15, (row["vip_tag"] or "").replace("[", "").replace("]", ""))
        # 主模型归因: 优先完整推导链，回退到短结论
        ws.write(r, 16, ((row["reasoning_path"] or row["reasoning"] or "")[:2000]).strip())
        # ── extra_models_consensus: 直接从 DB 列写入，已经是合法 JSON ──
        raw_consensus = _safe(row, "extra_models_consensus") or ""
        if isinstance(raw_consensus, str):
            raw_consensus = raw_consensus.strip()
        if raw_consensus:
            # 校验是否为合法 JSON，非法则写入原始字符串
            try:
                parsed = json.loads(raw_consensus) if isinstance(raw_consensus, str) else raw_consensus
                ws.write(r, 17, json.dumps(parsed, ensure_ascii=False))
            except (json.JSONDecodeError, TypeError, ValueError):
                ws.write(r, 17, str(raw_consensus)[:2000])
        else:
            ws.write(r, 17, "")
    wb.close()
    data = output.getvalue()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trident_signals.xlsx"},
    )


# ---------------------------------------------------------------------------
# File Upload — Agent Chat attachments
# ---------------------------------------------------------------------------

@app.post("/api/agent_chat/upload")
async def agent_chat_upload(
    file: UploadFile = File(...),
):
    """Upload an attachment for Data Copilot conversations."""
    # Validate size
    content = await file.read()
    size = len(content)
    if size > config.MAX_UPLOAD_SIZE_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": f"File too large ({size} bytes, max {config.MAX_UPLOAD_SIZE_BYTES})"},
        )

    # Validate type
    if file.content_type and file.content_type not in config.ALLOWED_UPLOAD_TYPES:
        return JSONResponse(
            status_code=415,
            content={"error": f"Unsupported file type: {file.content_type}"},
        )

    # Save file
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    import uuid
    ext = ""
    if "." in file.filename or file.filename is None:
        pass  # keep original extension
    else:
        # Infer extension from content type
        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "application/pdf": ".pdf",
            "text/plain": ".txt", "text/csv": ".csv", "text/html": ".html",
        }
        ext = ext_map.get(file.content_type, "")
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(config.UPLOAD_DIR, safe_name)

    with open(dest_path, "wb") as f:
        f.write(content)

    return JSONResponse(content={
        "url": f"/uploads/{safe_name}",
        "filename": file.filename or safe_name,
        "size": size,
        "content_type": file.content_type or "",
    })


# ---------------------------------------------------------------------------
# Agent Chat (Data Copilot) — Text-to-SQL + DB query
# ---------------------------------------------------------------------------

class AgentChatRequest(BaseModel):
    user_message: str
    active_market: str = "ALL"
    context: Dict[str, Any] = {}

class AgentChatResponse(BaseModel):
    reply: str
    sql: str = ""
    rows: List[Dict[str, Any]] = []
    error: str = ""

# ── Agent LLM config: Aiping OpenAI-compatible endpoint ──

def _agent_llm_client():
    """Return the Aiping OpenAI-compatible client."""
    import openai

    if not config.AIPING_API_KEY:
        raise RuntimeError("Aiping API key is not configured.")
    return openai.OpenAI(
        base_url=config.AIPING_BASE_URL,
        api_key=config.AIPING_API_KEY,
    )


def _agent_llm_model() -> str:
    return config.AIPING_MODEL

# ── DB Schema description for the Agent LLM ──

_SCHEMA_TEXT = """
You are Trident Data Copilot, an expert quant trading analyst with read-only SQLite access.

Tables & columns:

  ai_decisions: id, news_id(FK→raw_news.id), created_at, suggested_action(BUY|SELL|HOLD),
    sentiment_score(-1..+1), reasoning(short), reasoning_path(full chain-of-thought), market_category,
    target_asset, vip_tag, entry_price, exit_price, max_price, min_price, max_price_time,
    min_price_time, entry_time, is_correct(WIN|LOSS|""), settled(0|1), parent_id, child_count,
    cluster_size, doubao_action(HOLD), doubao_reasoning,
    extra_models_consensus(JSON: {"DeepSeek":{"action":"BUY","reasoning":"..."}, ...})

  raw_news: id, timestamp, source, content(full news text), status(NEW|PROCESSING|DONE|FAILED)

One ai_decisions row = one primary model decision + JSON-packed sub-model votes.
Use date(ad.created_at) for date filters. LIKE is case-insensitive.
json_extract(extra_models_consensus, '$.DeepSeek.action') pulls sub-model data.
Default LIMIT 20 if not specified.

Respond with a single valid JSON: {"sql": "<SELECT only or empty string>", "reply": "<Chinese explanation>"}

IMPORTANT — when to return empty sql:
- If the user is just greeting, chatting, saying thanks, or asking a question that does NOT require database access, set sql to "" and reply with a friendly Chinese greeting or acknowledgement.
- Only populate sql when the user is explicitly asking about trading signals, positions, win/loss stats, model votes, news events, or anything that requires querying ai_decisions or raw_news.

SQL rules:
- SELECT queries ONLY. Never INSERT/UPDATE/DELETE/DROP/ALTER.
- ORDER BY ad.id DESC for recent data.
- GROUP BY/COUNT/AVG/SUM for stats.
- NEVER access system tables or sqlite_master.
"""

# ── LLM-powered Text-to-SQL ──

def _time_context() -> str:
    """Return current system time for injection into LLM prompts."""
    now = datetime.now()
    return (
        f"【重要上下文】当前系统精准时间是: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(星期{['一','二','三','四','五','六','日'][now.weekday()]})。"
        f"当用户提到'今天'、'最近'、'昨天'、'本周'或省略年份的日期时，"
        f"请务必以此时间为基准来计算SQL的时间范围，切勿自行猜测年份！"
    )

def _llm_text_to_sql_sync(user_message: str, active_market: str) -> tuple[str, str]:
    """Call the LLM synchronously (runs in a thread via asyncio.to_thread)."""
    client = _agent_llm_client()
    model = _agent_llm_model()

    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        max_tokens=800,
        extra_body=config.AIPING_EXTRA_BODY,
        messages=[
            {"role": "system", "content": _SCHEMA_TEXT},
            {"role": "system", "content": _time_context()},
            {
                "role": "user",
                "content": (
                    f"当前活跃市场: {active_market}\n"
                    f"用户提问: {user_message}\n\n"
                    f"请根据表结构生成SQL查询，返回JSON。"
                ),
            },
        ],
        response_format=({"type": "json_object"} if config.AIPING_JSON_MODE else None),
    )

    raw = resp.choices[0].message.content.strip()
    result = json.loads(raw)
    return result.get("reply", "查询完成"), result.get("sql", "")

# ── Data-to-Text: second LLM pass for natural-language summary ──

_SUMMARIZE_PROMPT = """You are a professional quantitative trader writing a concise internal briefing.

Given the user's original question and a JSON array of database query results, produce a short, insightful answer in Chinese (≤150 characters).

Rules:
- Do NOT list every row. Extract the key pattern, trend, or answer.
- Mention counts, dominant assets, price ranges, and win/loss when relevant.
- Use professional but plain language. No markdown, no bullet points.
- If results are empty, say so honestly.
- Format: just the summary text, nothing else."""

def _clean_rows_for_frontend(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip verbose/text-heavy columns so the frontend table stays compact."""
    _STRIP_COLS = {
        "reasoning", "reasoning_path", "extra_models_consensus",
        "news_text", "content", "doubao_reasoning", "doubao_action",
    }
    cleaned = []
    for r in rows:
        cleaned.append({k: v for k, v in r.items() if k not in _STRIP_COLS})
    return cleaned

def _summarize_results_sync(user_message: str, rows: List[Dict[str, Any]]) -> str:
    """Second LLM call: data → natural-language insight."""
    # Only send first 5 rows to keep tokens low
    sample = rows[:5]
    # Also strip verbose fields from the sample sent to LLM
    _STRIP_FOR_LLM = {"reasoning_path", "extra_models_consensus", "doubao_reasoning"}
    sample_clean = [
        {k: v for k, v in r.items() if k not in _STRIP_FOR_LLM}
        for r in sample
    ]
    data_json = json.dumps(sample_clean, ensure_ascii=False, default=str)

    client = _agent_llm_client()
    model = _agent_llm_model()

    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        max_tokens=300,
        extra_body=config.AIPING_EXTRA_BODY,
        messages=[
            {"role": "system", "content": _SUMMARIZE_PROMPT},
            {"role": "system", "content": _time_context()},
            {
                "role": "user",
                "content": (
                    f"用户提问: {user_message}\n"
                    f"共 {len(rows)} 条结果，以下是前 {len(sample)} 条:\n"
                    f"{data_json}"
                ),
            },
        ],
    )

    return resp.choices[0].message.content.strip()

@app.post("/api/agent_chat", response_model=AgentChatResponse)
async def agent_chat_endpoint(req: AgentChatRequest):
    """
    Data Copilot — Two-pass LLM:
      1. Text-to-SQL → execute → get rows
      2. Data-to-Text → natural-language insight summary
    """
    # ── Step 1: LLM Text-to-SQL ──
    try:
        explanation, sql = await asyncio.to_thread(
            _llm_text_to_sql_sync, req.user_message, req.active_market
        )
    except Exception as e:
        return AgentChatResponse(
            reply="抱歉，LLM 调用失败，请稍后重试或换个问法。",
            error=f"LLM error: {type(e).__name__}",
        )

    # ── Step 2: Empty SQL = chitchat ──
    if not sql or not sql.strip():
        return AgentChatResponse(reply=explanation, sql="", rows=[])

    # ── Step 3: Safety gate ──
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        return AgentChatResponse(
            reply="出于安全考虑，我只执行 SELECT 查询。请重新描述你的需求。",
            sql=sql.strip(),
            error="Non-SELECT statement blocked",
        )

    if any(bad in stripped for bad in ("SQLITE_MASTER", "PRAGMA", "ATTACH", "DETACH")):
        return AgentChatResponse(
            reply="不允许访问系统表或执行管理命令。",
            sql=sql.strip(),
            error="Forbidden system access blocked",
        )

    # ── Step 4: Execute SQL ──
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql)
            rows_raw = await cursor.fetchall()
            await cursor.close()

        rows = [dict(r) for r in rows_raw]

        if not rows:
            return AgentChatResponse(
                reply="没有找到匹配的数据。换个条件试试？",
                sql=sql.strip(),
                rows=[],
            )

        # ── Step 5: Second LLM pass — data → natural-language insight ──
        try:
            summary = await asyncio.to_thread(
                _summarize_results_sync, req.user_message, rows
            )
        except Exception:
            summary = f"查询返回 {len(rows)} 条记录，详见下方表格。"

        # ── Step 6: Clean rows for compact frontend table ──
        clean_rows = _clean_rows_for_frontend(rows)

        return AgentChatResponse(
            reply=summary,
            sql=sql.strip(),
            rows=clean_rows,
        )

    except Exception as e:
        return AgentChatResponse(
            reply=f"SQL 执行出错，请换个问法试试。",
            sql=sql.strip(),
            error=str(e),
        )


@app.get("/uploads/{filename:path}")
async def serve_upload(filename: str):
    """Serve uploaded files from the uploads directory."""
    import mimetypes
    safe_name = os.path.basename(filename)
    file_path = os.path.join(config.UPLOAD_DIR, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    content_type, _ = mimetypes.guess_type(safe_name)
    content_type = content_type or "application/octet-stream"
    with open(file_path, "rb") as f:
        content = f.read()
    return StreamingResponse(
        io.BytesIO(content),
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@app.get("/api/macro/context")
async def get_macro_context(refresh: int = 0):
    """盘面结构 / 舆情 / 加息预期 / 资金流向。refresh=1 才打外部源。"""
    if refresh:
        pack = await asyncio.to_thread(macro_context.build_context)
        return pack
    latest = await asyncio.to_thread(macro_context.latest_snapshot, 80)
    if not latest:
        return {"status": "empty", "items": [], "note": "尚无沉淀，带 refresh=1 拉取"}
    return {"status": "ok", "items": latest}


@app.get("/api/macro/calendar")
async def get_macro_calendar(
    refresh: int = 0,
    start: str = "",
    end: str = "",
    limit: int = 200,
    verified_only: int = 0,
):
    """Government/macro schedule and releases, with source quality attached."""
    effective_start, effective_end = _macro_calendar_effective_window(start, end)
    sync_result: Dict[str, Any] | None = None
    if refresh:
        sync_result = await asyncio.to_thread(
            macro_context.sync_macro_calendars,
            start=effective_start,
            end=effective_end,
        )
    items = await asyncio.to_thread(
        macro_context.list_macro_events,
        start=effective_start,
        end=effective_end,
        limit=limit,
        decision_eligible=True if verified_only else None,
    )
    return {
        "status": "ok" if items else "empty",
        "items": items,
        "sync": sync_result,
        "window": {"start": effective_start, "end": effective_end},
        "note": "候选日历仅展示/验证；decision_eligible=true 才可进入 AI 或交易闸门。",
    }


def _data_quality_summary_sync() -> Dict[str, Any]:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        data_quality.ensure_schema(conn)
        rows = conn.execute(
            """SELECT source, kind,
                      COUNT(*) AS total,
                      SUM(accepted) AS accepted,
                      SUM(decision_eligible) AS decision_eligible,
                      SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) AS rejected,
                      ROUND(AVG(CASE WHEN latency_ms >= 0 THEN latency_ms END), 2) AS avg_latency_ms,
                      MAX(created_at) AS last_seen_at
               FROM data_quality_audit
               GROUP BY source, kind
               ORDER BY source, kind"""
        ).fetchall()
        quarantine = int(conn.execute("SELECT COUNT(*) FROM data_quarantine").fetchone()[0])
        return {"items": [dict(row) for row in rows], "quarantine_count": quarantine}
    finally:
        conn.close()


@app.get("/api/data-quality/policies")
async def get_data_quality_policies():
    return {
        "policy_version": "source-policy-v1",
        "fail_closed": True,
        "sources": data_quality.policies_as_dict(),
    }


@app.get("/api/data-quality/summary")
async def get_data_quality_summary():
    return await asyncio.to_thread(_data_quality_summary_sync)


def _news_source_status_sync() -> Dict[str, Any]:
    settings = news_sources.get_settings()
    enabled_sources = settings.get("sources") or {}
    cached = {
        "techflow": _TECHFLOW_CACHE.get("value"),
        "eastmoney": _EASTMONEY_CACHE.get("value"),
        "blockbeats": _BLOCKBEATS_CACHE.get("value"),
        "jin10": _JIN10_CACHE.get("value"),
    }
    source_patterns = {
        "financialjuice": ("WS:fj:%", "WS:financialjuice%"),
        "tree_news": ("%tree%", "%telegram%"),
        "techflow": ("%TechFlow%", "%深潮%"),
        "eastmoney": ("%EastMoney%", "%东方财富%"),
        "blockbeats": ("%BlockBeats%", "%律动%"),
        "jin10": ("%Jin10%", "%金十%"),
    }
    database: Dict[str, Dict[str, Any]] = {}
    conn = db.get_connection()
    try:
        for key, patterns in source_patterns.items():
            row = conn.execute(
                """SELECT COUNT(*) AS total, MAX(ts) AS last_ts,
                          MAX(timestamp) AS last_event_at
                   FROM raw_news WHERE source LIKE ? OR source LIKE ?""",
                patterns,
            ).fetchone()
            database[key] = {
                "stored": int(row[0] or 0),
                "last_event_ts": int(row[1]) if row[1] else None,
                "last_event_at": row[2],
            }
    finally:
        conn.close()

    now_epoch = int(time.time())
    result: Dict[str, Any] = {}
    for key in source_patterns:
        cache_value = cached.get(key) if isinstance(cached.get(key), dict) else {}
        db_value = database[key]
        last_ts = db_value["last_event_ts"]
        age = max(0, now_epoch - last_ts) if last_ts else None
        status = str(cache_value.get("status") or "unknown")
        if key in {"financialjuice", "tree_news"}:
            status = "empty" if age is None else ("ok" if age <= 300 else "stale")
        result[key] = {
            "enabled": bool(settings.get("enabled", True) and enabled_sources.get(key, True)),
            "status": status,
            "cached_items": len(cache_value.get("items") or []),
            "stored": db_value["stored"],
            "last_event_at": db_value["last_event_at"],
            "age_seconds": age,
            "error": str(cache_value.get("error") or "")[:200],
            "quality_status": "candidate",
            "decision_eligible": False,
        }
    return {
        "enabled": bool(settings.get("enabled", True)),
        "daily_target": int(settings.get("daily_target") or 0),
        "today_count": int(settings.get("today_count") or 0),
        "remaining": int(settings.get("remaining") or 0),
        "sources": result,
    }


@app.get("/api/data-sources/status")
async def get_data_sources_status():
    """Return safe provider configuration status without exposing credentials."""
    news_status = await asyncio.to_thread(_news_source_status_sync)
    return {
        "sources": {
            "okx": {
                "provider": "okx",
                "enabled": True,
                "configured": True,
                "source_type": "exchange_public_api",
                "decision_eligible": True,
            },
            "jin10": {
                **get_jin10_provider().health(),
                "decision_eligible": False,
                "note": "requires authorized key and explicit operator promotion",
            },
            "oanda": {
                **get_oanda_provider().health(),
                "decision_eligible": False,
            },
            "binance_paxg": {
                **get_binance_paxg_provider().health(),
                "decision_eligible": False,
            },
            "coingecko_paxg": {
                **get_coingecko_paxg_provider().health(),
                "decision_eligible": False,
            },
            "trading_economics": {
                **get_trading_economics_provider().health(),
                "decision_eligible": False,
            },
            "official_macro": {
                **get_official_macro_calendar_provider().health(),
                "policy_eligible": True,
            },
        },
        "news_ingest": news_status,
        "fail_closed": True,
    }


@app.get("/api/health")
async def health_check():
    """Liveness probe."""
    return {
        "status": "ok",
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "sse_clients": len(_SSE_QUEUES),
    }


# ---------------------------------------------------------------------------
# Trading System API — 精简接口供外部交易系统对接
# ---------------------------------------------------------------------------

@app.get("/api/trading/signals")
async def get_trading_signals(
    asset: str = "",
    action: str = "",
    limit: int = 20,
    settled: int = -1,  # -1=全部, 0=未结算, 1=已结算
):
    """
    交易系统对接接口 — 返回精简版信号数据。

    Query params:
      asset   — 品种过滤，如 BTC / XAU / WTI / ETH（留空=全部）
      action  — 方向过滤，BUY / SELL / HOLD（留空=全部）
      limit   — 返回条数，默认 20，最大 200
      settled — 结算状态，0=未结算 / 1=已结算 / -1=全部（默认）

    返回字段:
      signal_id, news_time, asset, action, score, reasoning, reasoning_path,
      market_category, event_strength, direct_catalyst, prediction_type,
      market_confirmation, entry_price, exit_price, is_correct, settled, created_at
    """
    limit = min(max(limit, 1), 200)

    where_clauses = []
    params: List[Any] = []

    if asset:
        where_clauses.append("UPPER(ad.target_asset) = ?")
        params.append(asset.upper())
    if action:
        where_clauses.append("UPPER(ad.suggested_action) = ?")
        params.append(action.upper())
    if settled >= 0:
        where_clauses.append("ad.settled = ?")
        params.append(settled)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                ad.id,
                rn.timestamp AS news_time,
                UPPER(ad.target_asset) AS asset,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.reasoning,
                ad.reasoning_path,
                ad.market_category,
                ad.event_strength,
                ad.direct_catalyst,
                ad.prediction_type,
                ad.market_confirmation,
                ad.entry_price,
                ad.exit_price,
                ad.is_correct,
                ad.settled,
                ad.created_at
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            WHERE {where_sql}
            ORDER BY ad.id DESC
            LIMIT ?
            """,
            params + [limit],
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


@app.get("/api/trading/latest")
async def get_latest_signals():
    """
    交易系统对接接口 — 每个品种的最新一条信号。

    返回: 按品种分组的最近信号，含 score/action/reasoning。
    覆盖品种: BTC, ETH, XAU, WTI, SOL
    """
    assets = ("BTC", "ETH", "XAU", "WTI", "SOL")
    result: Dict[str, Any] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for a in assets:
            cursor = await db.execute(
                """
                SELECT
                    ad.id,
                    rn.timestamp AS news_time,
                    UPPER(ad.target_asset) AS asset,
                    UPPER(ad.suggested_action) AS action,
                    ad.sentiment_score AS score,
                    ad.reasoning,
                    ad.reasoning_path,
                    ad.market_category,
                    ad.event_strength,
                    ad.direct_catalyst,
                    ad.prediction_type,
                    ad.market_confirmation,
                    ad.entry_price,
                    ad.created_at
                FROM ai_decisions ad
                INNER JOIN raw_news rn ON rn.id = ad.news_id
                WHERE UPPER(ad.target_asset) = ?
                ORDER BY ad.id DESC
                LIMIT 1
                """,
                (a,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            result[a] = dict(row) if row else None
    return result


def _authorize_trading_manager(token: str | None) -> None:
    expected = config.TRADING_MANAGER_TOKEN
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="invalid X-Trading-Token")


@app.post("/api/trading/manager/start")
async def start_trading_manager(x_trading_token: str | None = Header(default=None)):
    _authorize_trading_manager(x_trading_token)
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        raise HTTPException(status_code=400, detail="BINANCE_API_KEY / BINANCE_API_SECRET 未配置")
    return await asyncio.to_thread(_TRADING_MANAGER.start)


@app.post("/api/trading/manager/stop")
async def stop_trading_manager(x_trading_token: str | None = Header(default=None)):
    _authorize_trading_manager(x_trading_token)
    return await asyncio.to_thread(_TRADING_MANAGER.stop)


@app.get("/api/trading/manager/status")
async def trading_manager_status(x_trading_token: str | None = Header(default=None)):
    _authorize_trading_manager(x_trading_token)
    return _TRADING_MANAGER.status()


@app.get("/api/trading/manager/logs")
async def trading_manager_logs(tail: int = 200, x_trading_token: str | None = Header(default=None)):
    _authorize_trading_manager(x_trading_token)
    lines = await asyncio.to_thread(_TRADING_MANAGER.logs, tail)
    return {"lines": [line.rstrip("\r\n") for line in lines]}


# -- Replay endpoints (信号复盘看板 · 模拟盘) ----------------------------

_PASSED_TRADE_GATE_REASON = "证据充分，允许输出方向性结论"


def _valid_replay_entry_time(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def _replay_decision_eligible(item: Dict[str, Any]) -> bool:
    """Fail-closed production/research lane split for replay records."""
    action = str(item.get("action") or item.get("suggested_action") or "").upper()
    evidence_action = str(item.get("evidence_action") or "HOLD").upper()
    return (
        action in {"BUY", "SELL"}
        and str(item.get("quality_status") or "").lower() == "verified"
        and evidence_action == action
        and str(item.get("trade_gate_reason") or "") == _PASSED_TRADE_GATE_REASON
        and item.get("paper_trading_run_id") is not None
    )


def _effective_replay_verdict(item: Dict[str, Any]) -> str:
    """Return a compatible verdict, repairing legacy HOLD rows from signed PnL."""
    recorded = str(item.get("is_correct") or "").upper()
    if not int(item.get("settled") or 0):
        return recorded
    action = str(item.get("action") or item.get("suggested_action") or "").upper()
    pnl = item.get("forward_pnl")
    if action in {"BUY", "SELL"} and pnl is not None:
        return classify_directional_outcome(
            action, pnl, str(item.get("asset") or item.get("target_asset") or "")
        )
    return recorded


def _summarize_replay_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build production stats while retaining excluded research diagnostics."""
    production_settled: list[Dict[str, Any]] = []
    research_settled: list[Dict[str, Any]] = []
    tracking = 0
    legacy_untrackable = 0
    research_tracking = 0
    research_legacy_untrackable = 0

    for source in rows:
        item = dict(source)
        item["is_correct"] = _effective_replay_verdict(item)
        eligible = _replay_decision_eligible(item)
        if int(item.get("settled") or 0):
            (production_settled if eligible else research_settled).append(item)
        elif eligible:
            if _valid_replay_entry_time(item.get("entry_time")):
                tracking += 1
            else:
                legacy_untrackable += 1
        else:
            if _valid_replay_entry_time(item.get("entry_time")):
                research_tracking += 1
            else:
                research_legacy_untrackable += 1

    def settled_summary(items: list[Dict[str, Any]]) -> Dict[str, Any]:
        wins = sum(item["is_correct"] == "WIN" for item in items)
        losses = sum(item["is_correct"] == "LOSS" for item in items)
        pnls = [float(item["forward_pnl"]) for item in items if item.get("forward_pnl") is not None]
        mfes = [float(item["mfe_pct"]) for item in items if item.get("mfe_pct") is not None]
        maes = [float(item["mae_pct"]) for item in items if item.get("mae_pct") is not None]
        return {
            "settled": len(items),
            "wins": wins,
            "losses": losses,
            "holds": len(items) - wins - losses,
            "winrate": round(wins / (wins + losses), 4) if wins + losses else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else None,
            "avg_mfe": round(sum(mfes) / len(mfes), 6) if mfes else None,
            "avg_mae": round(sum(maes) / len(maes), 6) if maes else None,
            "best_trade": max(pnls) if pnls else None,
            "worst_trade": min(pnls) if pnls else None,
        }

    overall = settled_summary(production_settled)
    overall.update({
        "tracking": tracking,
        "legacy_untrackable": legacy_untrackable,
        "total": len(production_settled) + tracking,
    })
    research = settled_summary(research_settled)
    research.update({
        "tracking": research_tracking,
        "legacy_untrackable": research_legacy_untrackable,
        "total": len(research_settled) + research_tracking,
    })

    def grouped(field: str) -> list[Dict[str, Any]]:
        buckets: Dict[str, list[Dict[str, Any]]] = {}
        for item in production_settled:
            key = str(item.get(field) or "UNKNOWN").upper()
            buckets.setdefault(key, []).append(item)
        result = []
        for key, items in sorted(buckets.items()):
            summary = settled_summary(items)
            result.append({
                field: key,
                "total": summary["settled"],
                "wins": summary["wins"],
                "losses": summary["losses"],
                "holds": summary["holds"],
                "winrate": summary["winrate"],
                "avg_pnl": summary["avg_pnl"],
            })
        return result

    return {
        "overall": overall,
        "research_excluded": research,
        "by_asset": grouped("asset"),
        "by_action": grouped("action"),
    }

def _build_simulated_klines(
    *,
    entry_time_ts: int,
    entry_price: float,
    max_price: float | None,
    min_price: float | None,
    exit_price: float | None,
    asset: str,
    hours_before: int = 6,
    hours_after: int = 6,
) -> list[dict]:
    """
    Build a simulated 1-hour K-line series around the signal's entry_time.

    The series is anchored on ``entry_price`` and shaped so that the bar
    covering the entry moment has open == entry_price, and the highest /
    lowest extreme prices in the post-entry window match the recorded
    ``max_price`` / ``min_price`` (if available). This is a *paper-trading*
    visual reconstruction — it is NOT real market data, and the frontend
    surfaces a "模拟盘" badge next to the chart.
    """
    import math
    import random

    if entry_price <= 0:
        entry_price = 100.0

    asset_upper = (asset or "").upper()
    base_jitter = max(entry_price * 0.0035, 0.01)

    bars_before = max(hours_before, 1)
    bars_after = max(hours_after, 1)
    total = bars_before + 1 + bars_after  # +1 for the entry bar

    # Anchor on the entry hour (drop minutes/seconds)
    entry_dt = datetime.fromtimestamp(entry_time_ts, TZ_SHANGHAI).replace(
        minute=0, second=0, microsecond=0
    )
    start_dt = entry_dt - timedelta(hours=bars_before)

    rng = random.Random(int(entry_time_ts) ^ hash(asset_upper) & 0xFFFFFFFF)

    # Walk forward and let max/min guide the post-entry path
    bars: list[dict] = []
    last_close = entry_price
    # Pre-entry path: gentle mean-reversion around entry_price
    for i in range(bars_before):
        ts = int((start_dt + timedelta(hours=i)).timestamp())
        drift = rng.uniform(-base_jitter, base_jitter)
        o = last_close + drift * 0.3
        c = o + drift * 0.7
        h = max(o, c) + rng.uniform(0, base_jitter * 0.4)
        lo = min(o, c) - rng.uniform(0, base_jitter * 0.4)
        bars.append({
            "time": ts,
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(lo, 4),
            "close": round(c, 4),
            "volume": int(rng.uniform(2000, 12000)),
        })
        last_close = c

    # Entry bar (the trigger bar) — open at entry_price, bias close toward
    # the action direction so the chart visually "moves" on the signal.
    o = entry_price
    direction_bias = 1.0 if asset_upper in {"XAU", "GOLD"} else 1.0
    c = o + rng.uniform(-base_jitter * 0.6, base_jitter * 0.6) * direction_bias
    bars.append({
        "time": int(entry_dt.timestamp()),
        "open": round(o, 4),
        "high": round(max(o, c) + base_jitter * 0.3, 4),
        "low":  round(min(o, c) - base_jitter * 0.3, 4),
        "close": round(c, 4),
        "volume": int(rng.uniform(8000, 25000)),
    })
    last_close = c

    # Post-entry path: force the high/low extremes (if recorded) to appear.
    target_high = max_price if (max_price and max_price > 0) else None
    target_low = min_price if (min_price and min_price > 0) else None
    ext_pos = rng.randrange(1, bars_after + 1)  # where the high lives
    ext_low_pos = rng.randrange(1, bars_after + 1)  # where the low lives

    for i in range(1, bars_after + 1):
        ts = int((entry_dt + timedelta(hours=i)).timestamp())
        o = last_close
        # Determine this bar's high/low targets
        this_high = None
        this_low = None
        if target_high is not None and i == ext_pos:
            this_high = target_high
        if target_low is not None and i == ext_low_pos:
            this_low = target_low

        if this_high is not None or this_low is not None:
            # Build an extreme bar that respects both bounds.
            hi = this_high if this_high is not None else (o + base_jitter * 0.5)
            lo = this_low if this_low is not None else (o - base_jitter * 0.5)
            # Make sure high >= low
            if hi < lo:
                hi, lo = lo, hi
            c = (hi + lo) / 2 + rng.uniform(-base_jitter * 0.2, base_jitter * 0.2)
        else:
            drift = rng.uniform(-base_jitter, base_jitter)
            c = o + drift
            hi = max(o, c) + rng.uniform(0, base_jitter * 0.4)
            lo = min(o, c) - rng.uniform(0, base_jitter * 0.4)

        bars.append({
            "time": ts,
            "open": round(o, 4),
            "high": round(hi, 4),
            "low": round(lo, 4),
            "close": round(c, 4),
            "volume": int(rng.uniform(3000, 18000)),
        })
        last_close = c

    return bars


@app.get("/api/replay/signals")
async def get_replay_signals(
    asset: str = "",
    action: str = "",
    settled: int = 1,  # 0=未结算 1=已结算 -1=全部（默认只看已结算，便于复盘）
    limit: int = 100,
):
    """
    复盘看板列表接口：返回所有 EXECUTED 已结算的信号，含完整复盘字段。

    关键字段：entry_price, exit_price, max_price, min_price, max_price_time,
    min_price_time, mfe_pct, mae_pct, forward_pnl, event_strength,
    extra_models_consensus, reasoning, reasoning_path, is_correct。

    Query params:
      asset   — 品种过滤，如 BTC / XAU / WTI / ETH
      action  — 方向 BUY/SELL/HOLD
      settled — 0=未结算 1=已结算 -1=全部
      limit   — 返回条数，默认 100，最大 500
    """
    limit = min(max(limit, 1), 500)

    where_clauses = []
    params: List[Any] = []

    if asset:
        where_clauses.append("UPPER(ad.target_asset) = ?")
        params.append(asset.upper())
    if action:
        where_clauses.append("UPPER(ad.suggested_action) = ?")
        params.append(action.upper())
    if settled >= 0:
        where_clauses.append("ad.settled = ?")
        params.append(settled)
    # 模拟盘只看有 entry_price 的已结算信号
    where_clauses.append("ad.entry_price IS NOT NULL AND ad.entry_price > 0")

    where_sql = " AND ".join(where_clauses)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                ad.id,
                ad.news_id,
                rn.timestamp AS news_time,
                rn.source,
                rn.quality_status,
                rn.content AS news_text,
                UPPER(ad.target_asset) AS asset,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.reasoning,
                ad.reasoning_path,
                ad.market_category,
                ad.event_strength,
                ad.direct_catalyst,
                ad.prediction_type,
                ad.market_confirmation,
                ad.event_phase,
                ad.expected_horizon,
                ad.invalidation_condition,
                ad.decision_context,
                ad.entry_price,
                ad.exit_price,
                ad.max_price,
                ad.min_price,
                ad.max_price_time,
                ad.min_price_time,
                ad.entry_time,
                ad.exit_time,
                ad.exit_reason,
                ad.is_correct,
                ad.settled,
                ad.paper_trading_run_id,
                ad.evidence_action,
                ad.trade_gate_reason,
                ad.mfe_pct,
                ad.mae_pct,
                ad.forward_pnl,
                ad.mfe_time_mins,
                ad.analysis_type,
                ad.bullish_probability,
                ad.bearish_probability,
                ad.uncertainty,
                ad.bullish_force,
                ad.bearish_force,
                ad.impact_horizon,
                ad.impact_window,
                ad.entry_zone,
                ad.take_profit_pct,
                ad.stop_loss_pct,
                ad.exit_policy,
                ad.dual_side_candidate,
                ad.extra_models_consensus,
                ad.doubao_action,
                ad.doubao_reasoning,
                ad.cluster_size,
                ad.timeframe_match,
                ad.created_at,
                ad.strategy_id,
                ad.strategy_version_id,
                s.name AS strategy_name,
                sv.version AS strategy_version,
                sv.params AS strategy_params
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            LEFT JOIN strategies s ON s.id = ad.strategy_id
            LEFT JOIN strategy_versions sv ON sv.id = ad.strategy_version_id
            WHERE {where_sql}
            ORDER BY ad.id DESC
            LIMIT ?
            """,
            params + [limit],
        )
        rows = await cursor.fetchall()
        await cursor.close()

    # JSON-serialize the consensus field if present (it's stored as TEXT JSON)
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for json_field in ("extra_models_consensus", "strategy_params", "impact_window"):
            value = d.get(json_field)
            if value and isinstance(value, str):
                try:
                    d[json_field] = json.loads(value)
                except Exception:
                    pass
        # Ensure news_time / entry_time are ISO strings
        for k in ("news_time", "entry_time", "created_at"):
            v = d.get(k)
            if v is None:
                continue
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif not isinstance(v, str):
                d[k] = str(v)
        d["recorded_is_correct"] = d.get("is_correct") or ""
        d["is_correct"] = _effective_replay_verdict(d)
        d["decision_eligible"] = _replay_decision_eligible(d)
        if int(d.get("settled") or 0):
            d["tracking_quality"] = "SETTLED"
        elif not d["decision_eligible"]:
            # Keep historical/research rows visible for audit, but never show
            # them as formal pending positions or attach live account PnL.
            d["tracking_quality"] = "RESEARCH_EXCLUDED"
        elif _valid_replay_entry_time(d.get("entry_time")):
            d["tracking_quality"] = "TRACKABLE"
        else:
            d["tracking_quality"] = "LEGACY_UNTRACKABLE"
        out.append(d)
    return out


def _paper_metrics(signal: Dict[str, Any], current_price: Optional[float]) -> Dict[str, Any]:
    settled = bool(int(signal.get("settled") or 0))
    tracking_quality = str(signal.get("tracking_quality") or "")
    trackable = settled or tracking_quality == "TRACKABLE"
    if not trackable:
        # Preserve the row for audit, but do not turn a record with no reliable
        # start time into a live position or account PnL.
        current_price = None
    entry = float(signal.get("entry_price") or 0.0)
    action = str(signal.get("action") or "").upper()
    high = float(signal.get("max_price") or entry)
    low = float(signal.get("min_price") or entry)
    if current_price is not None and current_price > 0:
        high = max(high, current_price)
        low = min(low, current_price)
    pnl: Optional[float] = None
    mfe = 0.0
    mae = 0.0
    if entry > 0:
        if action == "BUY":
            if current_price is not None:
                pnl = (current_price - entry) / entry * 100
            mfe = (high - entry) / entry * 100
            mae = (entry - low) / entry * 100
        elif action == "SELL":
            if current_price is not None:
                pnl = (entry - current_price) / entry * 100
            mfe = (entry - low) / entry * 100
            mae = (high - entry) / entry * 100
    params = signal.get("strategy_params") or strategy_store.default_params()
    notional = float(params.get("notional_usdt") or 0.0)
    leverage = float(params.get("leverage") or 1.0)
    exit_reason = "tracking"
    if settled:
        verdict = str(signal.get("is_correct") or "").upper()
        exit_reason = {
            "WIN": "take_profit_or_horizon",
            "LOSS": "stop_loss_or_horizon",
            "HOLD": "horizon_without_edge",
            "BREAKEVEN": "breakeven",
        }.get(verdict, "settled")
    hold_minutes = None
    entry_time = signal.get("entry_time")
    if entry_time:
        try:
            started = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=TZ_SHANGHAI)
            end_value = signal.get("exit_time") if settled else None
            if end_value or not settled:
                if not settled:
                    ended = datetime.now(TZ_SHANGHAI)
                else:
                    ended = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
                    if ended.tzinfo is None:
                        ended = ended.replace(tzinfo=TZ_SHANGHAI)
                hold_minutes = max(0.0, (ended - started).total_seconds() / 60.0)
        except (TypeError, ValueError):
            hold_minutes = None
    return {
        **signal,
        "current_price": current_price,
        "current_pnl_pct": round(pnl, 4) if pnl is not None else None,
        "current_pnl_usdt": round(notional * leverage * pnl / 100, 4) if pnl is not None else None,
        "notional_usdt": round(notional, 4),
        "leverage": leverage,
        "margin_usdt": round(notional, 4),
        "gross_pnl_usdt": round(notional * leverage * pnl / 100, 4) if pnl is not None else None,
        "fees_usdt": 0.0,
        "slippage_usdt": 0.0,
        "hold_minutes": round(hold_minutes, 2) if hold_minutes is not None else None,
        "exit_reason": exit_reason,
        "live_mfe_pct": round(max(0.0, mfe), 4),
        "live_mae_pct": round(max(0.0, mae), 4),
        "pricing_status": "LIVE" if current_price is not None else "UNAVAILABLE",
        "paper_status": (
            "SETTLED" if settled
            else "TRACKING" if trackable
            else tracking_quality or "LEGACY_UNTRACKABLE"
        ),
    }


class PaperTradingBody(BaseModel):
    is_running: Optional[bool] = None
    tracks: Optional[List[str]] = None
    gate_enabled: Optional[bool] = None
    news_enabled: Optional[bool] = None
    news_daily_target: Optional[int] = None
    news_sources: Optional[Dict[str, bool]] = None


def _combined_replay_settings() -> Dict[str, Any]:
    trading = paper_trading.get_settings()
    news = news_sources.get_settings()
    return {**trading, "news_settings": news}


def _apply_replay_settings(body: PaperTradingBody) -> Dict[str, Any]:
    current = paper_trading.get_settings()
    next_running = current["is_running"] if body.is_running is None else bool(body.is_running)
    next_tracks = current["tracks"] if body.tracks is None else list(body.tracks)
    if body.is_running is not None or body.tracks is not None or body.gate_enabled is not None:
        paper_trading.set_settings(
            next_running,
            next_tracks,
            gate_enabled=body.gate_enabled,
        )
    if body.news_enabled is not None or body.news_daily_target is not None or body.news_sources is not None:
        news_sources.set_settings(
            enabled=body.news_enabled,
            daily_target=body.news_daily_target,
            sources=body.news_sources,
        )
    return _combined_replay_settings()


@app.get("/api/replay/settings")
async def get_replay_settings():
    return await asyncio.to_thread(_combined_replay_settings)


@app.post("/api/replay/settings")
async def update_replay_settings(body: PaperTradingBody):
    try:
        return await asyncio.to_thread(_apply_replay_settings, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class NewsSourceBody(BaseModel):
    enabled: Optional[bool] = None
    daily_target: Optional[int] = None
    sources: Optional[Dict[str, bool]] = None


@app.get("/api/news/sources")
async def get_news_sources():
    return await asyncio.to_thread(news_sources.get_settings)


@app.post("/api/news/sources")
async def update_news_sources(body: NewsSourceBody):
    return await asyncio.to_thread(
        news_sources.set_settings,
        enabled=body.enabled,
        daily_target=body.daily_target,
        sources=body.sources,
    )


def _replay_pairs(settings: Dict[str, Any], market: Dict[str, Any]) -> list[Dict[str, Any]]:
    item_map = {str(item.get("asset") or "").upper(): item for item in market.get("items", [])}
    symbols = {
        "crypto": (("BTC", "BTC/USDT"), ("ETH", "ETH/USDT"), ("SOL", "SOL/USDT")),
        "gold": (("XAU", "XAU/USD"),),
        "oil": (("WTI", "WTI/USD"),),
    }
    pairs: list[Dict[str, Any]] = []
    for track in settings.get("tracks", []):
        for asset, symbol in symbols.get(track, ()):
            item = item_map.get(asset)
            if item:
                quality = _market_item_quality(item, market)
                price = item.get("price")
                source = str(item.get("source") or "")
                quality_status = quality.quality_status
                quality_reason = quality.reason
                decision_eligible = quality.decision_eligible
            else:
                price = _get_current_price(asset)
                source = "OKX direct" if price else "unavailable"
                quality_status = "verified" if price else "unavailable"
                quality_reason = "verified_source" if price else "no_verified_quote"
                decision_eligible = bool(price)
            has_price = isinstance(price, (int, float)) and price > 0
            pairs.append({
                "asset": asset,
                "symbol": symbol,
                "track": track,
                "price": round(float(price), 6) if has_price else None,
                "change24h": round(float(item.get("change24h")), 4) if item and item.get("change24h") is not None else None,
                "source": source,
                "source_count": int(item.get("sourceCount") or 1) if item else (1 if price else 0),
                "status": "LIVE" if has_price and decision_eligible else ("OBSERVATION_ONLY" if has_price else "UNAVAILABLE"),
                "quality_status": quality_status,
                "quality_reason": quality_reason,
                "decision_eligible": decision_eligible,
            })
    return pairs


def _llm_performance_sync(model_id: str) -> Dict[str, Any]:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute(
            """SELECT ad.settled, ad.entry_time, ad.is_correct, ad.forward_pnl,
                      ad.mfe_pct, ad.mae_pct,
                      UPPER(ad.target_asset) AS asset,
                      UPPER(ad.suggested_action) AS action,
                      ad.paper_trading_run_id, ad.evidence_action,
                      ad.trade_gate_reason, rn.quality_status
               FROM ai_decisions ad
               INNER JOIN raw_news rn ON rn.id=ad.news_id
               WHERE ad.settled=1 AND ad.entry_price>0"""
        ).fetchall()]
        summary = _summarize_replay_rows(rows)
        overall = summary["overall"]
        settled = int(overall["settled"])
        wins = int(overall["wins"])
        losses = int(overall["losses"])
        decided = wins + losses
        accuracy = wins / decided * 100 if decided else None
        production_rows = [
            row for row in rows if _replay_decision_eligible(row)
        ]
        total_pnl = sum(float(row.get("forward_pnl") or 0.0) for row in production_rows)
        return {
            "model_id": model_id,
            "settled": settled,
            "wins": wins,
            "losses": losses,
            "holds": int(overall["holds"]),
            "accuracy_pct": round(accuracy, 2) if accuracy is not None else None,
            "avg_pnl_pct": overall["avg_pnl"],
            "total_pnl_pct": round(total_pnl, 4),
            "profitable": total_pnl > 0 if settled else None,
            "verdict": "样本不足，继续收集真实结算结果" if decided < 10 else ("历史结算表现为正" if total_pnl > 0 else "历史结算表现未盈利"),
            "method": "以该 LLM 已建立且真实结算的模拟交易计算，不使用模型自评",
            "research_excluded": summary["research_excluded"],
        }
    finally:
        conn.close()


def _run_feedback_sync(run_id: Optional[int]) -> Dict[str, Any]:
    if not run_id:
        return {"stage": "STOPPED", "message": "模拟操盘未启动", "evaluated": 0, "passed": 0, "rejected": 0, "latest": []}
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT started_at FROM paper_trading_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return {"stage": "STOPPED", "message": "运行记录不存在", "evaluated": 0, "passed": 0, "rejected": 0, "latest": []}
        rows = conn.execute(
            """SELECT ad.id, ad.target_asset,
                      UPPER(ad.suggested_action) AS suggested_action,
                      ad.evidence_confidence, ad.evidence_action,
                      ad.trade_gate_reason, ad.entry_price, ad.created_at,
                      ad.paper_trading_run_id, rn.quality_status
               FROM ai_decisions ad
               INNER JOIN raw_news rn ON rn.id=ad.news_id
               WHERE ad.created_at>=? AND ad.paper_trading_run_id=?
               ORDER BY ad.id DESC LIMIT 20""",
            (run["started_at"], run_id),
        ).fetchall()
        latest = [dict(row) for row in rows]
        for item in latest:
            item["decision_eligible"] = _replay_decision_eligible(item)
        production = [item for item in latest if item["decision_eligible"]]
        research = [item for item in latest if not item["decision_eligible"]]
        passed = sum(item["entry_price"] is not None for item in production)
        rejected = len(production) - passed
        stage = "POSITION_OPEN" if passed else ("SIGNAL_REJECTED" if rejected else "WAITING_NEWS")
        message = (
            "已自动建仓并跟踪实时盈亏" if passed
            else "已收到信号，但未通过 LLM 多证据与策略闸门" if rejected
            else "仅收到研究/未验证信号，已隔离且不计入模拟盘" if research
            else "运行正常，正在等待所选赛道的新新闻信号"
        )
        return {
            "stage": stage,
            "message": message,
            "evaluated": len(latest),
            "passed": passed,
            "rejected": rejected,
            "research_excluded": len(research),
            "latest": latest[:5],
        }
    finally:
        conn.close()


@app.get("/api/replay/positions")
async def get_replay_positions(limit: int = 200):
    signals = await get_replay_signals(settled=-1, limit=limit)
    assets = sorted({
        str(item["asset"]).upper()
        for item in signals
        if not item["settled"] and item.get("tracking_quality") == "TRACKABLE"
    })
    values = await asyncio.gather(
        *(asyncio.to_thread(_get_current_price, asset) for asset in assets),
        return_exceptions=True,
    )
    prices = {
        asset: value for asset, value in zip(assets, values)
        if isinstance(value, (int, float)) and value > 0
    }
    current = strategy_store.get_current_strategy()
    trading_settings = paper_trading.get_settings()
    market = await asyncio.to_thread(_cached_market_prices)
    pairs = await asyncio.to_thread(_replay_pairs, trading_settings, market)
    feedback = await asyncio.to_thread(_run_feedback_sync, trading_settings.get("active_run_id"))
    model_id = str((trading_settings.get("active_run") or {}).get("model_id") or config.get_selected_ai_model_id())
    llm_performance = await asyncio.to_thread(_llm_performance_sync, model_id)
    quick_evaluation = await asyncio.to_thread(quick_sim.snapshot)
    positions = [
        _paper_metrics(
            item,
            float(item["exit_price"]) if item["settled"] and item.get("exit_price") else prices.get(str(item["asset"]).upper()),
        )
        for item in signals
    ]
    production_positions = [item for item in positions if item.get("decision_eligible")]
    research_positions = [item for item in positions if not item.get("decision_eligible")]
    legacy_untrackable = [
        item for item in positions
        if not item["settled"] and item.get("tracking_quality") == "LEGACY_UNTRACKABLE"
    ]
    realized = sum(
        float(item.get("current_pnl_usdt") or 0.0)
        for item in production_positions if item["settled"]
    )
    tracking = [
        item for item in production_positions
        if not item["settled"] and item.get("tracking_quality") == "TRACKABLE"
    ]
    unrealized_values = [item.get("current_pnl_usdt") for item in tracking]
    unpriced = sum(value is None for value in unrealized_values)
    unrealized = sum(float(value or 0.0) for value in unrealized_values)
    used_margin = sum(
        float((item.get("strategy_params") or strategy_store.default_params()).get("notional_usdt") or 0.0)
        for item in tracking
    )
    fees = sum(float(item.get("fees_usdt") or 0.0) for item in production_positions)
    slippage = sum(float(item.get("slippage_usdt") or 0.0) for item in production_positions)
    initial = config.PAPER_INITIAL_EQUITY_USDT
    equity = initial + realized + unrealized
    return {
        "current_strategy": current,
        "trading_settings": trading_settings,
        "news_settings": news_sources.get_settings(),
        "pairs": pairs,
        "market_status": market.get("status", "unavailable"),
        "market_sources": market.get("sources", {}),
        "strategy_feedback": feedback,
        "llm_performance": llm_performance,
        "quick_sim": quick_evaluation,
        "positions": positions,
        "account": {
            "initial_equity_usdt": round(initial, 4),
            "realized_pnl_usdt": round(realized, 4),
            "unrealized_pnl_usdt": round(unrealized, 4) if not unpriced else None,
            "current_equity_usdt": round(equity, 4) if not unpriced else None,
            "used_margin_usdt": round(used_margin, 4),
            "fees_usdt": round(fees, 4),
            "slippage_usdt": round(slippage, 4),
            "trade_count": len(production_positions),
            "available_equity_usdt": round(equity - used_margin, 4) if not unpriced else None,
            "unpriced_positions": unpriced,
            "research_positions_excluded": len(research_positions),
            "legacy_untrackable_excluded": len(legacy_untrackable),
        },
        "updated_at_ms": int(time.time() * 1000),
        "is_paper_trading": True,
        "pricing_note": "实时浮盈亏按当前行情估算，未计手续费、滑点和资金费率。",
    }


@app.get("/api/replay/stats")
async def get_replay_stats(strategy_id: Optional[int] = None, version_id: Optional[int] = None):
    """当前策略模拟交易统计，胜率只使用有明确胜负的已结算交易。"""
    clauses = ["ad.entry_price IS NOT NULL", "ad.entry_price > 0"]
    filter_params: list[Any] = []
    if strategy_id is not None:
        clauses.append("ad.strategy_id = ?")
        filter_params.append(strategy_id)
    if version_id is not None:
        clauses.append("ad.strategy_version_id = ?")
        filter_params.append(version_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT ad.settled, ad.entry_time, ad.is_correct, ad.forward_pnl,
                   ad.mfe_pct, ad.mae_pct,
                   UPPER(ad.target_asset) AS asset,
                   UPPER(ad.suggested_action) AS action,
                   ad.paper_trading_run_id, ad.evidence_action,
                   ad.trade_gate_reason, rn.quality_status
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id=ad.news_id
            WHERE {' AND '.join(clauses)}
            """,
            filter_params,
        )
        rows = [dict(row) for row in await cur.fetchall()]
        await cur.close()

    summary = _summarize_replay_rows(rows)
    return {
        "overall": summary["overall"],
        "strategy_id": strategy_id,
        "strategy_version_id": version_id,
        "by_asset": summary["by_asset"],
        "by_action": summary["by_action"],
        "research_excluded": summary["research_excluded"],
        "is_paper_trading": True,
        "method": "verified_gate_passed_directional_pnl_v2",
    }


_REFLECTION_CRYPTO_ASSETS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "ADA", "AVAX", "PAXG",
})


def _reflection_context(value: Any) -> tuple[Dict[str, Any], str]:
    """Parse a stored decision snapshot without treating malformed data as evidence."""
    if isinstance(value, dict):
        return value, "ok"
    if not isinstance(value, str) or not value.strip():
        return {}, "missing"
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, "invalid"
    return (parsed, "ok") if isinstance(parsed, dict) else ({}, "invalid")


def _reflection_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _reflection_entry_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return parsed.astimezone(TZ_SHANGHAI)


def _reflection_asset_context(context: Dict[str, Any], asset: str) -> Dict[str, Any]:
    assets = context.get("assets")
    if not isinstance(assets, dict):
        return {}
    value = assets.get(asset.upper())
    return value if isinstance(value, dict) else {}


def _reflection_structure_observations(
    context: Dict[str, Any], asset: str,
) -> tuple[Optional[str], Optional[float]]:
    asset_context = _reflection_asset_context(context, asset)
    structure = asset_context.get("market_structure")
    if not isinstance(structure, dict):
        return None, None
    aggregate = structure.get("aggregate")
    aggregate = aggregate if isinstance(aggregate, dict) else structure
    trend = str(aggregate.get("trend") or "").lower() or None
    timeframes = structure.get("timeframes")
    if not isinstance(timeframes, dict):
        return trend, None
    for timeframe in ("15m", "1h"):
        frame = timeframes.get(timeframe)
        if not isinstance(frame, dict):
            continue
        indicators = frame.get("indicators")
        if not isinstance(indicators, dict):
            continue
        volume_ratio = _reflection_float(indicators.get("volume_ratio"))
        if volume_ratio is not None:
            return trend, volume_ratio
    return trend, None


def _build_replay_failure_diagnostics(
    losses: list[Dict[str, Any]], decided: list[Dict[str, Any]],
) -> tuple[list[Dict[str, Any]], float]:
    """Classify only observable failure associations; never manufacture causality."""
    definitions = {
        "data_gap": "数据缺失/快照不可用",
        "low_liquidity_session": "非核心时段/低流动性",
        "ignored_contradictory_factor": "方向与已知盘面因素冲突",
        "profit_not_locked": "已有浮盈但未及时锁定",
        "entry_or_stop_timing": "入场或止损时机不佳",
    }
    observations: Dict[str, Dict[int, list[str]]] = {
        key: {} for key in definitions
    }

    def record(key: str, item: Dict[str, Any], reason: str) -> None:
        signal_id = int(item.get("id") or 0)
        observations[key].setdefault(signal_id, []).append(reason)

    for item in losses:
        asset = str(item.get("asset") or "").upper()
        action = str(item.get("action") or "").upper()
        context, context_state = _reflection_context(item.get("decision_context"))

        data_reasons: list[str] = []
        if context_state != "ok":
            data_reasons.append(f"decision_context_{context_state}")
        else:
            snapshot_status = str(
                context.get("snapshot_status") or context.get("status") or ""
            ).lower()
            if snapshot_status and snapshot_status not in {"ok", "partial"}:
                data_reasons.append(f"snapshot_status={snapshot_status}")
            if context.get("market_context_eligible") is False:
                data_reasons.append("market_context_ineligible")
            if context.get("target_market_context_eligible") is False:
                data_reasons.append("target_asset_context_ineligible")
            macro = context.get("macro_context")
            if isinstance(macro, dict):
                ok_count = _reflection_float(macro.get("ok"))
                total_count = _reflection_float(macro.get("total"))
                if (
                    ok_count is not None and total_count is not None
                    and total_count > 0 and ok_count / total_count < 0.5
                ):
                    data_reasons.append(
                        f"macro_coverage={int(ok_count)}/{int(total_count)}"
                    )
        if data_reasons:
            record("data_gap", item, ", ".join(data_reasons))

        trend, volume_ratio = _reflection_structure_observations(context, asset)
        entry_time = _reflection_entry_time(item.get("entry_time"))
        liquidity_reasons: list[str] = []
        if volume_ratio is not None and volume_ratio < 0.5:
            liquidity_reasons.append(f"15m/1h volume_ratio={volume_ratio:.2f}")
        if (
            asset and asset not in _REFLECTION_CRYPTO_ASSETS
            and entry_time is not None and entry_time.weekday() >= 5
        ):
            liquidity_reasons.append("non_crypto_weekend")
        if liquidity_reasons:
            record("low_liquidity_session", item, ", ".join(liquidity_reasons))

        contradiction_reasons: list[str] = []
        confirmation = str(item.get("market_confirmation") or "").lower()
        if confirmation in {"negative", "rejected", "opposite"}:
            contradiction_reasons.append(f"market_confirmation={confirmation}")
        if context.get("timestamp_mismatch") is True:
            contradiction_reasons.append("snapshot_timestamp_mismatch")
        if (action == "BUY" and trend == "bearish") or (
            action == "SELL" and trend == "bullish"
        ):
            contradiction_reasons.append(f"{action.lower()}_against_{trend}_structure")
        if contradiction_reasons:
            record(
                "ignored_contradictory_factor", item,
                ", ".join(contradiction_reasons),
            )

        pnl = _reflection_float(item.get("forward_pnl"))
        mfe = _reflection_float(item.get("mfe_pct"))
        mae = _reflection_float(item.get("mae_pct"))
        if pnl is not None and pnl < 0 and mfe is not None and mfe > 0:
            record(
                "profit_not_locked", item,
                f"MFE={mfe:.3f}% then final PnL={pnl:.3f}%",
            )
        if mae is not None and mfe is not None and mae > mfe:
            record(
                "entry_or_stop_timing", item,
                f"MAE={mae:.3f}% > MFE={mfe:.3f}%",
            )

    observed_ids: set[int] = set()
    diagnostics: list[Dict[str, Any]] = []
    loss_count = len(losses)
    for key, label in definitions.items():
        rows = observations[key]
        observed_ids.update(rows)
        evidence = [
            f"#{signal_id}: {'; '.join(reasons)}"
            for signal_id, reasons in list(rows.items())[:3]
        ]
        diagnostics.append({
            "key": key,
            "label": label,
            "count": len(rows),
            "share": round(len(rows) / loss_count, 4) if loss_count else 0.0,
            "assessment": "observed" if rows else "not_observed",
            "evidence": evidence,
            "sample_signal_ids": list(rows)[:5],
        })

    ordered = sorted(decided, key=lambda item: int(item.get("id") or 0))
    minimum_window = 6
    regime: Dict[str, Any] = {
        "key": "regime_change_candidate",
        "label": "市场规则/状态变化（候选）",
        "count": 0,
        "share": 0.0,
        "assessment": "insufficient_sample",
        "evidence": ["至少需要前后各 6 笔正式结算交易才能检测胜率结构变化。"],
        "sample_signal_ids": [],
    }
    if len(ordered) >= minimum_window * 2:
        window = min(20, len(ordered) // 2)
        previous = ordered[-2 * window:-window]
        recent = ordered[-window:]
        previous_wr = sum(
            str(item.get("is_correct") or "").upper() == "WIN" for item in previous
        ) / window
        recent_wr = sum(
            str(item.get("is_correct") or "").upper() == "WIN" for item in recent
        ) / window
        delta = recent_wr - previous_wr
        recent_loss_ids = [
            int(item.get("id") or 0) for item in recent
            if str(item.get("is_correct") or "").upper() == "LOSS"
        ]
        regime.update({
            "count": len(recent_loss_ids) if delta <= -0.25 else 0,
            "share": round(len(recent_loss_ids) / loss_count, 4) if loss_count and delta <= -0.25 else 0.0,
            "assessment": "candidate" if delta <= -0.25 else "not_observed",
            "evidence": [
                f"前窗胜率 {previous_wr:.1%}，近窗胜率 {recent_wr:.1%}，变化 {delta:+.1%}。"
            ],
            "sample_signal_ids": recent_loss_ids[:5] if delta <= -0.25 else [],
        })
    diagnostics.append(regime)
    diagnostics.append({
        "key": "unobservable_external_information",
        "label": "系统外不可观测信息",
        "count": 0,
        "share": None,
        "assessment": "not_assessable",
        "evidence": ["现有数据不能证明某笔亏损由系统外信息造成；仅可标记未知，不能倒推原因。"],
        "sample_signal_ids": [],
    })
    coverage = round(len(observed_ids) / loss_count, 4) if loss_count else 0.0
    return diagnostics, coverage


@app.get("/api/replay/reflection")
async def get_replay_reflection(limit: int = 500):
    """Return a deterministic post-trade review for model/self-reflection UI.

    This endpoint intentionally reports observable patterns only.  It does not
    ask an LLM to invent causes; a later strategy version may feed this compact
    summary into a human-approved prompt.
    """
    signals = await get_replay_signals(settled=1, limit=min(max(limit, 20), 500))
    production_signals = [item for item in signals if item.get("decision_eligible")]
    research_signals = [item for item in signals if not item.get("decision_eligible")]
    decided = [
        item for item in production_signals
        if str(item.get("is_correct") or "").upper() in {"WIN", "LOSS"}
    ]
    research_decided = [
        item for item in research_signals
        if str(item.get("is_correct") or "").upper() in {"WIN", "LOSS"}
    ]
    wins = [item for item in decided if str(item.get("is_correct")).upper() == "WIN"]
    losses = [item for item in decided if str(item.get("is_correct")).upper() == "LOSS"]
    failure_diagnostics, observable_loss_coverage = _build_replay_failure_diagnostics(
        losses, decided,
    )
    diagnostic_counts = {
        item["key"]: item["count"] for item in failure_diagnostics
    }

    def grouped(field: str) -> list[Dict[str, Any]]:
        buckets: Dict[str, list[Dict[str, Any]]] = {}
        for item in decided:
            key = str(item.get(field) or "unknown")
            buckets.setdefault(key, []).append(item)
        result = []
        for key, rows in sorted(buckets.items(), key=lambda pair: -len(pair[1])):
            local_wins = sum(str(row.get("is_correct")).upper() == "WIN" for row in rows)
            pnls = [float(row.get("forward_pnl")) for row in rows if row.get("forward_pnl") is not None]
            result.append({
                "key": key,
                "sample": len(rows),
                "wins": local_wins,
                "losses": len(rows) - local_wins,
                "winrate": round(local_wins / len(rows), 4) if rows else 0.0,
                "avg_forward_pnl": round(sum(pnls) / len(pnls), 4) if pnls else None,
            })
        return result

    patterns = {
        "uncertain_direction_loss": sum(
            float(item.get("uncertainty") or 0) >= 0.4 for item in losses
        ),
        "conflict_event_loss": sum(
            str(item.get("analysis_type") or "").lower() == "conflict" for item in losses
        ),
        "trend_event_loss": sum(
            str(item.get("analysis_type") or "").lower() == "trend" for item in losses
        ),
        "adverse_excursion_dominated": sum(
            float(item.get("mae_pct") or 0) > float(item.get("mfe_pct") or 0) for item in losses
        ),
        "market_not_confirmed": sum(
            str(item.get("market_confirmation") or "").lower() == "negative" for item in losses
        ),
        "data_gap": diagnostic_counts["data_gap"],
        "low_liquidity_session": diagnostic_counts["low_liquidity_session"],
        "ignored_contradictory_factor": diagnostic_counts["ignored_contradictory_factor"],
        "profit_not_locked": diagnostic_counts["profit_not_locked"],
        "entry_or_stop_timing": diagnostic_counts["entry_or_stop_timing"],
        "regime_change_candidate": diagnostic_counts["regime_change_candidate"],
    }
    recommendations: list[str] = []
    if patterns["data_gap"]:
        recommendations.append("数据覆盖不足的亏损单不用于自动放大仓位；先补齐目标盘面与宏观快照。")
    if patterns["low_liquidity_session"]:
        recommendations.append("低流动性或非核心时段降低仓位，并要求成交量恢复后再确认方向。")
    if patterns["ignored_contradictory_factor"]:
        recommendations.append("新闻方向与盘面结构冲突时暂停单边交易，等待结构和资金流同向。")
    if patterns["profit_not_locked"]:
        recommendations.append("出现过浮盈后转亏的事件启用分批止盈或移动保护，按影响窗口及时了结。")
    if patterns["regime_change_candidate"]:
        recommendations.append("近期胜率相对历史窗口显著下降，冻结自动加仓并重新验证阈值。")
    if patterns["uncertain_direction_loss"]:
        recommendations.append("不确定性较高的信号先进入双向模拟/人工复核，不直接放大实盘仓位。")
    if patterns["adverse_excursion_dominated"]:
        recommendations.append("亏损单的 MAE 多于 MFE，优先复核入场节点与止损距离。")
    if patterns["market_not_confirmed"]:
        recommendations.append("盘面未确认的新闻降低阈值通过率，等待结构或资金流确认。")
    if not recommendations:
        recommendations.append("当前样本未发现稳定失败模式，继续积累可结算样本后再调参。")
    return {
        "sample": len(decided),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(decided), 4) if decided else 0.0,
        "by_analysis_type": grouped("analysis_type"),
        "by_horizon": grouped("impact_horizon"),
        "by_asset": grouped("asset"),
        "failure_patterns": patterns,
        "failure_diagnostics": failure_diagnostics,
        "observable_loss_coverage": observable_loss_coverage,
        "recommendations": recommendations,
        "research_excluded": {
            "sample": len(research_decided),
            "wins": sum(str(item.get("is_correct")).upper() == "WIN" for item in research_decided),
            "losses": sum(str(item.get("is_correct")).upper() == "LOSS" for item in research_decided),
        },
        "method": "deterministic_evidence_backed_replay_review_v3",
    }


@app.get("/api/replay/signal/{signal_id}/kline")
async def get_replay_signal_kline(signal_id: int):
    """根据信号 ID 生成模拟盘 K 线（用 entry/max/min 反推 1 小时 K 线序列）。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                ad.id, ad.entry_price, ad.exit_price, ad.max_price,
                ad.min_price, ad.max_price_time, ad.min_price_time,
                ad.target_asset, ad.entry_time, ad.exit_time,
                ad.suggested_action, ad.invalidation_condition, ad.settled,
                sv.params AS strategy_params
            FROM ai_decisions ad
            LEFT JOIN strategy_versions sv ON sv.id = ad.strategy_version_id
            WHERE ad.id = ?
            """,
            (signal_id,),
        )
        row = await cur.fetchone()
        await cur.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")

    d = dict(row)
    if not d.get("entry_price"):
        raise HTTPException(status_code=400, detail="signal has no entry_price")

    # Convert entry_time to epoch seconds
    entry_time = d.get("entry_time")
    if isinstance(entry_time, str) and entry_time.strip():
        try:
            entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="signal has no reliable entry_time"
            ) from exc
    elif not isinstance(entry_time, datetime):
        raise HTTPException(status_code=400, detail="signal has no reliable entry_time")
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=TZ_SHANGHAI)
    entry_ts = int(entry_time.timestamp())

    klines = _build_simulated_klines(
        entry_time_ts=entry_ts,
        entry_price=float(d["entry_price"]),
        max_price=d.get("max_price"),
        min_price=d.get("min_price"),
        exit_price=d.get("exit_price"),
        asset=str(d.get("target_asset") or ""),
    )
    action = str(d.get("suggested_action") or "HOLD").upper()
    try:
        params = json.loads(d.get("strategy_params") or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    trailing = float(params.get("trailing_callback_rate") or 0.0)
    entry_price = float(d["entry_price"])
    max_price = float(d["max_price"]) if d.get("max_price") else entry_price
    min_price = float(d["min_price"]) if d.get("min_price") else entry_price
    if action == "BUY":
        stop_loss = max_price * (1 - trailing / 100) if trailing > 0 else entry_price * (1 - 0.8 / 100)
        take_profit = max_price
    elif action == "SELL":
        stop_loss = min_price * (1 + trailing / 100) if trailing > 0 else entry_price * (1 + 0.8 / 100)
        take_profit = min_price
    else:
        stop_loss = None
        take_profit = None
    markers = [{
        "time": entry_ts,
        "position": "belowBar" if action == "BUY" else "aboveBar",
        "color": "#2ebd85" if action == "BUY" else "#f6465d",
        "shape": "arrowUp" if action == "BUY" else "arrowDown",
        "text": f"{action} {entry_price}",
        "kind": "entry",
    }]
    if d.get("exit_price"):
        exit_marker_ts = None
        exit_time = d.get("exit_time")
        if isinstance(exit_time, str) and exit_time.strip():
            try:
                parsed_exit = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                if parsed_exit.tzinfo is None:
                    parsed_exit = parsed_exit.replace(tzinfo=TZ_SHANGHAI)
                exit_marker_ts = int(parsed_exit.timestamp())
            except ValueError:
                exit_marker_ts = None
        if exit_marker_ts is None:
            extreme_ts = (
                d.get("max_price_time") if action == "BUY" else d.get("min_price_time")
            ) or d.get("max_price_time") or d.get("min_price_time") or 0
            exit_marker_ts = max(entry_ts, int(extreme_ts or 0))
        markers.append({
            "time": exit_marker_ts,
            "position": "aboveBar" if action == "BUY" else "belowBar",
            "color": "#9aa3af",
            "shape": "circle",
            "text": f"EXIT {float(d['exit_price']):.4f}",
            "kind": "exit",
        })
    return {
        "signal_id": signal_id,
        "asset": d.get("target_asset"),
        "action": action,
        "entry_price": entry_price,
        "entry_time": entry_ts,
        "exit_price": d.get("exit_price"),
        "stop_loss": round(stop_loss, 6) if stop_loss else None,
        "take_profit": round(take_profit, 6) if take_profit else None,
        "trailing_callback_rate": trailing,
        "invalidation_condition": d.get("invalidation_condition") or "",
        "settled": int(d.get("settled") or 0),
        "is_paper_trading": True,
        "markers": markers,
        "klines": klines,
    }


# -- Strategy library + honest backtest ------------------------------------

class StrategyCreateBody(BaseModel):
    name: str
    description: str = ""
    params: Dict[str, Any] = {}
    ai_prompt: str = ""
    analysis_structure: Dict[str, Any] = {}


class StrategyVersionBody(BaseModel):
    params: Dict[str, Any]
    note: str = ""
    source: str = "manual"
    ai_prompt: str = ""
    analysis_structure: Dict[str, Any] = {}


class StrategyActivateBody(BaseModel):
    version_id: Optional[int] = None


class BacktestBody(BaseModel):
    strategy_id: int
    version_id: Optional[int] = None
    params: Optional[Dict[str, Any]] = None
    asset: str = ""
    limit: int = 500
    persist: bool = True
    ai_prompt: Optional[str] = None
    analysis_structure: Optional[Dict[str, Any]] = None


class OptimizeBody(BaseModel):
    strategy_id: int
    version_id: Optional[int] = None
    use_llm: bool = True


def _strategy_or_404(strategy_id: int) -> Dict[str, Any]:
    try:
        return strategy_store.get_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/strategies")
async def list_strategies(include_archived: bool = False):
    return await asyncio.to_thread(strategy_store.list_strategies, include_archived)


@app.get("/api/strategies/current")
async def get_current_strategy():
    return await asyncio.to_thread(strategy_store.get_current_strategy)


@app.post("/api/strategies/{strategy_id}/activate")
async def activate_strategy(strategy_id: int, body: StrategyActivateBody):
    try:
        return await asyncio.to_thread(
            strategy_store.set_current_strategy, strategy_id, body.version_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/strategies")
async def create_strategy(body: StrategyCreateBody):
    try:
        return await asyncio.to_thread(
            strategy_store.create_strategy, body.name, body.description, body.params,
            ai_prompt=body.ai_prompt, analysis_structure=body.analysis_structure,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/strategies/meta/defaults")
async def strategy_defaults():
    return {
        "defaults": strategy_store.default_params(),
        "bounds": {
            "signal_threshold": {"min": 0.3, "max": 0.9},
            "notional_usdt": {"min": 5, "max": 500},
            "leverage": {"min": 1, "max": 20},
            "trailing_callback_rate": {"min": 0.1, "max": 5},
            "holding_horizon_minutes": {"min": 15, "max": 10080},
            "short_horizon_minutes": {"min": 5, "max": 240},
            "medium_horizon_minutes": {"min": 240, "max": 4320},
            "long_horizon_minutes": {"min": 4320, "max": 43200},
            "take_profit_pct": {"min": 0, "max": 20},
            "stop_loss_pct": {"min": 0, "max": 20},
            "uncertainty_threshold": {"min": 0.4, "max": 0.8},
        },
        "assets": sorted(a for a in strategy_store.ALLOWED_ASSETS if a),
        "strengths": [item for item in strategy_store.ALLOWED_STRENGTH if item],
    }


@app.get("/api/strategies/{strategy_id}")
async def get_strategy(strategy_id: int):
    return _strategy_or_404(strategy_id)


@app.post("/api/strategies/{strategy_id}/versions")
async def add_strategy_version(strategy_id: int, body: StrategyVersionBody):
    _strategy_or_404(strategy_id)
    try:
        return await asyncio.to_thread(
            strategy_store.add_version, strategy_id, body.params,
            source=body.source, note=body.note,
            ai_prompt=body.ai_prompt, analysis_structure=body.analysis_structure,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/strategies/{strategy_id}/archive")
async def archive_strategy(strategy_id: int):
    try:
        await asyncio.to_thread(strategy_store.archive_strategy, strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"archived": True, "id": strategy_id}


@app.post("/api/backtest/run")
async def run_strategy_backtest(body: BacktestBody):
    strategy = _strategy_or_404(body.strategy_id)
    if body.version_id:
        try:
            version = strategy_store.get_version(body.version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if version["strategy_id"] != body.strategy_id:
            raise HTTPException(status_code=400, detail="version does not belong to strategy")
    else:
        latest = strategy.get("latest_version")
        if not latest:
            raise HTTPException(status_code=400, detail="strategy has no version")
        version = latest
    params = strategy_store.clamp_params(body.params or version["params"])
    report = await asyncio.to_thread(backtest.run_backtest, params, asset=body.asset, limit=body.limit)
    report["ai_prompt"] = body.ai_prompt if body.ai_prompt is not None else version.get("ai_prompt", "")
    report["analysis_structure"] = (
        body.analysis_structure if body.analysis_structure is not None
        else version.get("analysis_structure", {})
    )
    run_id = None
    if body.persist:
        run_id = await asyncio.to_thread(
            strategy_store.save_backtest_run, body.strategy_id, version["id"], params, report,
        )
    return {
        "run_id": run_id,
        "strategy": {"id": strategy["id"], "name": strategy["name"], "slug": strategy["slug"]},
        "version": {"id": version["id"], "version": version["version"]},
        "report": report,
    }


@app.get("/api/backtest/runs")
async def list_backtest_runs(strategy_id: int = 0, limit: int = 20):
    sid = strategy_id or None
    return await asyncio.to_thread(strategy_store.list_backtest_runs, sid, limit)


def _llm_optimize_sync(params: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    response = _agent_llm_client().chat.completions.create(
        model=_agent_llm_model(), temperature=0.0, max_tokens=700,
        extra_body=config.AIPING_EXTRA_BODY, response_format=({"type": "json_object"} if config.AIPING_JSON_MODE else None),
        messages=[
            {"role": "system", "content": (
                "你是本平台策略参数优化器。只输出 JSON。"
                "只能引用已给出的回测报告数字，禁止编造胜率、行情或未发生的成交。"
                "样本不足或回撤过大时必须更保守。"
                "字段：signal_threshold, notional_usdt, leverage, trailing_callback_rate, "
                "holding_horizon_minutes, min_event_strength, asset_filter, require_direct_catalyst, note。"
            )},
            {"role": "user", "content": json.dumps({"current": params, "report": {
                k: report[k] for k in (
                    "sample_size", "taken", "skipped", "wins", "losses", "winrate",
                    "total_pnl_usdt", "max_drawdown_usdt", "profit_factor",
                    "insufficient_sample", "by_asset", "params",
                ) if k in report
            }}, ensure_ascii=False)},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    note = str(result.pop("note", "") or "LLM 基于已结算回测报告给出的下一组参数")
    return {"params": strategy_store.clamp_params(result), "note": note, "mode": "llm"}


@app.post("/api/backtest/optimize")
async def optimize_strategy(body: OptimizeBody):
    strategy = _strategy_or_404(body.strategy_id)
    if body.version_id:
        version = strategy_store.get_version(body.version_id)
    else:
        version = strategy.get("latest_version")
        if not version:
            raise HTTPException(status_code=400, detail="strategy has no version")
    report = await asyncio.to_thread(backtest.run_backtest, version["params"])
    mode = "rules"
    note = ""
    if body.use_llm:
        try:
            llm = await asyncio.to_thread(_llm_optimize_sync, version["params"], report)
            next_params, note, mode = llm["params"], llm["note"], "llm"
        except Exception:
            fallback = backtest.optimize_params(version["params"], report)
            next_params, note, mode = fallback["params"], "；".join(fallback["notes"]), "rules"
    else:
        fallback = backtest.optimize_params(version["params"], report)
        next_params, note, mode = fallback["params"], "；".join(fallback["notes"]), "rules"
    saved = await asyncio.to_thread(
        strategy_store.add_version, body.strategy_id, next_params,
        source="llm" if mode == "llm" else "backtest", note=note,
        ai_prompt=version.get("ai_prompt", ""),
        analysis_structure=version.get("analysis_structure", {}),
    )
    return {
        "mode": mode,
        "baseline_report": {
            "sample_size": report["sample_size"],
            "taken": report["taken"],
            "winrate": report["winrate"],
            "total_pnl_usdt": report["total_pnl_usdt"],
            "max_drawdown_usdt": report["max_drawdown_usdt"],
            "insufficient_sample": report["insufficient_sample"],
        },
        "version": saved,
        "note": note,
    }


@app.get("/api/dashboard/overview")
async def dashboard_overview():
    """对齐 dashboard/app.py 的离线复盘能力：信号统计 + 健康检查 + 最近已结算单。"""
    health = {
        "status": "ok",
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "sse_clients": len(_SSE_QUEUES),
    }
    stats = await get_replay_stats()
    recent = await get_replay_signals(settled=1, limit=12)
    strategies = await asyncio.to_thread(strategy_store.list_strategies, False)
    runs = await asyncio.to_thread(strategy_store.list_backtest_runs, None, 5)
    return {
        "health": health,
        "replay": stats,
        "recent_signals": recent,
        "strategies": strategies,
        "recent_runs": runs,
        "streamlit": {"url": "http://127.0.0.1:8501", "source": "dashboard/app.py"},
        "exports": {"signals_xlsx": "/api/export/signals"},
    }


# -- Entry point -----------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    _host = os.getenv("API_HOST", "0.0.0.0")
    _port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run(app, host=_host, port=_port, reload=False, log_level="info")
