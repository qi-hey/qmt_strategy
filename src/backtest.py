# -*- coding: utf-8 -*-
"""Backtesting engine - target monthly return > 20%."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

_log = logging.getLogger(__name__)
FEE_RATE = 0.000085

@dataclass
class Trade:
    date: str = ""; code: str = ""; action: str = ""
    price: float = 0.0; volume: int = 0

@dataclass
class BacktestResult:
    total_trades: int = 0; win_count: int = 0; loss_count: int = 0
    win_rate: float = 0.0; total_return: float = 0.0
    monthly_return: float = 0.0; max_drawdown: float = 0.0
    trades: list = field(default_factory=list)

class Backtest:
    def __init__(self, data_dir: Path): self.data_dir = data_dir

    def run(self, start_date: str, end_date: str, initial_capital: float = 100000) -> BacktestResult:
        from xtquant import xtdata
        xtdata.connect(port=58610)
        result = BacktestResult()
        cash = initial_capital; positions = {}
        dates = []
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        d = start
        while d <= end:
            if d.weekday() < 5: dates.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        daily_values = [initial_capital]
        for date_str in dates:
            date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            for code in list(positions.keys()):
                pos = positions[code]
                if pos["buy_date"] == date: continue
                market = "SH" if code.startswith("6") else "SZ"
                try:
                    df = xtdata.get_market_data_ex(
                        field_list=["open","high","close"], stock_list=[f"{code}.{market}"],
                        period="1d", start_time=date_str, end_time=date_str, count=1)
                    if f"{code}.{market}" not in df or df[f"{code}.{market}"].empty: continue
                    row = df[f"{code}.{market}"].iloc[-1]
                    high_p = float(row["high"]); open_p = float(row["open"])
                    cost_p = pos["cost"]; vol = pos["volume"]
                    sell_p = cost_p * 1.03 if high_p >= cost_p * 1.03 else open_p
                    revenue = vol * sell_p * (1 - FEE_RATE)
                    cash += revenue
                    result.trades.append(Trade(date=date,code=code,action="sell",price=sell_p,volume=vol))
                    if sell_p > cost_p: result.win_count += 1
                    else: result.loss_count += 1
                    del positions[code]
                except Exception: continue
            daily_values.append(cash + sum(p["volume"]*p["cost"] for p in positions.values()))
        result.total_trades = len(result.trades)
        n = max(result.total_trades/2, 1)
        result.win_rate = result.win_count/n*100
        final = daily_values[-1]
        result.total_return = (final/initial_capital-1)*100
        months = len(dates)/21
        result.monthly_return = ((final/initial_capital)**(1/months)-1)*100 if months>0 else 0
        peak = daily_values[0]; max_dd = 0
        for v in daily_values:
            if v>peak: peak=v
            dd = (peak-v)/peak*100
            if dd>max_dd: max_dd=dd
        result.max_drawdown = max_dd
        return result
