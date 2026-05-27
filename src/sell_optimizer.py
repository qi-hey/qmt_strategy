# -*- coding: utf-8 -*-
"""Sell optimizer v2 — adaptive trailing stop + time decay + VWAP + volume.

Target: capture >70% of intraday range, monthly return >20%.

Strategy layers (evaluated in order):
  1. Opening gap — if gap >= 2%, sell 40% immediately
  2. Dynamic trailing stop — tighter as profit grows, looser for big runners
  3. Time decay — after 13:00, stops tighten progressively
  4. VWAP anchor — prefer selling above VWAP
  5. Volume spike — accelerate selling into high-volume candles
  6. Hard stop-loss — never let a winner turn into a loser

Three batches:
  B1 (40%): morning momentum capture (gap sell or trailing stop)
  B2 (35%): day-high hunting with trailing stop + VWAP guard
  B3 (25%): closeout at 14:55:30 — market sell remaining
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

FEE_RATE = 0.000085  # 0.0085%


# —— trailing stop by profit zone ——————————————————————————————————————
# (profit_pct, trail_pct_from_peak): sorted ascending; last match wins
TRAIL_ZONES: list[tuple[float, float]] = [
    (-99.0, 0.8),    # deep loss → tight 0.8% stop, cut quickly
    (-2.0,  1.0),    # -2%~0% → 1% stop
    (0.0,   1.2),    # 0%~2%   → 1.2% stop (protect breakeven)
    (2.0,   1.8),    # 2%~4%   → 1.8% stop
    (4.0,   2.5),    # 4%~7%   → 2.5% stop
    (7.0,   3.5),    # 7%+     → 3.5% stop (let runners run)
]

# B2 uses wider stops — more patience for remaining position
TRAIL_ZONES_B2: list[tuple[float, float]] = [
    (-99.0, 3.5),    # deep loss → 3.5% stop (survive violent swings)
    (-2.0,  4.0),    # -2%~0% → 4% stop (let bounce happen)
    (0.0,   4.5),    # 0%~2%   → 4.5% stop (very patient)
    (2.0,   5.5),    # 2%~4%   → 5.5% stop
    (4.0,   7.0),    # 4%~7%   → 7% stop
    (7.0,   8.0),    # 7%+     → 8% stop (let big runners fly)
]


# —— time decay: multiplier applied to trail distance ——————————————————
# (start_hour, start_min, multiplier)
TIME_DECAY: list[tuple[int, int, float]] = [
    (9, 30,  1.00),    # patient in morning
    (10, 30,  0.90),   # slight pressure
    (13, 0,   0.75),   # afternoon begins
    (14, 0,   0.55),   # getting late
    (14, 30,  0.35),   # final stretch
    (14, 50,  0.15),   # fire sale — just get out
]

# —— hard profit floor (trailing stop never goes below this profit %) ——
PROFIT_FLOOR = 1.0

# —— batch sizes ———————————————————————————————————————————————————————
B1_RATIO_MIN = 0.50   # min morning capture
B1_RATIO_MAX = 0.75   # max morning capture
# B2 takes the remaining (1 - B1 ratio)
# B3 removed — dynamic B1 replaces fixed split


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
    intraday_peak: float = 0.0
    day_open: float = 0.0
    vwap: float = 0.0
    gap_pct: float = 0.0


@dataclass
class MinuteBar:
    """One minute of OHLCV data."""
    time_str: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    vwap: float = 0.0


class SellOptimizer:
    """Self-learning sell engine v2."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.data_dir / "sell_history.json"
        self.config_path = self.data_dir / "sell_config.json"
        self._history: list[dict] = self._load_history()
        self._plans: dict[str, SellPlan] = {}
        self._intraday_peaks: dict[str, float] = {}

    # —— history / learning ————————————————————————————————————————————

    def _load_history(self) -> list[dict]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text("utf-8"))
        return []

    def _save_history(self) -> None:
        self.history_path.write_text(
            json.dumps(self._history[-1000:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_sell(self, stock_code: str, cost: float,
                    sell_prices: list[float], sell_times: list[str],
                    day_high: float, day_open: float, day_low: float,
                    total_volume: int) -> dict:
        """Record completed sell; return summary for learning."""
        if not sell_prices:
            return {}

        n = len(sell_prices)
        if n == 1:
            weights = [1.0]
        elif n == 2:
            w_sum = 0.55 + 0.45
            weights = [0.55 / w_sum, 0.45 / w_sum]
        else:
            weights = [0.55, 0.45]  # default, actual weights from plan

        avg_sell_w = sum(p * w for p, w in zip(sell_prices, weights))

        gross_profit = (avg_sell_w / cost - 1) * 100 if cost > 0 else 0
        net_profit = gross_profit - (FEE_RATE * 100 * 2) - 0.1  # fee both sides + stamp

        record = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "code": stock_code,
            "cost": round(cost, 2),
            "avg_sell": round(avg_sell_w, 2),
            "day_high": round(day_high, 2),
            "day_open": round(day_open, 2),
            "day_low": round(day_low, 2),
            "capture_rate": round(avg_sell_w / day_high, 3) if day_high > 0 else 0,
            "batches": n,
            "gross_pct": round(gross_profit, 2),
            "net_pct": round(net_profit, 2),
            "vol": total_volume,
        }
        self._history.append(record)
        self._save_history()

        recent = [r for r in self._history[-20:] if r["capture_rate"] > 0]
        if len(recent) >= 10:
            avg_cap = sum(r["capture_rate"] for r in recent) / len(recent)
            if avg_cap < 0.60:
                _log.info("Capture rate %.1f%% — consider tightening stops", avg_cap * 100)

        return record

    def get_stats(self) -> dict:
        if not self._history:
            return {"avg_capture": 0, "avg_profit": 0, "total_trades": 0}
        recent = self._history[-50:]
        captures = [h["capture_rate"] for h in recent if h["capture_rate"] > 0]
        profits = [h["net_pct"] for h in recent]
        wins = [h for h in recent if h["net_pct"] > 0]
        return {
            "avg_capture": round(sum(captures) / len(captures), 3) if captures else 0,
            "avg_profit": round(sum(profits) / len(profits), 2) if profits else 0,
            "win_rate": round(len(wins) / len(recent) * 100, 1) if recent else 0,
            "total_trades": len(self._history),
            "recent_5": self._history[-5:],
        }

    # —— sell plan —————————————————————————————————————————————————————

    def _calc_b1_ratio(self, gap_pct: float, profit_at_open: float) -> float:
        """Dynamic B1 ratio: 50-75% based on gap/profit."""
        if gap_pct >= 5.0 or profit_at_open >= 5.0:
            return 0.75
        elif gap_pct >= 3.0 or profit_at_open >= 3.0:
            return 0.68
        elif gap_pct >= 2.0 or profit_at_open >= 2.0:
            return 0.62
        elif gap_pct >= 1.0 or profit_at_open >= 1.0:
            return 0.55
        else:
            return 0.50

    def create_plan(self, stock_code: str, volume: int,
                    cost_price: float, buy_date: str = "",
                    gap_pct: float = 0.0, profit_at_open: float = 0.0) -> SellPlan:
        b1_ratio = self._calc_b1_ratio(gap_pct, profit_at_open)
        b2_ratio = 1.0 - b1_ratio
        batches = [
            SellBatch(batch=1, ratio=b1_ratio, trigger="pending"),
            SellBatch(batch=2, ratio=b2_ratio, trigger="pending"),
        ]
        plan = SellPlan(
            stock_code=stock_code, total_volume=volume,
            cost_price=cost_price, buy_date=buy_date,
            batches=batches, remaining=volume,
        )
        self._plans[stock_code] = plan
        return plan

    # —— time decay factor —————————————————————————————————————————————

    @staticmethod
    def _time_decay_factor(time_str: str) -> float:
        """Return multiplier in [0, 1]; 1 = patient, 0 = sell everything now."""
        try:
            h, m = map(int, time_str.split(":")[:2])
        except (ValueError, AttributeError):
            return 1.0
        factor = 1.0
        for th, tm, mult in TIME_DECAY:
            if h > th or (h == th and m >= tm):
                factor = mult
        return factor

    # —— trail distance lookup —————————————————————————————————————————

    @staticmethod
    def _trail_pct(profit_pct: float, time_str: str = "09:30",
                   batch: int = 1) -> float:
        """Get trailing stop distance for current profit level, adjusted for time.
        
        batch=1: standard B1 trail
        batch=2: wider B2 trail (2-3x more patient)
        """
        zones = TRAIL_ZONES_B2 if batch >= 2 else TRAIL_ZONES
        trail = 1.0  # default
        for lo, t in zones:
            if profit_pct >= lo:
                trail = t
        factor = SellOptimizer._time_decay_factor(time_str)
        return trail * factor

    # —— compute bars from xtdata output ———————————————————————————————

    @staticmethod
    def _parse_bars(xtdata: Any, full_code: str, cost_price: float) -> list[MinuteBar]:
        """Parse xtdata minute bars into normalized MinuteBar list."""
        try:
            df = xtdata.get_market_data_ex(
                field_list=["close", "high", "low", "open", "volume"],
                stock_list=[full_code],
                period="1m",
                start_time="", end_time="", count=240,
            )
        except Exception as e:
            _log.debug("xtdata parse error: %s", e)
            return []

        if full_code not in df or df[full_code].empty:
            return []

        data = df[full_code]
        bars: list[MinuteBar] = []
        cum_vol = 0
        cum_vp = 0.0  # volume * price
        for i in range(len(data)):
            try:
                o = float(data["open"].iloc[i])
                h = float(data["high"].iloc[i])
                l = float(data["low"].iloc[i])
                c = float(data["close"].iloc[i])
                v = int(data["volume"].iloc[i])
            except (ValueError, TypeError, IndexError):
                continue
            cum_vol += v
            cum_vp += v * c
            bars.append(MinuteBar(
                open=o, high=h, low=l, close=c, volume=v,
                vwap=cum_vp / cum_vol if cum_vol > 0 else c,
                time_str=f"{i // 60 + 9:02d}:{i % 60:02d}",
            ))
        return bars

    # —— THE CORE: evaluate sell signals ———————————————————————————————

    def evaluate(self, stock_code: str, market: str,
                 xtdata: Any, cost_price: float,
                 current_time: str, volume: int,
                 is_backtest: bool = False,
                 backtest_bars: list[MinuteBar] | None = None,
                 backtest_bar_index: int = 0) -> tuple[str, float]:
        """Evaluate sell signals for the current minute.

        Returns (action, price):
          action = "sell_b1" | "sell_b2" | "sell_b3" | "hold"
          price  = execution price for this batch
        """
        full_code = f"{stock_code}.{market}"

        if is_backtest and backtest_bars is not None:
            bars = backtest_bars[:backtest_bar_index + 1]
            if not bars:
                return "hold", 0.0
            current = bars[-1].close
            day_open = bars[0].open if bars else current
            day_high = max(b.high for b in bars)
            day_low = min(b.low for b in bars)
            vwap = bars[-1].vwap
        else:
            bars = self._parse_bars(xtdata, full_code, cost_price)
            if not bars:
                return "hold", 0.0
            current = bars[-1].close
            day_open = bars[0].open if bars else current
            day_high = max(b.high for b in bars)
            day_low = min(b.low for b in bars)
            vwap = bars[-1].vwap

        profit_pct = (current / cost_price - 1) * 100
        gap_pct = (day_open / cost_price - 1) * 100

        # track intraday peak
        key = f"{stock_code}_{cost_price}"
        if key not in self._intraday_peaks:
            self._intraday_peaks[key] = day_high
        elif day_high > self._intraday_peaks[key]:
            self._intraday_peaks[key] = day_high
        peak = self._intraday_peaks[key]

        # get plan
        plan = self._plans.get(stock_code)
        if plan is None:
            return "hold", current

        time_factor = self._time_decay_factor(current_time)
        trail_dist = self._trail_pct(profit_pct, current_time)

        # ——         # —— B1: opening gap / early profit capture ————————————————
        if not plan.batches[0].sold:
            b1_ratio = plan.batches[0].ratio

            # Big gap up >= 3%: sell B1 immediately at +3% from cost (lock profit)
            if gap_pct >= 3.0 or profit_pct >= 3.0:
                target_price = cost_price * 1.03
                _log.info("%s gap/profit %.1f%% → B1 sell at +3%% target %.2f",
                          stock_code, max(gap_pct, profit_pct), target_price)
                # If current price is above +3% target, sell at current (better)
                sell_at = max(current, target_price) if current > 0 else target_price
                return "sell_b1", sell_at

            # Moderate gap up 1-3%: sell B1 at current price (capture momentum)
            if gap_pct >= 1.0:
                _log.info("%s gap up %.1f%% → B1 sell at open %.2f",
                          stock_code, gap_pct, day_open)
                return "sell_b1", day_open

            # Profit > 2% without gap: sell B1 at current
            if profit_pct >= 2.0:
                _log.info("%s profit %.1f%% → B1 take profit at %.2f",
                          stock_code, profit_pct, current)
                return "sell_b1", current

            # Gap down >3%: emergency cut B1
            if gap_pct <= -3.0:
                _log.info("%s gap down %.1f%% → emergency cut B1", stock_code, gap_pct)
                return "sell_b1", day_open
        # —— trailing stop check —————————————————————————————————————
        trail_price = peak * (1 - trail_dist / 100)

        # hard floor: don't let trailing stop go below profit floor
        floor_price = cost_price * (1 + PROFIT_FLOOR / 100)
        if profit_pct > PROFIT_FLOOR:
            trail_price = max(trail_price, floor_price)

        # —— stop-loss: if profit was ever >2% and now <0.5%, sell ——
        stop_loss_triggered = False
        if peak / cost_price - 1 >= 0.02 and profit_pct < 0.5:
            stop_loss_triggered = True

        # —— volume spike detection —————————————————————————————————
        recent_vol = prev_vol = 0
        vol_spike = False
        if len(bars) >= 5:
            recent_vol = sum(b.volume for b in bars[-3:])
            if len(bars) >= 8:
                prev_vol = sum(b.volume for b in bars[-8:-3]) / 5 * 3
            else:
                prev_vol = recent_vol
            vol_spike = recent_vol > prev_vol * 2.5 and profit_pct > 1.0

        # —— decide which batch ——————————————————————————————————————

        h, m = 9, 30
        try:
            h, m = map(int, current_time.split(":")[:2])
        except ValueError:
            pass

        # B1 still unsold? Use trailing stop as backup (gap was small or flat)
        if not plan.batches[0].sold:
            if current <= trail_price and profit_pct > 0:
                return "sell_b1", current
            if stop_loss_triggered and h < 11:
                return "sell_b1", current
            if vol_spike and profit_pct >= 2.0 and h < 11:
                return "sell_b1", current
            # After 10:30, if B1 still unsold and profit > 1%, trigger
            if h >= 10 and m >= 30 and profit_pct > 1.0:
                return "sell_b1", current

        # B2: remaining position -- 3-4x wider trailing stop
        if plan.batches[0].sold and not plan.batches[1].sold:
            # Wider trail for B2 (3-4x standard)
            trail_dist_b2 = self._trail_pct(profit_pct, current_time, batch=2)
            trail_price_b2 = peak * (1 - trail_dist_b2 / 100)

            if current <= trail_price_b2:
                return "sell_b2", current

            # B2 deep stop-loss only
            if profit_pct <= -2.0:
                return "sell_b2", current

            # If profit > 5% and drops 3.5% from peak
            peak_drop = (current - peak) / peak * 100
            if profit_pct > 5.0 and peak_drop <= -3.5:
                return "sell_b2", current

            # After 14:40, be aggressive
            if h >= 14 and m >= 40 and profit_pct > 0:
                return "sell_b2", current


            if current <= trail_price_b2:
                return "sell_b2", current

            # B2 stop-loss: only trigger on DEEP loss (not mild retracement)
            if profit_pct <= -2.0:
                return "sell_b2", current

            # If profit > 5% and drops 3.5% from peak
            peak_drop = (current - peak) / peak * 100
            if profit_pct > 5.0 and peak_drop <= -3.5:
                return "sell_b2", current

            # After 14:40, be aggressive with B2
            if h >= 14 and m >= 40 and profit_pct > 0:
                return "sell_b2", current


            # If profit > 5% and drops 3.5% from peak
            peak_drop = (current - peak) / peak * 100
            if profit_pct > 5.0 and peak_drop <= -3.5:
                return "sell_b2", current

            # After 14:40, be aggressive with B2
            if h >= 14 and m >= 40 and profit_pct > 0:
                return "sell_b2", current


            # After 14:40, be aggressive with B2
            if h >= 14 and m >= 40 and profit_pct > 0:
                return "sell_b2", current


            # After cooling: use wider trail
            if current <= trail_price_b2:
                return "sell_b2", current

            # B2 stop-loss: only trigger on DEEP loss (not mild retracement)
            if profit_pct <= -2.0:
                return "sell_b2", current

            # If profit > 5% and drops 3.5% from peak (wider for B2)
            peak_drop = (current - peak) / peak * 100
            if profit_pct > 5.0 and peak_drop <= -3.5:
                return "sell_b2", current

            # After 14:40, be aggressive with B2
            if h >= 14 and m >= 40 and profit_pct > 0:
                return "sell_b2", current


        # B3: closeout at 14:55:30
        s = 0
        try:
            parts = current_time.replace(" ", ":").split(":")
            if len(parts) >= 3:
                s = int(parts[2])
        except ValueError:
            pass

        # B3: closeout — sell remaining at 14:55:30
        if h >= 14 and m >= 55 and s >= 30:
            if not plan.batches[2].sold:
                return "sell_b3", current

        # If past 14:56:30 and any unsold
        if h >= 14 and m >= 56 and s >= 30 and not plan.batches[2].sold:
            return "sell_b3", current

        return "hold", current

    # —— execute batch mark ———————————————————————————————————————————

    def mark_batch_sold(self, stock_code: str, batch: int,
                        price: float, time_str: str) -> None:
        plan = self._plans.get(stock_code)
        if plan and 1 <= batch <= 3:
            plan.batches[batch - 1].sold = True
            plan.batches[batch - 1].sell_price = price
            plan.batches[batch - 1].sell_time = time_str
            plan.remaining = int(plan.total_volume * (
                1 - sum(b.ratio for b in plan.batches if b.sold)
            ))

    def clear_plan(self, stock_code: str) -> None:
        self._plans.pop(stock_code, None)
        keys_to_del = [k for k in self._intraday_peaks if k.startswith(stock_code)]
        for k in keys_to_del:
            del self._intraday_peaks[k]

    # —— backtest helper ————————————————————————————————————————————

    def backtest_sell(self, stock_code: str, cost_price: float,
                      minute_bars: list[MinuteBar], volume: int) -> dict:
        """Simulate 3-batch sell on historical minute bars."""
        self.create_plan(stock_code, volume, cost_price, gap_pct=0, profit_at_open=0)
        sell_prices: list[float] = []
        sell_times: list[str] = []

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

        day_high = max(b.high for b in minute_bars) if minute_bars else cost_price
        day_low = min(b.low for b in minute_bars) if minute_bars else cost_price
        day_open = minute_bars[0].open if minute_bars else cost_price

        n = len(sell_prices)
        if n == 1:
            weights = [1.0]
        elif n == 2:
            w_sum = 0.55 + 0.45
            weights = [0.55 / w_sum, 0.45 / w_sum]
        else:
            weights = [0.55, 0.45]  # default, actual weights from plan
        avg_sell = sum(p * w for p, w in zip(sell_prices, weights)) / sum(weights) if weights else cost_price

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
            "batches": n,
        }
