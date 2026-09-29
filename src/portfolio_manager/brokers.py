"""Narrow spot/Invest adapters. Submissions are deliberately never retried."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from .models import OrderUpdate, Portfolio, Position, now

D = Decimal


class BrokerError(RuntimeError):
    """Messages contain no credentials or raw HTTP responses."""


@dataclass
class Account:
    cash: Decimal
    holdings: dict[str, Decimal]
    unknown: list[str]
    open_orders: int
    total: Decimal | None = None


class Trading212:
    def __init__(self, client, key, secret):
        self.client = client
        self.auth = httpx.BasicAuth(key, secret)
        self.base = "https://live.trading212.com/api/v0"
        self.lock = asyncio.Lock()
        self.last = {}
        self.metadata = {}
        self.authorize = lambda: False

    async def request(self, method, path, **kwargs):
        # Per-endpoint pacing, also across concurrent snapshots and reconciliation.
        group = path if not path.rsplit("/", 1)[-1].isdigit() else "/equity/orders/id"
        interval = {"/equity/account/summary": 5.1, "/equity/orders": 5.1,
                    "/equity/metadata/instruments": 50.1, "/equity/history/orders": 10.1}.get(group, 2.1)
        async with self.lock:
            await asyncio.sleep(max(0, self.last.get(group, 0) + interval - time.monotonic()))
            self.last[group] = time.monotonic()
            if method == "POST" and not self.authorize():
                raise BrokerError("Live trading is disarmed")
            return await self.client.request(method, self.base + path, auth=self.auth, timeout=25, **kwargs)

    async def read(self, path, **kwargs):
        r = await self.request("GET", path, **kwargs)
        if r.status_code != 200:
            raise BrokerError(f"Trading 212 read failed ({r.status_code})")
        return r.json()

    async def check(self, instruments):
        self.metadata = {i["ticker"]: i for i in await self.read("/equity/metadata/instruments")}
        for item in instruments:
            meta = self.metadata.get(item.broker_symbol)
            if not meta or meta.get("type") not in {"STOCK", "ETF"} or meta.get("currencyCode") != item.currency:
                raise BrokerError(f"Trading 212 instrument metadata mismatch: {item.id}")
        return await self.account(instruments)

    async def account(self, instruments):
        summary = await self.read("/equity/account/summary")
        if summary["currency"] != "EUR":
            raise BrokerError("Trading 212 requires an EUR primary Invest account")
        positions = await self.read("/equity/positions")
        orders = await self.read("/equity/orders")
        by_symbol = {i.broker_symbol: i for i in instruments}
        holdings, unknown = {}, []
        for p in positions:
            symbol = p["instrument"]["ticker"]
            qty = D(str(p["quantity"]))
            if not qty:
                continue
            if symbol not in by_symbol or qty < 0:
                unknown.append(symbol)
            else:
                holdings[by_symbol[symbol].id] = qty
                if D(str(p["quantityAvailableForTrading"])) != qty:
                    unknown.append(f"{symbol}: restricted or reserved shares")
        cash = summary["cash"]
        if D(str(cash.get("inPies", 0))) != 0:
            unknown.append("Cash in Trading 212 Pies")
        return Account(D(str(cash["availableToTrade"])), holdings, unknown, len(orders), D(str(summary["totalValue"])))

    @staticmethod
    def order_update(value):
        status = value.get("status", "")
        mapped = {"FILLED": "FILLED", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED",
                  "PARTIALLY_FILLED": "PARTIAL", "NEW": "SUBMITTED", "LOCAL": "SUBMITTED",
                  "UNCONFIRMED": "SUBMITTED", "CONFIRMED": "SUBMITTED", "CANCELLING": "SUBMITTED"}
        return OrderUpdate(external_id=str(value["id"]), status=mapped.get(status, "UNKNOWN"),
                           filled_quantity=abs(D(str(value.get("filledQuantity", 0)))), detail=status)

    async def submit(self, proposal, instrument):
        signed = proposal.quantity if proposal.side == "BUY" else -proposal.quantity
        # Emit exact finite decimal JSON numbers; no float round trip.
        payload = ('{"ticker":' + json.dumps(instrument.broker_symbol)
                   + ',"quantity":' + str(signed) + ',"limitPrice":' + str(proposal.limit_price)
                   + ',"timeValidity":"DAY"}')
        try:
            r = await self.request("POST", "/equity/orders/limit", content=payload,
                                   headers={"Content-Type": "application/json"})
            if r.status_code in {400, 401, 403, 422, 429}:
                return OrderUpdate(status="REJECTED", detail=f"HTTP {r.status_code}")
            if r.status_code != 200:
                return OrderUpdate(status="UNKNOWN", detail=f"HTTP {r.status_code}; do not resubmit")
            return self.order_update(r.json())
        except (httpx.HTTPError, ValueError, KeyError):
            return OrderUpdate(status="UNKNOWN", detail="Submission outcome uncertain; do not resubmit")

    async def reconcile(self, order, proposal):
        external_id = order["external_id"]
        if not external_id:
            return OrderUpdate(status="UNKNOWN", detail="No broker ID; reconcile manually in the broker account")
        r = await self.request("GET", f"/equity/orders/{external_id}")
        if r.status_code == 200:
            update = self.order_update(r.json())
            if now() >= proposal.expires_at and update.status in {"SUBMITTED", "PARTIAL"}:
                # Cancellation stops unfilled quantity; it never sells a holding.
                await self.request("DELETE", f"/equity/orders/{external_id}")
            return update  # A cancellation request is not a confirmed cancellation.
        if r.status_code != 404:
            raise BrokerError(f"Trading 212 reconciliation failed ({r.status_code})")
        path, params = "/equity/history/orders", {"ticker": proposal.instrument_id, "limit": 50}
        # No ticker filter: application IDs and broker symbols differ.
        params.pop("ticker")
        for _ in range(5):
            page = await self.read(path, params=params)
            for row in page["items"]:
                value = row["order"]
                if str(value["id"]) == external_id:
                    return self.order_update(value)
            next_path = page.get("nextPagePath")
            if not next_path:
                break
            if not next_path.startswith("/api/v0/equity/history/orders?"):
                raise BrokerError("Unexpected history pagination path")
            path, params = next_path.removeprefix("/api/v0"), None
        return OrderUpdate(external_id=external_id, status="UNKNOWN", detail="Order absent from pending and recent history")


class Kraken:
    def __init__(self, client, ledger, key, secret):
        self.client, self.ledger, self.key, self.secret = client, ledger, key, secret
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.metadata = {}
        self.authorize = lambda: False

    async def private(self, endpoint, data=None):
        # Serial private requests prevent nonce inversion and respect starter limits.
        async with self.lock:
            await asyncio.sleep(max(0, self.last + 3.1 - time.monotonic()))
            path = "/0/private/" + endpoint
            value = {"nonce": str(self.ledger.next_nonce()), **(data or {})}
            body = urlencode(value)
            hashed = hashlib.sha256((value["nonce"] + body).encode()).digest()
            signature = base64.b64encode(hmac.new(base64.b64decode(self.secret), path.encode() + hashed,
                                                  hashlib.sha512).digest()).decode()
            self.last = time.monotonic()
            if endpoint == "AddOrder" and not self.authorize():
                raise BrokerError("Live trading is disarmed")
            r = await self.client.post("https://api.kraken.com" + path, content=body,
                headers={"API-Key": self.key, "API-Sign": signature,
                         "Content-Type": "application/x-www-form-urlencoded"}, timeout=25)
            if r.status_code != 200:
                raise BrokerError(f"Kraken HTTP {r.status_code}")
            result = r.json()
            if result.get("error"):
                raise BrokerError("Kraken rejected request; inspect account/API permissions")
            return result["result"]

    async def check(self, instruments):
        info = await self.private("GetApiKeyInfo")
        permissions = set(info["permissions"])
        allowed = {"query-funds", "query-open-trades", "query-closed-trades", "modify-trades",
                   "close-trades", "query-ledger", "export-data", "create-ws-token"}
        required = {"query-funds", "query-open-trades", "query-closed-trades", "modify-trades", "close-trades"}
        if permissions - allowed or not required.issubset(permissions):
            raise BrokerError("Kraken key must have trading/query permissions and no deposit, withdrawal or earn permissions")
        r = await self.client.get("https://api.kraken.com/0/public/AssetPairs", timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise BrokerError("Kraken metadata unavailable")
        self.metadata = {p["altname"]: p for p in data["result"].values()}
        for i in instruments:
            p = self.metadata.get(i.broker_symbol)
            if not p or p["quote"] != "ZEUR" or p["base"] != i.balance_asset or p.get("status") != "online":
                raise BrokerError(f"Kraken spot instrument mismatch: {i.id}")
            if i.min_quantity < D(p["ordermin"]) or i.min_notional < D(p.get("costmin", "0")):
                raise BrokerError(f"Update configured Kraken order minima: {i.id}")
            if i.quantity_step < D(10) ** -p["lot_decimals"]:
                raise BrokerError(f"Update configured Kraken quantity precision: {i.id}")
            if i.price_step < D(10) ** -p["pair_decimals"]:
                raise BrokerError(f"Update configured Kraken price precision: {i.id}")
        return await self.account(instruments)

    async def account(self, instruments):
        balance = await self.private("Balance")
        orders = await self.private("OpenOrders")
        by_asset = {i.balance_asset: i for i in instruments}
        holdings, unknown = {}, []
        for asset, value in balance.items():
            qty = D(value)
            if not qty or asset == "ZEUR":
                continue
            if asset not in by_asset or qty < 0:
                unknown.append(asset)
            else:
                holdings[by_asset[asset].id] = qty
        return Account(D(balance.get("ZEUR", "0")), holdings, unknown, len(orders["open"]))

    async def submit(self, proposal, instrument):
        fields = {"pair": instrument.broker_symbol, "type": proposal.side.lower(), "ordertype": "limit",
                  "volume": str(proposal.quantity), "price": str(proposal.limit_price),
                  "cl_ord_id": proposal.id, "timeinforce": "GTD", "expiretm": str(int(proposal.expires_at.timestamp()))}
        try:
            result = await self.private("AddOrder", fields)
            return OrderUpdate(external_id=result["txid"][0], status="SUBMITTED")
        except (httpx.HTTPError, BrokerError, ValueError, KeyError):
            # Even an error response is conservative: reconcile by cl_ord_id before any next order.
            return OrderUpdate(status="UNKNOWN", detail="Submission unconfirmed; will query broker, never retry")

    @staticmethod
    def order_update(external_id, value):
        status = value["status"]
        filled = D(value.get("vol_exec", "0"))
        mapped = {"closed": "FILLED", "canceled": "CANCELLED", "expired": "CANCELLED",
                  "pending": "SUBMITTED", "open": "PARTIAL" if filled else "SUBMITTED"}
        return OrderUpdate(external_id=external_id, status=mapped.get(status, "UNKNOWN"), filled_quantity=filled)

    async def reconcile(self, order, proposal):
        if order["external_id"]:
            result = await self.private("QueryOrders", {"txid": order["external_id"]})
            if order["external_id"] in result:
                return self.order_update(order["external_id"], result[order["external_id"]])
        else:
            for endpoint, field in [("OpenOrders", "open"), ("ClosedOrders", "closed")]:
                result = await self.private(endpoint)
                for key, value in result[field].items():
                    if value.get("cl_ord_id") == proposal.id:
                        return self.order_update(key, value)
        return OrderUpdate(status="UNKNOWN", detail="Order outcome not established; manual reconciliation needed")


class SimulatedBroker:
    """Persistent local spot fills, never a broker network request."""
    def __init__(self, ledger, settings):
        self.ledger, self.settings = ledger, settings
        if ledger.get("sim_accounts") is None:
            half = settings.initial_capital_eur / 2
            ledger.set("sim_accounts", {"cash": {"trading212": str(half), "kraken": str(half)}, "holdings": {}, "fills": {}})

    async def snapshot(self, quotes):
        state = self.ledger.get("sim_accounts")
        cash = {k: D(v) for k, v in state["cash"].items()}
        positions = [Position(instrument_id=k, quantity=D(v), value_eur=D(v)*quotes[k].price*quotes[k].fx_to_eur)
                     for k, v in state["holdings"].items() if D(v)]
        return Portfolio(cash_eur=cash, positions=positions,
                         equity_eur=sum(cash.values()) + sum(p.value_eur for p in positions))

    async def submit(self, proposal, instrument):
        with self.ledger.transaction():
            state = self.ledger.get("sim_accounts")
            if proposal.id in state["fills"]:
                return OrderUpdate.model_validate(state["fills"][proposal.id])
            value = proposal.quantity * proposal.limit_price * proposal.fx_to_eur
            fee = value * instrument.fee_bps / 10000
            sign = 1 if proposal.side == "BUY" else -1
            cash = D(state["cash"][instrument.broker]) - sign*value - fee
            qty = D(state["holdings"].get(instrument.id, "0")) + sign*proposal.quantity
            if cash < 0 or qty < 0:
                return OrderUpdate(status="REJECTED", detail="Simulated insufficient funds or shares")
            state["cash"][instrument.broker], state["holdings"][instrument.id] = str(cash), str(qty)
            update = OrderUpdate(external_id="sim-" + proposal.id, status="FILLED", filled_quantity=proposal.quantity,
                                 detail="Simulated fill at approved limit, configured fee; no liquidity model")
            state["fills"][proposal.id] = update.model_dump(mode="json")
            self.ledger.set("sim_accounts", state)
        return update

    async def reconcile(self, order, proposal):
        value = self.ledger.get("sim_accounts")["fills"].get(proposal.id)
        return OrderUpdate.model_validate(value) if value else OrderUpdate(status="CANCELLED", detail="No simulated fill")


async def live_snapshot(adapters, instruments, quotes):
    cash, positions, unknown, open_orders, equity = {}, [], [], 0, D(0)
    for name, broker in adapters.items():
        account = await broker.account([i for i in instruments if i.broker == name])
        cash[name] = account.cash
        unknown.extend(account.unknown)
        open_orders += account.open_orders
        position_value = D(0)
        for key, qty in account.holdings.items():
            quote = quotes.get(key)
            if quote is None:
                raise BrokerError(f"No valuation for held asset {key}")
            value = qty * quote.price * quote.fx_to_eur
            position_value += value
            positions.append(Position(instrument_id=key, quantity=qty, value_eur=value))
        # Account-level value includes reserved cash and unmapped positions. Unmapped assets block trading.
        equity += account.total if account.total is not None else account.cash + position_value
    return Portfolio(cash_eur=cash, positions=positions, unknown_assets=unknown,
                     external_open_orders=open_orders, equity_eur=equity)
