"""Scrape orchestration: cache -> governor -> static track -> browser track.

The escalation rule is deliberately cheap-first. A plain GET answers most pages in
~300ms; we only pay for a browser when the static response carries too little text
to be useful, or when the domain's policy says the content is behind JavaScript.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app.domain.models import Page
from app.scrape.fetchers import fetch_browser, fetch_static
from app.scrape.governor import DomainGovernor
from app.scrape.policy import PolicyBook
from app.search.browser import BrowserManager
from app.search.identity import IdentityPool, Outcome, PoolExhausted
from app.settings import Settings

log = logging.getLogger(__name__)

# A static response is "readable" when it carries enough real text to be worth
# keeping. Below this, the page almost certainly rendered client-side and a browser
# will do better.
#
# This used to test for domain-specific keywords, which meant any page about
# anything else failed the check and was escalated to a browser for no reason. The
# engine must not assume what it is reading about.
_MIN_TEXT_CHARS = 500

_TAG_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_STRIP_RE = re.compile(r"<[^>]+>")


def _looks_readable(html: str) -> bool:
    """Does this response carry substantive text, independent of subject?"""
    if not html:
        return False
    body = _TAG_RE.sub(" ", html[:400_000])
    text = _STRIP_RE.sub(" ", body)
    return len(" ".join(text.split())) >= _MIN_TEXT_CHARS


class Scraper:
    def __init__(
        self,
        policies: PolicyBook,
        governor: DomainGovernor,
        pool: IdentityPool,
        settings: Settings,
        cache=None,
        browsers: BrowserManager | None = None,
    ):
        """`pool` MUST be the scrape pool, never the search pool.

        Two Chrome instances cannot share one profile directory, and borrowing a
        search identity puts every page fetch behind Google's 30s cooldown — which
        is what turned an 8-second request into a 120-second one.
        """
        self._policies = policies
        self._gov = governor
        self._pool = pool
        self._s = settings
        self._cache = cache
        self._browsers = browsers
        self._sem = asyncio.Semaphore(settings.scrape_concurrency)

    async def fetch(
        self,
        url: str,
        *,
        geo: tuple[float, float] | None = None,
        force_fresh: bool = False,
    ) -> Page:
        policy = self._policies.for_url(url)

        if self._cache and not force_fresh:
            cached = await self._cache.get_page(url)
            if cached:
                return Page(
                    url=url,
                    html=cached["html"],
                    status=cached["status"],
                    fetched_at=cached["fetched_at"],
                    track="cache",
                    final_url=cached["final_url"],
                )

        async with self._sem:
            await self._gov.acquire(policy.domain, policy.rate_per_min)

            page: Page | None = None

            if not policy.needs_browser:
                try:
                    candidate = await fetch_static(
                        url, accept_language=self._s.accept_language
                    )
                    if candidate.status < 400 and _looks_readable(candidate.html):
                        page = candidate
                    else:
                        log.info(
                            "static track insufficient url=%s status=%s — escalating",
                            url, candidate.status,
                        )
                except ImportError as exc:
                    # A missing optional dependency is permanent: it will fail for
                    # every URL, forever, while looking like a per-page hiccup.
                    log.error(
                        "static track is BROKEN for every request (%s) — "
                        "install the missing extra; escalating to browser meanwhile",
                        exc,
                    )
                except Exception as exc:
                    log.warning(
                        "static track failed url=%s %s: %s — escalating",
                        url, type(exc).__name__, exc,
                    )

            if page is None:
                page = await self._fetch_with_browser(url, policy, geo)

        if self._cache:
            await self._cache.put_page(
                url, page.html, page.status, page.track, policy.cache_ttl_s, page.final_url
            )
        return page

    async def _fetch_with_browser(self, url: str, policy, geo) -> Page:
        """Browser track: a tab on a SHARED browser.

        Page fetching is paced per target domain by the governor, not per identity
        — so it picks an identity to share rather than leasing one exclusively.
        Exclusive leases here serialised fetches that had no reason to be
        serialised, and threw away the concurrency a shared Chrome already offers.

        Geo is the point for quick-commerce: those sites serve a different
        catalogue per location, and some hide content entirely for items not
        available there. We choose the location; a hosted vendor chooses for you.
        """
        try:
            ident = self._pool.pick_shared()
        except PoolExhausted as exc:
            raise RuntimeError(f"browser track unavailable for {url}: {exc}") from exc

        return await fetch_browser(
            url, ident, self._s, policy, geo_override=geo, browsers=self._browsers
        )

    async def fetch_many(
        self,
        urls: list[str],
        *,
        geo: tuple[float, float] | None = None,
        deadline_ms: int | None = None,
    ) -> list[Page]:
        """Fetch in parallel under a hard deadline. One dead URL is not fatal.

        Without the deadline a single slow site sets the whole response time — a
        a request was measured at 48s because one page took its own sweet time
        while three others had long since finished.
        """
        tasks = [asyncio.create_task(self.fetch(u, geo=geo)) for u in urls]
        timeout = (deadline_ms / 1000) if deadline_ms else None
        done, pending = await asyncio.wait(tasks, timeout=timeout)

        for task in pending:
            task.cancel()

        pages: list[Page] = []
        for url, task in zip(urls, tasks):
            if task not in done or task.cancelled():
                log.info("scrape deadline exceeded url=%s", url)
                continue
            exc = task.exception()
            if exc is not None:
                log.info("scrape failed url=%s %s", url, exc)
                continue
            pages.append(task.result())
        return pages

    async def map_domain(self, domain: str, search: str | None = None) -> list[str]:
        """Enumerate URLs from a domain's sitemap. Cheap, polite, no browser."""
        from selectolax.parser import HTMLParser

        seeds = [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"]
        found: list[str] = []
        needle = (search or "").lower()

        for seed in seeds:
            try:
                await self._gov.acquire(domain, self._policies.for_url(seed).rate_per_min)
                page = await fetch_static(
                    seed, timeout=20.0, accept_language=self._s.accept_language
                )
            except Exception:
                continue
            if page.status >= 400:
                continue
            tree = HTMLParser(page.html)
            for loc in tree.css("loc"):
                url = loc.text(strip=True)
                if not url:
                    continue
                if needle and needle not in url.lower():
                    continue
                found.append(url)
            if found:
                break

        # Deduplicate, keep discovery order.
        seen: set[str] = set()
        return [u for u in found if not (u in seen or seen.add(u))]
