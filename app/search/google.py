"""Google SERP via a leased identity.

Returns ranked organic results, or raises Blocked so the caller can retry on a
different identity. We never retry a block on the same identity — that is exactly
the behaviour that gets an exit burnt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import time
import urllib.parse

import httpx

from playwright.async_api import Page as PWPage

from app.domain.models import SearchResult
from app.search.browser import BrowserManager, chrome_context
from app.concurrency import SingleFlight
from app.search.identity import Identity, IdentityPool, Outcome, PoolExhausted
from app.settings import Settings

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.google.com/search"

# Extract organic results from the live DOM rather than a fixed class name.
# Google's generated class names churn constantly; "an anchor that owns an h3" has
# been stable for years.
#
# Every organic href now arrives wrapped as google.com/goto?url=<opaque>, so a
# naive "drop anything on a google host" filter throws away 100% of the real
# results. We keep the wrapper and resolve it afterwards (one 302 per result), and
# drop only genuine Google-internal links — AI Mode, nav, tools.
#
# `cite` is the visible host line. When it is present but not a URL ("20+ likes ·
# 9 months ago") the unit is a social/video card, not an organic result.
_EXTRACT_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a')) {
    const h3 = a.querySelector('h3');
    if (!h3) continue;
    const href = a.href;
    if (!href || seen.has(href)) continue;

    let u;
    try { u = new URL(href); } catch { continue; }
    const isGoogleHost = /(^|\\.)google\\.[a-z.]+$/.test(u.hostname);
    const isWrapper = isGoogleHost && (u.pathname === '/goto' || u.pathname === '/url');
    if (isGoogleHost && !isWrapper) continue;
    if (u.hostname === 'webcache.googleusercontent.com') continue;

    const card = a.closest('div[data-hveid]') || a.parentElement;
    const citeEl = card ? card.querySelector('cite') : null;
    const cite = citeEl ? citeEl.innerText.trim() : '';
    if (cite && !/^https?:\\/\\//i.test(cite)) continue;

    const snippetEl = card ? card.querySelector('div[data-sncf], div[role="text"], .VwiC3b') : null;
    seen.add(href);
    out.push({
      url: href,
      title: h3.innerText.trim(),
      snippet: snippetEl ? snippetEl.innerText.trim() : '',
      cite: cite,
      wrapped: isWrapper
    });
  }
  return out;
}
"""

# Markup that proves Google rendered a normal results page. If this is present and
# we still extracted nothing, the fault is OURS — see ExtractionFailed.
_RESULTS_MARKUP = "div#rso, div#search"

_BLOCK_MARKERS = (
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "why did this happen",
)


class Blocked(RuntimeError):
    """Google served a CAPTCHA or a /sorry/ redirect. The identity is burnt."""


class ExtractionFailed(RuntimeError):
    """Google served a normal results page and OUR parser got nothing out of it.

    Kept distinct from Blocked on purpose. Quarantining a perfectly good exit for
    five minutes because our own selector broke is both wrong and self-inflicted —
    and it is exactly what happened the first time this ran, when the new
    google.com/goto wrapper made the extractor discard every real result.
    """


class SearchUnavailable(RuntimeError):
    """Every retry was exhausted. Caller may fall back to a paid SERP."""


def build_query(
    query: str,
    brand: str | None = None,
    site: str | None = None,
    suffix: str = "",
) -> str:
    """`<brand> <query> <suffix>`, optionally pinned to one domain.

    Suffix is EMPTY by default: a search engine searches what you typed. A
    deployment aimed at one corpus can set `WS_QUERY_SUFFIX` to steer every query,
    and `raw: true` bypasses query building entirely.

    The `site:` form trades recall for precision, for when you already know which
    domain holds the answer.
    """
    parts = [p for p in (brand, query) if p]
    q = " ".join(parts).strip()
    if suffix:
        q = f"{q} {suffix}".strip()
    if site:
        q = f"site:{site} {q}"
    return q


async def _dismiss_consent(page: PWPage) -> None:
    """Click through a consent interstitial if one is shown. Best effort."""
    for label in ("Accept all", "I agree", "Reject all"):
        try:
            button = page.get_by_role("button", name=label)
            if await button.count():
                await button.first.click(timeout=2000)
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
                return
        except Exception:
            continue


async def _is_blocked(page: PWPage, deep: bool = False) -> bool:
    """Cheap checks first.

    `inner_text("body")` serialises the WHOLE document, and a Google SERP is ~1.6MB
    — doing that on every successful search costs more than the search. The URL and
    the CAPTCHA selectors settle the common cases for free; the text scan only runs
    when something already looks wrong (`deep=True`).
    """
    if "/sorry/" in page.url:
        return True
    try:
        if await page.locator("form#captcha-form, div#recaptcha, iframe[src*='recaptcha']").count():
            return True
        if not deep:
            return False
        body = (await page.inner_text("body"))[:4000].lower()
    except Exception:
        return False
    return any(marker in body for marker in _BLOCK_MARKERS)


async def _resolve_wrapped(items: list[dict], ident: Identity) -> list[dict]:
    """Turn google.com/goto?url=<opaque> into the real destination.

    The wrapper payload is opaque, so it cannot be decoded locally — but it is a
    plain 302, which costs one cheap request per result and resolves in parallel.
    We follow at most two hops and never download the destination body.
    """
    proxy = None
    if ident.proxy:
        auth = f"{ident.proxy.username}:{ident.proxy.password}@" if ident.proxy.username else ""
        scheme, _, hostport = ident.proxy.server.partition("://")
        proxy = f"{scheme}://{auth}{hostport}"

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=12.0, headers={"User-Agent": ident.user_agent}, proxy=proxy
    ) as client:

        async def one(item: dict) -> dict | None:
            if not item.get("wrapped"):
                return item
            url = item["url"]
            for _ in range(2):
                try:
                    resp = await client.get(url)
                except Exception as exc:
                    log.info("goto resolve failed %s", exc)
                    return None
                location = resp.headers.get("location")
                if resp.status_code in (301, 302, 303, 307, 308) and location:
                    url = location
                    if "google." not in urllib.parse.urlsplit(url).netloc:
                        return {**item, "url": url}
                    continue
                return None
            return None

        resolved = await asyncio.gather(*(one(i) for i in items))

    return [r for r in resolved if r]


async def _search_once(
    ident: Identity,
    query: str,
    limit: int,
    settings: Settings,
    referer: str | None,
    browsers: BrowserManager | None = None,
) -> list[SearchResult]:
    params = {"q": query, "num": str(min(limit + 5, 20)), "hl": "en", "gl": ident.country.lower()}
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    # A kept-open context saves ~1.3s of the ~2.7s a search costs. Scripts pass no
    # manager and fall back to a one-shot launch.
    if browsers is not None:
        ctx = await browsers.context(ident)
        owned = None
    else:
        owned = chrome_context(ident, settings)
        ctx = await owned.__aenter__()

    try:
        page = await ctx.new_page()
        try:
            # A missing referrer on a search navigation is a scored anomaly, so we
            # always arrive from somewhere.
            await page.goto(
                url,
                referer=referer or "https://www.google.com/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await _dismiss_consent(page)

            if await _is_blocked(page):
                raise Blocked(f"identity={ident.id}")

            try:
                await page.wait_for_selector("a h3", timeout=8_000)
            except Exception:
                if await _is_blocked(page, deep=True):
                    raise Blocked(f"identity={ident.id}")
                raise Blocked(f"no results rendered (identity={ident.id})")

            # A beat of dwell time before reading — instant scrape-and-leave is a
            # behavioural signal in its own right. Kept short and jittered: the
            # value is that it VARIES, not that it is long.
            await asyncio.sleep(random.uniform(settings.dwell_min_s, settings.dwell_max_s))

            raw = await page.evaluate(_EXTRACT_JS)
            has_results_markup = bool(await page.locator(_RESULTS_MARKUP).count())
        finally:
            await page.close()
    finally:
        if owned is not None:
            await owned.__aexit__(None, None, None)

    if not raw:
        # Google rendering a results page we cannot read is our bug, not a block.
        # Blaming the exit for it burns a good identity for five minutes.
        if has_results_markup:
            raise ExtractionFailed(
                f"results page rendered but 0 extracted (identity={ident.id}) — "
                "SERP markup has probably changed, run scripts/debug_serp.py"
            )
        raise Blocked(f"empty result set (identity={ident.id})")

    # Resolve a small buffer over `limit` so a dead redirect still leaves enough
    # results, but not the whole page — each one is a network round trip.
    resolved = await _resolve_wrapped(raw[: limit + 2], ident)
    if not resolved:
        raise ExtractionFailed(f"every result failed to resolve (identity={ident.id})")

    return [
        SearchResult(url=r["url"], title=r["title"], snippet=r["snippet"], rank=i + 1)
        for i, r in enumerate(resolved[:limit])
    ]


class GoogleSearcher:
    def __init__(
        self,
        pool: IdentityPool,
        settings: Settings,
        browsers: BrowserManager | None = None,
        cache=None,
    ):
        self._pool = pool
        self._s = settings
        self._browsers = browsers
        self._cache = cache
        # Ten concurrent requests for one query used to lease ten identities.
        self._flight = SingleFlight()

    async def search(
        self,
        food_name: str,
        *,
        brand: str | None = None,
        site: str | None = None,
        limit: int | None = None,
        raw_query: str | None = None,
        use_cache: bool = True,
        timing: dict | None = None,
    ) -> list[SearchResult]:
        query = raw_query or build_query(food_name, brand, site, self._s.query_suffix)
        limit = limit or self._s.search_results
        last: Exception | None = None

        # A cached SERP costs no identity and no cooldown — this is what makes a
        # repeated query return in milliseconds instead of waiting one out.
        cache_key = f"{query}|{limit}"
        if self._cache and use_cache:
            cached = await self._cache.get_serp(cache_key)
            if cached:
                if timing is not None:
                    timing["serp_cache_hit"] = True
                return [SearchResult(**row) for row in cached]

        # Past the cache, collapse concurrent identical searches into one. The
        # cache cannot help here: it only fills once the first search returns, and
        # a burst lives entirely inside that window.
        if self._s.coalesce_requests:
            before = self._flight.executed
            results = await self._flight.do(
                cache_key,
                lambda: self._search_uncoalesced(query, limit, cache_key, timing),
            )
            # A waiter rode someone else's search: its timing describes that search,
            # which is accurate, but the flag says the work was not its own.
            if timing is not None and self._flight.executed == before:
                timing["coalesced"] = True
            return results
        return await self._search_uncoalesced(query, limit, cache_key, timing)

    async def _search_uncoalesced(
        self,
        query: str,
        limit: int,
        cache_key: str,
        timing: dict | None = None,
    ) -> list[SearchResult]:
        last: Exception | None = None

        for attempt in range(self._s.search_max_retries):
            # Time spent WAITING for a free identity is not search work — it is
            # the pool being too small for the request rate, and it must be visible
            # as its own number or every slow request looks like a slow search.
            wait_started = time.perf_counter()
            try:
                ident = await self._pool.acquire()
            except PoolExhausted as exc:
                raise SearchUnavailable(str(exc)) from exc
            if timing is not None:
                timing["identity_wait_ms"] = (
                    timing.get("identity_wait_ms", 0)
                    + int((time.perf_counter() - wait_started) * 1000)
                )
            google_started = time.perf_counter()

            try:
                results = await _search_once(
                    ident, query, limit, self._s, referer=None, browsers=self._browsers
                )
            except ExtractionFailed as exc:
                # Our parser, not their block. Release as ERROR so the identity
                # takes a normal cooldown instead of a five-minute quarantine.
                log.error("serp extraction failed attempt=%d %s", attempt + 1, exc)
                await self._pool.release(ident, Outcome.ERROR)
                last = exc
                continue
            except Blocked as exc:
                log.warning("serp blocked attempt=%d %s", attempt + 1, exc)
                await self._pool.release(ident, Outcome.BLOCKED)
                last = exc
                continue
            except Exception as exc:
                log.warning("serp error attempt=%d identity=%s %s", attempt + 1, ident.id, exc)
                await self._pool.release(ident, Outcome.ERROR)
                last = exc
                continue

            await self._pool.release(ident, Outcome.OK)
            if timing is not None:
                timing["google_ms"] = int((time.perf_counter() - google_started) * 1000)
            if self._cache:
                await self._cache.put_serp(
                    cache_key,
                    query,
                    [dataclasses.asdict(r) for r in results],
                    self._s.serp_cache_ttl_s,
                )
            return results

        raise SearchUnavailable(f"all {self._s.search_max_retries} attempts failed: {last}")
