# -*- coding: utf-8 -*-
"""Backtest v2 - Tencent daily data + simulated minute bars + adaptive sell.

No QMT dependency. Simulates intraday minute bars from daily OHLC.
Usage: python run_backtest_v2.py [--start 2025-06-01] [--end 2026-05-26]
"""

import json, sys, time, urllib.request, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))
from sell_optimizer import SellOptimizer, MinuteBar, FEE_RATE

STAMP_TAX = 0.001
CAPITAL = 100_000.0
MAX_POS = 5
POS_PCT = 0.20
TOP_N = 2
MIN_PCT = 3.0; MAX_PCT = 7.0
MIN_TURN = 3.0; MAX_TURN = 10.0
MIN_VR = 1.5; MAX_VR = 8.0

def fetch_klines(code, market, limit=250):
    prefix = "sh" if market == "SH" else "sz"
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{limit},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        stock = data["data"].get(f"{prefix}{code}", {})
        raw = stock.get("qfqday") or stock.get("day") or []
        return code, market, raw
    except Exception:
        return code, market, []

def parse_klines(raw):
    result = []
    prev_close = None
    for row in raw:
        try:
            close = float(row[2])
            pct = round((close / prev_close - 1) * 100, 2) if prev_close and prev_close > 0 else 0.0
            result.append({"date": row[0], "open": float(row[1]), "close": close,
                "high": float(row[3]), "low": float(row[4]),
                "volume": float(row[5]), "pct": pct})
            prev_close = close
        except (ValueError, IndexError):
            continue
    return result

def simulate_minute_bars(day):
    """Simulate 240 minute bars using Brownian bridge with OHLC anchors."""
    o, h, l, c = day["open"], day["high"], day["low"], day["close"]
    vol = day.get("volume", 1000000)
    if o <= 0 or c <= 0:
        return []
    
    n_bars = 240
    vol_per_bar = max(vol / n_bars, 100)
    range_pct = (h - l) / o  # intraday range as fraction
    
    # Generate anchor points: open, high, low, close
    # Randomize timing of high and low within the day
    high_time = int(n_bars * random.uniform(0.15, 0.55))   # high at 10:00-13:00
    low_time = int(n_bars * random.uniform(0.10, 0.65))    # low at 09:45-13:30
    
    # Ensure high comes before low if up day, or mix it up
    if random.random() < 0.5:
        # Sometimes high first, sometimes low first
        pass
    else:
        high_time, low_time = low_time, high_time
    
    # Build price path: piecewise random walk between anchors
    # Anchor points: (0, o), (high_time, h), (low_time, l), (n_bars-1, c)
    # Sort by time
    anchors = sorted([
        (0, o),
        (min(high_time, n_bars-2), h if high_time < n_bars else o),
        (min(low_time, n_bars-2), l if low_time < n_bars else o),
        (n_bars-1, c),
    ], key=lambda x: x[0])
    
    # Deduplicate same-time anchors, keep last
    deduped = []
    for t, p in anchors:
        if deduped and deduped[-1][0] == t:
            deduped[-1] = (t, p)
        else:
            deduped.append((t, p))
    anchors = deduped
    
    bars = []
    cum_vol = 0
    cum_vp = 0.0
    price = o
    bar_idx = 0
    
    for seg_idx in range(len(anchors) - 1):
        t_start, p_start = anchors[seg_idx]
        t_end, p_end = anchors[seg_idx + 1]
        seg_len = t_end - t_start
        if seg_len <= 0:
            continue
        
        drift = (p_end - p_start) / seg_len
        volatility = abs(p_start) * range_pct * 0.05  # low noise for realistic fills
        
        for i in range(seg_len):
            t = (i + 1) / seg_len
            target = p_start + (p_end - p_start) * t
            # Brownian bridge: add noise that shrinks to 0 at endpoints
            noise_scale = volatility * (t * (1 - t) * 4) ** 0.5
            noise = random.gauss(0, noise_scale)
            price = target + noise
            
            bar_vol = int(vol_per_bar * random.uniform(0.6, 1.4))
            cum_vol += bar_vol
            cum_vp += bar_vol * price
            
            hi = max(price, price * (1 + random.uniform(0, 0.003)))
            lo = min(price, price * (1 - random.uniform(0, 0.003)))
            
            bars.append(MinuteBar(
                open=price, high=hi, low=lo, close=price,
                volume=bar_vol,
                vwap=cum_vp / cum_vol if cum_vol > 0 else price,
                time_str=f"{9 + (bar_idx // 60):02d}:{bar_idx % 60:02d}",
            ))
            bar_idx += 1
    
    # Fill remaining bars to 240
    while bar_idx < n_bars:
        bar_vol = int(vol_per_bar * random.uniform(0.5, 1.0))
        cum_vol += bar_vol
        cum_vp += bar_vol * c
        bars.append(MinuteBar(
            open=c, high=c*1.002, low=c*0.998, close=c,
            volume=bar_vol,
            vwap=cum_vp / cum_vol if cum_vol > 0 else c,
            time_str=f"{9 + (bar_idx // 60):02d}:{bar_idx % 60:02d}",
        ))
        bar_idx += 1
    
    return bars

def backtest(start, end):
    """Run backtest with simulated minute bars."""
    sep = "=" * 60
    print()
    print(sep)
    print("  BACKTEST V2 -- Adaptive Sell Optimizer")
    print(f"  {start} -> {end}")
    print(sep)
    print()

    # Step 1: Code universe
    print("[1/5] Building stock universe...")
    codes = []
    codes += [(f"60{i:04d}", "SH") for i in range(0, 6000)]
    codes += [(f"00{i:04d}", "SZ") for i in range(0, 4000)]
    sample = codes[::len(codes)//1200][:1200]
    print(f"  Sampling {len(sample)} stocks")

    # Step 2: Fetch klines
    print("[2/5] Fetching daily klines...")
    all_data = {}
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(fetch_klines, c, m, 250): (c, m) for c, m in sample}
        for i, f in enumerate(as_completed(futures)):
            code, market, raw = f.result()
            if raw:
                parsed = parse_klines(raw)
                if len(parsed) >= 30:
                    all_data[code] = {"market": market, "days": parsed}
            if (i + 1) % 200 == 0:
                print(f"  {i+1}/{len(sample)}...")
    print(f"  Done: {len(all_data)} valid stocks")
    print()

    # Step 3: Trading dates
    print("[3/5] Generating trading days...")
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    d = start_dt
    while d <= end_dt:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    print(f"  {len(dates)} trading days")
    print()

    # Step 4: Run backtest
    print("[4/5] Running backtest...")
    sell_opt = SellOptimizer(Path("data"))
    cash = CAPITAL
    positions = {}
    trades = []
    daily_values = []

    for day_idx, date in enumerate(dates):
        # --- SELL ---
        for code in list(positions.keys()):
            pos = positions[code]
            if pos["buy_date"] == date:
                continue
            sd = all_data.get(code)
            if not sd:
                continue
            day_data = None
            for d_stock in sd["days"]:
                if d_stock["date"] == date:
                    day_data = d_stock
                    break
            if day_data is None:
                continue
            minute_bars = simulate_minute_bars(day_data)
            if not minute_bars:
                continue
            result = sell_opt.backtest_sell(code, pos["cost"], minute_bars, pos["volume"])
            sell_prices = result.get("sell_prices", [])
            for j, sp in enumerate(sell_prices):
                ratios = [0.40, 0.35, 0.25]
                vol_ratio = ratios[j] if j < 3 else 0.25
                vol = int(pos["volume"] * vol_ratio)
                if j == len(sell_prices) - 1:
                    sold_so_far = sum(int(pos["volume"] * ratios[k]) for k in range(j))
                    vol = pos["volume"] - sold_so_far
                amount = vol * sp
                fee = amount * (FEE_RATE + STAMP_TAX)
                cash += amount - fee
                profit = (sp / pos["cost"] - 1) * 100
                trades.append({"date": date, "code": code, "action": f"sell_b{j+1}",
                    "price": round(sp, 2), "volume": vol, "profit_pct": round(profit, 2)})
            del positions[code]

        # --- SCREEN + BUY ---
        candidates = []
        for code, info in all_data.items():
            market = info["market"]
            days_list = info["days"]
            day_idx_in = -1
            day_info = None
            for i, d_stock in enumerate(days_list):
                if d_stock["date"] == date:
                    day_idx_in = i
                    day_info = d_stock
                    break
            if day_info is None or day_idx_in < 13:
                continue
            close = day_info["close"]
            pct = day_info["pct"]
            if code.startswith(("300", "301", "688")):
                continue
            if not (MIN_PCT < pct < MAX_PCT):
                continue
            prev_vols = [days_list[j]["volume"] for j in range(max(0, day_idx_in-5), day_idx_in) if days_list[j]["volume"] > 0]
            avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else day_info["volume"]
            vr = day_info["volume"] / avg_vol if avg_vol > 0 else 1.0
            if not (MIN_VR < vr < MAX_VR):
                continue
            limit_count = 0
            for j in range(max(0, day_idx_in - 13), day_idx_in):
                prev = days_list[j]
                if prev["pct"] >= 9.5:
                    limit_count += 1
            if limit_count < 2:
                continue
            score = pct * 3 + vr * 2 + limit_count * 3
            candidates.append({"code": code, "price": close, "pct": pct, "vr": vr, "score": score})
        candidates.sort(key=lambda x: x["score"], reverse=True)
        selected = candidates[:TOP_N]
        slots = MAX_POS - len(positions)
        for c_stock in selected[:slots]:
            if c_stock["code"] in positions:
                continue
            price = c_stock["price"]
            vol = int(cash * POS_PCT / price / 100) * 100
            if vol >= 100:
                cost = vol * price * (1 + FEE_RATE)
                if cash >= cost:
                    cash -= cost
                    positions[c_stock["code"]] = {"volume": vol, "cost": price, "buy_date": date}
                    trades.append({"date": date, "code": c_stock["code"], "action": "buy",
                        "price": round(price, 2), "volume": vol})
        pos_value = sum(p["volume"] * p["cost"] for p in positions.values())
        daily_values.append(cash + pos_value)
        if (day_idx + 1) % 50 == 0:
            pct_done = (day_idx+1)/len(dates)*100
            print(f"  {date}: value={daily_values[-1]:,.0f}, cash={cash:,.0f}, pos={len(positions)} ({pct_done:.0f}%)")

    # Step 5: Results
    print()
    print("[5/5] Computing results...")
    final_value = daily_values[-1] if daily_values else CAPITAL
    total_return = (final_value / CAPITAL - 1) * 100
    months = max(len(dates) / 21, 1)
    monthly_return = ((final_value / CAPITAL) ** (1 / months) - 1) * 100
    sells = [t for t in trades if t["action"].startswith("sell")]
    wins = [t for t in sells if t["profit_pct"] > 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0
    avg_profit = sum(t["profit_pct"] for t in sells) / len(sells) if sells else 0
    peak = daily_values[0] if daily_values else CAPITAL
    max_dd = 0
    for v in daily_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Monthly breakdown
    monthly_returns = []
    if daily_values:
        month_map = {}
        for dv, dt in zip(daily_values, dates):
            mk = dt[:7]
            if mk not in month_map:
                month_map[mk] = []
            month_map[mk].append(dv)
        for mk in sorted(month_map):
            vals = month_map[mk]
            monthly_returns.append({"month": mk, "return_pct": round((vals[-1]/vals[0]-1)*100, 2)})

    # Print results
    print()
    print(sep)
    print("  RESULTS -- Adaptive Sell Optimizer v2")
    print(sep)
    print(f"  Period:           {start} -> {end}")
    print(f"  Trading days:     {len(dates)}")
    print(f"  Stocks:           {len(all_data)}")
    print(f"  Initial:          {CAPITAL:,.0f}")
    print(f"  Final:            {final_value:,.0f}")
    print(f"  Total return:     {total_return:+.2f}%")
    print(f"  Monthly return:   {monthly_return:+.2f}%")
    print(f"  Win rate:         {win_rate:.1f}% ({len(wins)}/{len(sells)})")
    print(f"  Avg profit/trade: {avg_profit:+.2f}%")
    print(f"  Max drawdown:     {max_dd:.2f}%")
    print(f"  Total sells:      {len(sells)}")
    print(sep)
    if sells:
        sells_sorted = sorted(sells, key=lambda x: x["profit_pct"], reverse=True)
        print()
        print("  Top 3:")
        for t in sells_sorted[:3]:
            print(f"    {t["date"]} {t["code"]} +{t["profit_pct"]:.1f}%")
        print("  Worst 3:")
        for t in sells_sorted[-3:]:
            print(f"    {t["date"]} {t["code"]} {t["profit_pct"]:+.1f}%")
    if monthly_returns:
        print()
        print("  Monthly:")
        for mr in monthly_returns:
            print(f"    {mr["month"]}: {mr["return_pct"]:+.2f}%")
    Path("outputs").mkdir(exist_ok=True)
    result = {"period": f"{start} -> {end}", "stocks": len(all_data),
        "trading_days": len(dates), "initial": CAPITAL,
        "final": round(final_value,2), "total_return": round(total_return,2),
        "monthly_return": round(monthly_return,2), "win_rate": round(win_rate,1),
        "avg_profit": round(avg_profit,2), "max_drawdown": round(max_dd,2),
        "total_sells": len(sells), "monthly_returns": monthly_returns}
    Path("outputs/backtest_v2.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("Results saved to outputs/backtest_v2.json")
    return result

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-06-01")
    parser.add_argument("--end", default="2026-05-26")
    args = parser.parse_args()
    backtest(args.start, args.end)