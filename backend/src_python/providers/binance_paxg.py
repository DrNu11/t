"""Public Binance PAXG/USDT quote adapter.

PAXG is a tokenised-gold market, not the same contract as OKX's
``XAU-USDT-SWAP``.  This adapter is therefore deliberately additive and
candidate-only: it provides a free, keyless cross-check without replacing the
OKX quote or the market-structure input.
"""

from __future__ import annotations

import math
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


class BinancePaxgProvider(MarketProvider):
    """Fetch a public 24-hour ticker without an API key."""

    source = "binance_paxg"

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "https://data-api.binance.vision",
        symbol: str = "PAXGUSDT",
        timeout: float = 3.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.symbol = str(symbol or "PAXGUSDT").strip().upper()
        self.timeout = max(1.0, float(timeout))

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.symbol)

    def fetch_quotes(self, **_: Any) -> List[MarketQuote]:
        if not self.configured:
            return []
        response = requests.get(
            f"{self.base_url}/api/v3/ticker/24hr",
            params={"symbol": self.symbol},
            headers={"Accept": "application/json", "User-Agent": "TridentAgentMVP/1.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        row = response.json()
        if not isinstance(row, dict):
            raise ValueError("Binance PAXG ticker must be an object")
        price = _number(row.get("lastPrice"))
        if price is None or price <= 0:
            return []
        return [MarketQuote(
            symbol=self.symbol,
            asset="XAU",
            price=price,
            source=self.source,
            event_ts=_epoch_ms(row.get("closeTime") or row.get("openTime")),
            change_pct=_number(row.get("priceChangePercent")),
            volume=_number(row.get("volume")),
            bid=_number(row.get("bidPrice")),
            ask=_number(row.get("askPrice")),
            high=_number(row.get("highPrice")),
            low=_number(row.get("lowPrice")),
            previous_close=_number(row.get("prevClosePrice")),
        )]

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.source,
            "enabled": self.enabled,
            "configured": self.configured,
            "symbol": self.symbol,
            "source_type": "tokenized_gold_exchange_proxy",
            "decision_eligible": False,
        }


_PROVIDER: BinancePaxgProvider | None = None


def get_binance_paxg_provider() -> BinancePaxgProvider:
    global _PROVIDER
    if _PROVIDER is None:
        import config

        _PROVIDER = BinancePaxgProvider(
            enabled=getattr(config, "BINANCE_PAXG_ENABLED", False),
            base_url=getattr(config, "BINANCE_PAXG_BASE_URL", "https://data-api.binance.vision"),
            symbol=getattr(config, "BINANCE_PAXG_SYMBOL", "PAXGUSDT"),
            timeout=getattr(config, "BINANCE_PAXG_REQUEST_TIMEOUT", 3.0),
        )
    return _PROVIDER
