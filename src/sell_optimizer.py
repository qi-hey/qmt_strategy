# -*- coding: utf-8 -*-
"""Self-learning sell optimizer.

Strategy:
- Max 3 batches per position
- Batch 1 (35%): sell during morning surge (09:30-10:00), target +3%+
- Batch 2 (35%): sell at intraday high detection (price drops 1% from peak)
- Batch 3 (30%): sell remaining near close (14:55) at market

Self-learning: tracks sell outcomes vs theoretical max, adjusts timing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class SellBatch:
    batch: int = 0        # 1, 2, or 3
    ratio: float = 0.0     # percentage of position to sell
    trigger: str = ""      # "morning_surge" / "peak_drop" / "closeout"
    target_pct: float = 0.0
    sold: bool = False
    sell_price: float = 0.0
    sell_time: str = ""


@dataclass
class SellPlan:
    stock_code: str = ""
    total_volume: int = 0
    cost_price: float = 0.0
    batches: list[SellBatch] = field(default_factory=list)
    remaining: int = 0


@dataclass
class PeakTracker:
    """Track intraday peak for trailing-stop sell."""
    peak_price: float = 0.0
    peak_time: str = ""
    current_price: float = 0.0
    drawdown_pct: float = 0.0


class SellOptimizer:
    """Self-learning sell engine.

    Tracks selling performance and adjusts strategy over time.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.data_dir / "sell_history.json"
        self._history: list[dict] = self._load_history()
        self._peak_trackers: dict[str, PeakTracker] = {}

    # ------------------------------------------------------------------
    # History / Learning
    # ------------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text("utf-8"))
        return []

    def _save_history(self) -> None:
        self.history_path.write_text(
            json.dumps(self._history[-500:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_sell(self, stock_code: str, cost: float, sell_prices: list[float],
                    day_high: float, day_open: float) -> None:
        """Record a completed sell to train the optimizer."""
        if not sell_prices:
            return
        avg_sell = sum(sell_prices) / len(sell_prices)
        theoretical_max = day_high
        capture_rate = avg_sell / theoretical_max if theoretical_max > 0 else 0

        self._history.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "code": stock_code,
            "cost": cost,
            "avg_sell": round(avg_sell, 2),
            "day_high": round(day_high, 2),
            "day_open": round(day_open, 2),
            "capture_rate": round(capture_rate, 3),
            "batches": len(sell_prices),
            "profit_pct": round((avg_sell / cost - 1) * 100, 2),
        })
        self._save_history()

    def get_stats(self) -> dict:
        """Get aggregated sell performance stats."""
        if not self._history:
            return {"avg_capture": 0, "avg_profit": 0, "total_trades": 0}
        caps = [h["capture_rate"] for h in self._history]
        profits = [h["profit_pct"] for h in self._history]
        return {
            "avg_capture": round(sum(caps) / len(caps), 3),
            "avg_profit": round(sum(profits) / len(profits), 2),
            "total_trades": len(self._history),
            "recent_5": self._history[-5:],
        }

    # ------------------------------------------------------------------
    # Sell plan creation
    # ------------------------------------------------------------------

    def create_plan(self, stock_code: str, volume: int, cost_price: float) -> SellPlan:
        """Create a 3-batch sell plan for a position."""
        batches = [
            SellBatch(batch=1, ratio=0.35, trigger="morning_surge", target_pct=3.0),
            SellBatch(batch=2, ratio=0.35, trigger="peak_drop", target_pct=5.0),
            SellBatch(batch=3, ratio=0.30, trigger="closeout", target_pct=0.0),
        ]
        return SellPlan(
            stock_code=stock_code,
            total_volume=volume,
            cost_price=cost_price,
            batches=batches,
            remaining=volume,
        )

    # ------------------------------------------------------------------
    # Morning surge detection (Batch 1)
    # ------------------------------------------------------------------

    def check_morning_surge(self, stock_code: str, market: str,
                            xtdata: Any, cost_price: float,
                            current_time: str) -> tuple[bool, float]:
        """Check if morning surge sell signal triggers.

        Returns (should_sell, current_price).
        """
        full_code = f"{stock_code}.{market}"
        try:
            df = xtdata.get_market_data_ex(
                field_list=["close", "high", "volume"],
                stock_list=[full_code],
                period="1m",
                start_time="", end_time="", count=30,
            )
        except Exception:
            return False, 0.0

        if full_code not in df or df[full_code].empty:
            return False, 0.0

        data = df[full_code]
        close_prices = data["close"].tolist()
        volumes = data["volume"].tolist()

        if not close_prices:
            return False, 0.0

        current = close_prices[-1]
        day_open = close_prices[0] if len(close_prices) > 1 else current

        # Calculate profit %
        profit_pct = (current / cost_price - 1) * 100 if cost_price > 0 else 0

        # Signal: profit > 3% with increasing volume
        if profit_pct >= 3.0:
            recent_vol = sum(volumes[-3:]) if len(volumes) >= 3 else 0
            prev_vol = sum(volumes[-6:-3]) if len(volumes) >= 6 else 0
            vol_expanding = recent_vol > prev_vol * 1.2 if prev_vol > 0 else False

            if vol_expanding:
                return True, current

        # Track peak
        if stock_code not in self._peak_trackers:
            self._peak_trackers[stock_code] = PeakTracker()
        pk = self._peak_trackers[stock_code]

        if current > pk.peak_price:
            pk.peak_price = current
            pk.peak_time = current_time

        # Signal: dropped 1.5% from intraday peak (trailing stop)
        if pk.peak_price > 0:
            dd = (current - pk.peak_price) / pk.peak_price * 100
            if dd <= -1.5 and profit_pct > 1.0:
                return True, current

        return False, current

    # ------------------------------------------------------------------
    # Peak drop detection (Batch 2)
    # ------------------------------------------------------------------

    def check_peak_drop(self, stock_code: str, market: str,
                        xtdata: Any, cost_price: float) -> tuple[bool, float]:
        """Check for peak-drop sell signal during the day."""
        full_code = f"{stock_code}.{market}"
        try:
            df = xtdata.get_market_data_ex(
                field_list=["close", "high"],
                stock_list=[full_code],
                period="1m",
                start_time="", end_time="", count=240,  # full day
            )
        except Exception:
            return False, 0.0

        if full_code not in df or df[full_code].empty:
            return False, 0.0

        data = df[full_code]
        highs = data["high"].tolist()
        closes = data["close"].tolist()

        if not closes or not highs:
            return False, 0.0

        current = closes[-1]
        day_high = max(highs) if highs else current

        profit_pct = (current / cost_price - 1) * 100 if cost_price > 0 else 0

        # Sell if: reached 5%+ and now dropped 2% from high
        if day_high / cost_price - 1 >= 0.05 and (current - day_high) / day_high * 100 <= -2.0:
            return True, current

        # Sell if: profit but price breaking below MA10
        if len(closes) >= 10:
            ma10 = sum(closes[-10:]) / 10
            if current < ma10 and profit_pct > 0.5:
                return True, current

        return False, current

    # ------------------------------------------------------------------
    # Day high tracking
    # ------------------------------------------------------------------

    def get_day_high(self, stock_code: str, market: str, xtdata: Any) -> float:
        """Get the highest price of the day so far."""
        full_code = f"{stock_code}.{market}"
        try:
            df = xtdata.get_market_data_ex(
                field_list=["high"],
                stock_list=[full_code],
                period="1d",
                start_time="", end_time="", count=1,
            )
            if full_code in df and not df[full_code].empty:
                return float(df[full_code]["high"].iloc[-1])
        except Exception:
            pass
        return 0.0
