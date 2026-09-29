from decimal import Decimal

from .models import now


def unanimous(proposal, votes, expected_models, evidence_ids):
    expected = set(expected_models)
    return (
        len(votes) == len(expected) == 5
        and {v.model_id for v in votes} == expected
        and all(v.decision == "APPROVE" and v.proposal_hash == proposal.fingerprint
                and set(v.evidence_ids).issubset(evidence_ids) for v in votes)
    )


def check_risk(settings, proposal, portfolio, quotes, *, at=None):
    t = at or now()
    cfg = settings.risk
    errors = []
    instrument = next((i for i in settings.instruments if i.id == proposal.instrument_id), None)
    if not instrument or instrument.broker != proposal.broker:
        return ["Instrument or broker is outside the configured universe"]
    if t >= proposal.expires_at or proposal.created_at > t:
        errors.append("Proposal has expired or is dated in the future")
    if portfolio.balances_hash != proposal.portfolio_hash:
        errors.append("Account balances changed after proposal creation")
    if (t - portfolio.at).total_seconds() > 60 or portfolio.at > t:
        errors.append("Portfolio snapshot is stale or future-dated")
    if portfolio.unknown_assets or portfolio.external_open_orders:
        errors.append("Unmapped holdings or outstanding broker orders")
    relevant = {proposal.instrument_id} | {p.instrument_id for p in portfolio.positions if p.quantity > 0}
    for symbol in relevant:
        quote = quotes.get(symbol)
        item = next((i for i in settings.instruments if i.id == symbol), None)
        ttl = cfg.crypto_quote_ttl_seconds if item and item.asset_class == "crypto" else cfg.equity_quote_ttl_seconds
        if not quote or not (0 <= (t - quote.at).total_seconds() <= ttl):
            errors.append(f"Missing or stale price for {symbol}")
        elif not (0 <= (t - quote.fx_at).total_seconds() <= 3600):
            errors.append(f"Missing or stale FX rate for {symbol}")
    quote = quotes.get(proposal.instrument_id)
    if not quote:
        return errors
    if not quote.market_open:
        errors.append("Market is closed")
    if abs(quote.price / proposal.reference_price - 1) > cfg.max_price_drift:
        errors.append("Price moved outside the approved range")
    if abs(proposal.limit_price / proposal.reference_price - 1) > cfg.max_price_drift:
        errors.append("Limit price is outside the allowed range")
    if proposal.limit_price % instrument.price_step != 0:
        errors.append("Limit price violates the configured tick size")
    if abs(quote.fx_to_eur / proposal.fx_to_eur - 1) > cfg.max_price_drift:
        errors.append("FX moved outside the approved range")
    if quote.bid and quote.ask and (quote.ask < quote.bid or (quote.ask - quote.bid) / quote.price > cfg.max_spread):
        errors.append("Bid/ask spread is invalid or too wide")
    if proposal.quantity < instrument.min_quantity or proposal.quantity % instrument.quantity_step != 0:
        errors.append("Quantity violates the configured broker minimum or precision")
    if proposal.quantity * proposal.limit_price < instrument.min_notional:
        errors.append("Order is below the configured minimum notional")
    held = next((p.quantity for p in portfolio.positions if p.instrument_id == instrument.id), Decimal(0))
    value = proposal.quantity * proposal.limit_price * quote.fx_to_eur
    fee = value * instrument.fee_bps / 10000
    if instrument.fee_bps / 10000 > cfg.max_cost_fraction:
        errors.append("Estimated trading costs exceed the allowed fraction")
    if proposal.side == "SELL":
        if proposal.quantity > held:
            errors.append("Sell would create a short position")
        return errors
    floor = settings.initial_capital_eur * (1 - cfg.buy_halt_loss)
    if portfolio.equity_eur <= floor:
        errors.append(f"Buying halted at or below EUR {floor}")
    if value + fee > portfolio.cash_eur.get(instrument.broker, Decimal(0)):
        errors.append("Insufficient available cash at this broker")
    if held * quote.price * quote.fx_to_eur + value > portfolio.equity_eur * cfg.max_position_weight:
        errors.append("Position concentration limit exceeded")
    if sum(portfolio.cash_eur.values()) - value - fee < portfolio.equity_eur * cfg.min_cash_weight:
        errors.append("Cash reserve would be breached")
    if instrument.asset_class == "crypto":
        crypto_ids = {i.id for i in settings.instruments if i.asset_class == "crypto"}
        crypto_value = sum(p.value_eur for p in portfolio.positions if p.instrument_id in crypto_ids)
        if crypto_value + value > portfolio.equity_eur * cfg.max_crypto_weight:
            errors.append("Crypto exposure limit exceeded")
    return errors
