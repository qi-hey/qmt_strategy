# -*- coding: utf-8 -*-
"""Full strategy backtest using a_stock_strategy screening + Tencent data."""
import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add a_stock_strategy to path
STRATEGY_ROOT = Path(r"C:\Users\小七\Desktop\API+本地大模型\a_stock_strategy")
sys.path.insert(0, str(STRATEGY_ROOT))

from src.screener import StrategyRules, passes_base_rules, recent_limit_hits, infer_board
from src.models import DailyQuote, KLine
from src.analysis import estimate_red_open, zhuang_signature, detect_goldman
from src.trading_rules import is_under_investigation

FEE = 0.000085
START = "2025-12-01"
END = "2026-05-26"
CAPITAL = 100000.0
MAX_POS = 5
POS_PCT = 0.20
TOP_N = 3

def fetch_klines(code, market, limit=180):
    prefix = "sh" if market == "SH" else "sz"
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{limit},qfq"
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        stock = data["data"].get(f"{prefix}{code}", {})
        raw = stock.get("qfqday") or stock.get("day") or []
        return code, market, raw
    except:
        return code, market, []

def raw_to_kline(row, prev_close=None):
    """Convert Tencent kline row to DailyQuote-like info."""
    close_val = float(row[2])
    pct_val = 0.0
    if prev_close and prev_close > 0:
        pct_val = round((close_val / prev_close - 1) * 100, 2)
    return {
        "date": row[0], "open": float(row[1]), "close": close_val,
        "high": float(row[3]), "low": float(row[4]),
        "volume": float(row[5]), "pct": pct_val,
    }

def build_quote(code, market, day_info):
    """Build a DailyQuote-like object from kline data."""
    cap_est = day_info["close"] * 1e8  # rough estimate
    return DailyQuote(
        code=code, name="", market=market,
        board=infer_board(code),
        price=day_info["close"],
        pct=day_info["pct"],
        turnover=day_info["volume"] / 1e5,  # rough
        volume_ratio=1.0,
        total_market_cap=cap_est, float_market_cap=cap_est,
        plate="",
    )

def main():
    print(f"Full Strategy Backtest: {START} -> {END}")
    print()
    
    # Step 1: Fetch klines
    print("Step 1: Fetching klines...")
    codes = []
    codes += [(f"60{i:04d}", "SH") for i in range(0, 6000)]
    codes += [(f"00{i:04d}", "SZ") for i in range(0, 4000)]
    
    sample = codes[::len(codes)//800][:800]
    print(f"  {len(sample)} stocks to fetch")
    
    all_klines = {}
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(fetch_klines, c, m, 180): (c, m) for c, m in sample}
        for i, f in enumerate(as_completed(futures)):
            code, market, raw = f.result()
            if raw:
                all_klines[code] = {"market": market, "raw": raw}
            if (i+1) % 200 == 0:
                print(f"  {i+1}/{len(sample)}...")
    print(f"  Got {len(all_klines)} stocks")
    
    # Step 2: Parse into structured data
    print("Step 2: Parsing klines...")
    stock_data = {}  # code -> list of daily info dicts
    for code, info in all_klines.items():
        rows = info["raw"]
        parsed = []
        prev_close = None
        for row in rows:
            k = raw_to_kline(row, prev_close)
            parsed.append(k)
            prev_close = k["close"]
        stock_data[code] = {"market": info["market"], "days": parsed}
    
    # Step 3: Generate date range
    start = datetime.strptime(START, "%Y-%m-%d")
    end = datetime.strptime(END, "%Y-%m-%d")
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    print(f"Step 3: {len(dates)} trading days")
    
    # Step 4: Run backtest
    print("Step 4: Running backtest...")
    from dataclasses import replace
    rules = replace(StrategyRules(),
        min_float_market_cap=50_0000_0000,   # 50亿 (wider for backtest)
        max_float_market_cap=500_0000_0000,  # 500亿
        min_pct=2.0, max_pct=8.0,            # wider range
        min_turnover=1.5, max_turnover=15.0,
        min_volume_ratio=0.8, max_volume_ratio=15.0,
    )
    cash = CAPITAL
    positions = {}
    trades = []
    daily_values = []
    
    for day_idx, date in enumerate(dates):
        # === SELL ===
        for code in list(positions.keys()):
            pos = positions[code]
            if pos["buy_date"] == date:
                continue
            
            sd = stock_data.get(code)
            if not sd:
                continue
            
            day_info = None
            for d in sd["days"]:
                if d["date"] == date:
                    day_info = d
                    break
            
            if day_info:
                open_p = day_info["open"]
                high_p = day_info["high"]
                cost_p = pos["cost"]
                vol = pos["volume"]
                
                if high_p >= cost_p * 1.03:
                    sell_p = cost_p * 1.03
                else:
                    sell_p = open_p
                
                revenue = vol * sell_p * (1 - FEE)
                cash += revenue
                trades.append({
                    "date": date, "code": code, "action": "sell",
                    "buy_price": round(cost_p, 2), "sell_price": round(sell_p, 2),
                    "volume": vol, "profit_pct": round((sell_p/cost_p-1)*100, 2),
                })
                del positions[code]
        
        # === SCREEN ===
        candidates = []
        for code, sd in stock_data.items():
            market = sd["market"]
            
            # Find today data
            day_info = None
            day_idx_in = -1
            for i, d in enumerate(sd["days"]):
                if d["date"] == date:
                    day_info = d
                    day_idx_in = i
                    break
            if not day_info:
                continue
            
            # Calculate volume_ratio and market cap first
            prev_vols = []
            for j in range(max(0, day_idx_in-5), day_idx_in):
                prev_vols.append(sd["days"][j]["volume"])
            avg_v = sum(prev_vols)/len(prev_vols) if prev_vols else day_info["volume"]
            vr = day_info["volume"]/avg_v if avg_v>0 else 1.0
            # Realistic market cap: use stock-specific share estimates
            if code.startswith("6"):
                est_cap = day_info["close"] * 1.5e9  # ~1.5B shares for SH large caps
            else:
                est_cap = day_info["close"] * 8e8    # ~800M shares for SZ
            
            # Build quote with all fields
            quote = DailyQuote(
                code=code, name="", market=market,
                board=infer_board(code),
                price=day_info["close"],
                pct=day_info["pct"],
                turnover=(day_info["volume"] * 100) / (est_cap / day_info["close"]) * 100,
                volume_ratio=vr,
                total_market_cap=est_cap, float_market_cap=est_cap,
                plate="",
            )
            
            # Pass base rules
            if not passes_base_rules(quote, rules):
                continue
            
            # Build KLine list for this stock up to today
            klines = []
            for j in range(max(0, day_idx_in-20), day_idx_in+1):
                d = sd["days"][j]
                klines.append(KLine(date=d["date"], pct=d["pct"],
                    turnover=d["volume"], high=d["high"], low=d["low"]))
            
            # Check limit-up
            hits = recent_limit_hits(quote, klines, window=13)
            if not hits:
                continue
            
            # Scoring
            ro = estimate_red_open(quote, len(hits), "low", 0, None, klines)
            
            candidates.append({
                "code": code, "price": day_info["close"],
                "pct": day_info["pct"], "vr": quote.volume_ratio,
                "ro_score": ro.probability, "ro_label": ro.label,
                "limit_hits": len(hits),
            })
        
        # Rank and select top N
        candidates.sort(key=lambda x: (x["ro_score"], x["vr"]), reverse=True)
        selected = candidates[:TOP_N]
        
        # Buy
        available = MAX_POS - len(positions)
        for c in selected[:available]:
            if c["code"] in positions:
                continue
            price = c["price"]
            vol = int(cash * POS_PCT / price / 100) * 100
            if vol >= 100:
                cost = vol * price * (1 + FEE)
                if cash >= cost:
                    cash -= cost
                    positions[c["code"]] = {"volume": vol, "cost": price, "buy_date": date}
                    trades.append({
                        "date": date, "code": c["code"], "action": "buy",
                        "price": round(price, 2), "volume": vol,
                    })
        
        # Track value
        pos_val = sum(p["volume"] * p["cost"] for p in positions.values())
        daily_values.append(cash + pos_val)
        
        if (day_idx+1) % 40 == 0:
            print(f"  {date}: value={daily_values[-1]:.0f}, cash={cash:.0f}, pos={len(positions)}")
    
    # === Results ===
    final = daily_values[-1]
    total_ret = (final/CAPITAL-1)*100
    months = len(dates)/21
    monthly = ((final/CAPITAL)**(1/months)-1)*100
    
    sells = [t for t in trades if t["action"]=="sell"]
    wins = [t for t in sells if t["profit_pct"]>0]
    wr = len(wins)/len(sells)*100 if sells else 0
    avg_p = sum(t["profit_pct"] for t in sells)/len(sells) if sells else 0
    
    peak = daily_values[0]; max_dd = 0
    for v in daily_values:
        if v>peak: peak=v
        dd = (peak-v)/peak*100
        if dd>max_dd: max_dd=dd
    
    print()
    print("="*60)
    print("FULL STRATEGY BACKTEST")
    print("="*60)
    print(f"Period:         {START} -> {END}")
    print(f"Trading days:   {len(dates)}")
    print(f"Stocks:         {len(stock_data)}")
    print(f"Initial:        {CAPITAL:,.0f}")
    print(f"Final:          {final:,.0f}")
    print(f"Total return:   {total_ret:+.2f}%")
    print(f"Monthly return: {monthly:+.2f}%")
    print(f"Win rate:       {wr:.1f}% ({len(wins)}/{len(sells)})")
    print(f"Avg profit:     {avg_p:+.2f}%")
    print(f"Max drawdown:   {max_dd:.2f}%")
    print(f"Total sells:    {len(sells)}")
    print(f"Holding:        {len(positions)} positions")
    print("="*60)
    
    # Top/bottom trades
    if sells:
        sells_sorted = sorted(sells, key=lambda x: x["profit_pct"], reverse=True)
        print("\nTop 3 trades:")
        for t in sells_sorted[:3]:
            print(f"  {t['date']} {t['code']} +{t['profit_pct']:.1f}%")
        print("Worst 3 trades:")
        for t in sells_sorted[-3:]:
            print(f"  {t['date']} {t['code']} {t['profit_pct']:+.1f}%")
    
    Path("outputs").mkdir(exist_ok=True)
    Path("outputs/backtest_full.json").write_text(json.dumps({
        "period": f"{START} -> {END}",
        "stocks": len(stock_data), "trading_days": len(dates),
        "initial": CAPITAL, "final": round(final,2),
        "total_return": round(total_ret,2),
        "monthly_return": round(monthly,2),
        "win_rate": round(wr,1), "avg_profit": round(avg_p,2),
        "max_drawdown": round(max_dd,2), "total_sells": len(sells),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
