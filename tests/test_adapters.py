"""Adapters against saved pages. Pure functions, no network — this is the whole
reason adding a new site is one file plus one fixture."""

from pathlib import Path

from app.domain.models import Basis, Tier
from app.extract.adapters import NutrabayAdapter, ShopifyJsonLdAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestTwoColumnPanel:
    """The case that silently corrupts a corpus: a page printing BOTH a per-serving
    and a per-100g column. We must take the per-100g column verbatim and do no
    arithmetic at all."""

    def setup_method(self):
        self.html = load("nutrabay_two_column.html")
        self.panel = NutrabayAdapter().parse(
            self.html, "", "https://nutrabay.com/product/pure-whey"
        )

    def test_finds_a_panel(self):
        assert self.panel is not None

    def test_takes_the_per_100g_column_verbatim(self):
        # 400, not 132 (per-serving) and not 132*100/33 (arithmetic we must avoid).
        assert self.panel.per_100g["cal"] == 400.0
        assert self.panel.per_100g["prot"] == 75.0
        assert self.panel.per_100g["carb"] == 7.0

    def test_basis_is_read_not_assumed(self):
        assert self.panel.per_basis is Basis.PER_100G
        assert not any("assumed" in n for n in self.panel.notes)

    def test_drops_added_sugar_but_keeps_total_sugar(self):
        assert self.panel.per_100g["sugar"] == 3.0

    def test_drops_mufa_which_has_no_schema_field(self):
        # MUFA 1 g must not have leaked into any field.
        assert 1.0 not in [
            v for k, v in self.panel.per_100g.items() if k not in ("sat_fat",)
        ] or self.panel.per_100g.get("fat") == 4.0

    def test_printed_zero_is_kept_as_zero(self):
        assert self.panel.per_100g["trans_fat"] == 0.0

    def test_unprinted_nutrients_are_none(self):
        assert self.panel.per_100g["vit_b12"] is None
        assert self.panel.per_100g["k"] is None

    def test_micros_are_read(self):
        assert self.panel.per_100g["na"] == 180.0
        assert self.panel.per_100g["ca"] == 400.0

    def test_carries_provenance(self):
        assert self.panel.source_domain == "nutrabay.com"
        assert self.panel.tier is Tier.B
        assert self.panel.extractor == "adapter:nutrabay.com"


class TestJsonLdPerServing:
    """A per-serving JSON-LD panel must be normalised through serving_g, never
    assumed to be per-100g."""

    def setup_method(self):
        self.panel = ShopifyJsonLdAdapter().parse(
            load("brand_jsonld_per_serving.html"), "", "https://yogabar.in/products/bar"
        )

    def test_finds_a_panel(self):
        assert self.panel is not None

    def test_normalises_from_the_declared_serving_size(self):
        # 230 kcal per 60 g bar -> 383.3 per 100 g.
        assert self.panel.serving_g == 60.0
        assert self.panel.per_100g["cal"] == 383.3
        assert self.panel.per_100g["prot"] == 33.3

    def test_records_that_it_normalised(self):
        assert any("per-serving" in n for n in self.panel.notes)

    def test_reads_brand_from_structured_data(self):
        assert self.panel.brand == "Yogabar"

    def test_is_untrusted_without_a_policy_saying_otherwise(self):
        # Tidy JSON-LD proves nothing about provenance. Absent a `trust:` entry the
        # domain is unknown, which is tier d — a content farm must not outrank a
        # retailer's transcribed label just because its markup parses cleanly.
        assert self.panel.tier is Tier.D
        assert any("provenance unverified" in n for n in self.panel.notes)

    def test_earns_brand_tier_when_the_policy_vouches_for_the_domain(self):
        from app.scrape.policy import PolicyBook

        book = PolicyBook({}, {"yogabar.in": {"trust": "brand"}}, set())
        panel = ShopifyJsonLdAdapter(book).parse(
            load("brand_jsonld_per_serving.html"), "", "https://yogabar.in/products/bar"
        )
        assert panel.tier is Tier.B


class TestComboRejection:
    def test_variety_pack_returns_no_panel(self):
        html = load("nutrabay_two_column.html").replace(
            "Nutrabay Pure Whey Protein Concentrate - 1 kg",
            "Nutrabay Protein Variety Pack",
        )
        panel = NutrabayAdapter().parse(html, "", "https://nutrabay.com/product/x")
        assert panel is None
