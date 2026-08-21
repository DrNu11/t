"""看板 K 线源：加密走币安公共行情，原油走 Yahoo CL=F。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

BINANCE_KLINE_SYMBOLS: Dict[str, str] = {
    "XAU": "XAUUSDT",
    "GOLD": "XAUUSDT",
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT",
    "LTC": "LTCUSDT",
    "LINK": "LINKUSDT",
}
YAHOO_KLINE_SYMBOLS: Dict[str, str] = {
    "WTI": "CL=F",
    "OIL": "CL=F",
    "CL": "CL=F",
    "XAU": "GC=F",
    "GOLD": "GC=F",
}

BINANCE_KLINE_HOSTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://fapi.binance.com/fapi/v1/klines",
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def normalize_asset(asset_code: str) -> str:
    return str(asset_code or "").upper().strip()


def resolve_kline_route(asset_code: str) -> Tuple[str, str]:
    """返回 (venue, symbol)。WTI/OIL 只走 Yahoo，禁止把 CL=F 打给币安。"""
    code = normalize_asset(asset_code)
    if code in ("WTI", "OIL", "CL"):
        return "yahoo", YAHOO_KLINE_SYMBOLS[code]
    if code in BINANCE_KLINE_SYMBOLS:
        return "binance", BINANCE_KLINE_SYMBOLS[code]
    if code in YAHOO_KLINE_SYMBOLS:
        return "yahoo", YAHOO_KLINE_SYMBOLS[code]
    if code.endswith("USDT") or code.endswith("USDC"):
        return "binance", code
    return "binance", f"{code}USDT"


def fetch_binance_klines(
    symbol: str,
    start_ts: int,
    end_ts: int,
    *,
    get=requests.get,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    last_error = "币安行情不可用"
    for url in BINANCE_KLINE_HOSTS:
        try:
            resp = get(
                url,
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": start_ts,
                    "endTime": end_ts,
                    "limit": 1000,
                },
                proxies=proxies,
                timeout=timeout,
                headers={"User-Agent": "TridentDashboard/1.0", "Accept": "application/json"},
            )
            status = getattr(resp, "status_code", 200)
            if status == 451:
                last_error = f"{url} 返回 451（地区限制）"
                continue
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            raw = resp.json()
            if not raw or not isinstance(raw, list):
                last_error = f"{url} 无K线"
                continue
            rows = []
            for item in raw:
                rows.append({
                    "datetime": int(item[0]),
                    "Open": item[1],
                    "High": item[2],
                    "Low": item[3],
                    "Close": item[4],
                    "Volume": item[5],
                })
            if rows:
                return rows, url
        except Exception as exc:
            last_error = f"{url}: {type(exc).__name__}: {exc}"
    return None, last_error


def fetch_yahoo_klines(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    get=requests.get,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp()) + 60
    span_hours = max((period2 - period1) / 3600.0, 1.0)
    interval = "1m" if span_hours <= 7 * 24 else "5m" if span_hours <= 60 * 24 else "1h"
    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        resp = get(
            url,
            params={
                "period1": period1,
                "period2": period2,
                "interval": interval,
                "includePrePost": "false",
                "events": "div,splits",
            },
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        result = (((resp.json() or {}).get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None, f"Yahoo {symbol} 无K线"
        stamps = result.get("timestamp") or []
        quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        rows = []
        for idx, stamp in enumerate(stamps):
            close = closes[idx] if idx < len(closes) else None
            if close is None:
                continue
            rows.append({
                "datetime": int(stamp) * 1000,
                "Open": opens[idx] if idx < len(opens) and opens[idx] is not None else close,
                "High": highs[idx] if idx < len(highs) and highs[idx] is not None else close,
                "Low": lows[idx] if idx < len(lows) and lows[idx] is not None else close,
                "Close": close,
                "Volume": volumes[idx] if idx < len(volumes) and volumes[idx] is not None else 0,
            })
        if not rows:
            return None, f"Yahoo {symbol} 无有效OHLC"
        return rows, f"yahoo:{symbol}:{interval}"
    except Exception as exc:
        return None, f"Yahoo {symbol}: {type(exc).__name__}: {exc}"
