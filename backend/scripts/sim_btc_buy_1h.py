#!/usr/bin/env python3
"""比特币自动交易：快速判信号，自动买入/卖出，秒级止盈止损。

默认常驻纸上成交。Ctrl+C 停止。

口径：
  - 策略：strategies 表第一条（news-threshold）
  - 信号：BTC 的 BUY/SELL 且过策略过滤；HOLD 忽略
  - 开仓：BUY=多，SELL=空；用当时真实现价
  - 反手：持仓方向与最新有效信号相反时，立刻平仓再开反向
  - 出场：止盈 / 硬止损 / 追踪回撤 / 超时
  - 本金：500 人民币；名义仓位 = 本金 × 策略杠杆
  - 不保证盈利：现价失败本轮跳过，不编价格

用法（在 backend 目录）:
  python scripts/sim_btc_buy_1h.py
  python scripts/sim_btc_buy_1h.py --interval 1 --max-hold 45
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(BACKEND_DIR, "src_python")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import db  # noqa: E402
import strategy_store  # noqa: E402

TZ_SHANGHAI = timezone(timedelta(hours=8))
SINA_USDCNY = "https://hq.sinajs.cn/list=fx_susdcny"
STATE_PATH = os.path.join(SCRIPT_DIR, "sim_btc_buy_1h.state.json")
STATE_VERSION = 4
DEFAULT_SIGNAL_TTL_MINUTES = 24 * 60
DEFAULT_INTERVAL = 1.0
DEFAULT_MAX_HOLD = 45
DEFAULT_COOLDOWN = 3
DEFAULT_SIDE_FLOOR = 0.30


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ_SHANGHAI).strftime("%H:%M:%S")


def _http_json(url: str, params: Dict[str, Any], *, timeout: float = 3) -> Any:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    full = f"{url}?{query}" if params else url
    req = urllib.request.Request(
        full,
        headers={"User-Agent": "TridentAuto/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_usdcny() -> Optional[float]:
    try:
        req = urllib.request.Request(
            SINA_USDCNY,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("gbk", errors="replace")
        if '="' not in text:
            return None
        rate = float(text.split('="')[1].split(",")[1] or 0)
        return rate if 5 < rate < 10 else None
    except Exception:
        return None


def parse_ticker_payload(source: str, data: Any) -> Optional[float]:
    try:
        if source == "okx":
            last = float((((data or {}).get("data") or [{}])[0]).get("last") or 0)
            return last if last > 0 else None
        if source == "gate":
            row = data[0] if isinstance(data, list) and data else {}
            last = float((row or {}).get("last") or 0)
            return last if last > 0 else None
        if source == "binance":
            last = float((data or {}).get("price") or 0)
            return last if last > 0 else None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    return None


def fetch_btc_price() -> Optional[float]:
    """优先活价源。币安 vision 经常卡住，放到最后兜底。"""
    sources = (
        ("okx", "https://www.okx.com/api/v5/market/ticker", {"instId": "BTC-USDT"}),
        ("okx", "https://www.okx.com/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"}),
        ("gate", "https://api.gateio.ws/api/v4/spot/tickers", {"currency_pair": "BTC_USDT"}),
        ("binance", "https://data-api.binance.vision/api/v3/ticker/price", {"symbol": "BTCUSDT"}),
        ("binance", "https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"}),
        ("binance", "https://fapi.binance.com/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}),
    )
    for source, url, params in sources:
        try:
            price = parse_ticker_payload(source, _http_json(url, params, timeout=2.5))
            if price:
                return price
        except Exception:
            continue
    return None


def parse_signal_ts(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number // 1000 if number > 10**12 else number
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_SHANGHAI)
        return int(dt.timestamp())
    except ValueError:
        return None


def first_strategy() -> Dict[str, Any]:
    conn = db.get_connection()
    try:
        db.migrate(conn)
        conn.commit()
    finally:
        conn.close()
    strategy_store.seed_if_empty()
    items = strategy_store.list_strategies()
    if not items:
        raise RuntimeError("策略库为空")
    return items[0]


def _row_to_signal(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["signal_ts"] = parse_signal_ts(item.get("entry_time")) or parse_signal_ts(item.get("created_at"))
    item["side"] = "BUY" if item["action"] == "BUY" else "SELL"
    item["score"] = float(item.get("sentiment_score") or 0)
    return item


def load_btc_side_signals(
    params: Dict[str, Any],
    ttl_seconds: int,
    *,
    side_floor: float = DEFAULT_SIDE_FLOOR,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """同时取出有效 BUY 与 SELL。过策略阈值的优先；缺一侧再用 |score|>=floor 补。"""
    conn = db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ad.id, ad.news_id, ad.created_at, ad.entry_time, ad.entry_price,
                   ad.sentiment_score, UPPER(ad.suggested_action) AS action,
                   UPPER(ad.target_asset) AS asset, ad.event_strength, ad.direct_catalyst,
                   substr(rn.content, 1, 80) AS news
            FROM ai_decisions ad
            LEFT JOIN raw_news rn ON rn.id = ad.news_id
            WHERE UPPER(ad.suggested_action) IN ('BUY', 'SELL')
              AND UPPER(ad.target_asset) IN ('BTC', 'BTCUSDT')
            ORDER BY ad.id DESC
            LIMIT 240
            """
        ).fetchall()
    finally:
        conn.close()

    cutoff = now_ts() - ttl_seconds
    strict: Dict[str, Optional[Dict[str, Any]]] = {"BUY": None, "SELL": None}
    floor: Dict[str, Optional[Dict[str, Any]]] = {"BUY": None, "SELL": None}
    for row in rows:
        item = _row_to_signal(row)
        if not item["signal_ts"] or item["signal_ts"] < cutoff:
            continue
        side = item["side"]
        score = item["score"]
        if (side == "BUY" and score <= 0) or (side == "SELL" and score >= 0):
            continue
        matched = strategy_store.signal_matches(
            params,
            action=item["action"],
            score=score,
            asset="BTC",
            event_strength=item.get("event_strength") or "medium",
            direct_catalyst=int(item.get("direct_catalyst") or 0),
        )
        if matched and strict[side] is None:
            item["gate"] = "strict"
            strict[side] = item
        elif abs(score) >= float(side_floor) and floor[side] is None:
            item["gate"] = "floor"
            floor[side] = item
        if all(strict.values()):
            break
    return {
        "BUY": strict["BUY"] or floor["BUY"],
        "SELL": strict["SELL"] or floor["SELL"],
    }


def pick_trade_signal(
    sides: Dict[str, Optional[Dict[str, Any]]],
    used_ids: List[int],
) -> Optional[Dict[str, Any]]:
    """优先未用过的过阈值信号；再取未用过的 floor 信号；按 id 新优先。"""
    used = {int(x) for x in used_ids}
    candidates = [item for item in sides.values() if item]
    unused = [item for item in candidates if int(item["id"]) not in used]
    if not unused:
        return None

    def rank(item: Dict[str, Any]) -> Tuple[int, int, float]:
        return (
            1 if item.get("gate") == "strict" else 0,
            int(item["id"]),
            abs(float(item.get("score") or 0)),
        )

    return max(unused, key=rank)


def fmt_signal(item: Optional[Dict[str, Any]]) -> str:
    if not item:
        return "无"
    gate = "过线" if item.get("gate") == "strict" else "补位"
    return f"#{item['id']} {item['side']} {float(item.get('score') or 0):+.3f}({gate})"


def signed_pnl_pct(side: str, entry: float, spot: float) -> float:
    if entry <= 0 or spot <= 0:
        return 0.0
    raw = (spot - entry) / entry * 100.0
    return raw if str(side).upper() == "BUY" else -raw


def decide_exit(
    *,
    entry: float,
    peak: float,
    trough: float,
    spot: float,
    held_sec: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    trail_pct: float,
    max_hold_sec: float,
    side: str = "BUY",
) -> Optional[str]:
    """返回出场原因；None 表示继续持有。"""
    if entry <= 0 or spot <= 0:
        return None
    pnl_pct = signed_pnl_pct(side, entry, spot)
    if pnl_pct >= take_profit_pct:
        return "TP"
    if pnl_pct <= -abs(stop_loss_pct):
        return "SL"
    if trail_pct > 0:
        if str(side).upper() == "BUY" and peak > 0:
            drawdown = (peak - spot) / peak * 100.0
            if drawdown >= trail_pct and spot < peak:
                return "TRAIL"
        if str(side).upper() == "SELL" and trough > 0:
            bounce = (spot - trough) / trough * 100.0
            if bounce >= trail_pct and spot > trough:
                return "TRAIL"
    if held_sec >= max_hold_sec:
        return "TIME"
    return None


def load_state(capital_cny: float) -> Dict[str, Any]:
    blank = {
        "version": STATE_VERSION,
        "mode": "auto",
        "open": [],
        "closed": [],
        "cash": capital_cny,
        "trade_seq": 0,
        "cooldown_until": 0,
        "last_signal_id": 0,
    }
    if not os.path.exists(STATE_PATH):
        return blank
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return blank
    if int(data.get("version") or 0) != STATE_VERSION or data.get("mode") != "auto":
        print("状态文件口径已升级，已清空未结仓；已用过的信号不再重复开仓。", flush=True)
        closed = list(data.get("closed") or [])
        return {
            **blank,
            "cash": float(data.get("cash") if data.get("cash") is not None else capital_cny),
            "closed": closed,
            "used_signal_ids": [int(item["id"]) for item in closed if item.get("id") is not None],
        }
    data.setdefault("open", [])
    data.setdefault("closed", [])
    data.setdefault("trade_seq", 0)
    data.setdefault("cooldown_until", 0)
    data.setdefault("last_signal_id", 0)
    data.setdefault("used_signal_ids", [])
    if not data["used_signal_ids"]:
        data["used_signal_ids"] = [
            int(item["id"]) for item in data.get("closed") or [] if item.get("id") is not None
        ]
    if data.get("cash") is None:
        data["cash"] = capital_cny
    return data


def save_state(state: Dict[str, Any]) -> None:
    state["version"] = STATE_VERSION
    state["mode"] = "auto"
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def print_header(
    strategy: Dict[str, Any],
    params: Dict[str, Any],
    args: argparse.Namespace,
    usdcny: Optional[float],
    notional_cny: float,
    notional_usdt: Optional[float],
    take_profit_pct: float,
    stop_loss_pct: float,
    trail_pct: float,
) -> None:
    print("=" * 72, flush=True)
    print("比特币自动交易 · 快速判信号 · 自动买/卖 · 秒级平仓", flush=True)
    print("=" * 72, flush=True)
    print(f"策略#{strategy['id']} {strategy.get('slug')} / {strategy.get('name')}", flush=True)
    print(
        f"过滤 |score|>={params['signal_threshold']}  杠杆 {params['leverage']:g}x  "
        f"信号有效 {args.signal_ttl} 分钟",
        flush=True,
    )
    print(
        f"止盈 {take_profit_pct:.3f}%  止损 {stop_loss_pct:.3f}%  "
        f"追踪回撤 {trail_pct:.3f}%  最长持仓 {args.max_hold} 秒",
        flush=True,
    )
    print(f"本金 {args.capital_cny:.2f} 人民币  名义仓位 {notional_cny:.2f} 人民币", flush=True)
    if usdcny:
        print(f"USDCNY {usdcny:.4f}  名义仓位约 {notional_usdt:.2f} USDT", flush=True)
    else:
        print("USDCNY 不可用，人民币按涨跌幅记账", flush=True)
    print(f"轮询 {args.interval:g} 秒  冷却 {args.cooldown} 秒  卖出补位阈值 {args.side_floor:.2f}", flush=True)
    print("BUY 开多 / SELL 开空 / 反向立刻反手。盈亏显示到 0.0001 CNY。不保证盈利。Ctrl+C 停止。", flush=True)
    print("-" * 72, flush=True)


def hft_params(params: Dict[str, Any], args: argparse.Namespace) -> Tuple[float, float, float]:
    trail_pct = float(args.trail if args.trail is not None else params["trailing_callback_rate"])
    take_profit_pct = float(args.take_profit if args.take_profit is not None else max(0.08, trail_pct * 0.4))
    stop_loss_pct = float(args.stop_loss if args.stop_loss is not None else max(0.10, trail_pct * 0.6))
    return take_profit_pct, stop_loss_pct, trail_pct


def close_position(
    state: Dict[str, Any],
    pos: Dict[str, Any],
    spot: float,
    now: float,
    notional_cny: float,
    reason: str,
) -> None:
    pnl_pct = signed_pnl_pct(pos["side"], float(pos["entry"]), spot)
    pnl_cny = notional_cny * pnl_pct / 100.0
    state["cash"] = float(state["cash"]) + pnl_cny
    verdict = "WIN" if pnl_cny > 0 else "LOSS" if pnl_cny < 0 else "FLAT"
    held = now - float(pos["open_ts"])
    rec = {
        **pos,
        "exit": float(spot),
        "pnl_pct": pnl_pct,
        "pnl_cny": pnl_cny,
        "verdict": verdict,
        "reason": reason,
        "close_ts": now,
        "held_sec": held,
    }
    state["closed"].append(rec)
    verb = "卖出平多" if pos["side"] == "BUY" else "买入平空"
    print(
        f"{fmt_ts(now)}  {reason}/{verdict}  T{pos['trade_id']} {pos['side']}  {verb}  "
        f"入 {pos['entry']:.2f} → 出 {spot:.2f}  {pnl_pct:+.3f}%  {pnl_cny:+.2f} CNY  "
        f"持仓 {held:.0f}s  资金 {state['cash']:.2f}",
        flush=True,
    )


def open_position(state: Dict[str, Any], signal: Dict[str, Any], spot: float, now: float) -> Dict[str, Any]:
    state["trade_seq"] = int(state.get("trade_seq") or 0) + 1
    pos = {
        "trade_id": int(state["trade_seq"]),
        "id": int(signal["id"]),
        "news_id": signal.get("news_id"),
        "score": float(signal.get("sentiment_score") or 0),
        "side": signal["side"],
        "open_ts": now,
        "entry": float(spot),
        "peak": float(spot),
        "trough": float(spot),
        "news": (signal.get("news") or "")[:60],
    }
    verb = "买入开多" if pos["side"] == "BUY" else "卖出开空"
    print(
        f"{fmt_ts(now)}  OPEN  T{pos['trade_id']} sig#{pos['id']}  {verb}  "
        f"入 {pos['entry']:.2f}  score={pos['score']:+.3f}",
        flush=True,
    )
    state["last_signal_id"] = int(signal["id"])
    used = [int(x) for x in state.get("used_signal_ids") or []]
    if int(signal["id"]) not in used:
        used.append(int(signal["id"]))
    state["used_signal_ids"] = used[-80:]
    return pos


def run_once(args: argparse.Namespace) -> int:
    strategy = first_strategy()
    latest = strategy.get("latest_version") or {}
    params = strategy_store.clamp_params(latest.get("params") or strategy_store.default_params())
    params["asset_filter"] = "BTC"
    tp, sl, trail = hft_params(params, args)
    usdcny = fetch_usdcny()
    notional_cny = float(args.capital_cny) * float(params["leverage"])
    print_header(
        strategy, params, args, usdcny, notional_cny,
        (notional_cny / usdcny) if usdcny else None, tp, sl, trail,
    )
    print("[ONCE] 只扫一轮后退出", flush=True)
    sides = load_btc_side_signals(params, args.signal_ttl * 60, side_floor=args.side_floor)
    signal = pick_trade_signal(sides, [])
    spot = fetch_btc_price()
    print(
        f"现价 {spot if spot else '不可用'}  BUY {fmt_signal(sides.get('BUY'))}  "
        f"SELL {fmt_signal(sides.get('SELL'))}  下单 {fmt_signal(signal)}",
        flush=True,
    )
    return 0 if spot else 2


def run_live(args: argparse.Namespace) -> int:
    strategy = first_strategy()
    latest = strategy.get("latest_version") or {}
    params = strategy_store.clamp_params(latest.get("params") or strategy_store.default_params())
    params["asset_filter"] = "BTC"
    take_profit_pct, stop_loss_pct, trail_pct = hft_params(params, args)
    usdcny = fetch_usdcny()
    leverage = float(params["leverage"])
    capital_cny = float(args.capital_cny)
    notional_cny = capital_cny * leverage
    print_header(
        strategy, params, args, usdcny, notional_cny,
        (notional_cny / usdcny) if usdcny else None,
        take_profit_pct, stop_loss_pct, trail_pct,
    )

    state = load_state(capital_cny)
    last_fx_ts = 0.0
    last_stat_ts = 0.0
    last_mark_ts = 0.0
    last_mark_pnl = None
    miss_price = 0

    while True:
        loop_start = now_ts()
        if loop_start - last_fx_ts > 600:
            fx = fetch_usdcny()
            if fx:
                usdcny = fx
            last_fx_ts = loop_start

        spot = fetch_btc_price()
        if not spot:
            miss_price += 1
            if miss_price % 10 == 1:
                print(f"{fmt_ts(loop_start)}  WAIT  现价不可用，本轮跳过", flush=True)
            time.sleep(max(0.4, float(args.interval)))
            continue
        miss_price = 0

        sides = load_btc_side_signals(params, args.signal_ttl * 60, side_floor=args.side_floor)
        used_ids = [int(x) for x in state.get("used_signal_ids") or []]
        for pos in state.get("open") or []:
            if pos.get("id") is not None:
                used_ids.append(int(pos["id"]))
        signal = pick_trade_signal(sides, used_ids)
        opposite = None
        if state["open"]:
            hold_side = str(state["open"][0]["side"])
            other = "SELL" if hold_side == "BUY" else "BUY"
            cand = sides.get(other)
            if cand and int(cand["id"]) > int(state["open"][0]["id"]):
                opposite = cand

        still_open: List[Dict[str, Any]] = []
        for pos in state["open"]:
            pos["peak"] = max(float(pos.get("peak") or pos["entry"]), float(spot))
            pos["trough"] = min(float(pos.get("trough") or pos["entry"]), float(spot))
            flip = bool(opposite and opposite["side"] != pos["side"])
            held = loop_start - float(pos["open_ts"])
            reason = "FLIP" if flip else decide_exit(
                entry=float(pos["entry"]),
                peak=float(pos["peak"]),
                trough=float(pos["trough"]),
                spot=float(spot),
                held_sec=held,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                trail_pct=trail_pct,
                max_hold_sec=float(args.max_hold),
                side=str(pos["side"]),
            )
            if reason is None:
                pnl_pct = signed_pnl_pct(pos["side"], float(pos["entry"]), float(spot))
                changed = last_mark_pnl is None or abs(pnl_pct - last_mark_pnl) >= 0.0001
                if changed or loop_start - last_mark_ts >= 5:
                    float_cny = notional_cny * pnl_pct / 100.0
                    print(
                        f"{fmt_ts(loop_start)}  MARK  T{pos['trade_id']} {pos['side']}  {spot:.2f}  "
                        f"{pnl_pct:+.4f}% / {float_cny:+.4f} CNY  持仓 {held:.0f}s",
                        flush=True,
                    )
                    last_mark_ts = loop_start
                    last_mark_pnl = pnl_pct
                still_open.append(pos)
                continue
            close_position(state, pos, float(spot), loop_start, notional_cny, reason)
            if reason == "FLIP" and opposite:
                signal = opposite
            else:
                state["cooldown_until"] = loop_start + float(args.cooldown)

        state["open"] = still_open

        can_open = (
            not state["open"]
            and signal is not None
            and loop_start >= float(state.get("cooldown_until") or 0)
        )
        if can_open:
            pos = open_position(state, signal, float(spot), loop_start)
            state["open"] = [pos]
            last_mark_pnl = None

        if loop_start - last_stat_ts >= 10:
            wins = [x for x in state["closed"] if x.get("verdict") == "WIN"]
            losses = [x for x in state["closed"] if x.get("verdict") == "LOSS"]
            total_pnl = float(state["cash"]) - capital_cny
            print(
                f"{fmt_ts(loop_start)}  STAT  持仓 {len(state['open'])}  已结 {len(state['closed'])}  "
                f"胜 {len(wins)} 负 {len(losses)}  资金 {state['cash']:.4f}  "
                f"已实现 {total_pnl:+.4f} CNY  现价 {spot:.2f}  "
                f"BUY {fmt_signal(sides.get('BUY'))}  SELL {fmt_signal(sides.get('SELL'))}",
                flush=True,
            )
            last_stat_ts = loop_start
        save_state(state)

        elapsed = now_ts() - loop_start
        time.sleep(max(0.05, float(args.interval) - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC 自动买卖纸上量化，本金 500 人民币，策略第一个")
    parser.add_argument("--capital-cny", type=float, default=500.0)
    parser.add_argument("--signal-ttl", type=int, default=DEFAULT_SIGNAL_TTL_MINUTES)
    parser.add_argument("--horizon", type=int, default=None, help="兼容旧参数，等同 --signal-ttl")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD)
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    parser.add_argument("--take-profit", type=float, default=None)
    parser.add_argument("--stop-loss", type=float, default=None)
    parser.add_argument("--trail", type=float, default=None)
    parser.add_argument(
        "--side-floor",
        type=float,
        default=DEFAULT_SIDE_FLOOR,
        help="某一侧没有过策略阈值时，用 |score|>=该值补买卖信号",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.horizon is not None:
        args.signal_ttl = args.horizon
    if args.once:
        return run_once(args)
    try:
        return run_live(args)
    except KeyboardInterrupt:
        print("\n已停止自动交易。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
