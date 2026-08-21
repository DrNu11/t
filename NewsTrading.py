#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance USD-M Futures news-signal trader."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_DIR, "backend", "src_python")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import config  # noqa: E402

API_KEY = config.BINANCE_API_KEY
API_SECRET = config.BINANCE_API_SECRET
USE_TESTNET = config.BINANCE_USE_TESTNET
LIVE_TRADING_ENABLED = config.BINANCE_LIVE_TRADING_ENABLED
HEDGE_MODE_ENABLED = config.BINANCE_HEDGE_MODE_ENABLED
NOTIONAL_USDT = config.BINANCE_NOTIONAL_USDT
LEVERAGE = config.BINANCE_LEVERAGE
SIGNAL_THRESHOLD = config.BINANCE_SIGNAL_THRESHOLD
CALLBACK_RATE = config.BINANCE_TRAILING_CALLBACK_RATE
NEWS_API_URL = config.BINANCE_NEWS_API_URL
POLL_INTERVAL = config.BINANCE_POLL_INTERVAL
REQUEST_TIMEOUT = config.BINANCE_REQUEST_TIMEOUT
STATE_PATH = config.BINANCE_STATE_PATH
LOG_PATH = config.BINANCE_LOG_PATH

BASE_URL = "https://testnet.binancefuture.com" if USE_TESTNET else "https://fapi.binance.com"
SYMBOL_MAP = {"BTC": "BTCUSDT", "XAU": "XAUUSDT", "WTI": "CLUSDT"}

logger = logging.getLogger("NewsTrader")
_STOP_EVENT = threading.Event()


class BinanceAPIError(RuntimeError):
    def __init__(self, status_code: int, code: Any, message: str):
        self.status_code = status_code
        self.code = code
        super().__init__(f"Binance API error HTTP {status_code}, code={code}: {message}")


def configure_logging() -> None:
    directory = os.path.dirname(LOG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.time_offset_ms = 0
        self._exchange_info: Optional[Dict[str, Any]] = None
        self._symbols: Dict[str, Dict[str, Any]] = {}

    def create_signature(self, params: Dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        idempotent_codes: tuple[int, ...] = (),
    ) -> Any:
        request_params = dict(params or {})
        if signed:
            request_params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            request_params.setdefault("recvWindow", 10000)
            request_params["signature"] = self.create_signature(request_params)
        try:
            response = self.session.request(
                method.upper(), self.base_url + path, params=request_params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Binance request failed: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise BinanceAPIError(response.status_code, None, "non-JSON response") from exc
        if not response.ok:
            code = data.get("code") if isinstance(data, dict) else None
            if code not in idempotent_codes:
                message = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
                raise BinanceAPIError(response.status_code, code, message)
        return data

    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def sync_time(self) -> int:
        data = self._request("GET", "/fapi/v1/time")
        self.time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
        return self.time_offset_ms

    def get_exchange_info(self, refresh: bool = False) -> Dict[str, Any]:
        if self._exchange_info is None or refresh:
            self._exchange_info = self._request("GET", "/fapi/v1/exchangeInfo")
            self._symbols = {item["symbol"]: item for item in self._exchange_info.get("symbols", [])}
        return self._exchange_info

    def validate_symbol(self, symbol: str) -> Dict[str, Any]:
        self.get_exchange_info()
        info = self._symbols.get(symbol.upper())
        if not info:
            raise ValueError(f"Binance symbol does not exist: {symbol}")
        if info.get("status") != "TRADING":
            raise ValueError(f"Binance symbol is not TRADING: {symbol}")
        return info

    def get_symbol_filters(self, symbol: str) -> Dict[str, Decimal]:
        info = self.validate_symbol(symbol)
        filters = {item["filterType"]: item for item in info.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
        if Decimal(lot.get("stepSize", "0")) == 0:
            lot = filters.get("LOT_SIZE", {})
        return {
            "stepSize": Decimal(lot.get("stepSize", "0.001")),
            "minQty": Decimal(lot.get("minQty", "0.001")),
            "tickSize": Decimal(filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")),
        }

    @staticmethod
    def round_step(value: Any, step: Any) -> Decimal:
        number, increment = Decimal(str(value)), Decimal(str(step))
        if increment <= 0:
            return number
        return (number / increment).to_integral_value(rounding=ROUND_DOWN) * increment

    @staticmethod
    def _decimal_text(value: Any) -> str:
        return format(Decimal(str(value)), "f")

    def get_price(self, symbol: str) -> Decimal:
        return Decimal(self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol.upper()})["price"])

    def get_account(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_balances(self) -> list[Dict[str, Any]]:
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def get_positions(self, symbol: Optional[str] = None) -> list[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v2/positionRisk", params, signed=True)

    def get_position_mode(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/positionSide/dual", signed=True)

    def get_order(self, symbol: str, order_id: Optional[int] = None, client_order_id: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> list[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None, client_order_id: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()}, signed=True)

    def place_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> Dict[str, Any]:
        params = {"symbol": symbol.upper(), "side": side.upper(), "type": order_type.upper()}
        params.update(kwargs)
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def market_order(self, symbol: str, side: str, quantity: Any, reduce_only: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {"quantity": self._decimal_text(quantity)}
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.place_order(symbol, side, "MARKET", **params)

    def close_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        positions = [p for p in self.get_positions(symbol) if Decimal(str(p.get("positionAmt", "0"))) != 0]
        if not positions:
            return None
        if len(positions) != 1 or positions[0].get("positionSide", "BOTH") != "BOTH":
            raise RuntimeError("自动紧急平仓仅支持单向持仓模式")
        amount = Decimal(str(positions[0]["positionAmt"]))
        return self.market_order(symbol, "SELL" if amount > 0 else "BUY", abs(amount), reduce_only=True)

    def set_margin_type_isolated(self, symbol: str) -> Dict[str, Any]:
        return self._request(
            "POST", "/fapi/v1/marginType",
            {"symbol": symbol.upper(), "marginType": "ISOLATED"}, signed=True,
            idempotent_codes=(-4046,),
        )

    def set_leverage(self, symbol: str, leverage: int = LEVERAGE) -> Dict[str, Any]:
        return self._request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol.upper(), "leverage": leverage}, signed=True,
            idempotent_codes=(-4028,),
        )

    def place_algo_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> Dict[str, Any]:
        params = {
            "algoType": "CONDITIONAL", "symbol": symbol.upper(),
            "side": side.upper(), "type": order_type.upper(),
        }
        params.update(kwargs)
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def trailing_stop_order(self, symbol: str, side: str, quantity: Any, callback_rate: float, reduce_only: bool = True) -> Dict[str, Any]:
        return self.place_algo_order(
            symbol, side, "TRAILING_STOP_MARKET",
            quantity=self._decimal_text(quantity), callbackRate=self._decimal_text(callback_rate),
            reduceOnly="true" if reduce_only else "false", workingType="CONTRACT_PRICE",
        )

    def get_algo_order(self, algo_id: int) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)

    def get_open_algo_orders(self, symbol: Optional[str] = None) -> list[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/openAlgoOrders", params, signed=True)

    def cancel_algo_order(self, algo_id: int) -> Dict[str, Any]:
        return self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)


def load_state(path: str = STATE_PATH) -> tuple[Dict[str, Any], bool]:
    if not os.path.exists(path):
        return {key: None for key in SYMBOL_MAP}, False
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {key: None for key in SYMBOL_MAP}, True
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"读取交易状态失败: {exc}") from exc


def save_state_atomic(state: Dict[str, Any], path: str = STATE_PATH) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".news-trading-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def fetch_news() -> Dict[str, Any]:
    response = requests.get(NEWS_API_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("新闻 API 返回值不是对象")
    return data


def extract_news_item(data: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    direct = data.get(key)
    if isinstance(direct, dict):
        return direct
    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get(key), dict):
        return nested[key]
    if isinstance(nested, list):
        return next((item for item in nested if isinstance(item, dict) and item.get("symbol") == key), None)
    return None


def signal_direction(item: Dict[str, Any], threshold: float = SIGNAL_THRESHOLD) -> Optional[str]:
    action = str(item.get("action", "HOLD")).strip().upper()
    try:
        score = float(item.get("score", 0))
    except (TypeError, ValueError):
        return None
    if action == "BUY" and score > threshold:
        return "LONG"
    if action == "SELL" and score < -threshold:
        return "SHORT"
    return None


def calculate_quantity(client: BinanceFutures, symbol: str, notional: float) -> Decimal:
    price = client.get_price(symbol)
    filters = client.get_symbol_filters(symbol)
    quantity = client.round_step(Decimal(str(notional)) / price, filters["stepSize"])
    if quantity < filters["minQty"]:
        quantity = filters["minQty"]
    return client.round_step(quantity, filters["stepSize"])


def open_and_trail(client: BinanceFutures, news_key: str, direction: str, item: Dict[str, Any]) -> bool:
    symbol = SYMBOL_MAP[news_key]
    client.validate_symbol(symbol)
    # Scope the idempotency check to the requested symbol.  The previous
    # account-wide check incorrectly blocked BTC after an XAU/WTI position was
    # opened, making independent paper/live legs impossible.
    try:
        existing_positions = client.get_positions(symbol)
    except TypeError:  # compatibility with small test doubles
        existing_positions = client.get_positions()
    if any(Decimal(str(position.get("positionAmt", "0"))) != 0 for position in existing_positions):
        logger.warning("[%s] 该品种已有非零持仓，跳过重复信号", symbol)
        return True
    client.set_margin_type_isolated(symbol)
    client.set_leverage(symbol, LEVERAGE)
    quantity = calculate_quantity(client, symbol, NOTIONAL_USDT)
    if quantity <= 0:
        raise RuntimeError(f"[{symbol}] 计算出的下单数量无效")
    open_side, close_side = ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")
    order = client.market_order(symbol, open_side, quantity)
    if not order.get("orderId"):
        raise RuntimeError(f"[{symbol}] 市价开仓未返回 orderId")
    logger.info("[%s] 市价开仓成功 orderId=%s", symbol, order["orderId"])
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            protection = client.trailing_stop_order(symbol, close_side, quantity, CALLBACK_RATE)
            if not (protection.get("algoId") or protection.get("orderId")):
                raise RuntimeError("保护单未返回 algoId")
            logger.info("[%s] 跟踪止损成功 algoId=%s", symbol, protection.get("algoId") or protection.get("orderId"))
            return True
        except Exception as exc:
            last_error = exc
            logger.error("[%s] 跟踪止损第 %s 次失败: %s", symbol, attempt + 1, exc)
            if attempt == 0:
                time.sleep(0.5)
    logger.critical("[%s] 保护单失败，执行 reduce-only 紧急平仓", symbol)
    client.close_position(symbol)
    raise RuntimeError(f"[{symbol}] 保护单失败，已执行紧急平仓: {last_error}")


def process_news(client: BinanceFutures, data: Dict[str, Any], state: Dict[str, Any], baseline: bool = False) -> bool:
    changed = False
    for key in SYMBOL_MAP:
        item = extract_news_item(data, key)
        if not item or item.get("id") is None:
            continue
        previous = state.get(key)
        previous_id = previous.get("id") if isinstance(previous, dict) else None
        if str(item["id"]) == str(previous_id):
            continue
        if baseline or previous_id is None:
            state[key] = item
            changed = True
            continue
        direction = signal_direction(item)
        if direction is None:
            state[key] = item
            changed = True
            continue
        logger.info("[%s] 新信号 id=%s action=%s score=%s", key, item["id"], item.get("action"), item.get("score"))
        if open_and_trail(client, key, direction, item):
            state[key] = item
            changed = True
    return changed


def _request_stop(signum: int, _frame: Any) -> None:
    logger.info("收到停止信号 %s，正在退出", signum)
    _STOP_EVENT.set()


def main() -> int:
    configure_logging()
    if not LIVE_TRADING_ENABLED:
        logger.error(
            "拒绝启动：BINANCE_LIVE_TRADING_ENABLED=false。"
            "分析与模拟盘可以运行，真实下单需要单独显式开启。"
        )
        return 3
    if not API_KEY or not API_SECRET:
        logger.error("拒绝启动：BINANCE_API_KEY / BINANCE_API_SECRET 未配置")
        return 2
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)
    client = BinanceFutures(API_KEY, API_SECRET)
    try:
        client.ping()
        client.sync_time()
        client.get_exchange_info()
        state, exists = load_state()
        logger.warning(
            "交易器启动：%s，杠杆=%sx，名义价值=%s USDT，阈值=%s",
            "测试网" if USE_TESTNET else "实盘", LEVERAGE, NOTIONAL_USDT, SIGNAL_THRESHOLD,
        )
        while not _STOP_EVENT.is_set():
            try:
                data = fetch_news()
                changed = process_news(client, data, state, baseline=not exists)
                if changed:
                    save_state_atomic(state)
                if not exists and changed:
                    exists = True
                    logger.info("首次启动基线已建立，不交易历史信号")
            except Exception as exc:
                logger.exception("轮询处理失败: %s", exc)
            _STOP_EVENT.wait(POLL_INTERVAL)
    except Exception as exc:
        logger.exception("交易器启动失败: %s", exc)
        return 1
    logger.info("交易器已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
