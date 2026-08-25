"""Public CoinGecko PAXG price cross-check.

CoinGecko is an aggregator and may be rate-limited.  It is retained for
comparison/audit only and can never become the canonical quote or a trading
decision input.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List

import requests

from .contracts import MarketProvider, MarketQuote


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch_ms(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number if number >= 10_000_000_000 else number * 1000)


class CoinGeckoPaxgProvider(MarketProvider):
    """Fetch a public PAXG/USD aggregate price without credentials."""

    source = "coingecko_paxg"

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "https://api.coingecko.com/api/v3",
        coin_id: str = "pax-gold",
        timeout: float = 5.0,
        cache_seconds: float = 60.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.coin_id = str(coin_id or "pax-gold").strip()
        self.timeout = max(1.0, float(timeout))
        # CoinGecko's keyless endpoint is deliberately a slow audit source;
        # never poll it at the UI/market-cache cadence and trigger a 429.
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._last_fetch_monotonic = 0.0
        self._cached_quotes: List[MarketQuote] = []

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.coin_id)

    def fetch_quotes(self, **_: Any) -> List[MarketQuote]:
        if not self.configured:
            return []
        now = time.monotonic()
        if self._cached_quotes and now - self._last_fetch_monotonic < self.cache_seconds:
            return list(self._cached_quotes)
        response = requests.get(
            f"{self.base_url}/simple/price",
            params={
                "ids": self.coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
            headers={"Accept": "application/json", "User-Agent": "TridentAgentMVP/1.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        row = payload.get(self.coin_id) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return []
        price = _number(row.get("usd"))
        event_ts = _epoch_ms(row.get("last_updated_at"))
        # A provider timestamp is mandatory; local request time must not be
        # substituted for a stale/unknown aggregate observation.
        if price is None or price <= 0 or event_ts is None:
            return []
        quotes = [MarketQuote(
            symbol="PAXG/USD",
            asset="XAU",
            price=price,
            source=self.source,
            event_ts=event_ts,
            change_pct=_number(row.get("usd_24h_change")),
        )]
        self._cached_quotes = list(quotes)
        self._last_fetch_monotonic = now
        return quotes

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.source,
            "enabled": self.enabled,
            "configured": self.configured,
            "coin_id": self.coin_id,
            "source_type": "aggregated_tokenized_gold_check",
            "decision_eligible": False,
            "note": "public endpoint; rate-limited audit cross-check only",
            "cache_seconds": self.cache_seconds,
        }


_PROVIDER: CoinGeckoPaxgProvider | None = None


def get_coingecko_paxg_provider() -> CoinGeckoPaxgProvider:
    global _PROVIDER
    if _PROVIDER is None:
        import config

        _PROVIDER = CoinGeckoPaxgProvider(
            enabled=getattr(config, "COINGECKO_PAXG_ENABLED", False),
            base_url=getattr(config, "COINGECKO_PAXG_BASE_URL", "https://api.coingecko.com/api/v3"),
            coin_id=getattr(config, "COINGECKO_PAXG_COIN_ID", "pax-gold"),
            timeout=getattr(config, "COINGECKO_PAXG_REQUEST_TIMEOUT", 5.0),
            cache_seconds=getattr(config, "COINGECKO_PAXG_CACHE_SECONDS", 60.0),
        )
    return _PROVIDER
