# -*- coding: utf-8 -*-
"""Sell optimizer v4.0 BASELINE-QUALITY — Dual Trail: B1 tight / B2 wide.

Core design:
  B1 (50%): tight trailing stop — capture early profit or cut early loss
  B2 (50%): wide trailing stop — ride through the day, close at 14:25
  NO hard stops (safety net at -5% only)
  Gap capture at open for gaps >= 1.5%
  Time decay applies to B1 only
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

FEE_RATE = 0.000085

# ——— Batch sizes ———
B1_RATIO = 0.50
B2_RATIO = 0.50

# ——— B1 trail zones (tight, for quick capture) ———
TRAIL_ZONES_B1: list[tuple[float, float]] = [
    (-99.0, 0.8),   # deep loss → tight
    (-2.0,  1.0),   # -2%~0% → 1.0%
    (0.0,   1.2),   # 0%~2% → protect small profit
    (2.0,   2.0),   # 2%~4% → let it breathe
    (4.0,   3.0),   # 4%~7% → moderate
    (7.0,   4.0),   # 7%+ → let runner go
]

# ——— B2 trail zones (wide, for day-long ride) ———
TRAIL_ZONES_B2: list[tuple[float, float]] = [
    (-99.0, 99.0),  # B2 rides to closeout — trail never triggers
]

# ——— Time decay (B1 only) ———
TIME_DECAY_B1: list[tuple[int, int, float]] = [
    (9, 30,  1.00),
    (10, 30, 0.85),
    (11, 30, 0.70),
    (13, 0,  0.55),
    (13, 30, 0.40),
    (14, 0,  0.25),
]

# ——— Gap capture ———
GAP_CAPTURE: list[tuple[float, float]] = [
    (5.0, 1.00),   # gap >= 5%: sell ALL B1 at open
    (3.0, 0.80),   # gap >= 3%: sell 80% of B1
    (2.0, 0.60),   # gap >= 2%: sell 60% of B1
    (1.5, 0.40),   # gap >= 1.5%: sell 40% of B1
]


@dataclass
class SellBatch:
    batch: int = 0
    ratio: float = 0.0
    trigger: str = ""
    sold: bool = False
    sell_price: float = 0.0
    sell_time: str = ""


@dataclass
class SellPlan:
    stock_code: str = ""
    total_volume: int = 0
    cost_price: float = 0.0
    buy_date: str = ""
    batches: list[SellBatch] = field(default_factory=list)
    remaining: int = 0
    day_open: float = 0.0
    gap_pct: float = 0.0


@dataclass
class MinuteBar:
    time_str: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    vwap: float = 0.0


class SellOptimizer:

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.data_dir / "sell_history.json"
        self.config_path = self.data_dir / "sell_config.json"
        self._history: list[dict] = self._load_history()
        self._plans: dict[str, SellPlan] = {}
        self._intraday_peaks: dict[str, float] = {}

    def _load_history(self) -> list[dict]:
        if self.history_path.exists():
            try:
                return json.loads(self.history_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    @staticmethod
    def _time_to_minutes(time_str: str) -> int:
        try:
            h, m = map(int, time_str.split(":")[:2])
            return h * 60 + m
        except (ValueError, AttributeError):
            return 570

    @staticmethod
    def _time_decay_b1(time_str: str) -> float:
        tm = SellOptimizer._time_to_minutes(time_str)
        factor = 1.0
        for h, m, mult in TIME_DECAY_B1:
            if tm >= h * 60 + m:
                factor = mult
        return factor

    @staticmethod
    def _trail_pct(profit_pct: float, time_str: str = "09:30",
                   batch: int = 1) -> float:
        zones = TRAIL_ZONES_B2 if batch >= 2 else TRAIL_ZONES_B1
        trail = 1.0
        for lo, t in zones:
            if profit_pct >= lo:
                trail = t
        if batch == 1:
            trail *= SellOptimizer._time_decay_b1(time_str)
        return trail

    @staticmethod
    def _gap_sell_ratio(gap_pct: float) -> float:
        for threshold, r in GAP_CAPTURE:
            if gap_pct >= threshold:
                return r
        return 0.0

    def create_plan(self, stock_code: str, volume: int,
                    cost_price: float, buy_date: str = "",
                    gap_pct: float = 0.0, profit_at_open: float = 0.0) -> SellPlan:
        b1 = SellBatch(batch=1, ratio=B1_RATIO, trigger="pending")
        b2 = SellBatch(batch=2, ratio=B2_RATIO, trigger="pending")
        plan = SellPlan(
            stock_code=stock_code, total_volume=volume,
            cost_price=cost_price, buy_date=buy_date,
            batches=[b1, b2], remaining=volume,
        )
        self._plans[stock_code] = plan
        return plan

    def mark_batch_sold(self, stock_code: str, batch: int,
                        price: float, time_str: str) -> None:
        plan = self._plans.get(stock_code)
        if plan and 1 <= batch <= 2:
            plan.batches[batch - 1].sold = True
            plan.batches[batch - 1].sell_price = price
            plan.batches[batch - 1].sell_time = time_str
            sold_ratio = sum(b.ratio for b in plan.batches if b.sold)
            plan.remaining = max(0, int(plan.total_volume * (1 - sold_ratio)))

    def clear_plan(self, stock_code: str) -> None:
        self._plans.pop(stock_code, None)
        keys = [k for k in self._intraday_peaks if k.startswith(stock_code)]
        for k in keys:
            del self._intraday_peaks[k]

    def evaluate(self, stock_code: str, market: str,
                 xtdata: Any, cost_price: float,
                 current_time: str, volume: int,
                 is_backtest: bool = False,
                 backtest_bars: list[MinuteBar] | None = None,
                 backtest_bar_index: int = 0) -> tuple[str, float]:
        """B1 tight trail + same-bar reversal / B2 wide ride to closeout.
        Gap-down stocks (>0.5%): B1 delayed 5min unless a spike cancels it."""

        if is_backtest and backtest_bars is not None:
            bars = backtest_bars[:backtest_bar_index + 1]
            if not bars:
                return "hold", 0.0
            current_bar = bars[-1]
            current = current_bar.close
        else:
            return "hold", 0.0

        plan = self._plans.get(stock_code)
        if plan is None:
            return "hold", current

        h, m = 9, 30
        try:
            h, m = map(int, current_time.split(":")[:2])
        except ValueError:
            pass
        now_minutes = h * 60 + m

        # ------ Gap capture at first bar ------
        if backtest_bar_index == 0:
            gap_pct = (plan.day_open / plan.cost_price - 1) * 100 if plan.cost_price > 0 else 0
            # Gap-down delay: if gap < -0.5%, freeze B1 until 09:35
            if gap_pct <= -0.5:
                delay_key = f"{stock_code}_b1_delay_until"
                self._intraday_peaks[delay_key] = 9 * 60 + 35  # 09:35
            gsr = self._gap_sell_ratio(gap_pct)
            if gsr > 0 and not plan.batches[0].sold:
                return "sell_b1", current_bar.open

        # ------ Check B1 delay status ------
        delay_key = f"{stock_code}_b1_delay_until"
        delay_until = self._intraday_peaks.get(delay_key, 0)
        b1_blocked = now_minutes < delay_until if delay_until > 0 else False

        # ------ Gap-down spike cancels delay ------
        if b1_blocked and plan.day_open > 0:
            spike_pct = (current_bar.high / plan.day_open - 1) * 100
            if spike_pct >= 0.5 or current_bar.high >= plan.cost_price:
                self._intraday_peaks[delay_key] = 0  # cancel delay
                b1_blocked = False

        # ------ Track peak ------
        peak_key = f"{stock_code}_b1_peak"
        prev_peak = self._intraday_peaks.get(peak_key, current_bar.open)
        new_peak = max(prev_peak, current_bar.high)
        self._intraday_peaks[peak_key] = new_peak

        # ------ Same-bar reversal detection (always active, even during delay) ------
        if not plan.batches[0].sold:
            is_new_peak = current_bar.high > prev_peak
            if current_bar.high > 0:
                bar_drop_pct = (current_bar.high - current_bar.close) / current_bar.high * 100
            else:
                bar_drop_pct = 0
            REVERSAL_THRESHOLD = 1.0  # bar内回落>=1%触发反转卖出
            if is_new_peak and bar_drop_pct >= REVERSAL_THRESHOLD:
                sell_price = current_bar.high * 0.6 + current_bar.close * 0.4
                return "sell_b1", sell_price

        # ------ B1 trailing stop (only if not blocked) ------
        if not plan.batches[0].sold and not b1_blocked:
            profit_pct = (new_peak / plan.cost_price - 1) * 100
            trail = self._trail_pct(profit_pct, current_time, batch=1)
            stop_price = new_peak * (1 - trail / 100)
            if current <= stop_price:
                if current_bar.high > 0:
                    bar_drop2 = (current_bar.high - current) / current_bar.high * 100
                else:
                    bar_drop2 = 0
                sell_price = (current_bar.high + current) / 2 if bar_drop2 > 0.5 else current
                return "sell_b1", sell_price

        # ------ 14:25 forced closeout ------
        if h >= 14 and m >= 25:
            for bi in range(2):
                if not plan.batches[bi].sold:
                    return f"sell_b{bi+1}", current

        return "hold", current

    def backtest_sell(self, stock_code: str, cost_price: float,
                      minute_bars: list[MinuteBar], volume: int) -> dict:
        """Simulate sell on historical minute bars."""
        self.create_plan(stock_code, volume, cost_price)
        if minute_bars:
            plan = self._plans.get(stock_code)
            if plan:
                plan.day_open = minute_bars[0].open
        sell_prices: list[float] = []
        sell_times: list[str] = []
        sell_ratios: list[float] = []

        market = "SH" if stock_code.startswith("6") else "SZ"

        for i, bar in enumerate(minute_bars):
            plan = self._plans.get(stock_code)
            if plan and all(b.sold for b in plan.batches):
                break

            action, price = self.evaluate(
                stock_code, market, None, cost_price,
                bar.time_str, volume,
                is_backtest=True, backtest_bars=minute_bars,
                backtest_bar_index=i,
            )
            if action.startswith("sell_"):
                batch_num = int(action[-1])
                self.mark_batch_sold(stock_code, batch_num, price, bar.time_str)
                if price > 0:
                    sell_prices.append(price)
                    sell_times.append(bar.time_str)
                    sell_ratios.append(B1_RATIO if batch_num == 1 else B2_RATIO)

        # force sell remaining at last bar
        plan = self._plans.get(stock_code)
        if plan:
            last_bar = minute_bars[-1] if minute_bars else None
            last_price = last_bar.close if last_bar else cost_price
            last_time = last_bar.time_str if last_bar else "15:00"
            for i, b in enumerate(plan.batches):
                if not b.sold:
                    b.sold = True
                    b.sell_price = last_price
                    b.sell_time = last_time
                    b.trigger = "forced_close"
                    sell_prices.append(last_price)
                    sell_times.append(last_time)
                    sell_ratios.append(b.ratio)

        day_high = max(b.high for b in minute_bars) if minute_bars else cost_price
        day_low = min(b.low for b in minute_bars) if minute_bars else cost_price
        day_open = minute_bars[0].open if minute_bars else cost_price

        if sell_ratios and sum(sell_ratios) > 0:
            norm = sum(sell_ratios)
            avg_sell = sum(p * r for p, r in zip(sell_prices, sell_ratios)) / norm
        else:
            avg_sell = cost_price

        self.clear_plan(stock_code)

        return {
            "code": stock_code, "cost": cost_price,
            "avg_sell": round(avg_sell, 2),
            "sell_prices": [round(p, 2) for p in sell_prices],
            "sell_times": sell_times,
            "day_open": round(day_open, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "gross_pct": round((avg_sell / cost_price - 1) * 100, 2),
            "net_pct": round((avg_sell / cost_price - 1) * 100 - 0.117, 2),
            "batches": len(sell_prices),
        }
