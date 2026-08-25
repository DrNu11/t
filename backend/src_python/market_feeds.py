"""Public multi-exchange spot ticker aggregation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Dict

import requests

import config
import data_quality
from providers.binance_paxg import get_binance_paxg_provider
from providers.coingecko_paxg import get_coingecko_paxg_provider
from providers.jin10 import get_jin10_provider
from providers.oanda import get_oanda_provider

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
            prices = [float(value["price"]) for _, value in valid]
            median_price = median(prices)
            max_deviation_bps = (
                max(abs(price / median_price - 1.0) for price in prices) * 10000.0
                if median_price > 0 else 0.0
            )
            items.append({
                "asset": asset,
                "price": median_price,
                "change24h": median(value["change24h"] for _, value in valid),
                "source": "median",
                "sourceCount": len(valid),
                "sourceNames": [source for source, _ in valid],
                "maxDeviationBps": round(max_deviation_bps, 4),
                "updated_at": updated_at,
            })

    # Optional Jin10 spot gold projection. It is additive: existing
    # BTC/ETH/SOL median behavior and the market_ticks schema stay unchanged.
    jin10_error = ""
    if getattr(config, "JIN10_ENABLED", False):
        try:
            provider = get_jin10_provider()
            if provider.configured:
                quote = next((item for item in provider.fetch_quotes(
                    asset_type=config.JIN10_MARKET_TYPE,
                    codes=config.JIN10_MARKET_CODES,
                ) if item.asset == "XAU"), None)
                if quote is not None:
                    items.append({
                        "asset": "XAU",
                        "price": quote.price,
                        "change24h": quote.change_pct or 0.0,
                        "source": "金十",
                        "sourceCount": 1,
                        "symbol": quote.symbol,
                        "event_ts": quote.event_ts,
                        "volume": quote.volume,
                        "updated_at": updated_at,
                    })
        except Exception as exc:
            jin10_error = f"XAU: {type(exc).__name__}"

    sources = {
        source: {
            "status": "ok" if len(values) == len(ASSETS) else ("partial" if values else "unavailable"),
            "assets": sorted(values),
            "error": "; ".join(source_errors[source]),
        }
        for source, values in source_values.items()
    }
    if getattr(config, "JIN10_ENABLED", False):
        has_xau = any(item.get("source") == "金十" for item in items)
        sources["Jin10"] = {
            "status": "ok" if has_xau else "unavailable",
            "assets": ["XAU"] if has_xau else [],
            "error": jin10_error,
        }
    # Optional real XAU/USD broker quote.  It is additive and deliberately
    # does not participate in the BTC/ETH/SOL median or core status.
    oanda_error = ""
    if getattr(config, "OANDA_ENABLED", False):
        try:
            provider = get_oanda_provider()
            if provider.configured:
                quote = next((item for item in provider.fetch_quotes() if item.asset == "XAU"), None)
                if quote is not None:
                    items.append({
                        "asset": "XAU",
                        "price": quote.price,
                        "change24h": quote.change_pct or 0.0,
                        "source": "oanda",
                        "sourceCount": 1,
                        "sourceNames": ["oanda"],
                        "event_ts": quote.event_ts,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "instrument_id": quote.symbol,
                        "instrument_type": "broker_quote",
                        "updated_at": updated_at,
                    })
        except Exception as exc:
            oanda_error = f"XAU: {type(exc).__name__}"
        has_xau = any(item.get("source") == "oanda" for item in items)
        sources["OANDA"] = {
            "status": "ok" if has_xau else "unavailable",
            "assets": ["XAU"] if has_xau else [],
            "error": oanda_error,
            "decision_eligible": False,
            "note": "broker quote; does not replace OKX structure instrument",
        }
    # Free PAXG proxies are deliberately additive.  Their prices are useful
    # for divergence/audit, but neither may overwrite OKX XAU or enter the
    # canonical median/decision path.
    for provider, label in (
        (get_binance_paxg_provider(), "BinancePAXG"),
        (get_coingecko_paxg_provider(), "CoinGeckoPAXG"),
    ):
        if not provider.enabled:
            continue
        error = ""
        quote = None
        try:
            quote = next(iter(provider.fetch_quotes()), None)
        except Exception as exc:
            error = f"XAU: {type(exc).__name__}"
        if quote is not None:
            raw_quote = quote.to_dict()
            quality = data_quality.assess(
                source=quote.source,
                kind="market",
                event_id=f"XAU:{quote.source}:{quote.event_ts or 'missing-time'}",
                published_at=quote.event_ts,
                payload=raw_quote,
            )
            items.append({
                "asset": "XAU",
                "price": quote.price,
                "change24h": quote.change_pct or 0.0,
                "source": quote.source,
                "sourceCount": 1,
                "sourceNames": [quote.source],
                "event_ts": quote.event_ts,
                "bid": quote.bid,
                "ask": quote.ask,
                "volume": quote.volume,
                "instrument_id": quote.symbol,
                "instrument_type": "tokenized_gold_proxy",
                "quality_status": quality.quality_status,
                "quality_reason": quality.reason,
                "decision_eligible": False,
                "updated_at": updated_at,
            })
        sources[label] = {
            "status": "ok" if quote is not None else "unavailable",
            "assets": ["XAU"] if quote is not None else [],
            "error": error,
            "decision_eligible": False,
            "note": provider.health().get("note") or "PAXG tokenized-gold proxy; audit only",
        }
    core_count = sum(str(item.get("asset") or "") in ASSETS for item in items)
    return {
        "status": "ok" if core_count == len(ASSETS) else ("partial" if core_count else "unavailable"),
        "items": items,
        "sources": sources,
        "updated_at": updated_at,
    }
