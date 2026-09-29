from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import httpx

from .brokers import Kraken, SimulatedBroker, Trading212, live_snapshot
from .council import Council, OpenRouter, parse_json
from .ledger import Ledger, dumps
from .market import DemoMarket, MarketData, market_open
from .models import OrderUpdate, Proposal, Vote, digest, now
from .quota import Quotas
from .research import Research, evidence
from .risk import check_risk

D = Decimal


class WriterLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError("Another portfolio process owns the writer lock") from exc
        return self

    def __exit__(self, *_):
        self.handle.close()


def credential_fingerprint():
    names = ["OPENROUTER_API_KEY", "TAVILY_API_KEY", "TWELVE_DATA_API_KEY", "TRADING212_API_KEY",
             "TRADING212_API_SECRET", "KRAKEN_API_KEY", "KRAKEN_API_SECRET"]
    return digest({name: os.getenv(name, "") for name in names})


class Runtime:
    def __init__(self, settings, mode):
        if mode not in {"demo", "paper", "live"}:
            raise ValueError("Unknown mode")
        self.settings, self.mode = settings, mode
        root = Path(settings.data_dir)
        self.ledger = Ledger(root / f"{mode}.sqlite")
        self.quota_ledger = Ledger(root / "quotas.sqlite")
        self.quotas = Quotas(self.quota_ledger, settings.quota)
        self.client = httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "PortfolioCouncil/0.1 personal research"})
        self.ledger.set("mode", mode)
        self.ledger.set("settings", settings.model_dump(mode="json"))
        self.sim = SimulatedBroker(self.ledger, settings) if mode != "live" else None
        self.market = DemoMarket(self.ledger, settings) if mode == "demo" else MarketData(
            self.client, self.quotas, self.ledger, settings, os.getenv("TWELVE_DATA_API_KEY", ""))
        self.provider = OpenRouter(self.client, os.getenv("OPENROUTER_API_KEY", ""), self.quotas, self.ledger)
        self.research = Research(self.client, self.quotas, self.ledger, os.getenv("TAVILY_API_KEY", ""), settings.rss_feeds)
        self.council = Council(settings, self.provider, self.research, self.ledger)
        self.adapters = {}
        if mode == "live":
            configured = {i.broker for i in settings.instruments}
            if "trading212" in configured:
                self.adapters["trading212"] = Trading212(self.client, os.getenv("TRADING212_API_KEY", ""), os.getenv("TRADING212_API_SECRET", ""))
            if "kraken" in configured:
                self.adapters["kraken"] = Kraken(self.client, self.ledger, os.getenv("KRAKEN_API_KEY", ""), os.getenv("KRAKEN_API_SECRET", ""))
        self.account_lock = asyncio.Lock()
        self.order_lock = asyncio.Lock()
        for adapter in self.adapters.values():
            adapter.authorize = self.armed

    async def close(self):
        await self.client.aclose()
        self.ledger.close()
        self.quota_ledger.close()

    def armed(self):
        record = self.ledger.get("armed", {})
        return (record.get("enabled") is True and record.get("settings_hash") == self.settings.fingerprint
                and record.get("credentials_hash") == credential_fingerprint())

    def execution_enabled(self):
        return self.mode != "live" or self.armed()

    async def snapshot(self, refresh=True):
        async with self.account_lock:
            if refresh:
                await self.market.refresh()
            if self.sim:
                portfolio = await self.sim.snapshot(self.market.quotes)
            else:
                portfolio = await live_snapshot(self.adapters, self.settings.instruments, self.market.quotes)
            self.ledger.record_snapshot(portfolio)
            return portfolio

    async def preflight(self, probe=False):
        required = ["OPENROUTER_API_KEY", "TAVILY_API_KEY", "TWELVE_DATA_API_KEY"]
        if self.mode == "live":
            if "trading212" in self.adapters:
                required += ["TRADING212_API_KEY", "TRADING212_API_SECRET"]
            if "kraken" in self.adapters:
                required += ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        await self.provider.validate_models(self.settings.models)
        for name, broker in self.adapters.items():
            await broker.check([i for i in self.settings.instruments if i.broker == name])
        portfolio = await self.snapshot()
        if portfolio.unknown_assets or portfolio.external_open_orders or self.ledger.pending_orders():
            raise ValueError("Resolve unmapped holdings or outstanding orders before arming")
        if not self.ledger.get("first_armed_at") and portfolio.equity_eur > self.settings.initial_capital_eur*D("1.10"):
            raise ValueError("Account balances exceed this EUR 100 configuration; use dedicated accounts or configure the intended capital")
        for item in self.settings.instruments:
            if item.id not in self.market.quotes:
                raise ValueError(f"No market data entitlement for {item.id}")
        if probe:
            # Uses five free requests. Tests inference connectivity, not investment skill.
            async def check_model(seat):
                message = await self.provider.complete(seat.id, [{"role": "user", "content": 'Return JSON only: {"ready":true}'}])
                if parse_json(message.get("content")) != {"ready": True}:
                    raise ValueError(f"Model connectivity probe failed: {seat.id}")
            results = await asyncio.gather(*(check_model(s) for s in self.settings.models), return_exceptions=True)
            if any(isinstance(r, BaseException) for r in results):
                raise ValueError("At least one model connectivity probe failed; no arming")
            if not await self.research.search("European Central Bank latest monetary policy"):
                raise ValueError("Research connectivity probe returned no sources")
        report = {"checked_at": now().isoformat(), "settings_hash": self.settings.fingerprint,
                  "credentials_hash": credential_fingerprint(), "probed": probe,
                  "equity_eur": str(portfolio.equity_eur), "status": "passed"}
        self.ledger.set("preflight", report)
        return {k: v for k, v in report.items() if not k.endswith("hash")}

    def make_proposal(self, instrument, portfolio, quote, side, reason):
        price = (quote.price / instrument.price_step).to_integral_value(rounding=ROUND_DOWN)*instrument.price_step
        if side == "SELL":
            qty = next(p.quantity for p in portfolio.positions if p.instrument_id == instrument.id)
        else:
            qty = portfolio.equity_eur*self.settings.risk.target_trade_weight/(price*quote.fx_to_eur)
        qty = (qty/instrument.quantity_step).to_integral_value(rounding=ROUND_DOWN)*instrument.quantity_step
        if qty <= 0:
            raise ValueError("Rounded quantity is zero")
        return Proposal(instrument_id=instrument.id, broker=instrument.broker, side=side,
            quantity=qty, limit_price=price, reference_price=quote.price, fx_to_eur=quote.fx_to_eur,
            expires_at=now()+timedelta(seconds=self.settings.risk.proposal_ttl_seconds),
            portfolio_hash=portfolio.balances_hash, reason=reason)

    async def candidates(self, portfolio):
        candidates = []
        held = {p.instrument_id for p in portfolio.positions if p.quantity > 0}
        for item in self.settings.instruments:
            quote = self.market.quotes.get(item.id)
            if not quote or (self.mode != "demo" and not market_open(item)):
                continue
            try:
                values = await self.market.history(item)
                fast, slow = sum(values[-20:])/20, sum(values[-60:])/60
                side = None
                if item.id in held and (fast < slow or quote.price < slow):
                    side = "SELL"
                elif item.id not in held and fast > slow and quote.price > slow:
                    side = "BUY"
                if side and not self.ledger.instrument_recently_reviewed(item.id, side):
                    candidates.append((item, side, f"Completed daily 20/60 SMA: {fast:.4f}/{slow:.4f}; current {quote.price}. A research candidate, not proof of an edge."))
            except Exception as exc:
                self.ledger.event("signal_unavailable", {"instrument": item.id, "error": type(exc).__name__})
        return sorted(candidates, key=lambda candidate: candidate[1] != "SELL")

    async def demo_vote(self, proposal):
        item = evidence("Illustrative scenario", "demo://scenario", "Synthetic momentum example. These are scripted votes, not model responses.")
        self.ledger.save_evidence(item)
        votes = []
        for seat in self.settings.models:
            vote = Vote(model_id=seat.id, proposal_hash=proposal.fingerprint, decision="APPROVE",
                        rationale="DEMO: scripted approval to demonstrate the order lifecycle. No inference was called.", evidence_ids=[item.id])
            self.ledger.vote(proposal.id, seat.id, 1, {"thesis": "DEMO: a synthetic trend passes the example rule.", "risks": ["No real market evidence"], "evidence_ids": [item.id]})
            self.ledger.vote(proposal.id, seat.id, 2, vote.model_dump(mode="json"))
            votes.append(vote)
        return True, votes, "DEMO: five scripted votes"

    async def review(self, instrument, side, reason):
        # Refresh after history retrieval; quota pacing can make earlier quotes stale.
        portfolio = await self.snapshot()
        await self.market.refresh(force=True)
        quote = self.market.quotes[instrument.id]
        proposal = self.make_proposal(instrument, portfolio, quote, side, reason)
        errors = check_risk(self.settings, proposal, portfolio, self.market.quotes)
        if errors:
            self.ledger.event("risk_blocked", {"instrument": instrument.id, "side": side, "reasons": errors})
            return
        if self.mode != "demo":
            used = self.quotas.summary()["openrouter"]
            cap = self.settings.quota.openrouter_per_day - (self.settings.quota.reserve_for_sells if side == "BUY" else 0)
            if used + 15 > cap:
                self.ledger.event("budget_wait", {"reason": "Insufficient budget for a full council with research continuations", "side": side})
                return
        self.ledger.proposal(proposal)
        self.ledger.set("activity", {"stage": "deliberating", "proposal_id": proposal.id, "at": now().isoformat()})
        try:
            if self.mode == "demo":
                agreed, votes, detail = await self.demo_vote(proposal)
            else:
                await self.provider.validate_models(self.settings.models)
                items = await self.research.collect(instrument, proposal, quote)
                recent = self.ledger.recent("backtests", 1)
                if recent:
                    report = recent[0]["payload"]
                    summary = {k: v for k, v in report.items() if k not in {"equity", "dates", "benchmark_equity"}}
                    recorded = evidence("Recorded local backtest; inspect assumptions and provenance", "local://backtest", dumps(summary))
                    self.ledger.save_evidence(recorded)
                    items.append(recorded)
                agreed, votes, detail = await self.council.deliberate(proposal, portfolio, items)
            if not agreed:
                self.ledger.proposal_status(proposal.id, "HELD", detail)
                return
            self.ledger.proposal_status(proposal.id, "APPROVED", detail)
            # No changes to quantity/price after voting. Changed balances or stale evidence expire the order.
            refreshed = await self.snapshot()
            await self.market.refresh(force=True)
            errors = check_risk(self.settings, proposal, refreshed, self.market.quotes)
            if not self.execution_enabled():
                errors.append("Live mode is disarmed or configuration/credentials changed")
            if self.ledger.pending_orders():
                errors.append("Another order is pending or uncertain")
            if errors:
                self.ledger.proposal_status(proposal.id, "BLOCKED", "; ".join(errors))
                return
            broker = self.sim or self.adapters[instrument.broker]
            async with self.order_lock:
                self.ledger.begin_order(proposal)
                try:
                    update = await broker.submit(proposal, instrument)
                except BaseException:
                    self.ledger.update_order(proposal.id, OrderUpdate(status="UNKNOWN", detail="Interrupted or failed after durable submission intent"))
                    raise
                self.ledger.update_order(proposal.id, update)
                self.ledger.proposal_status(proposal.id, update.status, update.detail)
        except Exception as exc:
            self.ledger.proposal_status(proposal.id, "ERROR", type(exc).__name__)
            self.ledger.event("review_error", {"proposal": proposal.id, "error": type(exc).__name__})
        finally:
            self.ledger.set("activity", {"stage": "watching", "at": now().isoformat()})

    async def scan(self):
        if not self.execution_enabled() or self.ledger.pending_orders():
            return
        portfolio = await self.snapshot()
        for instrument, side, reason in await self.candidates(portfolio):
            if side == "BUY" and self.ledger.buy_count_today() >= self.settings.risk.max_buy_councils_per_day:
                continue
            await self.review(instrument, side, reason)
            break  # One deliberation at a time; later scans pick up other candidates.
        self.ledger.set("last_scan", now().isoformat())

    async def reconcile(self):
        async with self.order_lock:
            await self._reconcile()

    async def _reconcile(self):
        for order in self.ledger.pending_orders():
            proposal = Proposal.model_validate_json(order["payload"])
            broker = self.sim or self.adapters[proposal.broker]
            try:
                update = await broker.reconcile(order, proposal)
                self.ledger.update_order(order["id"], update)
                self.ledger.proposal_status(proposal.id, update.status, update.detail)
            except Exception as exc:
                self.ledger.event("reconcile_error", {"order": order["id"], "error": type(exc).__name__})

    async def run(self, once=False):
        lock_name = "demo.lock" if self.mode == "demo" else "connected.lock"
        with WriterLock(Path(self.settings.data_dir)/lock_name):
            if self.mode == "live" and not self.armed():
                raise RuntimeError("Live is disarmed. Run doctor --mode live --probe-models, then arm --acknowledge-live")
            if self.mode != "demo":
                # Startup rechecks entitlement, instrument definitions and broker key permissions.
                await self.provider.validate_models(self.settings.models)
                for name, broker in self.adapters.items():
                    await broker.check([i for i in self.settings.instruments if i.broker == name])
            self.ledger.set("started_at", now().isoformat())
            await self.market.refresh()
            await self.reconcile()
            await self.snapshot(refresh=False)
            if once:
                await self.scan()
                await self.snapshot()
                self.ledger.set("heartbeat", now().isoformat())
                return
            stream = asyncio.create_task(self.market.stream()) if self.mode != "demo" else None
            scan_task, account_task = None, None
            last_scan, last_snapshot = 0.0, 0.0

            async def account_cycle():
                await self.reconcile()
                await self.snapshot()

            try:
                while True:
                    self.ledger.set("heartbeat", now().isoformat())
                    self.ledger.set("quotas", self.quotas.summary())
                    self.market.persist_quotes()
                    for task, name in [(scan_task, "scan"), (account_task, "snapshot")]:
                        if task and task.done() and not task.cancelled() and task.exception():
                            error = {"component": name, "error": type(task.exception()).__name__}
                            if self.ledger.get("last_runtime_error") != error:
                                self.ledger.event("runtime_error", error)
                                self.ledger.set("last_runtime_error", error)
                    clock = asyncio.get_running_loop().time()
                    if (scan_task is None or scan_task.done()) and clock-last_scan >= self.settings.scan_interval_seconds:
                        scan_task, last_scan = asyncio.create_task(self.scan()), clock
                    # Account requests may run during slow deliberation; snapshot lock serializes them.
                    if (account_task is None or account_task.done()) and clock-last_snapshot >= self.settings.reconcile_interval_seconds:
                        account_task, last_snapshot = asyncio.create_task(account_cycle()), clock
                    await asyncio.sleep(1)
            finally:
                tasks = [t for t in [stream, scan_task, account_task] if t]
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                self.ledger.set("activity", {"stage": "stopped", "at": now().isoformat()})
