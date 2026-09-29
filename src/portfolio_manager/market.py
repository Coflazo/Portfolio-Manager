from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import exchange_calendars as xcals
import pandas as pd
import websockets

from .models import Quote, now

D = Decimal


def market_open(instrument, at=None):
    if instrument.asset_class == "crypto":
        return True
    if not instrument.calendar:
        return False
    calendar = xcals.get_calendar(instrument.calendar)
    return bool(calendar.is_open_on_minute(pd.Timestamp(at or now()).floor("min")))


class MarketData:
    def __init__(self, client, quotas, ledger, settings, key):
        self.client, self.quotas, self.ledger, self.settings, self.key = client, quotas, ledger, settings, key
        self.quotes = {}
        self.fx = None
        self.history_cache = {}
        self.twelve_lock = asyncio.Lock()

    async def twelve(self, endpoint, **params):
        if not self.key:
            raise ValueError("TWELVE_DATA_API_KEY is missing")
        async with self.twelve_lock:
            await self.quotas.acquire("twelvedata")
            r = await self.client.get("https://api.twelvedata.com/" + endpoint,
                                      params={**params, "apikey": self.key}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "error":
                raise ValueError("Twelve Data rejected query; check free plan entitlement/symbol")
            return data

    async def usd_eur(self):
        if self.fx and (now()-self.fx[1]).total_seconds() < 1800:
            return self.fx
        value = await self.twelve("quote", symbol="USD/EUR")
        self.fx = (D(value["close"]), datetime.fromtimestamp(int(value["timestamp"]), UTC))
        return self.fx

    async def quote(self, instrument, force=False):
        cached = self.quotes.get(instrument.id)
        if not force and cached:
            ttl = 10 if instrument.broker == "kraken" else 120
            if (now()-cached.at).total_seconds() < ttl or (instrument.broker != "kraken" and not market_open(instrument)):
                return cached
        if instrument.broker == "kraken":
            r = await self.client.get("https://api.kraken.com/0/public/Depth",
                                     params={"pair": instrument.broker_symbol, "count": 1}, timeout=10)
            r.raise_for_status()
            value = r.json()
            if value.get("error"):
                raise ValueError("Kraken depth unavailable")
            book = next(iter(value["result"].values()))
            bid, ask = D(book["bids"][0][0]), D(book["asks"][0][0])
            quote = Quote(instrument_id=instrument.id, price=(bid+ask)/2, bid=bid, ask=ask,
                          at=now(), source="https://api.kraken.com/0/public/Depth")
        else:
            value = await self.twelve("quote", symbol=instrument.data_symbol)
            if value["currency"] != instrument.currency:
                raise ValueError(f"Data currency mismatch: {instrument.id}")
            fx, fx_at = (D(1), now()) if instrument.currency == "EUR" else await self.usd_eur()
            quote = Quote(instrument_id=instrument.id, price=D(value["close"]),
                          at=datetime.fromtimestamp(int(value["timestamp"]), UTC), fx_to_eur=fx, fx_at=fx_at,
                          market_open=market_open(instrument), source="https://api.twelvedata.com/quote")
        self.quotes[instrument.id] = quote
        self.persist_quotes()
        return quote

    def persist_quotes(self):
        self.ledger.set("quotes", {k: v.model_dump(mode="json") for k, v in self.quotes.items()})

    async def refresh(self, force=False):
        for instrument in self.settings.instruments:
            try:
                await self.quote(instrument, force=force)
            except Exception as exc:
                self.ledger.event("market_error", {"instrument": instrument.id, "error": type(exc).__name__})
        return dict(self.quotes)

    async def history(self, instrument):
        """Completed daily closes only. No partial current bar in the signal."""
        cached = self.history_cache.get(instrument.id)
        if cached and (now()-cached[0]).total_seconds() < 21600:
            return cached[1]
        if instrument.broker == "kraken":
            r = await self.client.get("https://api.kraken.com/0/public/OHLC",
                                     params={"pair": instrument.broker_symbol, "interval": 1440}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise ValueError("Kraken OHLC unavailable")
            rows = next(v for k, v in data["result"].items() if k != "last")
            values = [D(r[4]) for r in rows if int(r[0]) + 86400 <= now().timestamp()]
        else:
            data = await self.twelve("time_series", symbol=instrument.data_symbol, interval="1day",
                                      outputsize=120, order="ASC", timezone="UTC", adjust="splits")
            # Excluding all of today's bar is deliberately conservative even after market close.
            values = [D(r["close"]) for r in data["values"] if r["datetime"][:10] < now().date().isoformat()]
        if len(values) < 60 or any(v <= 0 for v in values):
            raise ValueError("At least 60 valid completed daily bars are required")
        self.history_cache[instrument.id] = (now(), values)
        return values

    async def stream(self):
        symbols = {i.data_symbol: i for i in self.settings.instruments if i.broker == "kraken"}
        if not symbols:
            return
        delay = 2
        while True:
            try:
                async with websockets.connect("wss://ws.kraken.com/v2", ping_interval=20, ping_timeout=20,
                                               max_size=1_000_000) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "params": {
                        "channel": "ticker", "symbol": list(symbols), "event_trigger": "bbo", "snapshot": True}}))
                    self.ledger.set("stream", {"status": "connected", "at": now().isoformat()})
                    delay = 2
                    async for raw in ws:
                        data = json.loads(raw)
                        if data.get("success") is False:
                            raise ValueError("Kraken subscription rejected")
                        if data.get("channel") != "ticker":
                            continue
                        for row in data.get("data", []):
                            instrument = symbols.get(row.get("symbol"))
                            if not instrument or not row.get("timestamp"):
                                continue  # Never invent exchange timestamps for streamed messages.
                            bid, ask = D(str(row["bid"])), D(str(row["ask"]))
                            self.quotes[instrument.id] = Quote(instrument_id=instrument.id, price=(bid+ask)/2,
                                bid=bid, ask=ask, at=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                                source="wss://ws.kraken.com/v2")
                        # Daemon flushes once a second, not on every tick.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ledger.set("stream", {"status": "disconnected", "error": type(exc).__name__, "at": now().isoformat()})
                await asyncio.sleep(delay)
                delay = min(60, delay*2)


class DemoMarket:
    """Illustrative values only; no HTTP clients or credentials."""
    def __init__(self, ledger, settings):
        self.ledger, self.settings, self.quotes = ledger, settings, {}
        self.start = now()

    async def refresh(self, force=False):
        bases = {"AAPL": 230, "MSFT": 420, "BTC-EUR": 80000, "ETH-EUR": 2500}
        phase = (now()-self.start).total_seconds()/120
        for n, item in enumerate(self.settings.instruments):
            price = D(str(round(bases.get(item.id, 100)*(1+0.001*math.sin(phase+n)), 2)))
            self.quotes[item.id] = Quote(instrument_id=item.id, price=price, at=now(),
                fx_to_eur=D("0.9") if item.currency == "USD" else D(1),
                bid=price*D("0.9998"), ask=price*D("1.0002"), source="demo://synthetic")
        self.persist_quotes()
        return self.quotes

    async def quote(self, instrument, force=False):
        await self.refresh()
        return self.quotes[instrument.id]

    async def history(self, instrument):
        price = self.quotes[instrument.id].price
        return [price*(D("0.8")+D(k)/500) for k in range(100)]

    def persist_quotes(self):
        self.ledger.set("quotes", {k: v.model_dump(mode="json") for k, v in self.quotes.items()})
