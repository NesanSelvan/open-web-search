"""Chrome launcher and a long-lived browser manager.

Headless is a *detection class*, not a flag — a screenless browser reports different
screen metrics, different WebGL behaviour, and says `HeadlessChrome` in its own UA.
So we launch REAL Google Chrome, headful, and give it a virtual display instead
(Xvfb, set up in the Dockerfile). Chrome renders into a monitor that doesn't
physically exist and nothing reads as screenless.

`channel="chrome"` is required: bundled Chromium has its own tells.

Contexts are kept alive between requests. Measured cold launch is ~1.33s of a
2.68s search — over half the request spent starting a browser we are about to throw
away.

But a live context is a full Chrome (~400-600MB), so "one per identity, forever"
is not a design, it is a memory leak with good latency: it OOM-killed the service
on a dev laptop, and would take a 4GB VPS with it. The open set is therefore capped
(`max_open_contexts`, LRU eviction) and idle contexts are reaped
(`context_idle_ttl_s`). Paying ~1.3s occasionally beats paying it always, and beats
being killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from app.search.identity import Identity
from app.settings import Settings

log = logging.getLogger(__name__)

# Flags that remove automation tells without pretending to be a stealth plugin.
# --disable-blink-features=AutomationControlled is the one that actually matters:
# without it navigator.webdriver is true and nothing else you do will save you.
_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints",
    "--password-store=basic",
    "--start-maximized",
]


def _proxy_dict(ident: Identity) -> dict | None:
    if not ident.proxy:
        return None
    proxy = {"server": ident.proxy.server}
    if ident.proxy.username:
        proxy["username"] = ident.proxy.username
        proxy["password"] = ident.proxy.password or ""
    return proxy


async def _launch(pw: Playwright, ident: Identity, settings: Settings) -> BrowserContext:
    # A Chrome that was SIGKILLed (container stop, OOM) leaves its Singleton files
    # behind, and the lock names the OLD container's hostname — so the next launch
    # believes another machine holds the profile and, being headful, sits on a
    # "profile in use" dialog until Playwright gives up. Measured: 180s per
    # search on that identity after every redeploy. We are the only launcher of
    # this profile, so a lock present before we start is always stale.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        with contextlib.suppress(OSError):
            (ident.profile_dir / name).unlink(missing_ok=True)
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(ident.profile_dir),
        # Playwright's default is 180s. A launch that has not produced a browser
        # in 30s is wedged; fail it so the search retries on another identity.
        timeout=30_000,
        channel=settings.chrome_channel,
        headless=settings.headless,
        args=_ARGS,
        proxy=_proxy_dict(ident),
        user_agent=ident.user_agent,
        viewport={"width": ident.viewport[0], "height": ident.viewport[1]},
        locale=ident.locale,
        timezone_id=ident.timezone,
        permissions=["geolocation"],
        ignore_default_args=["--enable-automation"],
    )


class BrowserManager:
    """A small, bounded pool of live Chrome contexts, keyed by identity.

    Playwright is started once. Contexts are created lazily, reused while hot,
    evicted LRU past `max_open_contexts`, and reaped once idle past
    `context_idle_ttl_s`. Geolocation is applied per request rather than baked in
    at launch, so one context can serve different stores.
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._pw: Playwright | None = None
        self._contexts: dict[str, BrowserContext] = {}
        # Tabs, not browsers, are how this gets concurrency. A new tab costs ~55ms
        # and tens of MB; a second browser costs ~1330ms and ~1GB. The semaphore
        # stops one burst opening a hundred tabs in a single Chrome.
        self._tab_sems: dict[str, asyncio.Semaphore] = {}
        # The search tab each identity keeps between requests (see search_tab),
        # and the in-flight "blank it" task from its last search (see park).
        self._search_tabs: dict[str, Page] = {}
        self._parking: dict[str, asyncio.Task] = {}
        self._last_used: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        """Close contexts nobody has used lately.

        Without this, memory only ever grows: every identity that has ever served
        one request holds a Chrome open forever. That is what OOM-killed the
        service.
        """
        while True:
            try:
                await asyncio.sleep(30)
                cutoff = time.monotonic() - self._s.context_idle_ttl_s
                for ident_id, last in list(self._last_used.items()):
                    if last < cutoff:
                        await self._close_one(ident_id, reason="idle")
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # never let the reaper die
                log.warning("context reaper error: %s", exc)

    async def _close_one(self, ident_id: str, reason: str) -> None:
        ctx = self._contexts.pop(ident_id, None)
        self._last_used.pop(ident_id, None)
        self._tab_sems.pop(ident_id, None)
        self._search_tabs.pop(ident_id, None)
        parking = self._parking.pop(ident_id, None)
        if parking is not None:
            parking.cancel()
        if ctx is not None:
            log.info("closing context %s (%s)", ident_id, reason)
            with contextlib.suppress(Exception):
                await ctx.close()

    async def _evict_if_needed(self, keep: str) -> None:
        """Hold at most `max_open_contexts` browsers. Evict least-recently-used."""
        while len(self._contexts) >= self._s.max_open_contexts:
            candidates = [k for k in self._contexts if k != keep]
            if not candidates:
                return
            oldest = min(candidates, key=lambda k: self._last_used.get(k, 0.0))
            await self._close_one(oldest, reason="lru evict")

    async def _warm_profile(self, ctx: BrowserContext, ident: Identity) -> None:
        """Give a brand-new profile a browsing history before it searches.

        Measured on a fresh Contabo box: of 12 queries across 3 new profiles, every
        block landed on a profile's FIRST query, and one profile that got through
        once then went 9-for-9. The datacenter IP was not the problem — a Chrome
        profile with no cookies, no consent state and no history was.

        So the first thing a new profile does is land on google.com like a person
        would, settle the consent dialog, and sit there a moment. Only then is it
        allowed to search.
        """
        marker = ident.profile_dir / ".warmed"
        first_run = not marker.exists()
        if not first_run and not ident.needs_warmup:
            return

        # `needs_warmup` is set when a search came back BLOCKED. Walking a
        # profile the engine just scored straight back onto the results page is
        # how a five-minute quarantine becomes a thirty-minute one, so it lands
        # on the home page and settles again first.
        log.info(
            "warming profile %s before search (%s)",
            ident.id,
            "new profile" if first_run else "after block",
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=30_000)
            for label in ("Accept all", "I agree", "Reject all"):
                try:
                    button = page.get_by_role("button", name=label)
                    if await button.count():
                        await button.first.click(timeout=2000)
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                        break
                except Exception:
                    continue
            await asyncio.sleep(random.uniform(1.5, 3.0))
            marker.write_text("warmed\n")
            # Only a warm-up that actually ran clears the flag; a failed one
            # leaves it set so the next attempt tries again rather than
            # silently dropping the protection.
            ident.needs_warmup = False
            log.info("profile %s warmed", ident.id)
        except Exception as exc:
            # A failed warm-up is not fatal: the search path has its own retry and
            # will simply pay the cold-profile risk once more.
            log.warning("warm-up failed for %s: %s", ident.id, exc)
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def _lock_for(self, ident_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(ident_id, asyncio.Lock())

    async def context(
        self, ident: Identity, geo: tuple[float, float] | None = None
    ) -> BrowserContext:
        await self.start()
        lock = await self._lock_for(ident.id)
        async with lock:
            ctx = self._contexts.get(ident.id)
            if ctx is not None:
                try:
                    # Cheap liveness probe: a crashed context raises here rather
                    # than failing later inside the caller's navigation.
                    _ = ctx.pages
                except Exception:
                    log.warning("context for %s died, relaunching", ident.id)
                    ctx = None

            if ctx is None:
                assert self._pw is not None
                await self._evict_if_needed(keep=ident.id)
                ctx = await _launch(self._pw, ident, self._s)
                self._contexts[ident.id] = ctx
                await self._warm_profile(ctx, ident)
            elif ident.needs_warmup:
                # The context outlives the quarantine, so a blocked identity
                # usually comes back to a browser that is still open. Re-warm on
                # the live context rather than only on a fresh launch.
                await self._warm_profile(ctx, ident)

            self._last_used[ident.id] = time.monotonic()

        if geo:
            with contextlib.suppress(Exception):
                await ctx.set_geolocation({"latitude": geo[0], "longitude": geo[1]})
        return ctx

    async def search_tab(self, ident: Identity) -> Page:
        """The one tab this identity searches from, created on first use.

        A search leases its identity exclusively, so the tab is never shared.
        Opening and closing a fresh tab per search measured 90–920ms; navigating
        one that already exists is ~20ms. The tab is parked on about:blank between
        searches, so it holds no page and runs no scripts while idle.
        """
        ctx = await self.context(ident)
        parking = self._parking.pop(ident.id, None)
        if parking is not None:
            with contextlib.suppress(Exception):
                await parking
        tab = self._search_tabs.get(ident.id)
        if tab is not None and not tab.is_closed():
            return tab
        # A persistent context opens with one blank tab; use it rather than
        # leaving it around and opening another.
        blank = next((p for p in ctx.pages if p.url == "about:blank" and not p.is_closed()), None)
        tab = blank or await ctx.new_page()
        self._search_tabs[ident.id] = tab
        return tab

    def park(self, ident: Identity, tab: Page) -> None:
        """Send the search tab to about:blank without making the request wait.

        The identity is cooling for 20s+ before it can search again, so the
        unload happens in the background; `search_tab` joins it before handing
        the tab out again.
        """

        async def blank() -> None:
            with contextlib.suppress(Exception):
                await tab.goto("about:blank", timeout=5_000)

        self._last_used[ident.id] = time.monotonic()
        self._parking[ident.id] = asyncio.create_task(blank())

    @contextlib.asynccontextmanager
    async def page(self, ident: Identity, geo: tuple[float, float] | None = None):
        """A tab on this identity's shared context.

        Deliberately NOT an exclusive lease. Several fetches may share one Chrome
        at the same time — that is the cheap axis of concurrency, and serialising
        them behind one identity was throttling work that needed no throttling.
        Per-target pacing is the domain governor's job, not the browser's.
        """
        ctx = await self.context(ident, geo=geo)
        sem = self._tab_sems.setdefault(
            ident.id, asyncio.Semaphore(self._s.max_tabs_per_context)
        )
        async with sem:
            tab = await ctx.new_page()
            try:
                yield tab
            finally:
                with contextlib.suppress(Exception):
                    await tab.close()

    def open_contexts(self) -> int:
        return len(self._contexts)

    def open_tabs(self) -> int:
        return sum(len(c.pages) for c in self._contexts.values())

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for ident_id, ctx in list(self._contexts.items()):
            with contextlib.suppress(Exception):
                await ctx.close()
            self._contexts.pop(ident_id, None)
            self._last_used.pop(ident_id, None)
            self._tab_sems.pop(ident_id, None)
            self._search_tabs.pop(ident_id, None)
            parking = self._parking.pop(ident_id, None)
            if parking is not None:
                parking.cancel()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None


@contextlib.asynccontextmanager
async def chrome_context(
    ident: Identity,
    settings: Settings,
    geo: tuple[float, float] | None = None,
) -> AsyncIterator[BrowserContext]:
    """One-shot context. Used by scripts and tests, NOT by the request path.

    The service uses BrowserManager so it does not pay ~1.3s of cold launch on
    every request.
    """
    async with async_playwright() as pw:
        context = await _launch(pw, ident, settings)
        if geo:
            with contextlib.suppress(Exception):
                await context.set_geolocation({"latitude": geo[0], "longitude": geo[1]})
        try:
            yield context
        finally:
            with contextlib.suppress(Exception):
                await context.close()
