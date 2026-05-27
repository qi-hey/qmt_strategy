# -*- coding: utf-8 -*-
"""Backtest engine v2 — minute-level simulation with adaptive sell.

Key features:
  - Daily screening via a_stock_strategy screener or simplified rules
  - Buy at day's closing price (14:56 simulation)
  - Sell via SellOptimizer.backtest_sell() on next-day minute bars
  - Tracks MFE/MAE, batch-level P&L, daily NAV
  - Target: monthly return > 20%
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# add a_stock_strategy to path for screener
_A_STOCK = Path(__file__).resolve().parent.parent.parent / "a_stock_strategy" / "src"
if str(_A_STOCK) not in sys.path:
    sys.path.insert(0, str(_A_STOCK))

_log = logging.getLogger(__name__)

FEE_RATE = 0.000085      # 0.0085% per side
STAMP_TAX = 0.001        # 0.1% sell only
MAX_POSITIONS = 5
POSITION_SIZE = 0.20     # 20% of capital per position
INITIAL_CAPITAL = 100_000


@dataclass
class Trade:
    date: str = ""
    code: str = ""
    name: str = ""
    action: str = ""       # buy / sell_b1 / sell_b2 / sell_b3
    price: float = 0.0
    volume: int = 0
    amount: float = 0.0    # total value
    fee: float = 0.0
    profit_pct: float = 0.0


@dataclass
class Position:
    code: str = ""
    name: str = ""
    buy_date: str = ""
    buy_price: float = 0.0
    volume: int = 0
    amount: float = 0.0    # total cost incl fee
    sold: bool = False
    sell_results: dict | None = None


@dataclass
class BacktestResult:
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = INITIAL_CAPITAL
    final_capital: float = INITIAL_CAPITAL
    total_return: float = 0.0
    monthly_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    daily_values: list[dict] = field(default_factory=list)
    monthly_returns: list[dict] = field(default_factory=list)


class Backtest:
    """Backtest engine with minute-level sell simulation."""

    def __init__(self, data_dir: Path, screener_project: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.screener_project = Path(screener_project) if screener_project else None
        self._cache: dict[str, Any] = {}

    # —— helpers ——————————————————————————————————————————————————————

    def _trading_days(self, start: str, end: str) -> list[str]:
        """List all trading days between start and end (YYYYMMDD format)."""
        from xtquant import xtdata
        try:
            # Try multiple formats
            days = xtdata.get_trading_dates("SH", start, end)
            if not days:
                # Try with shorter date format
                days = xtdata.get_trading_dates("SH", start[:6], end[:6])
            result = []
            for d in days:
                if isinstance(d, str):
                    d_clean = d.replace("-", "").replace("/", "")
                    if len(d_clean) == 8:
                        result.append(d_clean)
                    elif len(d_clean) == 6:
                        result.append(d_clean)
                elif isinstance(d, int):
                    result.append(str(d))
            if result:
                return result
        except Exception as e:
            _log.debug("QMT trading days failed: %s", e)
        
        # fallback: all weekdays
        result = []
        try:
            s = datetime.strptime(start[:8], "%Y%m%d")
            e = datetime.strptime(end[:8], "%Y%m%d")
            d = s
            while d <= e:
                if d.weekday() < 5:
                    result.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        except ValueError:
            # Try YYYY-MM-DD format
            try:
                s = datetime.strptime(start[:10], "%Y-%m-%d")
                e = datetime.strptime(end[:10], "%Y-%m-%d")
                d = s
                while d <= e:
                    if d.weekday() < 5:
                        result.append(d.strftime("%Y%m%d"))
                    d += timedelta(days=1)
            except ValueError:
                pass
        return result

    def _get_code_market(self, code: str) -> str:
        return "SH" if code.startswith(("6", "9")) else "SZ"

    # —— stock universe ———————————————————————————————————————————————

    def _get_universe(self) -> list[dict]:
        """Get A-share universe for backtesting."""
        try:
            from xtquant import xtdata
            # Download sector data first
            xtdata.download_sector_data()
            all_a = xtdata.get_stock_list_in_sector("沪深A股")
            if all_a and len(all_a) > 100:
                stocks = []
                for code in all_a:
                    if code.startswith(("300", "301", "688")):
                        continue
                    if len(code) != 6 or not code.isdigit():
                        continue
                    stocks.append({"code": code, "market": self._get_code_market(code)})
                _log.info("QMT universe: %d stocks (from sector)", len(stocks))
                return stocks
        except Exception as e:
            _log.warning("QMT sector failed: %s", e)
        
        # Fallback: build from common codes
        _log.info("Building fallback universe from code ranges...")
        stocks = []
        for i in range(0, 6000):
            stocks.append({"code": f"60{i:04d}", "market": "SH"})
        for i in range(0, 4000):
            stocks.append({"code": f"00{i:04d}", "market": "SZ"})
        # Sample to avoid overwhelming QMT
        return stocks[::3][:1500]  # ~1500 stocks

    # —— daily bar cache ——————————————————————————————————————————————

    def _get_daily_bar(self, code: str, market: str, date_str: str) -> dict | None:
        """Get daily bar for a stock on a specific date."""
        cache_key = f"d_{code}_{date_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            from xtquant import xtdata
            full = f"{code}.{market}"
            df = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=[full], period="1d",
                start_time=date_str, end_time=date_str, count=1,
            )
            if full in df and not df[full].empty:
                row = df[full].iloc[-1]
                result = {
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "amount": float(row["amount"]),
                }
                self._cache[cache_key] = result
                return result
        except Exception:
            pass
        return None

    # —— minute bar cache —————————————————————————————————————————————

    def _get_minute_bars(self, code: str, market: str,
                         date_str: str) -> list | None:
        """Get minute bars for a stock on a specific date."""
        cache_key = f"m_{code}_{date_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            from xtquant import xtdata
            from sell_optimizer import MinuteBar

            full = f"{code}.{market}"
            df = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume"],
                stock_list=[full], period="1m",
                start_time=date_str, end_time=date_str, count=240,
            )
            if full not in df or df[full].empty:
                # try downloading
                xtdata.download_history_data(full, "1m", date_str, date_str)
                time.sleep(0.3)
                df = xtdata.get_market_data_ex(
                    field_list=["open", "high", "low", "close", "volume"],
                    stock_list=[full], period="1m",
                    start_time=date_str, end_time=date_str, count=240,
                )
            if full not in df or df[full].empty:
                return None

            data = df[full]
            bars = []
            cum_vol = 0
            cum_vp = 0.0
            for i in range(len(data)):
                try:
                    o = float(data["open"].iloc[i])
                    h = float(data["high"].iloc[i])
                    l = float(data["low"].iloc[i])
                    c = float(data["close"].iloc[i])
                    v = int(data["volume"].iloc[i])
                except Exception:
                    continue
                if o == 0 and h == 0 and l == 0 and c == 0:
                    continue
                cum_vol += v
                cum_vp += v * c
                bar = MinuteBar(
                    open=o, high=h, low=l, close=c, volume=v,
                    vwap=cum_vp / cum_vol if cum_vol > 0 else c,
                    time_str=f"{i // 60 + 9:02d}:{i % 60:02d}",
                )
                bars.append(bar)

            if bars:
                self._cache[cache_key] = bars
                return bars
        except Exception as e:
            _log.debug("Minute bars failed for %s on %s: %s", code, date_str, e)

        return None

    # —— simplified screener —————————————————————————————————————————

    def _screen_daily(self, date_str: str, universe: list[dict],
                      top_n: int = 15) -> list[dict]:
        """Simplified screen using daily bar data.
        Conditions: market cap 90-280B estimate, return 3-7%,
                    turnover 3-10%, volume ratio 1-10, no ST.
        """
        candidates = []
        for stock in universe:
            code = stock["code"]
            market = stock["market"]
            bar = self._get_daily_bar(code, market, date_str)
            if bar is None:
                continue
            close = bar["close"]
            open_p = bar["open"]
            prev_close = open_p  # approximate, will use actual data
            # get previous day close
            prev_date = self._get_prev_trading_day(date_str)
            prev_bar = self._get_daily_bar(code, market, prev_date)
            if prev_bar is None:
                continue
            prev_close = prev_bar["close"]
            if prev_close <= 0:
                continue

            ret_pct = (close / prev_close - 1) * 100
            if ret_pct < 3.0 or ret_pct > 7.0:
                continue

            # turnover rate estimate
            vol = bar["volume"]
            if vol <= 0:
                continue
            # rough turnover: volume / (market_cap / close) but we don't have market cap
            # use a simpler filter: skip stocks with very low volume
            amount = bar["amount"]
            if amount < 50_000_000:  # < 5000万成交额，没流动性
                continue

            # rough market cap estimate from amount/volume * close
            # actually let me skip market cap filter here - we'll use the screener from a_stock_strategy

            candidates.append({
                "code": code, "market": market,
                "close": close, "ret_pct": round(ret_pct, 2),
                "volume": vol, "amount": amount,
            })

        # sort by return desc, take top N
        candidates.sort(key=lambda x: x["ret_pct"], reverse=True)
        return candidates[:top_n]

    def _get_prev_trading_day(self, date_str: str) -> str:
        """Get previous trading day."""
        from xtquant import xtdata
        try:
            days = xtdata.get_trading_dates("SH", "20200101", date_str)
            if days and len(days) >= 2:
                return str(days[-2])
        except Exception:
            pass
        d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

    # —— MAIN BACKTEST LOOP ——————————————————————————————————————————

    def run(self, start_date: str, end_date: str,
            initial_capital: float = INITIAL_CAPITAL,
            use_screener: bool = True) -> BacktestResult:
        """Run full backtest.

        Args:
            start_date: YYYY-MM-DD or YYYYMMDD
            end_date: YYYY-MM-DD or YYYYMMDD
            initial_capital: starting cash
            use_screener: if True, use a_stock_strategy screener;
                         if False, use simplified screen
        """
        from sell_optimizer import SellOptimizer

        sd = start_date.replace("-", "")[:8]
        ed = end_date.replace("-", "")[:8]

        result = BacktestResult(
            start_date=start_date, end_date=end_date,
            initial_capital=initial_capital,
        )

        sell_opt = SellOptimizer(self.data_dir / "sell_history")

        days = self._trading_days(sd, ed)
        _log.info("Backtest: %d trading days from %s to %s", len(days), sd, ed)

        universe = self._get_universe()
        _log.info("Universe: %d stocks", len(universe))

        cash = initial_capital
        positions: list[Position] = []
        daily_values: list[dict] = []
        monthly_returns: list[dict] = []

        peak_value = initial_capital
        max_drawdown = 0.0

        # cache daily returns for sharpe
        daily_returns: list[float] = []

        for i, date_str in enumerate(days):
            date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            # —— SELL: process positions that need selling today ——
            for pos in list(positions):
                if pos.sold:
                    continue
                if pos.buy_date == date:
                    continue  # bought today, T+1

                market = self._get_code_market(pos.code)
                minute_bars = self._get_minute_bars(pos.code, market, date_str)

                if minute_bars is None or len(minute_bars) < 10:
                    # fallback: sell at open using daily bar
                    bar = self._get_daily_bar(pos.code, market, date_str)
                    if bar:
                        sell_price = bar["open"]
                        sell_amount = pos.volume * sell_price * (1 - FEE_RATE - STAMP_TAX)
                        cash += sell_amount
                        profit = (sell_price / pos.buy_price - 1) * 100
                        result.trades.append(Trade(
                            date=date, code=pos.code, name=pos.name,
                            action="sell_b1", price=sell_price,
                            volume=pos.volume, amount=sell_amount,
                            fee=sell_amount * (FEE_RATE + STAMP_TAX),
                            profit_pct=round(profit, 2),
                        ))
                        pos.sold = True
                        if profit > 0:
                            result.win_count += 1
                        else:
                            result.loss_count += 1
                    positions.remove(pos)
                    continue

                # run sell optimizer on minute bars
                sell_result = sell_opt.backtest_sell(
                    pos.code, pos.buy_price, minute_bars, pos.volume,
                )

                # execute sells
                for j, (sp, st) in enumerate(zip(
                    sell_result.get("sell_prices", []),
                    sell_result.get("sell_times", [])
                )):
                    batch_num = j + 1
                    vol_ratio = [0.40, 0.35, 0.25][j] if j < 3 else 0.25
                    vol = int(pos.volume * vol_ratio)
                    if j == len(sell_result.get("sell_prices", [])) - 1:
                        # last batch: sell remaining
                        sold_so_far = sum(
                            int(pos.volume * [0.40, 0.35, 0.25][k])
                            for k in range(j)
                        )
                        vol = pos.volume - sold_so_far

                    amount = vol * sp
                    fee = amount * (FEE_RATE + STAMP_TAX)
                    cash += amount - fee

                    profit = (sp / pos.buy_price - 1) * 100
                    result.trades.append(Trade(
                        date=date, code=pos.code, name=pos.name,
                        action=f"sell_b{batch_num}", price=sp,
                        volume=vol, amount=amount, fee=fee,
                        profit_pct=round(profit, 2),
                    ))

                pos.sold = True
                pos.sell_results = sell_result
                net = sell_result.get("net_pct", 0)
                if net > 0:
                    result.win_count += 1
                else:
                    result.loss_count += 1

                positions.remove(pos)

            # calculate current portfolio value
            portfolio_value = cash
            for pos in positions:
                if pos.sold:
                    continue
                bar = self._get_daily_bar(pos.code, self._get_code_market(pos.code), date_str)
                if bar:
                    portfolio_value += pos.volume * bar["close"]

            daily_values.append({
                "date": date, "cash": round(cash, 2),
                "holdings": len([p for p in positions if not p.sold]),
                "total": round(portfolio_value, 2),
                "return_pct": round(
                    (portfolio_value / (daily_values[-1]["total"] if daily_values else initial_capital) - 1) * 100, 2
                ) if daily_values else 0,
            })

            prev_total = daily_values[-2]["total"] if len(daily_values) >= 2 else initial_capital
            daily_ret = (portfolio_value / prev_total - 1) if prev_total > 0 else 0
            daily_returns.append(daily_ret)

            # update drawdown
            if portfolio_value > peak_value:
                peak_value = portfolio_value
            dd = (peak_value - portfolio_value) / peak_value * 100
            if dd > max_drawdown:
                max_drawdown = dd

            # —— BUY: screen and buy at close ——
            if len([p for p in positions if not p.sold]) < MAX_POSITIONS:
                slots = MAX_POSITIONS - len([p for p in positions if not p.sold])

                if use_screener and self.screener_project:
                    candidates = self._run_a_stock_screener(date)
                else:
                    candidates = self._screen_daily(date_str, universe, top_n=slots * 3)

                for cand in candidates[:slots]:
                    code = cand["code"]
                    market = cand.get("market", self._get_code_market(code))

                    bar = self._get_daily_bar(code, market, date_str)
                    if bar is None or bar["close"] <= 0:
                        continue

                    buy_price = bar["close"]
                    # estimate volume based on position size
                    position_amount = cash * POSITION_SIZE
                    vol = int(position_amount / buy_price / 100) * 100
                    if vol < 100:
                        continue

                    cost = vol * buy_price
                    fee = cost * FEE_RATE
                    if cash < cost + fee:
                        continue

                    cash -= cost + fee
                    positions.append(Position(
                        code=code,
                        name=cand.get("name", code),
                        buy_date=date,
                        buy_price=buy_price,
                        volume=vol,
                        amount=cost + fee,
                    ))
                    result.trades.append(Trade(
                        date=date, code=code, name=cand.get("name", code),
                        action="buy", price=buy_price, volume=vol,
                        amount=cost, fee=fee,
                    ))

            # log progress
            if (i + 1) % 20 == 0:
                _log.info("Day %d/%d: value=%.0f, pos=%d, trades=%d",
                          i + 1, len(days), portfolio_value,
                          len([p for p in positions if not p.sold]),
                          len(result.trades))

        # —— finalize ——
        # force sell any remaining positions at last day close
        if positions:
            last_date = days[-1]
            last_d = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
            for pos in positions:
                if pos.sold:
                    continue
                bar = self._get_daily_bar(pos.code, self._get_code_market(pos.code), last_date)
                sell_price = bar["close"] if bar else pos.buy_price
                amount = pos.volume * sell_price
                fee = amount * (FEE_RATE + STAMP_TAX)
                cash += amount - fee
                profit = (sell_price / pos.buy_price - 1) * 100
                result.trades.append(Trade(
                    date=last_d, code=pos.code, name=pos.name,
                    action="sell_closeout", price=sell_price,
                    volume=pos.volume, amount=amount, fee=fee,
                    profit_pct=round(profit, 2),
                ))
                if profit > 0:
                    result.win_count += 1
                else:
                    result.loss_count += 1

        # metrics
        result.final_capital = round(cash, 2)
        result.total_trades = len(result.trades)
        total_sells = result.win_count + result.loss_count
        if total_sells > 0:
            result.win_rate = round(result.win_count / total_sells * 100, 1)
        result.total_return = round((cash / initial_capital - 1) * 100, 2)

        months = max(len(days) / 21, 1)
        result.monthly_return = round(
            ((cash / initial_capital) ** (1 / months) - 1) * 100, 2
        )

        result.max_drawdown = round(max_drawdown, 2)
        result.daily_values = daily_values

        # sharpe ratio
        if daily_returns and len(daily_returns) > 1:
            avg_dr = sum(daily_returns) / len(daily_returns)
            var_dr = sum((r - avg_dr) ** 2 for r in daily_returns) / len(daily_returns)
            std_dr = var_dr ** 0.5
            if std_dr > 0:
                result.sharpe_ratio = round(avg_dr / std_dr * (252 ** 0.5), 2)

        # avg win/loss, profit factor
        sell_trades = [t for t in result.trades if t.action.startswith("sell")]
        wins = [t.profit_pct for t in sell_trades if t.profit_pct > 0]
        losses = [t.profit_pct for t in sell_trades if t.profit_pct <= 0]
        result.avg_win = round(sum(wins) / len(wins), 2) if wins else 0
        result.avg_loss = round(sum(losses) / len(losses), 2) if losses else 0

        total_gain = sum(wins) if wins else 0
        total_loss = abs(sum(losses)) if losses else 1
        result.profit_factor = round(total_gain / total_loss, 2) if total_loss > 0 else 0

        # monthly breakdown
        monthly_map: dict[str, list[float]] = {}
        for dv in daily_values:
            month_key = dv["date"][:7]
            monthly_map.setdefault(month_key, []).append(dv["total"])
        for month_key, vals in sorted(monthly_map.items()):
            first = vals[0]
            last = vals[-1]
            monthly_returns.append({
                "month": month_key,
                "return_pct": round((last / first - 1) * 100, 2),
                "end_value": round(last, 2),
            })
        result.monthly_returns = monthly_returns

        return result

    # —— use a_stock_strategy screener ———————————————————————————————

    def _run_a_stock_screener(self, date: str) -> list[dict]:
        """Run the a_stock_strategy screener for a given date."""
        try:
            from screener import Screener
            from trading_rules import TradingRules

            s = Screener(self.screener_project.parent / "data")
            rules = TradingRules()
            candidates = s.screen(date=date, top_n=10)
            return [
                {"code": c.get("code", ""), "name": c.get("name", ""),
                 "close": c.get("close", 0), "market": c.get("market", "")}
                for c in candidates
            ]
        except Exception as e:
            _log.warning("a_stock screener failed: %s, using simplified", e)
            return []

    # —— save results ————————————————————————————————————————————————

    def save_result(self, result: BacktestResult, path: Path) -> None:
        """Save backtest result to JSON."""
        data = {
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
            "total_return_pct": result.total_return,
            "monthly_return_pct": result.monthly_return,
            "max_drawdown_pct": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "total_trades": result.total_trades,
            "win_count": result.win_count,
            "loss_count": result.loss_count,
            "win_rate_pct": result.win_rate,
            "avg_win_pct": result.avg_win,
            "avg_loss_pct": result.avg_loss,
            "profit_factor": result.profit_factor,
            "monthly_returns": result.monthly_returns,
            "last_10_trades": [
                {"date": t.date, "code": t.code, "action": t.action,
                 "price": t.price, "profit_pct": t.profit_pct}
                for t in result.trades[-10:]
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _log.info("Backtest result saved to %s", path)
