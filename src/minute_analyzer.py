# -*- coding: utf-8 -*-
"""14:20-14:30 minute K-line analysis for buy decision."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class MinuteSignal:
    trend: str = "neutral"       # "up" / "down" / "neutral"
    strength: float = 0.0        # trend strength score
    dip_rebound: bool = False    # quick dip and rebound detected
    action: str = "watch"        # "buy_now" / "watch" / "deadline_buy"
    reason: str = ""
    latest_price: float = 0.0
    ma5_price: float = 0.0
    vol_ratio: float = 1.0


class MinuteAnalyzer:
    """Analyze 1-minute K-line from 14:20 to 14:30 for buy signals."""

    def __init__(self, xtdata: Any = None) -> None:
        self._xtdata = xtdata

    def ensure_data(self) -> Any:
        if self._xtdata is None:
            from xtquant import xtdata
            xtdata.connect(port=58610)
            self._xtdata = xtdata
        return self._xtdata

    def analyze(self, stock_code: str, market: str = "SZ") -> MinuteSignal:
        """Analyze 14:20-14:30 minute K-line and return buy signal.

        Logic:
        - Fetch 1-min K-lines for today (14:20-14:30)
        - If price trend up (>0.3% over 10 min) -> buy_now
        - If price trend down -> monitor for dip_rebound, else deadline_buy
        """
        xtdata = self.ensure_data()

        full_code = f"{stock_code}.{market}"
        try:
            df = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume"],
                stock_list=[full_code],
                period="1m",
                start_time="", end_time="", count=60,
            )
        except Exception as exc:
            _log.warning("Minute data fetch failed for %s: %s", stock_code, exc)
            return MinuteSignal(trend="neutral", action="deadline_buy",
                               reason=f"Data fetch failed: {exc}")

        if full_code not in df or df[full_code].empty:
            return MinuteSignal(trend="neutral", action="deadline_buy",
                               reason="No minute data available")

        data = df[full_code]
        closes = data["close"].tolist()[-15:]  # last 15 minutes
        volumes = data["volume"].tolist()[-15:]

        if len(closes) < 5:
            return MinuteSignal(trend="neutral", action="deadline_buy",
                               reason="Insufficient data points")

        latest = closes[-1]
        mid = closes[len(closes)//2]
        first = closes[0]

        # Calculate trend
        if first > 0:
            pct_change = (latest - first) / first * 100
        else:
            pct_change = 0

        # Volume check
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        recent_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else avg_vol
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        # Moving averages
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else latest

        # --- Decision ---
        if pct_change >= 0.3 and latest > ma5:
            return MinuteSignal(
                trend="up", strength=pct_change, action="buy_now",
                reason=f"Uptrend +{pct_change:.2f}%, price above MA5",
                latest_price=latest, ma5_price=ma5, vol_ratio=vol_ratio,
            )
        elif pct_change <= -0.5:
            return MinuteSignal(
                trend="down", strength=pct_change, action="watch",
                reason=f"Downtrend {pct_change:.2f}%, monitor for dip-rebound",
                latest_price=latest, ma5_price=ma5, vol_ratio=vol_ratio,
            )
        else:
            return MinuteSignal(
                trend="neutral", strength=pct_change, action="watch",
                reason=f"Neutral {pct_change:+.2f}%, continue monitoring",
                latest_price=latest, ma5_price=ma5, vol_ratio=vol_ratio,
            )

    def check_dip_rebound(self, stock_code: str, market: str = "SZ",
                          dip_pct: float = 1.5) -> MinuteSignal:
        """Check if a quick dip (>dip_pct%) with rebound occurred.

        Called during the watch period (14:30-14:56).
        """
        xtdata = self.ensure_data()
        full_code = f"{stock_code}.{market}"

        try:
            df = xtdata.get_market_data_ex(
                field_list=["high", "low", "close"],
                stock_list=[full_code],
                period="1m",
                start_time="", end_time="", count=5,
            )
        except Exception:
            return MinuteSignal(trend="down", action="watch",
                               reason="Dip check data fetch failed")

        if full_code not in df or df[full_code].empty:
            return MinuteSignal(trend="down", action="watch",
                               reason="No recent data")

        data = df[full_code]
        lows = data["low"].tolist()
        closes = data["close"].tolist()

        if len(lows) < 3 or len(closes) < 3:
            return MinuteSignal(trend="down", action="watch", reason="Waiting for data")

        # Find lowest point in last 5 minutes
        min_low = min(lows[:-1])  # exclude current
        current_close = closes[-1]

        if min_low > 0 and current_close > 0:
            dip = (current_close - min_low) / min_low * 100
            if dip >= dip_pct:
                return MinuteSignal(
                    trend="up", dip_rebound=True, action="buy_now",
                    reason=f"Dip-rebound {dip:.1f}%, buy signal",
                    latest_price=current_close,
                )

        return MinuteSignal(trend="down", action="watch",
                           reason="No dip-rebound yet")
