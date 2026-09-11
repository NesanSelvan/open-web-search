"""Service wiring and lifespan.

Everything is constructed once at startup and hung off `app.state.services`, so the
routes stay thin and every component can be built standalone in a test.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from app.api.routes import router
from app.cache.store import CacheStore
from app.concurrency import AdmissionControl
from app.extract.registry import Reader
from app.orchestrator import Orchestrator
from app.scrape.governor import DomainGovernor
from app.scrape.policy import PolicyBook
from app.scrape.scraper import Scraper
from app.search.browser import BrowserManager
from app.search.google import GoogleSearcher
from app.search.identity import IdentityPool
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Services:
    settings: Settings
    pool: IdentityPool
    scrape_pool: IdentityPool
    browsers: BrowserManager
    policies: PolicyBook
    governor: DomainGovernor
    cache: CacheStore
    searcher: GoogleSearcher
    scraper: Scraper
    reader: Reader
    orchestrator: Orchestrator
    admission: AdmissionControl


def _scrape_pool(settings: Settings) -> IdentityPool:
    """Identities for page fetching — separate profiles, near-zero cooldown.

    Pacing for retail domains is the per-domain governor's job, not an identity
    cooldown; Google's 30s is a Google tax and must not leak onto Swiggy.
    """
    path = settings.scrape_identities_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Auto-created. Identities for PAGE FETCHING, separate from search.\n"
            "# Add a residential exit for the quick-commerce domains (needs_residential\n"
            "# in domains.yaml) so Swiggy resolves to the store WE choose.\n"
            "scrape1||IN\n"
        )
        log.warning("created default scrape identity file at %s", path)

    scrape_settings = settings.model_copy(
        update={
            "identities_file": path,
            "cooldown_min_s": settings.scrape_cooldown_min_s,
            "cooldown_max_s": settings.scrape_cooldown_max_s,
        }
    )
    return IdentityPool.from_file(scrape_settings)


def build_services(settings: Settings) -> Services:
    pool = IdentityPool.from_file(settings)
    scrape_pool = _scrape_pool(settings)
    policies = PolicyBook.load(settings.domains_file)
    governor = DomainGovernor()
    cache = CacheStore(settings.db_path)
    browsers = BrowserManager(settings)
    searcher = GoogleSearcher(pool, settings, browsers=browsers, cache=cache)
    scraper = Scraper(
        policies, governor, scrape_pool, settings, cache=cache, browsers=browsers
    )
    reader = Reader(settings, policies=policies)
    orchestrator = Orchestrator(searcher, scraper, reader, policies, cache, settings)
    admission = AdmissionControl(
        max_in_flight=settings.max_in_flight,
        max_queued=settings.max_queued,
        retry_after_s=settings.overload_retry_after_s,
    )
    return Services(
        settings=settings,
        pool=pool,
        scrape_pool=scrape_pool,
        browsers=browsers,
        policies=policies,
        governor=governor,
        cache=cache,
        searcher=searcher,
        scraper=scraper,
        reader=reader,
        orchestrator=orchestrator,
        admission=admission,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    services = build_services(settings)
    await services.cache.open()
    # Start Chrome once, not per request: cold launch is ~1.3s of a ~2.7s search.
    await services.browsers.start()
    app.state.services = services
    log.info("open-web-search up — %s", services.pool.health())
    try:
        yield
    finally:
        await services.browsers.close()
        await services.cache.close()


app = FastAPI(
    title="open-web-search",
    version="0.1.0",
    summary="Self-hosted web search + scrape for food nutrition data.",
    lifespan=lifespan,
)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level=s.log_level.lower())
