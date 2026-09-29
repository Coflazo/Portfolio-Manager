# Portfolio Council

A small-account trading system where no order goes out unless five independent language models
approve it, a deterministic risk gate passes it, and every call stays inside free API quotas.

**Status: work in progress.** The engine, risk gate, ledger, broker adapters and both backtest
engines are written. Not yet written: the `portfolioctl` command-line entry point that
`pyproject.toml` declares, and the test suite. It has not traded real money and has no recorded
backtest results. Do not run it in `live` mode in this state.

## How a trade is decided

1. **Proposal.** A trade is proposed for one instrument in a fixed universe (`config.yaml`: Apple,
   Microsoft, Bitcoin, Ethereum), priced from a fresh quote and fingerprinted together with the
   account balances it was made against.
2. **Risk gate, first pass.** `risk.check_risk` checks the proposal against live state before
   any model is called: proposal expiry, balances unchanged since the proposal, quote and FX
   freshness, price drift, spread, tick size, minimum quantity and notional, trading costs, no
   shorting, 20% max per position, 50% max crypto, 10% minimum cash, and a buy halt once equity
   falls 20% below starting capital. Any failure blocks the order.
3. **Council.** Five free OpenRouter models, each given a role (risk, macro, fundamentals and news,
   skeptical reviewer, strategy and maths), review the proposal separately, with the latest
   recorded backtest attached as evidence when one exists. Retrieved documents and peer analyses
   are passed as untrusted data, never as instructions. Each returns a JSON vote that must cite
   evidence IDs it was actually given.
4. **Unanimity, then a second pass.** Every order, buy or sell, needs all five `APPROVE` votes,
   each bound to the same proposal fingerprint and citing only known evidence (`risk.unanimous`).
   The approved order is never resized; the risk gate runs again on a fresh snapshot, and changed
   balances, stale prices or another pending order expire it.
5. **Execution.** Narrow adapters for Trading212 (Invest) and Kraken (spot). Order submissions are
   deliberately never retried, and broker errors never carry credentials or raw responses.

## Staying free

- Before any model call, the OpenRouter catalogue is re-validated (at most an hour old) to confirm
  every seat is a `:free`, zero-priced model that supports tool calls.
- An unexpected provider charge pauses all inference.
- A shared quota ledger enforces the free tiers (OpenRouter 50/day, Twelve Data 800/day, Tavily
  1,000/month) and reserves part of the model budget for sells.
- Research uses Tavily search plus primary-source feeds (Federal Reserve, ECB, SEC, EIA, USDA).

## Modes

`demo` uses illustrative prices with no network or credentials. `paper` uses real market data with
a simulated broker that persists fills locally. `live` uses real brokers and must be explicitly
armed after model connectivity checks pass. Each mode keeps its own SQLite ledger in `data/`.

## Backtesting

`backtest.run_backtest` runs a fixed 20/60-day moving-average momentum rule on daily bars:

- Signals at the close fill at the next open, with fees and slippage charged.
- Prices are raw, with explicit splits and per-share dividends, and no double adjustment.
- Results are compared with equal-weight buy-and-hold and with cash, plus a chronological forward
  window.
- The same simulation exists in Python and in C++ (`cpp/backtest.cpp`, exposed through pybind11).

Limitations, stated in the output itself: no parameter fitting, fractional fills, unlimited
liquidity, and no tax, borrow or market-impact model.

## Layout

```
config.yaml                 universe, model seats, risk limits, quotas
src/portfolio_manager/
  council.py                OpenRouter client and the five-seat council
  risk.py                   unanimity rule and the risk gate
  runtime.py                demo / paper / live orchestration and arming
  brokers.py                Trading212, Kraken and the simulated broker
  market.py, research.py    quotes, calendars and primary-source research
  ledger.py, quota.py       SQLite ledger and shared quota accounting
  backtest.py               Python backtester and metrics
cpp/backtest.cpp            C++ backtester (pybind11)
```

## Setup

```bash
uv sync
cp .env.example .env    # fill in keys; broker keys must not allow withdrawals
```

Python 3.12 or later. Nothing here is investment advice.

## License

MIT
