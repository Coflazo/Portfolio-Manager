"""Auditable daily-bar backtests; signals at close can first fill at the next open."""
from __future__ import annotations

import csv
import math
from pathlib import Path


def simulate_python(opens, closes, fx_opens, fx_closes, splits, dividends, targets, capital, fee_bps, slippage_bps):
    n, m = len(opens), len(opens[0])
    cash, quantities, equity, trades, fees = float(capital), [0.0]*m, [], 0, 0.0
    rate, slip = fee_bps/10000, slippage_bps/10000
    for t in range(n):
        for j in range(m):
            quantities[j] *= splits[t][j]
            cash += quantities[j]*dividends[t][j]*fx_opens[t][j]
        if t > 0:
            nav = cash + sum(quantities[j]*opens[t][j]*fx_opens[t][j] for j in range(m))
            desired = [targets[t-1][j]*nav/(opens[t][j]*fx_opens[t][j]) for j in range(m)]
            for j in range(m):
                qty = quantities[j]-desired[j]
                if qty > 1e-10:
                    value = qty*opens[t][j]*fx_opens[t][j]*(1-slip)
                    fee = value*rate
                    cash += value-fee
                    quantities[j] -= qty
                    fees += fee
                    trades += 1
            needed = [max(0, desired[j]-quantities[j]) for j in range(m)]
            costs = [needed[j]*opens[t][j]*fx_opens[t][j]*(1+slip)*(1+rate) for j in range(m)]
            total = sum(costs)
            scale = min(1.0, max(0.0, cash)/total) if total else 0.0
            for j in range(m):
                qty = needed[j]*scale
                if qty > 1e-10:
                    value = qty*opens[t][j]*fx_opens[t][j]*(1+slip)
                    fee = value*rate
                    cash -= value+fee
                    quantities[j] += qty
                    fees += fee
                    trades += 1
        equity.append(cash+sum(quantities[j]*closes[t][j]*fx_closes[t][j] for j in range(m)))
    return {"equity": equity, "cash": cash, "quantities": quantities, "trades": trades, "fees": fees}


def load_bars(path):
    """CSV uses raw prices, explicit splits and per-share cash dividends (no double adjustments)."""
    required = {"date", "symbol", "open", "close", "fx_open", "fx_close", "split", "dividend"}
    with Path(path).open(newline="") as f:
        reader = csv.DictReader(f)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV needs: " + ", ".join(sorted(required)))
        rows = list(reader)
    if not rows:
        raise ValueError("Empty bars")
    dates = sorted({r["date"] for r in rows})
    symbols = sorted({r["symbol"] for r in rows})
    lookup = {}
    for row in rows:
        key = (row["date"], row["symbol"])
        if key in lookup:
            raise ValueError("Duplicate date/symbol")
        lookup[key] = row
    if len(rows) != len(dates)*len(symbols):
        raise ValueError("Every date must have every symbol; do not silently forward-fill missing bars")
    arrays = []
    for field in ["open", "close", "fx_open", "fx_close", "split", "dividend"]:
        value = [[float(lookup[(d, s)][field]) for s in symbols] for d in dates]
        if any(not math.isfinite(x) or (x < 0 if field == "dividend" else x <= 0) for row in value for x in row):
            raise ValueError(f"Invalid {field}")
        arrays.append(value)
    return dates, symbols, arrays


def momentum_targets(closes, splits, fast=20, slow=60, max_weight=0.20):
    if not 1 <= fast < slow or not 0 < max_weight <= 1:
        raise ValueError("Require 1 <= fast < slow and 0 < weight <= 1")
    m, history, result = len(closes[0]), [[] for _ in closes[0]], []
    for t, row in enumerate(closes):
        targets = [0.0]*m
        for j, price in enumerate(row):
            # Normalize only past prices by the split known today. No future split data.
            history[j] = [v/splits[t][j] for v in history[j]]
            history[j].append(price)
            if len(history[j]) >= slow:
                short = sum(history[j][-fast:])/fast
                long = sum(history[j][-slow:])/slow
                if short > long and price > long:
                    targets[j] = min(max_weight, 0.90/m)
        result.append(targets)
    return result


def metrics(equity, capital):
    peak, drawdown = capital, 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value/peak-1)
    return {"final_eur": equity[-1], "return_pct": (equity[-1]/capital-1)*100,
            "max_drawdown_pct": drawdown*100}


def run_backtest(path, *, engine="python", capital=100.0, fee_bps=40.0, slippage_bps=10.0):
    if capital <= 0 or not all(math.isfinite(x) for x in [capital, fee_bps, slippage_bps]) or min(fee_bps, slippage_bps) < 0 or max(fee_bps, slippage_bps) >= 10000:
        raise ValueError("Invalid capital or cost assumptions")
    dates, symbols, arrays = load_bars(path)
    if len(dates) < 90:
        raise ValueError("At least 90 common daily bars are required")
    targets = momentum_targets(arrays[1], arrays[4])
    simulator = simulate_python
    if engine == "cpp":
        from ._backtest import simulate
        simulator = simulate
    result = simulator(*arrays, targets, capital, fee_bps, slippage_bps)
    # Equal-weight buy-and-hold: emulate once, then mark/dividends/splits without daily rebalance.
    hold_qty = [capital/len(symbols)/(arrays[0][1][j]*arrays[2][1][j]*(1+slippage_bps/10000)*(1+fee_bps/10000)) for j in range(len(symbols))]
    hold_curve, hold_cash = [capital], 0.0
    for t in range(1, len(dates)):
        if t > 1:
            for j in range(len(symbols)):
                hold_qty[j] *= arrays[4][t][j]
                hold_cash += hold_qty[j]*arrays[5][t][j]*arrays[2][t][j]
        hold_curve.append(hold_cash + sum(hold_qty[j]*arrays[1][t][j]*arrays[3][t][j] for j in range(len(symbols))))
    split = max(60, len(dates)//2)
    prior = result["equity"][split-1]
    return {"engine": engine, "symbols": symbols, "start": dates[0], "end": dates[-1], "bars": len(dates),
            "assumptions": {"capital_eur": capital, "fee_bps": fee_bps, "slippage_bps": slippage_bps,
                            "strategy": "20/60 SMA, 20% max per asset, lagged next-open execution"},
            "strategy": metrics(result["equity"], capital), "buy_and_hold": metrics(hold_curve, capital),
            "cash": metrics([capital]*len(dates), capital),
            "forward_window": {"start": dates[split], **metrics(result["equity"][split:], prior)},
            "fees_eur": result["fees"], "trades": result["trades"],
            "dates": dates, "equity": result["equity"], "benchmark_equity": hold_curve,
            "limitations": "Fixed strategy; no parameter fitting. Forward window is chronological, not independent research validation. Fractional fills, unlimited liquidity, no tax/borrow/market impact model. CSV provenance must be verified."}
