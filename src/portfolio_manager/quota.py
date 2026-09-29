import asyncio
from datetime import timedelta

from .models import now


class QuotaExceeded(RuntimeError):
    pass


class Quotas:
    """Shared across demo-free, paper, live, and research processes; failed calls count."""

    def __init__(self, ledger, config):
        self.ledger, self.config = ledger, config

    def usage(self, provider, start):
        return self.ledger.db.execute("SELECT COALESCE(SUM(cost),0) FROM usage WHERE provider=? AND at>=?", (provider, start.isoformat())).fetchone()[0]

    def summary(self):
        t = now()
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"openrouter": self.usage("openrouter", day), "twelvedata": self.usage("twelvedata", day), "tavily": self.usage("tavily", day.replace(day=1))}

    async def acquire(self, provider, *, buy=False, cost=1):
        if cost <= 0:
            raise ValueError("Quota costs must be positive")
        c = self.config
        cap = {"openrouter": c.openrouter_per_day, "tavily": c.tavily_per_month, "twelvedata": c.twelvedata_per_day}[provider]
        minute_cap = {"openrouter": c.openrouter_per_minute, "twelvedata": c.twelvedata_per_minute}.get(provider)
        reserve = c.reserve_for_sells if provider == "openrouter" and buy else 0
        while True:
            stamp = now()
            start = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            if provider == "tavily":
                start = start.replace(day=1)
            with self.ledger.transaction():
                if self.usage(provider, start) + cost > cap - reserve:
                    raise QuotaExceeded(f"{provider} free quota or reserved sell budget reached")
                if minute_cap and self.usage(provider, stamp - timedelta(seconds=60)) + cost > minute_cap:
                    wait = True
                else:
                    self.ledger.db.execute("INSERT INTO usage(provider,at,cost) VALUES (?,?,?)", (provider, stamp.isoformat(), cost))
                    return
            if wait:
                await asyncio.sleep(1)
