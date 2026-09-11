"""`/resolve` — food name in, nutrition panel out.

    cache -> google -> scrape top N in parallel -> read each -> pick best -> cache

Cold: ~6-8s. Cached: ~15ms. Both are honest numbers; there is no arrangement of
real web search and real page fetching that answers in milliseconds the first time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from app.cache.store import CacheStore, normalise_name
from app.domain.models import Panel, Rejection, SearchResult
from app.extract.registry import Reader
from app.scrape.policy import PolicyBook, registrable_domain
from app.scrape.scraper import Scraper
from app.search.google import GoogleSearcher, SearchUnavailable
from app.settings import Settings

log = logging.getLogger(__name__)

# A site's own search-results page can never carry a single product's panel, and
# Google returns them often. Opening one costs a fetch and always ends in no_panel.
_SEARCH_PAGE_RE = re.compile(r"/search\b|[?&]q=|[?&]query=|/s\?k=", re.I)

RESOLUTION_TTL_S = 30 * 24 * 3600


@dataclass(slots=True)
class ResolveOutcome:
    """`panel` is the serialised panel dict, so a cache hit and a fresh read look
    identical to the caller — no branching on where the answer came from."""

    status: str                                   # resolved | not_found | rejected
    panel: dict | None = None
    rejections: list[Rejection] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    from_cache: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "panel": self.panel,
            "rejections": [r.to_dict() for r in self.rejections],
            "considered": self.considered,
            "elapsed_ms": self.elapsed_ms,
            "from_cache": self.from_cache,
        }


_UNRANKED = 10_000


def _rank(results: list[SearchResult], policies: PolicyBook) -> list[SearchResult]:
    """Ranked domains first, then the search engine's own order.

    The ranking lives in domains.yaml, not here. Which sites are worth opening
    first is a deployment's opinion — an Indian nutrition corpus and a US one
    disagree completely — and baking it into the engine makes the engine
    single-purpose.
    """

    def key(r: SearchResult) -> tuple[int, int]:
        policy = policies.for_url(r.url)
        return (policy.rank if policy.rank is not None else _UNRANKED, r.rank)

    return sorted(results, key=key)


def _better(a: Panel, b: Panel) -> Panel:
    """Pick between two readings of the same food.

    Tier first — a printed label beats a crowd aggregate regardless of how complete
    the aggregate looks. Only within a tier does completeness decide.
    """
    if a.tier.value != b.tier.value:
        return a if a.tier.value < b.tier.value else b
    return a if a.confidence >= b.confidence else b


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class Orchestrator:
    def __init__(
        self,
        searcher: GoogleSearcher,
        scraper: Scraper,
        reader: Reader,
        policies: PolicyBook,
        cache: CacheStore,
        settings: Settings,
    ):
        self._search = searcher
        self._scrape = scraper
        self._read = reader
        self._policies = policies
        self._cache = cache
        self._s = settings

    async def resolve(
        self,
        food_name: str,
        *,
        brand: str | None = None,
        site: str | None = None,
        geo: tuple[float, float] | None = None,
        max_pages: int = 4,
        use_cache: bool = True,
        scrape_deadline_ms: int = 4000,
    ) -> ResolveOutcome:
        started = time.perf_counter()
        key = normalise_name(food_name, brand)

        if use_cache:
            cached = await self._cache.get_resolution(key)
            if cached:
                return ResolveOutcome(
                    status="resolved",
                    panel=cached,
                    considered=[cached.get("source_url", "")],
                    elapsed_ms=_elapsed_ms(started),
                    from_cache=True,
                )

        try:
            results = await self._search.search(food_name, brand=brand, site=site)
        except SearchUnavailable as exc:
            log.error("search unavailable food=%r %s", food_name, exc)
            await self._cache.record_miss(key, food_name, "search_unavailable")
            return ResolveOutcome(status="not_found", elapsed_ms=_elapsed_ms(started))

        ordered = [
        r for r in _rank(results, self._policies) if not _SEARCH_PAGE_RE.search(r.url)
    ][:max_pages]
        urls = [r.url for r in ordered]
        pages = await self._scrape.fetch_many(
            urls, geo=geo, deadline_ms=scrape_deadline_ms
        )

        readings = await asyncio.gather(
            *(
                self._read.read(page, self._policies.for_url(page.final_url or page.url))
                for page in pages
            ),
            return_exceptions=True,
        )

        best: Panel | None = None
        rejections: list[Rejection] = []
        for res in readings:
            if isinstance(res, Exception):
                log.info("reader raised: %s", res)
            elif isinstance(res, Rejection):
                rejections.append(res)
            else:
                best = res if best is None else _better(best, res)

        if best is not None:
            panel_dict = best.to_dict()
            await self._cache.put_resolution(key, food_name, panel_dict, RESOLUTION_TTL_S)
            return ResolveOutcome(
                status="resolved",
                panel=panel_dict,
                rejections=rejections,
                considered=urls,
                elapsed_ms=_elapsed_ms(started),
            )

        # Nothing readable. Distinguish "could not find it" from "found it and it is
        # not honestly extractable" — the caller must treat those differently.
        if rejections:
            await self._cache.record_miss(key, food_name, rejections[0].reason.value)
            return ResolveOutcome(
                status="rejected",
                rejections=rejections,
                considered=urls,
                elapsed_ms=_elapsed_ms(started),
            )

        await self._cache.record_miss(key, food_name, "no_panel")
        return ResolveOutcome(
            status="not_found", considered=urls, elapsed_ms=_elapsed_ms(started)
        )
