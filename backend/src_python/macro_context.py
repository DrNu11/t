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
import data_quality
from providers.jin10 import get_jin10_provider

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
        # Transparent directional proxy for UI/AI ranking.  It is not a CME
        # FedWatch probability and is labelled as such everywhere.
        hike_probability_proxy = max(0.0, min(1.0, 0.5 + spread_bp / 50.0))
        rows.append(_metric(
            "fed", "derived", "next_move_bp",
            spread_bp, unit="bp",
            extra={
                "next_move": next_move,
                "hike_probability_proxy": round(hike_probability_proxy, 4),
                "method": "ZQ implied − EFFR；非 CME FedWatch 官方概率",
            },
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
    layers = {
        "structure": fetch_structure(get),
        "sentiment": fetch_sentiment(get),
        "fed": fetch_fed(get),
        "flow": fetch_flow(get),
    }
    return apply_quality(layers)


def apply_quality(
    layers: Dict[str, List[Dict[str, Any]]],
    *,
    observed_at: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Attach source-governance results without changing the raw metric value."""
    stamp = int(observed_at or _now())
    governed: Dict[str, List[Dict[str, Any]]] = {}
    for category, values in layers.items():
        governed[category] = []
        for index, original in enumerate(values or []):
            item = dict(original)
            decision = data_quality.assess(
                source=str(item.get("source") or ""),
                kind=str(item.get("category") or category),
                event_id=f"{item.get('metric_key') or index}:{stamp}",
                published_at=stamp,
                payload={"value": item.get("value"), **(item.get("payload") or {})},
                observed_at=stamp,
            )
            available = item.get("status") == "ok" and item.get("value") is not None
            item["quality_status"] = decision.quality_status if available else "unavailable"
            item["quality_reason"] = decision.reason if available else "upstream_unavailable"
            item["decision_eligible"] = bool(decision.decision_eligible and available)
            item["authority_tier"] = decision.authority_tier
            item["quality_event_id"] = decision.event_id
            governed[category].append(item)
    return governed


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
    stamp = int(ts or _now())
    rows = flatten(layers)
    if rows and any("quality_event_id" not in item for item in rows):
        layers = apply_quality(layers, observed_at=stamp)
        rows = flatten(layers)
    if not rows:
        return 0
    owned = connection is None
    conn = connection or db.get_connection()
    try:
        data_quality.ensure_schema(conn)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_snapshots'"
        ).fetchone()
        if exists is None:
            return 0
        payload = []
        for item in rows:
            quality = data_quality.QualityDecision(
                source=data_quality.canonical_source(str(item.get("source") or "")),
                kind=str(item.get("category") or "macro"),
                event_id=str(item.get("quality_event_id") or f"{item.get('metric_key')}:{stamp}"),
                accepted=item.get("quality_status") != "blocked",
                decision_eligible=bool(item.get("decision_eligible")),
                authority_tier=str(item.get("authority_tier") or "unknown"),
                reason=str(item.get("quality_reason") or ""),
                quality_status=str(item.get("quality_status") or "unknown"),
            )
            audit_payload = {"value": item.get("value"), **(item.get("payload") or {})}
            # Availability failures belong in macro_snapshots as an explicit
            # gap, not in the source-quality audit as if they were observations.
            if item.get("status") == "ok" and item.get("value") is not None:
                if not data_quality.observation_already_recorded(conn, quality, audit_payload):
                    data_quality.record(
                        conn, quality, published_at=stamp, payload=audit_payload,
                        metadata={"metric_key": item.get("metric_key")},
                        quarantine=quality.quality_status == "blocked",
                        observed_at=stamp,
                    )
            stored_payload = {
                **(item.get("payload") or {}),
                "quality_status": item.get("quality_status"),
                "quality_reason": item.get("quality_reason"),
                "decision_eligible": bool(item.get("decision_eligible")),
                "authority_tier": item.get("authority_tier"),
            }
            payload.append((
                stamp,
                item["category"],
                item["source"],
                item["metric_key"],
                item.get("value"),
                item.get("unit") or "",
                item.get("status") or "unavailable",
                json.dumps(stored_payload, ensure_ascii=False),
            ))
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
        proxy = extra.get("hike_probability_proxy")
        suffix = f"，加息概率代理={float(proxy):.0%}" if proxy is not None else ""
        return f"期货隐含相对 EFFR {value:+.1f}bp → {extra.get('next_move')}{suffix}"
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
        ok_items = [
            item for item in items
            if item.get("status") == "ok"
            and item.get("value") is not None
            and item.get("decision_eligible") is True
        ]
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
    ok = sum(
        1 for item in flatten(layers)
        if item.get("status") == "ok"
        and item.get("value") is not None
        and item.get("decision_eligible") is True
    )
    total = len(flatten(layers))
    candidates = sum(1 for item in flatten(layers) if item.get("quality_status") == "candidate")
    status = "ok" if ok == total and total else ("partial" if ok else "unavailable")
    return {
        "status": status,
        "ok": ok,
        "total": total,
        "candidate_count": candidates,
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


def persist_macro_events(
    events: List[Any],
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """Upsert canonical calendar/releases while retaining their quality state."""
    if not events:
        return 0
    owned = connection is None
    conn = connection or db.get_connection()
    written = 0
    try:
        data_quality.ensure_schema(conn)
        for event in events:
            raw = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
            source = str(raw.get("source") or "")
            external_id = str(raw.get("event_id") or "").strip()
            indicator = str(raw.get("indicator") or "").strip()
            published_at = str(raw.get("published_at") or "").strip()
            if not external_id or not indicator:
                quality = data_quality.assess(
                    source=source, kind="calendar", event_id=external_id,
                    published_at=published_at, payload=raw,
                )
                quality = data_quality.QualityDecision(
                    quality.source, quality.kind, quality.event_id, False, False,
                    quality.authority_tier, "missing_macro_identity", "blocked", quality.latency_ms,
                )
                data_quality.record(conn, quality, published_at=published_at, payload=raw, quarantine=True)
                continue
            event_id = f"{data_quality.canonical_source(source)}:{external_id}"
            kind = "macro" if raw.get("actual") is not None else "calendar"
            quality = data_quality.assess(
                source=source, kind=kind, event_id=event_id,
                published_at=published_at,
                payload={"value": raw.get("actual"), **raw},
            )
            if not data_quality.observation_already_recorded(conn, quality, raw):
                data_quality.record(
                    conn, quality, published_at=published_at, payload=raw,
                    metadata={"indicator": indicator, "external_event_id": external_id},
                    quarantine=not quality.decision_eligible,
                )
            if not quality.accepted:
                continue
            status = "released" if raw.get("actual") is not None else "scheduled"
            stored_payload = {
                **raw,
                "external_event_id": external_id,
                "quality_status": quality.quality_status,
                "quality_reason": quality.reason,
                "decision_eligible": quality.decision_eligible,
            }
            conn.execute(
                """INSERT INTO macro_events(
                       event_id, source, indicator, published_at, country, importance,
                       actual, consensus, previous, unit, time_period, status,
                       quality_status, quality_reason, decision_eligible, payload, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(event_id) DO UPDATE SET
                       source=excluded.source,
                       indicator=excluded.indicator,
                       published_at=excluded.published_at,
                       country=excluded.country,
                       importance=excluded.importance,
                       actual=excluded.actual,
                       consensus=excluded.consensus,
                       previous=excluded.previous,
                       unit=excluded.unit,
                       time_period=excluded.time_period,
                       status=excluded.status,
                       quality_status=excluded.quality_status,
                       quality_reason=excluded.quality_reason,
                       decision_eligible=excluded.decision_eligible,
                       payload=excluded.payload,
                       updated_at=datetime('now')""",
                (
                    event_id, source, indicator, published_at,
                    str(raw.get("country") or ""), raw.get("impact"),
                    raw.get("actual"), raw.get("consensus"), raw.get("previous"),
                    str(raw.get("unit") or ""), str(raw.get("time_period") or ""), status,
                    quality.quality_status, quality.reason, int(quality.decision_eligible),
                    json.dumps(stored_payload, ensure_ascii=False, default=str),
                ),
            )
            written += 1
        if owned:
            conn.commit()
        return written
    finally:
        if owned:
            conn.close()


def sync_jin10_calendar() -> Dict[str, Any]:
    """Fetch the explicitly configured/authorised Jin10 calendar once."""
    provider = get_jin10_provider()
    health = provider.health()
    if not provider.configured:
        return {"status": "disabled", "written": 0, **health, "reason": "jin10_not_configured"}
    if not provider.calendar_url:
        return {"status": "disabled", "written": 0, **health, "reason": "calendar_url_not_configured"}
    try:
        events = provider.fetch_macro(category=getattr(config, "JIN10_CALENDAR_CATEGORY", "cj"))
        written = persist_macro_events(events)
        return {"status": "ok", "fetched": len(events), "written": written, **health}
    except Exception as exc:
        return {
            "status": "unavailable", "fetched": 0, "written": 0, **health,
            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def list_macro_events(
    *,
    start: str = "",
    end: str = "",
    limit: int = 200,
    decision_eligible: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if start:
        clauses.append("published_at >= ?")
        params.append(start)
    if end:
        clauses.append("published_at <= ?")
        params.append(end)
    if decision_eligible is not None:
        clauses.append("decision_eligible = ?")
        params.append(int(decision_eligible))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT event_id, source, indicator, published_at, country, importance,
                       actual, consensus, previous, unit, time_period, status, notified,
                       quality_status, quality_reason, decision_eligible, created_at, updated_at
                FROM macro_events {where}
                ORDER BY published_at ASC, importance DESC LIMIT ?""",
            params,
        ).fetchall()
        return [{**dict(row), "decision_eligible": bool(row["decision_eligible"])} for row in rows]
    finally:
        conn.close()
