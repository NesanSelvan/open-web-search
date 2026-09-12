"""Re-warming a profile Google has just caught.

Warm-up existed but ran once per profile, ever: a `.warmed` marker written on
first launch. So an identity that got blocked came back from quarantine and went
straight to /search on a profile the engine had just scored — the exact state
warm-up exists to avoid. No browser or network here; the page is a stub.
"""

import tempfile
from pathlib import Path

import pytest

from app.search.browser import BrowserManager
from app.search.identity import Identity
from app.settings import Settings


class FakeLocator:
    async def count(self) -> int:
        return 0


class FakePage:
    def __init__(self):
        self.visited: list[str] = []
        self.closed = False

    async def goto(self, url, **kw):
        self.visited.append(url)

    def get_by_role(self, *a, **kw):
        return FakeLocator()

    async def wait_for_load_state(self, *a, **kw):
        pass

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.pages_opened: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages_opened.append(page)
        return page


@pytest.fixture
def ident() -> Identity:
    root = Path(tempfile.mkdtemp())
    (root / "p0").mkdir()
    return Identity(id="p0", profile_dir=root / "p0", proxy=None)


@pytest.fixture(autouse=True)
def no_dwell(monkeypatch):
    # The warm-up sits on google.com for 1.5-3s like a person would; a test
    # should not.
    monkeypatch.setattr("app.search.browser.random.uniform", lambda a, b: 0.0)


def manager() -> BrowserManager:
    return BrowserManager(Settings(api_key="k"))


async def test_fresh_profile_is_warmed(ident):
    ctx = FakeContext()
    await manager()._warm_profile(ctx, ident)

    assert ctx.pages_opened[0].visited == ["https://www.google.com/"]
    assert (ident.profile_dir / ".warmed").exists()


async def test_warmed_profile_is_not_warmed_again(ident):
    (ident.profile_dir / ".warmed").write_text("warmed\n")
    ctx = FakeContext()
    await manager()._warm_profile(ctx, ident)

    assert ctx.pages_opened == []


async def test_blocked_profile_is_re_warmed_despite_the_marker(ident):
    (ident.profile_dir / ".warmed").write_text("warmed\n")
    ident.needs_warmup = True
    ctx = FakeContext()

    await manager()._warm_profile(ctx, ident)

    assert ctx.pages_opened[0].visited == ["https://www.google.com/"]
    assert ident.needs_warmup is False


async def test_failed_re_warm_keeps_the_flag_for_the_next_attempt(ident, monkeypatch):
    """A warm-up that could not run must not be recorded as done, or the
    identity quietly loses the protection for good."""
    (ident.profile_dir / ".warmed").write_text("warmed\n")
    ident.needs_warmup = True

    class BrokenContext(FakeContext):
        async def new_page(self):
            page = await super().new_page()

            async def boom(url, **kw):
                raise RuntimeError("net::ERR_CONNECTION_REFUSED")

            page.goto = boom
            return page

    await manager()._warm_profile(BrokenContext(), ident)
    assert ident.needs_warmup is True
