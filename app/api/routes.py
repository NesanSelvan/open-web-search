"""HTTP surface. Every route except /health requires the API key."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from selectolax.parser import HTMLParser

from app.api.schemas import (
    MapRequest,
    MapResponse,
    ScrapeRequest,
    ScrapeResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.concurrency import Overloaded
from app.scrape.clean import page_title, to_markdown
from app.search.google import SearchUnavailable, build_query
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter()


async def require_api_key(x_api_key: str = Header(default="", alias="X-API-Key")) -> None:
    """One key, one header. Constant-time compare so a wrong key costs the same
    as a right one, and an unset key rejects everything rather than nothing."""
    expected = get_settings().api_key
    if not expected or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")


def _unavailable(pool, detail: str) -> HTTPException:
    """503 for a pool that has nothing to search with, with an honest wait.

    The identity timers already know when the next one comes back, so the caller
    is told the real number instead of retrying blind into a pool that is parked
    precisely because it was hit too hard.
    """
    wait = pool.health().get("ready_in_s")
    headers = {"Retry-After": str(max(1, int(wait)))} if wait is not None else None
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail, headers=headers)


@contextlib.asynccontextmanager
async def _admit(request: Request):
    """Take a concurrency slot, or refuse now with Retry-After.

    A 503 the caller can act on beats a 90-second wait that ends in a timeout,
    because the client can back off or shed the request itself.
    """
    svc = request.app.state.services
    try:
        async with svc.admission.slot():
            yield
    except Overloaded as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"overloaded: {exc}",
            headers={"Retry-After": str(int(exc.retry_after_s))},
        ) from exc


def _extract_links(html: str) -> list[str]:
    tree = HTMLParser(html)
    seen: set[str] = set()
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "")
        if href.startswith("http") and href not in seen:
            seen.add(href)
            links.append(href)
    return links


@router.post("/search", response_model=SearchResponse, dependencies=[Depends(require_api_key)])
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    async with _admit(request):
        return await _search(req, request)


async def _search(req: SearchRequest, request: Request) -> SearchResponse:
    """Search, and optionally scrape and read every result in the same call.

    `scrape: []`               -> URLs only, ~3s.
    `scrape: ["markdown"]`     -> each result's page content too.
    """
    svc = request.app.state.services
    settings = get_settings()
    query = req.q if req.raw else build_query(
        req.q, req.brand, req.site, settings.query_suffix
    )

    started = time.perf_counter()
    timing: dict = {"identity_wait_ms": 0, "serp_cache_hit": False}

    try:
        results = await svc.searcher.search(
            req.q,
            brand=req.brand,
            site=req.site,
            limit=req.limit,
            raw_query=req.q if req.raw else None,
            timing=timing,
        )
    except SearchUnavailable as exc:
        raise _unavailable(request.app.state.services.pool, str(exc)) from exc

    hits = [
        SearchHit(url=r.url, title=r.title, snippet=r.snippet, rank=r.rank) for r in results
    ]

    formats = req.scrape
    if not formats:
        timing["total_ms"] = int((time.perf_counter() - started) * 1000)
        return SearchResponse(query=query, results=hits, timing_ms=timing)

    geo = (
        (req.lat, req.lng)
        if req.lat is not None and req.lng is not None
        else (settings.default_geo_lat, settings.default_geo_lng)
    )

    # Fetch every result in parallel; one dead URL must not sink the response.
    # Each page is timed on its own — a 40s request is usually ONE slow site, and
    # an aggregate number hides which.
    async def timed_fetch(url: str) -> tuple[object, int]:
        t0 = time.perf_counter()
        try:
            page = await svc.scraper.fetch(url, geo=geo)
        except Exception as exc:
            return exc, int((time.perf_counter() - t0) * 1000)
        return page, int((time.perf_counter() - t0) * 1000)

    targets = hits[: req.scrape_top] if req.scrape_top else hits
    scrape_started = time.perf_counter()
    tasks = [asyncio.create_task(timed_fetch(h.url)) for h in targets]
    done, pending = await asyncio.wait(
        tasks, timeout=req.scrape_deadline_ms / 1000, return_when=asyncio.ALL_COMPLETED
    )
    for task in pending:
        task.cancel()

    fetched: list[tuple[object, int]] = []
    for task in tasks:
        if task in done and not task.cancelled():
            fetched.append(task.result())
        else:
            fetched.append((TimeoutError("scrape deadline exceeded"), req.scrape_deadline_ms))

    timing["scrape_ms"] = int((time.perf_counter() - scrape_started) * 1000)
    timing["scrape_deadline_ms"] = req.scrape_deadline_ms
    timing["timed_out_pages"] = sum(1 for p, _ in fetched if isinstance(p, TimeoutError))
    pages = [p for p, _ in fetched]
    timing["per_page_ms"] = {
        h.url.split("/")[2] if "/" in h.url else h.url: ms for h, (_, ms) in zip(targets, fetched)
    }

    # Reading a page into markdown is CPU work (~0.4s per page) and was running on the
    # event loop, one page after another: eight cached pages cost ~3s AFTER the search
    # was done, and every other request — /health included — waited behind it. Run the
    # conversions in threads, all at once; lxml releases the GIL for most of it.
    async def read(hit: SearchHit, page: object) -> None:
        if isinstance(page, TimeoutError):
            hit.status = 0
            hit.track = "timeout"
            return
        if isinstance(page, Exception):
            log.info("scrape failed url=%s %s", hit.url, page)
            hit.status = 0
            hit.track = "failed"
            return
        hit.status = page.status
        hit.track = page.track
        hit.final_url = page.final_url
        hit.page_title = await asyncio.to_thread(page_title, page.html)
        if "markdown" in formats:
            hit.markdown = await asyncio.to_thread(
                to_markdown, page.html, page.final_url or page.url
            )
        if "links" in formats:
            hit.links = await asyncio.to_thread(_extract_links, page.html)
        if "html" in formats:
            hit.html = page.html

    convert_started = time.perf_counter()
    await asyncio.gather(*(read(h, p) for h, p in zip(targets, pages)))
    timing["convert_ms"] = int((time.perf_counter() - convert_started) * 1000)

    timing["total_ms"] = int((time.perf_counter() - started) * 1000)
    return SearchResponse(query=query, results=hits, timing_ms=timing)


@router.post("/scrape", response_model=ScrapeResponse, dependencies=[Depends(require_api_key)])
async def scrape(req: ScrapeRequest, request: Request) -> ScrapeResponse:
    async with _admit(request):
        return await _scrape(req, request)


async def _scrape(req: ScrapeRequest, request: Request) -> ScrapeResponse:
    svc = request.app.state.services
    geo = (req.lat, req.lng) if req.lat is not None and req.lng is not None else None

    try:
        page = await svc.scraper.fetch(str(req.url), geo=geo, force_fresh=req.force_fresh)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"fetch failed: {exc}") from exc

    out = ScrapeResponse(
        url=page.url,
        final_url=page.final_url,
        status=page.status,
        track=page.track,
        title=await asyncio.to_thread(page_title, page.html),
    )
    if "markdown" in req.formats:
        out.markdown = await asyncio.to_thread(to_markdown, page.html, page.final_url or page.url)
    if "links" in req.formats:
        out.links = await asyncio.to_thread(_extract_links, page.html)
    if "html" in req.formats:
        out.html = page.html
    return out


@router.post("/map", response_model=MapResponse, dependencies=[Depends(require_api_key)])
async def map_domain(req: MapRequest, request: Request) -> MapResponse:
    svc = request.app.state.services
    urls = await svc.scraper.map_domain(req.domain, req.search)
    return MapResponse(domain=req.domain, urls=urls[: req.limit])



@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness, plus everything worth knowing about the pool.

    Always 200 while the process is answering: the container healthcheck reads
    this, and an unhealthy container is one autoheal rule away from a restart
    that discards the warm Chrome contexts — the opposite of what a blocked
    identity needs. `status` carries the bad news; /ready is what pages you.
    """
    svc = request.app.state.services
    pool = svc.pool.health()
    usable = bool(pool.get("usable", True))
    return {
        "ok": usable,
        "status": "ok" if usable else "degraded",
        "identity_pool": pool,
        "browsers": {
            "open_contexts": svc.browsers.open_contexts(),
            "open_tabs": svc.browsers.open_tabs(),
            "max_tabs_per_context": svc.settings.max_tabs_per_context,
            "max_open": svc.settings.max_open_contexts,
            "idle_ttl_s": svc.settings.context_idle_ttl_s,
        },
        "concurrency": {
            **svc.admission.stats(),
            "coalescing": svc.searcher._flight.stats(),
        },
        "cache": await svc.cache.stats(),
    }


@router.get("/ready")
async def ready(request: Request) -> dict:
    """Readiness: can a search run right now?

    Split from /health after an outage where both identities sat quarantined,
    every /search 503'd, and /health answered `ok: true` for the whole window —
    so no monitor ever fired. 503 here is the signal to alert on, and the
    Retry-After is exact rather than guessed: it comes from the soonest
    identity's own timer.
    """
    pool = request.app.state.services.pool.health()
    wait = pool.get("ready_in_s")
    if pool.get("usable", True):
        return {"ready": True, "ready_in_s": wait, "identity_pool": pool}

    retry_after = int(wait) if wait is not None else 3600
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"no identity can search: {pool['states']}",
        headers={"Retry-After": str(max(1, retry_after))},
    )
