"""Domain resolution and the per-domain rate governor."""

from pathlib import Path

from app.scrape.governor import DomainGovernor
from app.scrape.policy import PolicyBook, registrable_domain

CONFIG = Path(__file__).resolve().parents[1] / "config" / "domains.yaml"


class TestRegistrableDomain:
    def test_strips_scheme_path_and_www(self):
        assert registrable_domain("https://www.swiggy.com/instamart/p/123") == "swiggy.com"

    def test_handles_second_level_suffix(self):
        # fatsecret.co.in must not collapse to co.in.
        assert registrable_domain("https://www.fatsecret.co.in/calories-nutrition") == "fatsecret.co.in"

    def test_handles_deep_subdomains(self):
        assert registrable_domain("https://cdn.assets.zeptonow.com/x") == "zeptonow.com"

    def test_bare_host(self):
        assert registrable_domain("nutrabay.com") == "nutrabay.com"


class TestPolicyBook:
    def setup_method(self):
        self.book = PolicyBook.load(CONFIG)

    def test_google_is_the_slow_one(self):
        # The 30s pacing is a GOOGLE tax, not a global rate. This is the assertion
        # that keeps that correction from regressing.
        google = self.book.for_url("https://www.google.com/search?q=x")
        swiggy = self.book.for_url("https://www.swiggy.com/instamart/p/1")
        assert google.rate_per_min < swiggy.rate_per_min

    def test_default_config_holds_no_market_opinions(self):
        """The shipped config must stay generic — one entry, and only because
        search engines need slower pacing than content sites."""
        assert set(self.book._domains) <= {"google.com"}


    def test_unknown_domain_falls_back_to_polite_defaults(self):
        policy = self.book.for_url("https://some-random-food-blog.example/post")
        assert policy.rate_per_min == 6
        assert policy.respect_robots is True
        assert policy.needs_browser is False



class TestGovernor:
    async def test_first_call_is_immediate(self):
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        gov = DomainGovernor(clock=lambda: 0.0, sleep=fake_sleep)
        await gov.acquire("swiggy.com", 60)
        assert slept == []

    async def test_second_call_waits_for_refill(self):
        slept: list[float] = []
        now = {"t": 0.0}

        async def fake_sleep(s):
            slept.append(s)
            now["t"] += s

        gov = DomainGovernor(clock=lambda: now["t"], sleep=fake_sleep)
        await gov.acquire("swiggy.com", 60)     # 1/s
        await gov.acquire("swiggy.com", 60)
        assert slept and 0.9 <= slept[0] <= 1.1

    async def test_domains_are_independent(self):
        slept: list[float] = []
        now = {"t": 0.0}

        async def fake_sleep(s):
            slept.append(s)
            now["t"] += s

        gov = DomainGovernor(clock=lambda: now["t"], sleep=fake_sleep)
        await gov.acquire("swiggy.com", 60)
        await gov.acquire("zeptonow.com", 60)
        # A slow domain must not throttle a fast one.
        assert slept == []


class TestSearchResultSerialisation:
    """SearchResult is a slots dataclass, so vars() raises. The SERP cache round
    trip must survive that — it did not, and every cached write 500'd."""

    def test_round_trips_through_dict(self):
        import dataclasses
        from app.domain.models import SearchResult

        original = SearchResult(url="https://x.test/a", title="T", snippet="S", rank=1)
        as_dict = dataclasses.asdict(original)
        assert as_dict["url"] == "https://x.test/a"
        assert SearchResult(**as_dict) == original

    def test_vars_is_unavailable_on_slots_dataclass(self):
        from app.domain.models import SearchResult

        import pytest as _pytest
        with _pytest.raises(TypeError):
            vars(SearchResult(url="https://x.test/a"))


class TestStaticFetchHeaders:
    """Regression: a hardcoded Accept-Encoding broke every static fetch.

    Advertising brotli that httpx cannot decode makes servers reply
    `content-encoding: br`, and the response body decodes to binary garbage. The
    readability check then failed for every page and the scraper escalated to a
    browser 100% of the time — roughly 2.8s of avoidable latency per page.
    """

    def test_does_not_hardcode_accept_encoding(self):
        from app.scrape.fetchers import _HEADERS

        assert not any(k.lower() == "accept-encoding" for k in _HEADERS), (
            "let httpx negotiate encoding — it advertises only codecs it can decode"
        )

    def test_still_sends_a_browser_like_identity(self):
        from app.scrape.fetchers import _HEADERS, _headers

        assert "Chrome/" in _HEADERS["User-Agent"]
        # Accept-Language is a deployment choice, not a constant baked into the
        # engine — it comes from settings at request time.
        assert _headers("hi-IN,hi;q=0.9")["Accept-Language"] == "hi-IN,hi;q=0.9"
        assert _headers(None)["Accept-Language"].startswith("en-US")


