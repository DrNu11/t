#!/usr/bin/env python3
"""新闻之外的宏观/盘面数据源。

四层（全部公开接口，失败标 unavailable，禁止造数）：
  structure  盘面结构 — 资金费率、基差、持仓量、多空比
  sentiment  舆情情绪 — Crypto Fear & Greed
  fed        美联储预期 — NY Fed EFFR + 联邦基金期货隐含利率
  flow       资金流向 — 永续主动买卖比 / 可选 ETF 净流入
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import config
import db

HttpGet = Callable[[str, int], Any]

_TIMEOUT = 8
_SYMBOLS = (
    ("BTC", "BTCUSDT"),
    ("ETH", "ETHUSDT"),
    ("XAU", "XAUUSDT"),
)
_BINANCE_FAPI = "https://fapi.binance.com"
_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"
_NYFED_EFFR = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"
_YAHOO_ZQ = "https://query1.finance.yahoo.com/v8/finance/chart/ZQ=F?interval=1d&range=5d"


def _now() -> int:
    return int(time.time())


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TridentAgentMVP/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _metric(
    category: str,
    source: str,
    metric_key: str,
    value: Optional[float],
    *,
    unit: str = "",
    label: str = "",
    status: str = "ok",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = extra or {}
    if label:
        payload["label"] = label
    return {
        "category": category,
        "source": source,
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": status if value is not None or status != "ok" else "unavailable",
        "payload": payload,
    }


def _fail(category: str, source: str, metric_key: str, note: str) -> Dict[str, Any]:
    return _metric(category, source, metric_key, None, status="unavailable", extra={"note": note})


def fetch_structure(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset, symbol in _SYMBOLS:
        try:
            prem = get(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex?symbol={symbol}", _TIMEOUT)
            last = _num((prem or {}).get("markPrice"))
            index = _num((prem or {}).get("indexPrice"))
            funding = _num((prem or {}).get("lastFundingRate"))
            basis_bps = None
            if last and index and index != 0:
                basis_bps = (last / index - 1.0) * 10000.0
            rows.append(_metric("structure", "binance_fapi", f"{asset}.mark", last, unit="USDT", label=asset))
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.funding",
                funding * 100 if funding is not None else None,
                unit="%", label=asset,
            ))
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.basis_bps",
                basis_bps, unit="bp", label=asset,
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.premium", f"{type(exc).__name__}"))
        try:
            oi = get(f"{_BINANCE_FAPI}/fapi/v1/openInterest?symbol={symbol}", _TIMEOUT)
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.oi",
                _num((oi or {}).get("openInterest")), unit="contracts", label=asset,
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.oi", f"{type(exc).__name__}"))
        try:
            ratio = get(
                f"{_BINANCE_FAPI}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=1",
                _TIMEOUT,
            )
            item = ratio[0] if isinstance(ratio, list) and ratio else {}
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.ls_ratio",
                _num(item.get("longShortRatio")), unit="x", label=asset,
                extra={"longAccount": _num(item.get("longAccount")), "shortAccount": _num(item.get("shortAccount"))},
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.ls_ratio", f"{type(exc).__name__}"))
    return rows


def fetch_sentiment(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    try:
        body = get(_FNG_URL, _TIMEOUT)
        item = ((body or {}).get("data") or [None])[0] or {}
        value = _num(item.get("value"))
        classification = str(item.get("value_classification") or "")
        return [_metric(
            "sentiment", "alternative.me", "crypto_fng",
            value, unit="index",
            extra={"classification": classification, "attribution": "alternative.me"},
        )]
    except Exception as exc:
        return [_fail("sentiment", "alternative.me", "crypto_fng", f"{type(exc).__name__}")]


def fetch_fed(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    effr = None
    try:
        body = get(_NYFED_EFFR, _TIMEOUT)
        ref = ((body or {}).get("refRates") or [None])[0] or {}
        effr = _num(ref.get("percentRate"))
        rows.append(_metric(
            "fed", "nyfed", "effr",
            effr, unit="%",
            extra={"effectiveDate": ref.get("effectiveDate"), "type": ref.get("type")},
        ))
    except Exception as exc:
        rows.append(_fail("fed", "nyfed", "effr", f"{type(exc).__name__}"))

    implied = None
    try:
        body = get(_YAHOO_ZQ, _TIMEOUT)
        result = (((body or {}).get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        implied_price = _num(meta.get("regularMarketPrice"))
        if implied_price is None:
            closes = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
            for item in reversed(closes):
                implied_price = _num(item)
                if implied_price is not None:
                    break
        if implied_price is not None:
            implied = 100.0 - implied_price
        rows.append(_metric(
            "fed", "yahoo_zq", "implied_ff",
            implied, unit="%",
            extra={"futures_price": implied_price, "symbol": "ZQ=F"},
        ))
    except Exception as exc:
        rows.append(_fail("fed", "yahoo_zq", "implied_ff", f"{type(exc).__name__}"))

    if effr is not None and implied is not None:
        spread_bp = (implied - effr) * 100.0
        if spread_bp >= 12.5:
            next_move = "hike"
        elif spread_bp <= -12.5:
            next_move = "cut"
        else:
            next_move = "hold"
        rows.append(_metric(
            "fed", "derived", "next_move_bp",
            spread_bp, unit="bp",
            extra={"next_move": next_move, "method": "ZQ implied − EFFR；非 CME FedWatch 官方概率"},
        ))
    else:
        rows.append(_fail("fed", "derived", "next_move_bp", "缺少 EFFR 或隐含利率"))
    return rows


def fetch_flow(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset, symbol in _SYMBOLS:
        try:
            body = get(
                f"{_BINANCE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period=1h&limit=1",
                _TIMEOUT,
            )
            item = body[0] if isinstance(body, list) and body else {}
            buy = _num(item.get("buyVol"))
            sell = _num(item.get("sellVol"))
            ratio = _num(item.get("buySellRatio"))
            net = None
            if buy is not None and sell is not None:
                net = buy - sell
            rows.append(_metric(
                "flow", "binance_taker", f"{asset}.taker_buy_sell",
                ratio, unit="x", label=asset,
                extra={"buyVol": buy, "sellVol": sell, "netVol": net},
            ))
        except Exception as exc:
            rows.append(_fail("flow", "binance_taker", f"{asset}.taker_buy_sell", f"{type(exc).__name__}"))

    etf_url = str(getattr(config, "SOSOVALUE_ETF_URL", "") or "").strip()
    if not etf_url:
        rows.append(_fail("flow", "sosovalue", "btc_etf_net", "未配置 SOSOVALUE_ETF_URL"))
        return rows
    try:
        body = get(etf_url, _TIMEOUT)
        items = body
        if isinstance(body, dict):
            items = body.get("data") or body.get("list") or body.get("items") or []
        if not isinstance(items, list):
            items = []
        picked = None
        for item in items:
            if not isinstance(item, dict):
                continue
            blob = json.dumps(item, ensure_ascii=False).upper()
            if "BTC" in blob or "BITCOIN" in blob:
                picked = item
                break
        if picked is None and items:
            picked = items[0] if isinstance(items[0], dict) else None
        value = None
        if isinstance(picked, dict):
            for key in ("netInflow", "dailyNetInflow", "netflow", "netFlow", "value"):
                value = _num(picked.get(key))
                if value is not None:
                    break
        rows.append(_metric(
            "flow", "sosovalue", "btc_etf_net",
            value, unit="usd",
            extra={"raw_keys": sorted(picked.keys())[:12] if isinstance(picked, dict) else []},
        ))
    except Exception as exc:
        rows.append(_fail("flow", "sosovalue", "btc_etf_net", f"{type(exc).__name__}"))
    return rows


def collect_layers(get: HttpGet = _default_get) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "structure": fetch_structure(get),
        "sentiment": fetch_sentiment(get),
        "fed": fetch_fed(get),
        "flow": fetch_flow(get),
    }


def flatten(layers: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key in ("structure", "sentiment", "fed", "flow"):
        rows.extend(layers.get(key) or [])
    return rows


def persist_layers(
    layers: Dict[str, List[Dict[str, Any]]],
    *,
    ts: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    rows = flatten(layers)
    if not rows:
        return 0
    stamp = int(ts or _now())
    owned = connection is None
    conn = connection or db.get_connection()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_snapshots'"
        ).fetchone()
        if exists is None:
            return 0
        payload = [
            (
                stamp,
                item["category"],
                item["source"],
                item["metric_key"],
                item.get("value"),
                item.get("unit") or "",
                item.get("status") or "unavailable",
                json.dumps(item.get("payload") or {}, ensure_ascii=False),
            )
            for item in rows
        ]
        cursor = conn.executemany(
            """INSERT INTO macro_snapshots(ts, category, source, metric_key, value, unit, status, payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            payload,
        )
        if owned:
            conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        if owned:
            conn.close()


def _fmt(item: Dict[str, Any]) -> str:
    if item.get("status") != "ok" or item.get("value") is None:
        note = (item.get("payload") or {}).get("note") or "unavailable"
        return f"{item['metric_key']}=N/A ({note})"
    value = item["value"]
    unit = item.get("unit") or ""
    extra = item.get("payload") or {}
    if item["metric_key"] == "crypto_fng":
        return f"Fear&Greed {value:.0f} {extra.get('classification') or ''}".strip()
    if item["metric_key"] == "next_move_bp":
        return f"期货隐含相对 EFFR {value:+.1f}bp → {extra.get('next_move')}"
    if isinstance(value, float) and abs(value) >= 1000:
        text = f"{value:,.0f}"
    else:
        text = f"{value:.4g}"
    return f"{item['metric_key']}={text}{unit}"


def render_prompt_block(layers: Dict[str, List[Dict[str, Any]]]) -> str:
    titles = {
        "structure": "盘面结构",
        "sentiment": "舆情情绪",
        "fed": "美联储加息预期",
        "flow": "资金流向",
    }
    lines = ["── 非新闻数据层 ──"]
    any_ok = False
    for key, title in titles.items():
        items = layers.get(key) or []
        ok_items = [item for item in items if item.get("status") == "ok" and item.get("value") is not None]
        if not ok_items:
            lines.append(f"{title}: 当前不可用，不得臆造")
            continue
        any_ok = True
        shown = ok_items[:8]
        lines.append(f"{title}: " + " | ".join(_fmt(item) for item in shown))
    if not any_ok:
        return "── 非新闻数据层 ──\n全部外部源不可用，仅基于新闻与已有快照判断，禁止补造宏观数字。"
    lines.append("以上仅作交叉验证；缺失字段标 N/A，不得补造。最终仍输出 14 字段 JSON。")
    return "\n".join(lines)


def build_context(get: HttpGet = _default_get, persist: bool = True) -> Dict[str, Any]:
    layers = collect_layers(get)
    written = persist_layers(layers) if persist else 0
    ok = sum(1 for item in flatten(layers) if item.get("status") == "ok" and item.get("value") is not None)
    total = len(flatten(layers))
    status = "ok" if ok == total and total else ("partial" if ok else "unavailable")
    return {
        "status": status,
        "ok": ok,
        "total": total,
        "written": written,
        "layers": layers,
        "summary": render_prompt_block(layers),
        "ts": _now(),
    }


def latest_snapshot(limit: int = 80, connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    owned = connection is None
    conn = connection or db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_snapshots'"
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            """SELECT ts, category, source, metric_key, value, unit, status, payload
               FROM macro_snapshots
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            out.append(item)
        return out
    finally:
        if owned:
            conn.close()
