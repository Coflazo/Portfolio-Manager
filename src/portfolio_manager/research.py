from __future__ import annotations

import asyncio
import hashlib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from .models import Evidence, now


def evidence(title, url, text, published_at=None):
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    return Evidence(id=hashlib.sha256((url + content_hash).encode()).hexdigest()[:20], title=title[:300], url=url,
                    text=text[:6000], content_hash=content_hash, published_at=published_at)


def parse_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


class Research:
    def __init__(self, client, quotas, ledger, key, feeds):
        self.client, self.quotas, self.ledger = client, quotas, ledger
        self.key, self.feeds = key, feeds
        self.cache = {}

    async def search(self, query):
        query = query.strip()[:240]
        cached = self.cache.get(query)
        if cached and (now() - cached[0]).total_seconds() < 900:
            return cached[1]
        if not self.key:
            raise RuntimeError("TAVILY_API_KEY is required for council web research")
        await self.quotas.acquire("tavily")
        response = await self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.key}"},
                                          json={"query": query, "search_depth": "basic", "max_results": 5,
                                                "include_answer": False, "include_raw_content": False}, timeout=30)
        response.raise_for_status()
        result = [evidence(r.get("title", "Search result"), r["url"], r.get("content", ""), parse_date(r.get("published_date")))
                  for r in response.json().get("results", []) if r.get("url", "").startswith("https://") and r.get("content")]
        for item in result:
            self.ledger.save_evidence(item)
        self.cache[query] = (now(), result)
        return result

    async def feed(self, url):
        # URLs are administrator configuration; models never supply fetch URLs.
        if not url.startswith(("https://www.federalreserve.gov/", "https://www.ecb.europa.eu/", "https://www.sec.gov/", "https://www.eia.gov/", "https://www.usda.gov/")):
            raise ValueError("RSS source must be an approved primary-source HTTPS domain")
        response = await self.client.get(url, timeout=15)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Feed too large")
        root = ET.fromstring(response.content)
        result = []
        for item in root.findall(".//item")[:15]:
            published = parse_date(item.findtext("pubDate"))
            if published and published < now() - timedelta(days=7):
                continue
            result.append(evidence(item.findtext("title", "Release"), item.findtext("link", url), item.findtext("description", item.findtext("title", "")), published))
        return result

    async def collect(self, instrument, proposal, quote):
        market = evidence(f"Observed quote: {instrument.id}", quote.source,
                          f"{instrument.id}: price={quote.price} {instrument.currency}; quote_timestamp={quote.at.isoformat()}; "
                          f"FX_to_EUR={quote.fx_to_eur}; FX_timestamp={quote.fx_at.isoformat()}. This is a price observation, not a forecast.", quote.at)
        query = f"{instrument.name} {instrument.id} market news earnings risks {now().date()}"
        results = await asyncio.gather(self.search(query), *(self.feed(url) for url in self.feeds), return_exceptions=True)
        merged = {market.id: market}
        for result in results:
            if isinstance(result, BaseException):
                self.ledger.event("research_unavailable", {"error": type(result).__name__})
            else:
                merged.update({item.id: item for item in result})
        # Missing external research must not be silently replaced by a quote.
        if len(merged) < 2:
            raise RuntimeError("No external research evidence available")
        for item in merged.values():
            self.ledger.save_evidence(item)
        return list(merged.values())
