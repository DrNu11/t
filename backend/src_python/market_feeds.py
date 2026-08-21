"""Public multi-exchange spot ticker aggregation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Dict

import requests

ASSETS = ("BTC", "ETH", "SOL")
SOURCES = ("Binance", "OKX", "Bitget", "Gate.io")
TIMEOUT = 1.5


def _number(value: Any) -> float:
    number = float(value)
    if number != number:
        raise ValueError("NaN")
    return number


def _fetch_binance(asset: str, get: Callable[..., Any] = requests.get) -> Dict[str, Any]:
    path = f"/api/v3/ticker/24hr?symbol={asset}USDT"
    error: Exception | None = None
    for base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            response = get(base + path, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return {"price": _number(data["lastPrice"]), "change24h": _number(data["priceChangePercent"])}
        except Exception as exc:
            error = exc
    raise RuntimeError("Binance unavailable") from error


def _fetch_okx(asset: str, get: Callable[..., Any] = requests.get) -> Dict[str, Any]:
    response = get(f"https://www.okx.com/api/v5/market/ticker?instId={asset}-USDT", timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()["data"][0]
    price = _number(data["last"])
    open24h = _number(data["open24h"])
    return {"price": price, "change24h": (price / open24h - 1) * 100}


def _fetch_bitget(asset: str, get: Callable[..., Any] = requests.get) -> Dict[str, Any]:
    response = get(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={asset}USDT", timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()["data"][0]
    return {"price": _number(data["lastPr"]), "change24h": _number(data["change24h"]) * 100}


def _fetch_gate(asset: str, get: Callable[..., Any] = requests.get) -> Dict[str, Any]:
    response = get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={asset}_USDT", timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()[0]
    return {"price": _number(data["last"]), "change24h": _number(data["change_percentage"])}


_FETCHERS = {
    "Binance": _fetch_binance,
    "OKX": _fetch_okx,
    "Bitget": _fetch_bitget,
    "Gate.io": _fetch_gate,
}


def fetch_market_prices() -> Dict[str, Any]:
    """Fetch all asset/source pairs concurrently and median-aggregate valid values."""
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_values: Dict[str, Dict[str, Dict[str, Any]]] = {source: {} for source in SOURCES}
    source_errors: Dict[str, list[str]] = {source: [] for source in SOURCES}
    with ThreadPoolExecutor(max_workers=len(ASSETS) * len(SOURCES)) as pool:
        futures = {
            pool.submit(fetcher, asset): (source, asset)
            for source, fetcher in _FETCHERS.items()
            for asset in ASSETS
        }
        for future in as_completed(futures):
            source, asset = futures[future]
            try:
                value = future.result()
                if value["price"] <= 0:
                    raise ValueError("non-positive price")
                source_values[source][asset] = value
            except Exception as exc:
                source_errors[source].append(f"{asset}: {type(exc).__name__}")

    items = []
    for asset in ASSETS:
        valid = [(source, values[asset]) for source, values in source_values.items() if asset in values]
        if valid:
            items.append({
                "asset": asset,
                "price": median(value["price"] for _, value in valid),
                "change24h": median(value["change24h"] for _, value in valid),
                "source": "median",
                "sourceCount": len(valid),
                "updated_at": updated_at,
            })

    sources = {
        source: {
            "status": "ok" if len(values) == len(ASSETS) else ("partial" if values else "unavailable"),
            "assets": sorted(values),
            "error": "; ".join(source_errors[source]),
        }
        for source, values in source_values.items()
    }
    return {
        "status": "ok" if len(items) == len(ASSETS) else ("partial" if items else "unavailable"),
        "items": items,
        "sources": sources,
        "updated_at": updated_at,
    }
