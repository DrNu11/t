"""Optional Trading Economics calendar adapter.

Trading Economics is used as a normalized calendar supplement, not as a
replacement for the official publisher.  The adapter stays disabled until an
operator supplies an explicit calendar URL and credentials.  This prevents a
guest/demo endpoint or an undocumented mirror from entering the event bus.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import math
import re
from typing import Any, Dict, List, Optional

import requests

from .contracts import MacroEvent, MacroProvider


_MISSING = {"", "-", "--", "N/A", "NA", "NULL", "NONE"}


def _clean(value: Any) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(str(value or ""))).split())


def _number(value: Any) -> Optional[float]:
    if value is None or str(value).strip().upper() in _MISSING:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> str:
    if value in (None, ""):
        raise ValueError("calendar event is missing Date")
    text = str(value).strip()
    if text.isdigit() and len(text) in (10, 13):
        epoch = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or payload.get("results") or payload.get("items") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _impact(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"low", "1"}:
        return 1
    if text in {"medium", "med", "2"}:
        return 3
    if text in {"high", "3"}:
        return 5
    try:
        return max(0, min(5, int(float(value))))
    except (TypeError, ValueError):
        return None


class TradingEconomicsProvider(MacroProvider):
    source = "trading_economics"

    def __init__(
        self,
        *,
        enabled: bool = False,
        credentials: str = "",
        calendar_url: str = "",
        countries: str = "United States",
        timeout: float = 8.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.credentials = str(credentials or "").strip()
        self.calendar_url = str(calendar_url or "").strip()
        self.countries = str(countries or "United States").strip()
        self.timeout = max(1.0, float(timeout))

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.credentials and self.calendar_url)

    def _get(self, *, start: str = "", end: str = "") -> Any:
        if not self.configured:
            return []
        params: Dict[str, Any] = {"c": self.credentials}
        if start:
            params["d1"] = start
        if end:
            params["d2"] = end
        response = requests.get(
            self.calendar_url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "TridentAgentMVP/1.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def fetch_macro(self, *, start: str = "", end: str = "", **_: Any) -> List[MacroEvent]:
        events: List[MacroEvent] = []
        for row in _rows(self._get(start=start, end=end)):
            indicator = _clean(row.get("Event") or row.get("event") or row.get("Category") or row.get("category"))
            if not indicator:
                continue
            published_at = _timestamp(row.get("Date") or row.get("date"))
            identity = str(row.get("CalendarId") or row.get("calendar_id") or "").strip()
            if not identity:
                identity = hashlib.sha256(
                    f"{indicator}|{published_at}|{row.get('Country') or row.get('country')}".encode("utf-8")
                ).hexdigest()[:24]
            events.append(MacroEvent(
                event_id=identity,
                indicator=indicator,
                source=self.source,
                published_at=published_at,
                actual=_number(row.get("Actual") if "Actual" in row else row.get("actual")),
                previous=_number(row.get("Previous") if "Previous" in row else row.get("previous")),
                consensus=_number(
                    row.get("Forecast") if "Forecast" in row else row.get("forecast")
                ),
                unit=_clean(row.get("Unit") or row.get("unit")),
                country=_clean(row.get("Country") or row.get("country")),
                impact=_impact(row.get("Importance") if "Importance" in row else row.get("importance")),
                time_period=_clean(row.get("Reference") or row.get("reference")),
            ))
        return events

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.source,
            "enabled": self.enabled,
            "configured": self.configured,
            "countries": self.countries,
            "source_type": "commercial_calendar_aggregator",
        }


_PROVIDER: Optional[TradingEconomicsProvider] = None


def get_trading_economics_provider() -> TradingEconomicsProvider:
    global _PROVIDER
    if _PROVIDER is None:
        import config

        _PROVIDER = TradingEconomicsProvider(
            enabled=getattr(config, "TRADING_ECONOMICS_ENABLED", False),
            credentials=getattr(config, "TRADING_ECONOMICS_CREDENTIALS", ""),
            calendar_url=getattr(config, "TRADING_ECONOMICS_CALENDAR_URL", ""),
            countries=getattr(config, "TRADING_ECONOMICS_COUNTRIES", "United States"),
            timeout=getattr(config, "TRADING_ECONOMICS_REQUEST_TIMEOUT", 8.0),
        )
    return _PROVIDER

