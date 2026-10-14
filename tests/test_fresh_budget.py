"""Box-wide fresh-search budget: no network, no browser."""

import tempfile
from pathlib import Path

import pytest

import app.search.google as google_mod
from app.concurrency import FreshSearchBudget
from app.domain.models import SearchResult
from app.search.browser import _ARGS
from app.search.google import Blocked, GoogleSearcher, SearchUnavailable
from app.search.identity import Identity, IdentityPool
from app.settings import Settings


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestFreshSearchBudget:
    def test_zero_means_no_ceiling(self):
        b = FreshSearchBudget(0)
        assert all(b.try_take() is None for _ in range(500))

    def test_refuses_past_the_ceiling_with_the_exact_wait(self):
        clock = Clock()
        b = FreshSearchBudget(3, clock=clock)
        assert [b.try_take() for _ in range(3)] == [None, None, None]
        clock.t += 10
        assert b.try_take() == pytest.approx(50.0)
        assert b.refused == 1

    def test_window_rolls(self):
        clock = Clock()
        b = FreshSearchBudget(2, clock=clock)
        b.try_take()
        clock.t += 30
        b.try_take()
        clock.t += 30.5  # the first stamp has aged out, the second has not
        assert b.try_take() is None
        assert b.try_take() is not None
        assert b.stats()["used_last_min"] == 2


def _searcher(per_min: int, n_ids: int = 4) -> GoogleSearcher:
    root = Path(tempfile.mkdtemp())
    ids = [Identity(id=f"s{i}", profile_dir=root / f"s{i}", proxy=None) for i in range(n_ids)]
    s = Settings(fresh_serp_per_min=per_min, cooldown_min_s=0, cooldown_max_s=0)
    return GoogleSearcher(IdentityPool(ids, s), s)


class TestSearcherSpendsTheBudget:
    async def test_over_budget_is_refused_without_touching_google(self, monkeypatch):
        calls = 0

        async def ok(ident, query, limit, settings, **kw):
            nonlocal calls
            calls += 1
            return [SearchResult(url="https://a.example", title="t", snippet="s", rank=1)]

        monkeypatch.setattr(google_mod, "_search_once", ok)
        sr = _searcher(per_min=2)
        await sr.search("a", raw_query="a", use_cache=False)
        await sr.search("b", raw_query="b", use_cache=False)
        with pytest.raises(SearchUnavailable) as exc:
            await sr.search("c", raw_query="c", use_cache=False)
        assert calls == 2, "the refused search must never reach the engine"
        assert exc.value.retry_after_s is not None and exc.value.retry_after_s > 0

    async def test_retries_spend_the_budget_too(self, monkeypatch):
        """A block retried on the next identity is another hit on the same IP."""
        calls = 0

        async def blocked(ident, query, limit, settings, **kw):
            nonlocal calls
            calls += 1
            raise Blocked(f"identity={ident.id}")

        monkeypatch.setattr(google_mod, "_search_once", blocked)
        sr = _searcher(per_min=2)
        with pytest.raises(SearchUnavailable) as exc:
            await sr.search("a", raw_query="a", use_cache=False)
        assert calls == 2, "3 retries allowed, but the budget stops the third"
        assert "budget" in str(exc.value)


def test_accept_ch_frame_is_in_the_only_disable_features_flag():
    flags = [a for a in _ARGS if a.startswith("--disable-features=")]
    assert len(flags) == 1, "Chrome honours only the last --disable-features"
    assert "AcceptCHFrame" in flags[0]
