"""Optional OANDA pricing adapter for XAU/USD and FX.

OANDA is deliberately opt-in.  A broker quote is useful for a real XAU/USD
price, but it is not the same instrument as an exchange-traded gold future or
the OKX XAU swap used by the structure calculator.  The adapter therefore
keeps the provider identity and instrument type explicit and never makes a
network request without an account id, token and enable flag.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Dict, List, Optional

import requests

from .contracts import MarketProvider, MarketQuote


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 10_000_000_000:
            number *= 1000.0
        return int(number)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _asset_for(instrument: str) -> str:
    symbol = instrument.upper().replace("/", "_")
    if symbol in {"XAU_USD", "GOLD_USD", "XAUUSD"}:
        return "XAU"
    return symbol.replace("_USD", "").replace("_USDT", "")


class OandaProvider(MarketProvider):
    """Fetch closeout bid/ask prices from the OANDA v20 pricing endpoint."""

    source = "oanda"

    def __init__(
        self,
        *,
        enabled: bool = False,
        api_token: str = "",
        account_id: str = "",
        base_url: str = "https://api-fxtrade.oanda.com",
        instruments: str = "XAU_USD",
        timeout: float = 5.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.api_token = str(api_token or "").strip()
        self.account_id = str(account_id or "").strip()
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.instruments = str(instruments or "XAU_USD").strip()
        self.timeout = max(1.0, float(timeout))

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_token and self.account_id and self.base_url)

    def _get(self, instruments: str) -> Dict[str, Any]:
        if not self.configured:
            return {"prices": []}
        response = requests.get(
            f"{self.base_url}/v3/accounts/{self.account_id}/pricing",
            params={"instruments": instruments},
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OANDA response must be a JSON object")
        return payload

    def fetch_quotes(self, *, instruments: str = "", **_: Any) -> List[MarketQuote]:
        requested = str(instruments or self.instruments).strip()
        payload = self._get(requested)
        prices = payload.get("prices") or []
        if not isinstance(prices, list):
            raise ValueError("OANDA prices must be a list")
        quotes: List[MarketQuote] = []
        for row in prices:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("instrument") or "").strip()
            bid_rows = row.get("bids") or []
            ask_rows = row.get("asks") or []
            bid = _number(bid_rows[0].get("price")) if bid_rows and isinstance(bid_rows[0], dict) else _number(row.get("closeoutBid"))
            ask = _number(ask_rows[0].get("price")) if ask_rows and isinstance(ask_rows[0], dict) else _number(row.get("closeoutAsk"))
            if bid is None and ask is None:
                continue
            price = (bid + ask) / 2.0 if bid is not None and ask is not None else bid or ask
            if price is None or price <= 0:
                continue
            quotes.append(MarketQuote(
                symbol=symbol,
                asset=_asset_for(symbol),
                price=price,
                source=self.source,
                event_ts=_epoch_ms(row.get("time")),
                bid=bid,
                ask=ask,
            ))
        return quotes

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.source,
            "enabled": self.enabled,
            "configured": self.configured,
            "instrument": self.instruments,
            "source_type": "broker_quote",
        }


_PROVIDER: Optional[OandaProvider] = None


def get_oanda_provider() -> OandaProvider:
    global _PROVIDER
    if _PROVIDER is None:
        import config

        _PROVIDER = OandaProvider(
            enabled=getattr(config, "OANDA_ENABLED", False),
            api_token=getattr(config, "OANDA_API_TOKEN", ""),
            account_id=getattr(config, "OANDA_ACCOUNT_ID", ""),
            base_url=getattr(config, "OANDA_BASE_URL", "https://api-fxtrade.oanda.com"),
            instruments=getattr(config, "OANDA_INSTRUMENTS", "XAU_USD"),
            timeout=getattr(config, "OANDA_REQUEST_TIMEOUT", 5.0),
        )
    return _PROVIDER

