"""HTTP surface. Internal-only — every route requires the shared token."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from selectolax.parser import HTMLParser

from app.api.schemas import (
    MapRequest,
    MapResponse,
    ResolveRequest,
    ResolveResponse,
    ScrapeRequest,
    ScrapeResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.concurrency import Overloaded
from app.domain.models import Rejection
from app.extract.clean import to_markdown
from app.search.google import SearchUnavailable, build_query
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter()


async def require_token(x_internal_token: str = Header(default="")) -> None:
    expected = get_settings().internal_token
    if not x_internal_token or x_internal_token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Internal-Token")


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


@router.post("/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    async with _admit(request):
        return await _search(req, request)


async def _search(req: SearchRequest, request: Request) -> SearchResponse:
    """Search, and optionally scrape and read every result in the same call.

    `scrape: []`               -> URLs only, ~3s.
    `scrape: ["markdown"]`     -> each result's page content too.
    `extract: true`            -> also run the nutrition reader on each page.
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
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    hits = [
        SearchHit(url=r.url, title=r.title, snippet=r.snippet, rank=r.rank) for r in results
    ]

    formats = req.scrape or (["markdown"] if req.extract else [])
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

    scrape_started = time.perf_counter()
    tasks = [asyncio.create_task(timed_fetch(h.url)) for h in hits]
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
        h.url.split("/")[2] if "/" in h.url else h.url: ms for h, (_, ms) in zip(hits, fetched)
    }
    read_started = time.perf_counter()

    for hit, page in zip(hits, pages):
        if isinstance(page, TimeoutError):
            hit.status = 0
            hit.track = "timeout"
            continue
        if isinstance(page, Exception):
            log.info("scrape failed url=%s %s", hit.url, page)
            hit.status = 0
            hit.track = "failed"
            continue

        hit.status = page.status
        hit.track = page.track
        hit.final_url = page.final_url
        if "markdown" in formats:
            hit.markdown = to_markdown(page.html, page.final_url or page.url)
        if "links" in formats:
            hit.links = _extract_links(page.html)
        if "html" in formats:
            hit.html = page.html

        if req.extract:
            policy = svc.policies.for_url(page.final_url or page.url)
            try:
                outcome = await svc.reader.read(page, policy)
            except Exception as exc:
                log.info("reader raised url=%s %s", hit.url, exc)
                continue
            if isinstance(outcome, Rejection):
                hit.rejected = outcome.to_dict()
            else:
                hit.panel = outcome.to_dict()

    if req.extract:
        timing["extract_ms"] = int((time.perf_counter() - read_started) * 1000)
    timing["total_ms"] = int((time.perf_counter() - started) * 1000)
    return SearchResponse(query=query, results=hits, timing_ms=timing)


@router.post("/scrape", response_model=ScrapeResponse, dependencies=[Depends(require_token)])
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
        url=page.url, final_url=page.final_url, status=page.status, track=page.track
    )
    if "markdown" in req.formats:
        out.markdown = to_markdown(page.html, page.final_url or page.url)
    if "links" in req.formats:
        out.links = _extract_links(page.html)
    if "html" in req.formats:
        out.html = page.html
    return out


@router.post("/map", response_model=MapResponse, dependencies=[Depends(require_token)])
async def map_domain(req: MapRequest, request: Request) -> MapResponse:
    svc = request.app.state.services
    urls = await svc.scraper.map_domain(req.domain, req.search)
    return MapResponse(domain=req.domain, urls=urls[: req.limit])


@router.post("/resolve", response_model=ResolveResponse, dependencies=[Depends(require_token)])
async def resolve(req: ResolveRequest, request: Request) -> ResolveResponse:
    async with _admit(request):
        return await _resolve(req, request)


async def _resolve(req: ResolveRequest, request: Request) -> ResolveResponse:
    svc = request.app.state.services
    settings = get_settings()
    geo = (
        (req.lat, req.lng)
        if req.lat is not None and req.lng is not None
        else (settings.default_geo_lat, settings.default_geo_lng)
    )

    outcome = await svc.orchestrator.resolve(
        req.food_name,
        brand=req.brand,
        site=req.site,
        geo=geo,
        max_pages=req.max_pages,
        use_cache=req.use_cache,
        scrape_deadline_ms=req.scrape_deadline_ms,
    )
    return ResolveResponse(**outcome.to_dict())


@router.get("/health")
async def health(request: Request) -> dict:
    svc = request.app.state.services
    return {
        "ok": True,
        "identity_pool": svc.pool.health(),
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
