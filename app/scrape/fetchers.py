"""Two fetch tracks.

Track A (static) is a plain HTTP GET and returns in ~300ms — most pages are
server-rendered and this is all they need. Track B (browser) exists for pages that
render in JavaScript, and for sites where the exit IP and the geolocation decide
*what you are even shown*.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.domain.models import Page
from app.scrape.policy import DomainPolicy
from app.search.browser import BrowserManager, chrome_context
from app.search.identity import Identity
from app.settings import Settings

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    # Overridden per deployment from settings.accept_language; see _headers().
    # NO Accept-Encoding here, deliberately. Hardcoding "gzip, deflate, br"
    # advertises brotli support that httpx only has when the brotli package is
    # installed; servers then reply `content-encoding: br` and every response
    # decodes to binary garbage. httpx sets this header itself, to exactly the
    # codecs it can actually decode. Silent, total, and it made every static fetch
    # look unreadable so the scraper escalated to a browser 100% of the time.
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _headers(accept_language: str | None = None) -> dict[str, str]:
    headers = dict(_HEADERS)
    headers["Accept-Language"] = accept_language or "en-US,en;q=0.9"
    return headers


async def fetch_static(
    url: str,
    *,
    timeout: float = 15.0,
    proxy: str | None = None,
    accept_language: str | None = None,
) -> Page:
    async with httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=timeout,
        headers=_headers(accept_language),
        proxy=proxy,
    ) as client:
        resp = await client.get(url)
        return Page(
            url=url,
            html=resp.text,
            status=resp.status_code,
            fetched_at=time.time(),
            track="static",
            final_url=str(resp.url),
        )


async def fetch_browser(
    url: str,
    ident: Identity,
    settings: Settings,
    policy: DomainPolicy,
    geo_override: tuple[float, float] | None = None,
    browsers: BrowserManager | None = None,
) -> Page:
    """Render with a real browser, through this identity's exit.

    `geo` is the whole point for quick-commerce: Swiggy serves a different catalogue
    per location, and some hide content entirely for items unavailable there.
    Setting it ourselves is what a hosted vendor cannot give us.
    """
    geo = geo_override or policy.geo

    async def render(page) -> Page:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass  # networkidle is a nicety; a live SPA may never reach it
        return Page(
            url=url,
            html=await page.content(),
            status=resp.status if resp else 0,
            fetched_at=time.time(),
            track="browser",
            final_url=page.url,
        )

    if browsers is not None:
        # A TAB on the shared Chrome, not a browser of our own. Several fetches
        # render at once in one browser: ~55ms and tens of MB per tab, against
        # ~1330ms and ~1GB for another browser.
        async with browsers.page(ident, geo=geo) as page:
            return await render(page)

    # No manager (scripts, tests): fall back to a throwaway browser.
    async with chrome_context(ident, settings, geo=geo) as ctx:
        page = await ctx.new_page()
        try:
            return await render(page)
        finally:
            await page.close()
