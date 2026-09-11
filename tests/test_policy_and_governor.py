"""Domain resolution and the per-domain rate governor."""

from pathlib import Path

from app.scrape.governor import DomainGovernor
from app.scrape.policy import PolicyBook, registrable_domain

CONFIG = Path(__file__).resolve().parents[1] / "config" / "domains.yaml"
INDIA = Path(__file__).resolve().parents[1] / "config" / "domains.india.example.yaml"


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
        """The shipped config must be generic. Anyone can run this service against
        any corpus; baking one country's retailers into the default makes it a
        single-purpose tool wearing a general one's clothes."""
        assert set(self.book._domains) <= {"google.com"}
        assert self.book._image_only == set()

    def test_results_use_the_search_engine_order_by_default(self):
        # No domain carries a `rank`, so nothing overrides how the engine ranked
        # its own results.
        for domain in self.book._domains:
            assert self.book.for_url(f"https://{domain}/x").rank is None

    def test_unknown_domain_falls_back_to_polite_defaults(self):
        policy = self.book.for_url("https://some-random-food-blog.example/post")
        assert policy.rate_per_min == 6
        assert policy.respect_robots is True
        assert policy.needs_browser is False

    def test_unknown_domain_is_untrusted_not_untrustworthy(self):
        # "unknown" simply means nobody vouched for it -> tier d, never a refusal.
        policy = self.book.for_url("https://some-food-blog.example/post")
        assert policy.trust == "unknown"
        assert policy.tier == "d"


class TestMarketOverlay:
    """The India overlay is what a deployment's OPINIONS look like — loaded only if
    you point WS_DOMAINS_FILE at it."""

    def setup_method(self):
        self.book = PolicyBook.load(INDIA)

    def test_quick_commerce_carries_geo_and_residential(self):
        policy = self.book.for_url("https://www.swiggy.com/instamart/p/1")
        assert policy.needs_residential
        assert policy.geo is not None

    def test_swiggy_ttl_is_short_because_sold_out_strips_the_panel(self):
        policy = self.book.for_url("https://www.swiggy.com/instamart/p/1")
        assert policy.cache_ttl_s <= 3600

    def test_image_only_domains_are_flagged(self):
        assert self.book.for_url("https://blinkit.com/prn/x").image_only_panel
        assert not self.book.for_url("https://nutrabay.com/product/x").image_only_panel

    def test_trust_maps_to_tier(self):
        assert self.book.for_url("https://anuvaad.org.in/x").tier == "a"      # lab
        assert self.book.for_url("https://nutrabay.com/x").tier == "b"        # brand
        assert self.book.for_url("https://bigbasket.com/x").tier == "c"       # retailer
        assert self.book.for_url("https://fatsecret.co.in/x").tier == "d"     # aggregate

    def test_a_retailer_label_outranks_an_aggregate(self):
        # The whole point of trust: provenance decides, not markup convenience.
        assert self.book.for_url("https://bigbasket.com/x").tier < \
               self.book.for_url("https://fatsecret.co.in/x").tier


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


class TestTrustOverridesAdapterTier:
    """Tier is a judgement about a SOURCE, so the deployment's config wins over
    whatever an adapter defaulted to. An adapter only knows how it parsed a page,
    not whether anyone vouches for the site."""

    async def test_config_trust_beats_the_adapter_default(self):
        from app.domain.models import Basis, Page, Tier
        from app.extract.registry import Reader
        from app.scrape.policy import PolicyBook
        from app.settings import Settings

        html = (
            "<html><body><h1>Semolina Upma</h1>"
            "<h2>Nutritional Information</h2><table>"
            "<tr><th>NUTRIENT</th><th>Amount</th><th>Unit</th></tr>"
            "<tr><td>Energy</td><td>147.89</td><td>kcal</td></tr>"
            "<tr><td>Protein</td><td>3.3</td><td>g</td></tr>"
            "<tr><td>Carbohydrate</td><td>16.31</td><td>g</td></tr>"
            "</table></body></html>"
        )
        url = "https://anuvaad.org.in/nutrition-fact/upma/"

        book = PolicyBook({}, {"anuvaad.org.in": {"trust": "lab"}}, set())
        reader = Reader(Settings(), policies=book)
        panel = await reader.read(
            Page(url=url, html=html, status=200), book.for_url(url)
        )

        # The generic reader defaults to tier d; the config says this is laboratory
        # composition data, which outranks every retailer transcription.
        assert panel.tier is Tier.A
        assert any("domain trust" in n for n in panel.notes)

    async def test_unvouched_domain_keeps_the_cautious_default(self):
        from app.domain.models import Page, Tier
        from app.extract.registry import Reader
        from app.scrape.policy import PolicyBook
        from app.settings import Settings

        html = (
            "<html><body><h1>Something</h1><h2>Nutrition</h2><table>"
            "<tr><th>N</th><th>Amount</th><th>Unit</th></tr>"
            "<tr><td>Energy</td><td>100</td><td>kcal</td></tr>"
            "<tr><td>Protein</td><td>5</td><td>g</td></tr>"
            "</table></body></html>"
        )
        url = "https://random-blog.example/food"
        book = PolicyBook({}, {}, set())
        reader = Reader(Settings(), policies=book)
        panel = await reader.read(Page(url=url, html=html, status=200), book.for_url(url))
        assert panel.tier is Tier.D
