"""Authorized Jin10 Open Data adapter.

This adapter uses the documented HTTP endpoints only.  It is disabled unless
the operator explicitly enables it and supplies an authorized ``secret-key``.
No key is included in logs, fixtures, or source control.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

import requests

from .contracts import MacroEvent, MacroProvider, MarketProvider, MarketQuote, NewsEvent, NewsProvider


_TAG_RE = re.compile(r"<[^>]+>")
_ASSET_HINTS = (
    (("黄金", "金价", "XAU", "GOLD"), "XAU"),
    (("比特币", "BTC", "Bitcoin"), "BTC"),
    (("以太坊", "ETH", "Ethereum"), "ETH"),
    (("美元", "USD", "美联储", "非农", "CPI"), "USD"),
)


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    return " ".join(_TAG_RE.sub(" ", text).split())


def _number(value: Any) -> Optional[float]:
    if value in (None, "", "--", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _asset_hints(text: str, tags: Any = None) -> List[str]:
    haystack = f"{text} {tags or ''}".upper()
    result: List[str] = []
    for needles, asset in _ASSET_HINTS:
        if any(str(needle).upper() in haystack for needle in needles):
            result.append(asset)
    return result


def _title_from_body(body: str, category: str) -> str:
    if not body:
        return category or "金十快讯"
    first = re.split(r"[。！？!?\n]", body, maxsplit=1)[0].strip()
    return (first or body)[:120]


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data", [])
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("list") or rows.get("items") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


class Jin10Provider(NewsProvider, MarketProvider, MacroProvider):
    source = "金十"

    def __init__(
        self,
        *,
        enabled: bool = False,
        api_key: str = "",
        flash_url: str = "https://open-data-api.jin10.com/data-api/flash",
        quote_url: str = "https://open-data-api.jin10.com/data-api/quotes",
        calendar_url: str = "",
        timeout: float = 5.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.api_key = api_key.strip()
        self.flash_url = flash_url.strip()
        self.quote_url = quote_url.strip()
        self.calendar_url = calendar_url.strip()
        self.timeout = max(1.0, float(timeout))

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            return {"data": []}
        response = requests.get(
            url,
            params={key: value for key, value in params.items() if value not in (None, "")},
            headers={"secret-key": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"data": []}

    def fetch_news(self, *, category: str = "1,2,3,4,5", last_id: Optional[str] = None, **_: Any) -> List[NewsEvent]:
        payload = self._get(self.flash_url, {"category": category, "last_id": last_id})
        events: List[NewsEvent] = []
        for row in _rows(payload):
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            body = _clean_text(data.get("content") or row.get("content"))
            if not body:
                continue
            labels = row.get("classify") or data.get("classify") or []
            if isinstance(labels, list):
                label_text = ",".join(_clean_text(item) for item in labels if item)
            else:
                label_text = _clean_text(labels)
            category_name = label_text or "财经快讯"
            events.append(NewsEvent(
                event_id=str(row.get("id") or body),
                published_at=str(row.get("time") or ""),
                source=self.source,
                title=_title_from_body(body, category_name),
                body=body,
                category=category_name,
                impact_assets=_asset_hints(body, row.get("qh_tags")),
                importance=int(row["important"]) if str(row.get("important", "")).isdigit() else None,
            ))
        return events

    def fetch_quotes(self, *, asset_type: str = "GOODS", codes: str = "XAUUSD", **_: Any) -> List[MarketQuote]:
        payload = self._get(self.quote_url, {"type": asset_type, "codes": codes})
        quotes: List[MarketQuote] = []
        for row in _rows(payload):
            symbol = str(row.get("c") or "").strip()
            price = _number(row.get("p"))
            if not symbol or price is None or price <= 0:
                continue
            asset = "XAU" if symbol.upper() == "XAUUSD" else symbol.upper()
            previous = _number(row.get("hc"))
            change_pct = ((price / previous) - 1.0) * 100.0 if previous and previous > 0 else None
            quotes.append(MarketQuote(
                symbol=symbol,
                asset=asset,
                price=price,
                source=self.source,
                event_ts=int(_number(row.get("t")) or 0) or None,
                change_pct=change_pct,
                volume=_number(row.get("v") or row.get("volume")),
                bid=_number(row.get("b")),
                ask=_number(row.get("a")),
                high=_number(row.get("h")),
                low=_number(row.get("l")),
                previous_close=previous,
            ))
        return quotes

    def fetch_macro(self, *, category: str = "cj", **_: Any) -> List[MacroEvent]:
        if not self.calendar_url:
            return []
        payload = self._get(self.calendar_url, {"category": category})
        events: List[MacroEvent] = []
        for row in _rows(payload):
            indicator = _clean_text(row.get("name") or row.get("event_content") or row.get("title"))
            if not indicator:
                continue
            events.append(MacroEvent(
                event_id=str(row.get("id") or indicator),
                indicator=indicator,
                source=self.source,
                published_at=str(row.get("pub_time") or row.get("event_time") or ""),
                actual=_number(row.get("actual")),
                previous=_number(row.get("previous")),
                consensus=_number(row.get("consensus")),
                unit=_clean_text(row.get("unit")),
                country=_clean_text(row.get("country") or row.get("region")),
                impact=int(row["star"]) if str(row.get("star", "")).isdigit() else None,
                time_period=_clean_text(row.get("time_period") or row.get("full_time_period")),
            ))
        return events

    def health(self) -> Dict[str, Any]:
        return {"provider": "jin10", "enabled": self.enabled, "configured": self.configured}


_PROVIDER: Optional[Jin10Provider] = None


def get_jin10_provider() -> Jin10Provider:
    global _PROVIDER
    if _PROVIDER is None:
        import config
        _PROVIDER = Jin10Provider(
            enabled=getattr(config, "JIN10_ENABLED", False),
            api_key=getattr(config, "JIN10_API_KEY", ""),
            flash_url=getattr(config, "JIN10_FLASH_URL", "https://open-data-api.jin10.com/data-api/flash"),
            quote_url=getattr(config, "JIN10_QUOTE_URL", "https://open-data-api.jin10.com/data-api/quotes"),
            calendar_url=getattr(config, "JIN10_CALENDAR_URL", ""),
            timeout=getattr(config, "JIN10_REQUEST_TIMEOUT", 5.0),
        )
    return _PROVIDER
