"""Forward tracker — 2-hour simulated trade verification.

Tracks max/min, settles with WIN/LOSS verdict.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import List

from config import IMPACT_THRESHOLD, TZ_SHANGHAI

import timeseries

from .prices import _get_current_price
from .utils import _now, _open_db


_FORWARD_DURATION_HOURS = 2

# ``IMPACT_THRESHOLD`` remains the asset-specific materiality threshold used by
# the analysis layer. Settlement needs a much narrower neutral band: otherwise
# a small but real directional PnL is incorrectly recorded as HOLD and cannot be
# used by replay/reflection. One percent of the configured impact threshold is
# enough to absorb quote/rounding noise without hiding an actual outcome.
_NEUTRAL_BAND_SCALE = 0.01
_MIN_NEUTRAL_BAND_PCT = 0.0001
_MAX_NEUTRAL_BAND_PCT = 0.02


def _parse_entry_time(value) -> datetime | None:
    """Validate the persisted entry timestamp for settlement.

    Only ``ai_decisions.entry_time`` is authoritative.  In particular, an old
    row must never infer its entry from ``created_at`` or the related news time:
    doing so would settle a historical decision against a current quote and
    manufacture PnL.  Naive entry timestamps are interpreted as platform local
    time, while explicit offsets are preserved before conversion.
    """
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return parsed.astimezone(TZ_SHANGHAI)


def _neutral_pnl_band_pct(asset: str) -> float:
    """Small explicit HOLD band derived from the existing asset threshold."""
    try:
        impact = abs(float(IMPACT_THRESHOLD.get(str(asset or "").upper(), 1.0)))
    except (TypeError, ValueError):
        impact = 1.0
    return max(
        _MIN_NEUTRAL_BAND_PCT,
        min(impact * _NEUTRAL_BAND_SCALE, _MAX_NEUTRAL_BAND_PCT),
    )


def directional_pnl_pct(action: str, entry_price: float, exit_price: float) -> float:
    """Calculate BUY/SELL PnL with profit positive and loss negative."""
    entry = float(entry_price)
    exit_value = float(exit_price)
    if entry <= 0:
        return 0.0
    raw = (exit_value - entry) / entry * 100
    return -raw if str(action or "").upper() == "SELL" else raw


def classify_directional_outcome(action: str, forward_pnl: float, asset: str) -> str:
    """Classify a settled directional trade from signed PnL.

    MFE/MAE remain diagnostic path metrics; they must not override the actual
    realised direction at exit. Non-directional or sub-noise outcomes are HOLD.
    """
    if str(action or "").upper() not in {"BUY", "SELL"}:
        return "HOLD"
    try:
        pnl = float(forward_pnl)
    except (TypeError, ValueError):
        return "HOLD"
    band = _neutral_pnl_band_pct(asset)
    if pnl > band:
        return "WIN"
    if pnl < -band:
        return "LOSS"
    return "HOLD"


async def forward_tracker() -> None:
    """2-hour simulated trade verification. Tracks max/min, settles with WIN/LOSS verdict."""
    print("[TRACKER] Forward tracker started - 2h verification window")
    loop = asyncio.get_running_loop()

    while True:
        try:

            def _track_cycle():
                updates = []
                conn = _open_db()
                try:
                    rows = conn.execute(
                        "SELECT ad.id, ad.suggested_action, ad.target_asset, ad.entry_price,"
                        " ad.max_price, ad.min_price, ad.max_price_time, ad.min_price_time,"
                        " ad.entry_time,"
                        " ad.settled, sv.params AS strategy_params"
                        ", ad.impact_horizon, ad.take_profit_pct, ad.stop_loss_pct"
                        ", ad.exit_policy"
                        " FROM ai_decisions ad"
                        " LEFT JOIN strategy_versions sv ON sv.id = ad.strategy_version_id"
                        " WHERE ad.settled = 0 AND ad.entry_price IS NOT NULL"
                        " AND COALESCE(TRIM(ad.entry_time), '') != ''"
                    ).fetchall()

                    # MFE/MAE remain path diagnostics; realised signed PnL
                    # determines the final directional verdict.
                    for row in rows:
                        eid = row["id"]
                        action = (row["suggested_action"] or "").upper()
                        asset_raw = (row["target_asset"] or "NONE").upper()
                        entry = row["entry_price"]
                        cur_max = row["max_price"]
                        cur_min = row["min_price"]
                        max_ptime = row["max_price_time"] or 0
                        min_ptime = row["min_price_time"] or 0
                        et = _parse_entry_time(row["entry_time"])
                        if et is None:
                            continue

                        elapsed = datetime.now(TZ_SHANGHAI) - et
                        price = _get_current_price(asset_raw)  # used for tracking only
                        horizon_minutes = _FORWARD_DURATION_HOURS * 60
                        trailing_callback = 0.0
                        strategy_params = {}
                        try:
                            strategy_params = json.loads(row["strategy_params"] or "{}")
                            horizon_minutes = int(strategy_params.get("holding_horizon_minutes", horizon_minutes))
                            trailing_callback = float(strategy_params.get("trailing_callback_rate") or 0.0)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            strategy_params = {}

                        # Use the AI-labelled impact interval when available;
                        # old decisions continue to use holding_horizon_minutes.
                        impact_horizon = str(row["impact_horizon"] or "").lower()
                        selected_horizon = {
                            "short": strategy_params.get("short_horizon_minutes"),
                            "medium": strategy_params.get("medium_horizon_minutes"),
                            "long": strategy_params.get("long_horizon_minutes"),
                        }.get(impact_horizon)
                        if selected_horizon:
                            try:
                                horizon_minutes = max(5, int(selected_horizon))
                            except (TypeError, ValueError):
                                pass

                        try:
                            take_profit_pct = float(row["take_profit_pct"] or strategy_params.get("take_profit_pct") or 0.0)
                        except (TypeError, ValueError):
                            take_profit_pct = 0.0
                        try:
                            stop_loss_pct = float(row["stop_loss_pct"] or strategy_params.get("stop_loss_pct") or 0.0)
                        except (TypeError, ValueError):
                            stop_loss_pct = 0.0

                        stop_hit = False
                        take_profit_hit = False
                        exit_reason = ""
                        if price is not None and entry and entry > 0:
                            favourable = (
                                (price - entry) / entry * 100
                                if action == "BUY"
                                else (entry - price) / entry * 100
                                if action == "SELL" else 0.0
                            )
                            adverse = (
                                (entry - price) / entry * 100
                                if action == "BUY"
                                else (price - entry) / entry * 100
                                if action == "SELL" else 0.0
                            )
                            take_profit_hit = take_profit_pct > 0 and favourable >= take_profit_pct
                            stop_hit = stop_loss_pct > 0 and adverse >= stop_loss_pct
                            if take_profit_hit:
                                exit_reason = "take_profit"
                            elif stop_hit:
                                exit_reason = "stop_loss"

                        if not exit_reason and price is not None and entry and entry > 0 and trailing_callback > 0:
                            if action == "BUY" and cur_max and cur_max > 0:
                                stop_price = cur_max * (1 - trailing_callback / 100)
                                stop_hit = price <= stop_price
                            elif action == "SELL" and cur_min and cur_min > 0:
                                stop_price = cur_min * (1 + trailing_callback / 100)
                                stop_hit = price >= stop_price
                            if stop_hit:
                                exit_reason = "trailing_stop"

                        if not exit_reason and elapsed.total_seconds() >= horizon_minutes * 60:
                            exit_reason = "impact_horizon"

                        if exit_reason:
                            if price is None:
                                continue
                            exit_p = price
                            if exit_p is not None:
                                now_unix = int(time.time())
                                if cur_max is None or exit_p > cur_max:
                                    cur_max = exit_p
                                    max_ptime = now_unix
                                if cur_min is None or exit_p < cur_min:
                                    cur_min = exit_p
                                    min_ptime = now_unix
                            entry_unix = int(et.timestamp())

                            # ── Impact metrics (defensive against zero entry) ──
                            mfe_pct: float = 0.0
                            mae_pct: float = 0.0
                            mfe_time_mins: float = 0.0

                            if entry and entry > 0:
                                if action == "BUY":
                                    if cur_max and cur_max > 0:
                                        mfe_pct = (cur_max - entry) / entry * 100
                                    if cur_min and cur_min > 0:
                                        mae_pct = (entry - cur_min) / entry * 100
                                    if max_ptime > 0:
                                        mfe_time_mins = (max_ptime - entry_unix) / 60
                                elif action == "SELL":
                                    if cur_min and cur_min > 0:
                                        mfe_pct = (entry - cur_min) / entry * 100
                                    if cur_max and cur_max > 0:
                                        mae_pct = (cur_max - entry) / entry * 100
                                    if min_ptime > 0:
                                        mfe_time_mins = (min_ptime - entry_unix) / 60

                            # ── forward_pnl: signed PnL % from entry to exit ──
                            fwd_pnl = directional_pnl_pct(action, entry, exit_p)
                            verdict = classify_directional_outcome(
                                action, fwd_pnl, asset_raw
                            )

                            conn.execute(
                                "UPDATE ai_decisions SET exit_price = ?, exit_time = ?, exit_reason = ?, is_correct = ?,"
                                " settled = 1, mfe_pct = ?, mae_pct = ?, forward_pnl = ?,"
                                " mfe_time_mins = ?, max_price=?, min_price=?,"
                                " max_price_time=?, min_price_time=? WHERE id = ?",
                                (round(exit_p, 2), datetime.now(TZ_SHANGHAI).isoformat(), exit_reason, verdict,
                                 round(max(0.0, mfe_pct), 4), round(max(0.0, mae_pct), 4),
                                 round(fwd_pnl, 4), round(mfe_time_mins, 1),
                                 round(cur_max, 2) if cur_max is not None else None,
                                 round(cur_min, 2) if cur_min is not None else None,
                                 max_ptime, min_ptime, eid),
                            )
                            conn.commit()
                            timeseries.record_signal_performance([{
                                "decision_id": eid, "asset": asset_raw, "action": action,
                                "is_correct": verdict, "forward_pnl": round(fwd_pnl, 4),
                                "mfe_pct": round(mfe_pct, 4), "mae_pct": round(mae_pct, 4),
                            }], connection=conn)
                            updates.append({
                                "id": eid, "asset": asset_raw, "action": action,
                                "entry": entry, "exit": round(exit_p, 2), "verdict": verdict,
                                "mfe": round(mfe_pct, 4), "mae": round(mae_pct, 4),
                                "forward_pnl": round(fwd_pnl, 4), "mfe_mins": round(mfe_time_mins, 1),
                            })
                        elif price is not None:
                            now_unix = int(time.time())
                            new_max = round(max(cur_max or price, price), 2)
                            new_min = round(min(cur_min or price, price), 2)
                            sets: List[str] = []
                            params: list = []
                            if new_max != cur_max:
                                sets.append("max_price = ?")
                                params.append(new_max)
                                sets.append("max_price_time = ?")
                                params.append(now_unix)
                            if new_min != cur_min:
                                sets.append("min_price = ?")
                                params.append(new_min)
                                sets.append("min_price_time = ?")
                                params.append(now_unix)
                            if sets:
                                params.append(eid)
                                conn.execute(
                                    f"UPDATE ai_decisions SET {', '.join(sets)} WHERE id = ?",
                                    params,
                                )
                    conn.commit()

                finally:
                    conn.close()
                return updates

            settled = await loop.run_in_executor(None, _track_cycle)
            for s in settled:
                print(
                    f"  [{_now()}] SETTLED #{s['id']} {s['action']} {s['asset']}"
                    f" | entry={s['entry']} exit={s['exit']} -> {s['verdict']}"
                    f" | MFE={s.get('mfe', 0):+.2f}% MAE={s.get('mae', 0):+.2f}% PnL={s.get('forward_pnl', 0):+.2f}%"
                )

        except Exception as e:
            print(f"[TRACKER] ERROR: {type(e).__name__}: {e}")

        await asyncio.sleep(30)
