# -*- coding: utf-8 -*-
"""Backtest using Tencent API - 1 year, full strategy simulation."""
import json, time, urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

FEE = 0.000085
START = "2025-06-01"
END = "2026-05-26"
CAPITAL = 100000.0
MAX_POSITIONS = 5
POSITION_PCT = 0.2

# Screening params (from a_stock_strategy)
MIN_CAP = 90e8; MAX_CAP = 280e8
MIN_PCT = 3.0; MAX_PCT = 7.0
MIN_TURN = 3.0; MAX_TURN = 10.0
MIN_VR = 1.0; MAX_VR = 10.0

def fetch_kline(code, market, limit=250):
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

def backtest():
    print(f"Backtest: {START} -> {END}, capital={CAPITAL}")
    
    # Step 1: Fetch all stock klines
    print("Fetching stock data...")
    codes = []
    codes += [(f"60{i:04d}", "SH") for i in range(0, 10000) if i < 6000]  # SH stocks
    codes += [(f"00{i:04d}", "SZ") for i in range(0, 10000) if i < 4000]  # SZ main
    codes += [(f"30{i:04d}", "SZ") for i in range(0, 10000) if i < 2000]  # GEM
    
    print(f"  Total codes to try: {len(codes)}")
    
    all_data = {}
    batch_size = 20
    total = len(codes)
    
    # Sample ~500 stocks for speed (diversified)
    sample = codes[::len(codes)//500][:500]
    print(f"  Sampling {len(sample)} stocks")
    
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_kline, c, m, 250): (c, m) for c, m in sample}
        for i, f in enumerate(as_completed(futures)):
            code, market, raw = f.result()
            if raw:
                all_data[code] = {"market": market, "klines": raw}
            if (i+1) % 100 == 0:
                print(f"  Fetched {i+1}/{len(sample)}...")
    
    print(f"  Done: {len(all_data)} stocks with data")
    
    # Step 2: Generate trading dates
    start = datetime.strptime(START, "%Y-%m-%d")
    end = datetime.strptime(END, "%Y-%m-%d")
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    
    print(f"  Trading days: {len(dates)}")
    
    # Step 3: Run backtest
    cash = CAPITAL
    positions = {}  # code -> {volume, cost, buy_date}
    trades_log = []
    daily_values = []
    
    for day_idx, date in enumerate(dates):
        # === SELL (T+1: sell yesterday positions) ===
        for code in list(positions.keys()):
            pos = positions[code]
            if pos["buy_date"] == date:
                continue  # T+1 constraint
            
            klines = all_data.get(code, {}).get("klines", [])
            day_kl = None
            for k in klines:
                if k[0] == date:
                    day_kl = k
                    break
            
            if day_kl:
                open_p = float(day_kl[1])
                high_p = float(day_kl[3])
                cost_p = pos["cost"]
                vol = pos["volume"]
                
                # Simulate 3-batch sell
                # Batch 1 (35%): sell at +3% if high reaches it
                # Batch 2+3: sell at close
                if high_p >= cost_p * 1.03:
                    sell_p = cost_p * 1.03
                else:
                    sell_p = open_p
                
                revenue = vol * sell_p * (1 - FEE)
                cash += revenue
                
                profit = (sell_p / cost_p - 1) * 100
                trades_log.append({
                    "date": date, "code": code, "action": "sell",
                    "buy_price": cost_p, "sell_price": sell_p,
                    "volume": vol, "profit_pct": round(profit, 2),
                })
                
                del positions[code]
        
        # === BUY ===
        # Screen stocks for this day
        candidates = []
        for code, info in all_data.items():
            klines = info["klines"]
            market = info["market"]
            
            # Find today kline
            day_kl = None
            day_idx_in_kl = -1
            for i, k in enumerate(klines):
                if k[0] == date:
                    day_kl = k
                    day_idx_in_kl = i
                    break
            
            if not day_kl:
                continue
            
            open_p = float(day_kl[1])
            close_p = float(day_kl[2])
            high_p = float(day_kl[3])
            low_p = float(day_kl[4])
            volume = float(day_kl[5])
            
            if open_p <= 0 or close_p <= 0:
                continue
            
            pct = (close_p / open_p - 1) * 100
            
            # Exclude GEM (300xxx), STAR (688xx), ST
            if code.startswith("300") or code.startswith("301") or code.startswith("688"):
                continue
            
            # Base screening
            if not (MIN_PCT < pct < MAX_PCT):
                continue
            
            # Volume ratio: today vol / avg 5-day vol
            prev_vols = []
            for j in range(max(0, day_idx_in_kl - 5), day_idx_in_kl):
                try:
                    prev_vols.append(float(klines[j][5]))
                except:
                    pass
            avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else volume
            vr = volume / avg_vol if avg_vol > 0 else 1.0
            
            if not (MIN_VR < vr < MAX_VR):
                continue
            
            # Check limit-up in past 13 days
            has_limit = False
            for j in range(max(0, day_idx_in_kl - 13), day_idx_in_kl):
                try:
                    prev = klines[j]
                    prev_close = float(prev[2])
                    if prev_close <= 0: continue
                    # Simple limit-up: close/open > 9.5%
                    prev_open = float(prev[1])
                    if prev_open > 0 and (prev_close / prev_open - 1) * 100 >= 9.5:
                        has_limit = True
                        break
                except:
                    pass
            
            if not has_limit:
                continue
            
            candidates.append({
                "code": code, "price": close_p, "pct": pct, "vr": vr,
            })
        
        # Sort by VR (volume ratio as proxy for quality)
        candidates.sort(key=lambda x: x["vr"], reverse=True)
        candidates = candidates[:MAX_POSITIONS]
        
        # Buy if slots available
        available_slots = MAX_POSITIONS - len(positions)
        for c in candidates[:available_slots]:
            vol = int(cash * POSITION_PCT / c["price"] / 100) * 100
            if vol >= 100:
                cost = vol * c["price"] * (1 + FEE)
                if cash >= cost:
                    cash -= cost
                    positions[c["code"]] = {"volume": vol, "cost": c["price"], "buy_date": date}
                    trades_log.append({
                        "date": date, "code": c["code"], "action": "buy",
                        "price": c["price"], "volume": vol,
                    })
        
        # Track daily portfolio value
        pos_value = sum(p["volume"] * p["cost"] for p in positions.values())
        daily_values.append(cash + pos_value)
        
        if (day_idx + 1) % 50 == 0:
            print(f"  Day {day_idx+1}/{len(dates)}: value={daily_values[-1]:.0f}, positions={len(positions)}")
    
    # === Results ===
    final_value = daily_values[-1]
    total_return = (final_value / CAPITAL - 1) * 100
    
    # Metrics
    sells = [t for t in trades_log if t["action"] == "sell"]
    wins = [t for t in sells if t["profit_pct"] > 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0
    avg_profit = sum(t["profit_pct"] for t in sells) / len(sells) if sells else 0
    
    # Monthly return
    months = len(dates) / 21
    monthly_return = ((final_value / CAPITAL) ** (1 / months) - 1) * 100
    
    # Max drawdown
    peak = daily_values[0]
    max_dd = 0
    for v in daily_values:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd
    
    print()
    print("=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period:         {START} -> {END}")
    print(f"Trading days:   {len(dates)}")
    print(f"Initial capital: {CAPITAL:,.0f}")
    print(f"Final value:    {final_value:,.0f}")
    print(f"Total return:   {total_return:+.2f}%")
    print(f"Monthly return: {monthly_return:+.2f}%")
    print(f"Win rate:       {win_rate:.1f}% ({len(wins)}/{len(sells)})")
    print(f"Avg profit:     {avg_profit:+.2f}%")
    print(f"Max drawdown:   {max_dd:.2f}%")
    print(f"Total trades:   {len(sells)}")
    print(f"Positions held: {len(positions)}")
    print("=" * 60)
    
    # Save results
    result = {
        "period": f"{START} -> {END}",
        "trading_days": len(dates),
        "initial_capital": CAPITAL,
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "monthly_return": round(monthly_return, 2),
        "win_rate": round(win_rate, 1),
        "avg_profit": round(avg_profit, 2),
        "max_drawdown": round(max_dd, 2),
        "total_sells": len(sells),
        "trades": trades_log[-20:],  # last 20 trades
    }
    Path("outputs").mkdir(exist_ok=True)
    Path("outputs/backtest_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nResults saved to outputs/backtest_result.json")

if __name__ == "__main__":
    backtest()
