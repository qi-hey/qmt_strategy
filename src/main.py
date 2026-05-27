# -*- coding: utf-8 -*-
"""QMT Quantitative Trading Strategy - Main Entry Point.

Flow:
  14:20 - Run screener from a_stock_strategy
  14:20-14:30 - Analyze minute K-line for each candidate
    -> Uptrend: buy immediately
    -> Downtrend/Neutral: monitor until 14:56
  14:30-14:56 - Monitor for dip-rebound signals, buy on detection
  14:56:40 - Market buy remaining candidates

  Next day:
  09:30-10:00 - Morning surge: sell batch 1 (35%) at +3%+
  10:00-14:55 - Peak tracking: sell batch 2 (35%) on peak-drop
  14:55 - Closeout: sell batch 3 (30%) at market
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_log = logging.getLogger("qmt_strategy")


def load_config(path: str = "config.json") -> dict:
    return json.loads(Path(path).read_text("utf-8-sig"))


def run_buy_phase(config: dict, trade_date: str, simulate: bool = True) -> list[dict]:
    """Execute the buy phase (14:20-14:56)."""
    from .screener import candidates_to_stocks, load_screen_results
    from .minute_analyzer import MinuteAnalyzer
    from .trader import Trader

    cfg_trade = config["trading"]

    # Connect trader
    trader = Trader(config["qmt_path"], config["qmt_session"], config["account_id"])
    if not trader.connect():
        if simulate:
            _log.info("SIMULATION: Trader unavailable, running screen-only mode")
        else:
            _log.error("Cannot connect to QMT trader, aborting")
            return []

    # Load screening results
    candidates = load_screen_results(config["screener_project"], trade_date)
    stocks = candidates_to_stocks(candidates)
    _log.info("Loaded %d candidates from screener", len(stocks))

    if not stocks:
        _log.warning("No candidates, nothing to buy")
        trader.disconnect()
        return []

    # Filter: skip already held
    positions = trader.positions
    max_positions = cfg_trade.get("max_positions", 5)
    available_slots = max_positions - len(positions)
    if available_slots <= 0:
        _log.info("Max positions (%d) reached, skipping buy phase", max_positions)
        trader.disconnect()
        return []

    stocks_to_buy = []
    for s in stocks:
        if s["code"] not in positions:
            stocks_to_buy.append(s)
        if len(stocks_to_buy) >= available_slots:
            break

    _log.info("Buy candidates: %s", [s["code"] for s in stocks_to_buy])

    # Query available cash
    asset = trader.query_asset()
    available_cash = asset["available"] if asset else 0
    _log.info("Available cash: %.2f", available_cash)

    if available_cash <= 0:
        _log.warning("No available cash")
        trader.disconnect()
        return []

    analyzer = MinuteAnalyzer()
    buy_results = []
    watch_list = []

    # Phase 1: 14:20-14:30 analysis
    _log.info("=== Phase 1: 14:20-14:30 minute analysis ===")
    for stock in stocks_to_buy:
        market = "SH" if stock["code"].startswith("6") else "SZ"
        signal = analyzer.analyze(stock["code"], market)
        _log.info("  %s %s: %s -> %s", stock["code"], stock["name"],
                  signal.trend, signal.action)

        if signal.action == "buy_now":
            # Calculate volume
            vol = trader.calc_buy_volume(
                signal.latest_price, available_cash,
                cfg_trade.get("position_ratio", 0.2),
            )
            if vol > 0:
                oid = 99999 if simulate else trader.buy(stock["code"], 0, vol,
                                 f"uptrend_{trade_date}")
                if oid:
                    available_cash -= vol * signal.latest_price
                    buy_results.append({**stock, "phase": "uptrend", "volume": vol, "price": signal.latest_price})
                    _log.info("  => [SIM] BOUGHT %s x%d", stock["code"], vol)
            else:
                watch_list.append(stock)
        else:
            watch_list.append(stock)

    # Phase 2: 14:30-14:56 monitoring
    if watch_list:
        _log.info("=== Phase 2: 14:30-14:56 dip-rebound monitoring ===")
        deadline = datetime.strptime("14:56:40", "%H:%M:%S").time()
        while True:
            now = datetime.now().time()
            if now >= deadline:
                break

            for stock in list(watch_list):
                market = "SH" if stock["code"].startswith("6") else "SZ"
                signal = analyzer.check_dip_rebound(
                    stock["code"], market,
                    dip_pct=cfg_trade.get("dip_rebound_pct", 1.5),
                )
                if signal.action == "buy_now":
                    vol = trader.calc_buy_volume(
                        signal.latest_price, available_cash,
                        cfg_trade.get("position_ratio", 0.2),
                    )
                    if vol > 0:
                        oid = 99999 if simulate else trader.buy(stock["code"], 0, vol,
                                         f"dip_rebound_{trade_date}")
                        if oid:
                            available_cash -= vol * signal.latest_price
                            buy_results.append({**stock, "phase": "dip_rebound", "volume": vol, "price": signal.latest_price})
                            watch_list.remove(stock)
                            _log.info("  => [SIM] BOUGHT %s x%d (dip-rebound)", stock["code"], vol)

            if not watch_list:
                break
            time.sleep(30)

        # Phase 3: Deadline buy at 14:56:40
        if watch_list:
            _log.info("=== Phase 3: 14:56:40 deadline buy ===")
            for stock in watch_list:
                vol = trader.calc_buy_volume(
                    stock["price"], available_cash,
                    cfg_trade.get("position_ratio", 0.2),
                )
                if vol > 0:
                    oid = 99999 if simulate else trader.buy(stock["code"], 0, vol,
                                     f"deadline_{trade_date}")
                    if oid:
                        buy_results.append({**stock, "phase": "deadline", "volume": vol, "price": stock["price"]})
                        _log.info("  => [SIM] BOUGHT %s x%d (deadline)", stock["code"], vol)

    # Save buy record
    output_dir = Path(config.get("output_dir", "outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"buy_{trade_date}.json").write_text(
        json.dumps(buy_results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    trader.disconnect()
    return buy_results


def run_sell_phase(config: dict, trade_date: str, simulate: bool = True) -> list[dict]:
    """Execute the sell phase (next trading day)."""
    from .sell_optimizer import SellOptimizer
    from .trader import Trader

    cfg_trade = config["trading"]

    trader = Trader(config["qmt_path"], config["qmt_session"], config["account_id"])
    if not trader.connect():
        if simulate:
            _log.info("SIMULATION: Trader unavailable, using xtdata for price monitoring only")
        else:
            _log.error("Cannot connect to QMT trader for sell phase")
            return []

    optimizer = SellOptimizer(Path(config.get("data_dir", "data")))

    # Get current positions (bought yesterday)
    positions = trader.positions
    if not positions:
        _log.info("No positions to sell")
        trader.disconnect()
        return []

    _log.info("Positions to sell: %d", len(positions))

    # Connect to xtdata for price monitoring
    from xtquant import xtdata
    xtdata.connect(port=58610)

    sell_plans = {}
    for code, pos in positions.items():
        sell_plans[code] = optimizer.create_plan(code, pos.available, pos.avg_price)

    sell_results = []
    max_batches = cfg_trade.get("max_sell_batches", 3)

    # === Unified sell loop using v2 adaptive optimizer ===
    _log.info("=== Sell Phase: Adaptive Multi-Batch (v2) ===")
    sell_complete = set()
    deadline = datetime.strptime("14:56:40", "%H:%M:%S").time()

    while datetime.now().time() < deadline:
        now_str = datetime.now().strftime("%H:%M:%S")
        for code, plan in list(sell_plans.items()):
            if code in sell_complete or plan.remaining <= 0:
                continue

            market = "SH" if code.startswith("6") else "SZ"
            action, price = optimizer.evaluate(
                code, market, xtdata, plan.cost_price,
                now_str, plan.total_volume,
            )

            if action.startswith("sell_"):
                batch_num = int(action[-1])
                batch = plan.batches[batch_num - 1]
                if batch.sold:
                    continue

                if batch_num == 3:
                    vol = plan.remaining
                else:
                    vol = trader.round_lot(int(plan.total_volume * batch.ratio))

                if vol > 0:
                    trigger_name = {1: "morning", 2: "peak_trail", 3: "closeout"}.get(batch_num, "sell")
                    oid = 99999 if simulate else trader.sell(code, 0, vol, f"{trigger_name}_{trade_date}")
                    if oid:
                        optimizer.mark_batch_sold(code, batch_num, price, now_str)
                        plan.remaining -= vol
                        sell_results.append({
                            "code": code, "batch": batch_num, "price": price,
                            "volume": vol, "trigger": trigger_name,
                        })
                        _log.info("  => [SIM] SOLD %s B%d x%d @ %.2f (%s)", code, batch_num, vol, price, trigger_name)

                        if plan.remaining <= 0:
                            sell_complete.add(code)
                            # Record for learning
                            all_prices = [b.sell_price for b in plan.batches if b.sold and b.sell_price > 0]
                            if all_prices:
                                optimizer.record_sell(
                                    code, plan.cost_price, all_prices,
                                    [b.sell_time for b in plan.batches if b.sold],
                                    0, 0, 0, plan.total_volume,
                                )
                            optimizer.clear_plan(code)
        time.sleep(5)

    # Force closeout for unsold positions
    _log.info("=== Force Closeout ===")
    for code, plan in list(sell_plans.items()):
        if code in sell_complete or plan.remaining <= 0:
            continue
        vol = plan.remaining
        oid = 99999 if simulate else trader.sell(code, 0, vol, f"forced_close_{trade_date}")
        if oid:
            optimizer.mark_batch_sold(code, 3, 0, datetime.now().strftime("%H:%M:%S"))
            sell_results.append({
                "code": code, "batch": 3, "price": 0,
                "volume": vol, "trigger": "forced_close",
            })
            _log.info("  FORCED SOLD %s x%d", code, vol)
            optimizer.clear_plan(code)

    # Save sell record
    output_dir = Path(config.get("output_dir", "outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"sell_{trade_date}.json").write_text(
        json.dumps(sell_results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # Print stats
    stats = optimizer.get_stats()
    _log.info("Sell performance: avg_capture=%.1f%%, avg_profit=%.1f%%, trades=%d",
              stats["avg_capture"] * 100, stats["avg_profit"], stats["total_trades"])

    trader.disconnect()
    return sell_results


def main() -> None:
    parser = argparse.ArgumentParser(description="QMT Quantitative Trading Strategy")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("command", choices=["buy", "sell", "run"])
    parser.add_argument("--date", help="Trade date (YYYY-MM-DD)")
    parser.add_argument("--live", action="store_true", default=False,
                        help="REAL trading mode. Without this flag, runs in simulation only.")
    args = parser.parse_args()

    config = load_config(args.config)
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")

    simulate = not getattr(args, "live", False)
    if simulate:
        _log.info("=== SIMULATION MODE - no real orders will be placed ===")
    
    if args.command == "buy":
        results = run_buy_phase(config, trade_date, simulate)
        print(f"Buy phase complete: {len(results)} orders (LIVE={not simulate})")
    elif args.command == "sell":
        results = run_sell_phase(config, trade_date, simulate)
        print(f"Sell phase complete: {len(results)} orders (LIVE={not simulate})")
    elif args.command == "run":
        # Full day: buy + next-day sell
        buy_results = run_buy_phase(config, trade_date, simulate)
        print(f"Buy: {len(buy_results)} orders (LIVE={not simulate})")
        # Sell would run next day via separate invocation


if __name__ == "__main__":
    main()
