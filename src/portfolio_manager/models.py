from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Weight = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
BrokerName = Literal["trading212", "kraken"]
Mode = Literal["demo", "paper", "live"]


def now() -> datetime:
    return datetime.now(UTC)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Instrument(Record):
    id: str
    name: str
    broker: BrokerName
    broker_symbol: str
    data_symbol: str
    balance_asset: str | None = None
    asset_class: Literal["stock", "etf", "crypto"]
    currency: Literal["EUR", "USD"]
    calendar: str | None = None
    quantity_step: Positive
    price_step: Positive = Decimal("0.01")
    min_quantity: Positive
    min_notional: Positive
    fee_bps: Nonnegative


class ModelSeat(Record):
    id: str
    role: str


class RiskConfig(Record):
    max_position_weight: Weight = Decimal("0.20")
    max_crypto_weight: Weight = Decimal("0.50")
    min_cash_weight: Weight = Decimal("0.10")
    buy_halt_loss: Weight = Decimal("0.20")
    target_trade_weight: Weight = Decimal("0.10")
    max_price_drift: Weight = Decimal("0.01")
    max_spread: Weight = Decimal("0.01")
    max_cost_fraction: Weight = Decimal("0.01")
    equity_quote_ttl_seconds: int = Field(default=120, gt=0)
    crypto_quote_ttl_seconds: int = Field(default=15, gt=0)
    proposal_ttl_seconds: int = Field(default=900, gt=0)
    max_buy_councils_per_day: int = Field(default=2, ge=0, le=2)


class QuotaConfig(Record):
    openrouter_per_day: int = Field(default=50, ge=1, le=50)
    openrouter_per_minute: int = Field(default=20, ge=1, le=20)
    reserve_for_sells: int = Field(default=20, ge=0, le=40)
    tavily_per_month: int = Field(default=1000, ge=1, le=1000)
    twelvedata_per_day: int = Field(default=800, ge=1, le=800)
    twelvedata_per_minute: int = Field(default=8, ge=1, le=8)


class Settings(Record):
    name: str = "Portfolio Council"
    data_dir: str = "data"
    initial_capital_eur: Positive = Decimal("100")
    models: list[ModelSeat]
    risk: RiskConfig = RiskConfig()
    quota: QuotaConfig = QuotaConfig()
    scan_interval_seconds: int = Field(default=900, ge=300)
    reconcile_interval_seconds: int = Field(default=30, ge=10)
    rss_feeds: list[str] = []
    instruments: list[Instrument]

    @model_validator(mode="after")
    def distinct(self):
        if len(self.models) != 5 or len({m.id for m in self.models}) != 5:
            raise ValueError("Exactly five distinct model IDs are required")
        if any(not m.id.endswith(":free") for m in self.models):
            raise ValueError("Only explicit :free models are allowed; no automatic router")
        if len({i.id for i in self.instruments}) != len(self.instruments):
            raise ValueError("Duplicate instrument IDs")
        if len({(i.broker, i.broker_symbol) for i in self.instruments}) != len(self.instruments):
            raise ValueError("Duplicate broker instruments")
        if any(i.broker == "kraken" and (i.currency != "EUR" or i.asset_class != "crypto" or not i.balance_asset) for i in self.instruments):
            raise ValueError("Kraken adapter supports configured EUR crypto spot pairs only")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class Quote(Record):
    instrument_id: str
    price: Positive
    at: AwareDatetime
    fx_to_eur: Positive = Decimal(1)
    fx_at: AwareDatetime = Field(default_factory=now)
    bid: Positive | None = None
    ask: Positive | None = None
    market_open: bool = True
    source: str


class Position(Record):
    instrument_id: str
    quantity: Nonnegative
    value_eur: Nonnegative


class Portfolio(Record):
    at: AwareDatetime = Field(default_factory=now)
    cash_eur: dict[BrokerName, Nonnegative]
    positions: list[Position]
    equity_eur: Positive
    unknown_assets: list[str] = []
    external_open_orders: int = Field(default=0, ge=0)

    @property
    def balances_hash(self) -> str:
        return digest({"cash": self.cash_eur, "positions": sorted((p.instrument_id, str(p.quantity)) for p in self.positions)})


class Proposal(Record):
    id: str = Field(default_factory=lambda: str(uuid4()))
    instrument_id: str
    broker: BrokerName
    side: Literal["BUY", "SELL"]
    quantity: Positive
    limit_price: Positive
    reference_price: Positive
    fx_to_eur: Positive
    created_at: AwareDatetime = Field(default_factory=now)
    expires_at: AwareDatetime
    portfolio_hash: str
    reason: str

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class Evidence(Record):
    id: str
    title: str
    url: str
    text: str
    retrieved_at: AwareDatetime = Field(default_factory=now)
    published_at: AwareDatetime | None = None
    content_hash: str


class Analysis(Record):
    thesis: str = Field(min_length=1, max_length=5000)
    risks: list[str] = Field(max_length=12)
    evidence_ids: list[str] = Field(max_length=20)


class Vote(Record):
    model_id: str
    proposal_hash: str
    decision: Literal["APPROVE", "REJECT", "ABSTAIN"]
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class OrderUpdate(Record):
    external_id: str | None = None
    status: Literal["SUBMITTED", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"]
    filled_quantity: Nonnegative = Decimal(0)
    detail: str = ""
