"""Adapters against REAL captured pages.

The fixtures in `tests/fixtures/live/` are unedited HTML pulled from the live
service on 2026-09-10. This file exists because the previous adapters were written
against markup that was *assumed*, and every one of them returned nothing — or
worse, returned `cal: 728` for two different foods off a page with no nutrition
table at all.

Recapture with:
    curl -X POST $HOST/scrape -d '{"url":"...","formats":["html"],"force_fresh":true}'
"""

from pathlib import Path

import pytest

from app.domain.models import Basis, Tier
from app.extract.adapters import BigBasketAdapter, FatSecretAdapter, ShopifyJsonLdAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "live"


def load(name: str) -> str:
    path = FIXTURES / f"{name}.html"
    if not path.exists():
        pytest.skip(f"fixture {path} not captured")
    return path.read_text()


class TestFatSecret:
    """Panel lives in div.factPanel with labels FUSED to values ("Cals211"), using
    abbreviations a "calories"/"protein" label map never matched."""

    URL = "https://www.fatsecret.co.in/calories-nutrition/generic/paneer-butter-masala"

    def setup_method(self):
        self.html = load("fatsecret")
        self.panel = FatSecretAdapter().parse(self.html, "", self.URL)

    def test_finds_a_panel(self):
        assert self.panel is not None, "div.factPanel present but nothing parsed"

    def test_reads_the_product_name(self):
        assert self.panel.product == "Paneer Butter Masala"

    def test_reads_the_four_macros_the_page_prints(self):
        v = self.panel.per_100g
        assert v["cal"] == 211.0
        assert v["prot"] == 7.7
        assert v["carb"] == 8.8
        assert v["fat"] == 16.7

    def test_basis_is_read_from_the_page_prose_not_assumed(self):
        # "There are 211 calories in 100 grams of Paneer Butter Masala."
        assert self.panel.per_basis is Basis.PER_100G
        assert not any("assumed" in n for n in self.panel.notes)

    def test_nutrients_the_page_does_not_print_stay_null(self):
        v = self.panel.per_100g
        assert v["fib"] is None
        assert v["na"] is None
        assert v["vit_b12"] is None

    def test_stays_tier_d_as_a_crowd_aggregate(self):
        assert self.panel.tier is Tier.D


class TestBigBasket:
    """Panel is one run of prose: `Amount per 100 g)Energy - 62 kcalEnergy from
    Fat - 28 kcal...` with no separators between entries."""

    URL = "https://www.bigbasket.com/pd/40006925/amul-masti-dahi-400-g-cup/"

    def setup_method(self):
        self.html = load("bigbasket")
        self.panel = BigBasketAdapter().parse(self.html, "", self.URL)

    def test_finds_a_panel(self):
        assert self.panel is not None

    def test_energy_is_the_real_value_not_energy_from_fat(self):
        # THE trap on this page. "Energy from Fat - 28 kcal" contains both "energy"
        # and "fat"; a longest-match label map maps it to energy and silently
        # overwrites the real 62 with 28.
        assert self.panel.per_100g["cal"] == 62.0

    def test_reads_macros(self):
        v = self.panel.per_100g
        assert v["prot"] == 4.1
        assert v["carb"] == 4.4
        assert v["fat"] == 3.1
        assert v["sat_fat"] == 1.9

    def test_reads_micros_too(self):
        v = self.panel.per_100g
        assert v["ca"] == 183.0
        assert v["p"] == 158.0
        assert v["na"] == 61.0
        assert v["vit_b1"] == 51.5

    def test_added_sugar_never_lands_in_sugar(self):
        # The page prints "Added Sugar - 0 g" and no total sugar. Keeping added
        # sugar would double-count against a real total later.
        assert self.panel.per_100g["sugar"] is None

    def test_earns_retailer_label_tier(self):
        assert self.panel.tier is Tier.C

    def test_beats_fatsecret_on_the_same_food(self):
        """A transcribed pack label must outrank a crowd aggregate."""
        from app.orchestrator import _better

        fs = FatSecretAdapter().parse(load("fatsecret"), "", TestFatSecret.URL)
        assert _better(self.panel, fs) is self.panel
        assert _better(fs, self.panel) is self.panel


class TestBareJsonLd:
    """eatthismuch emits a bare NutritionInformation — no Product wrapper, no
    og:title — and a per-SERVING panel that must be normalised, not assumed."""

    URL = "https://www.eatthismuch.com/calories/x-2000234"

    def setup_method(self):
        self.html = load("eatthismuch")
        self.panel = ShopifyJsonLdAdapter().parse(self.html, "", self.URL)

    def test_finds_a_panel_without_a_product_node(self):
        assert self.panel is not None
        assert self.panel.product == "Cultivated Blueberries"

    def test_normalises_per_serving_to_per_100g(self):
        # servingSize "140 grams", 70 kcal per serving -> 50 per 100g.
        assert self.panel.serving_g == 140.0
        assert self.panel.per_100g["cal"] == 50.0
        assert self.panel.per_basis is Basis.PER_100G

    def test_says_that_it_normalised(self):
        assert any("per-serving" in n for n in self.panel.notes)

    def test_printed_zero_survives_as_zero(self):
        # transFatContent: 0 is a real printed zero, not a missing value.
        assert self.panel.per_100g["trans_fat"] == 0.0


class TestGenericTableFallback:
    """anuvaad.org.in — an Indian composition table on a domain we had never seen.

    Before the generic reader became a fallback this returned `no_panel`, purely
    because nobody had hardcoded the domain. It then surfaced three separate ways
    to silently corrupt a row, all of which are asserted here.
    """

    URL = "https://www.anuvaad.org.in/nutrition-fact/semolina-upma-suji-rava-upma/"

    def setup_method(self):
        from app.extract.adapters import GenericTableAdapter

        self.panel = GenericTableAdapter().parse(load("upma_anuvaad"), "", self.URL)

    def test_reads_an_unknown_domain(self):
        assert self.panel is not None
        assert self.panel.product == "Semolina upma (Suji/Rava upma)"

    def test_per_serving_rows_do_not_overwrite_per_100g(self):
        # The SAME table lists "Energy 147.89 kcal" and later "Energy Per Serving
        # 157.14 kcal". Last-write-wins shifted every macro by the serving ratio.
        assert self.panel.per_100g["cal"] == 147.9
        assert self.panel.per_100g["prot"] == 3.3
        assert self.panel.per_100g["carb"] == 16.3
        assert self.panel.per_100g["fat"] == 7.5

    def test_unsaturated_is_not_read_as_saturated(self):
        # "Poly Unsaturated Fatty Acids" CONTAINS the substring "saturated fat",
        # so it was landing in sat_fat as 4942 g per 100 g.
        assert self.panel.per_100g["sat_fat"] == 0.9

    def test_milligrams_are_converted_for_gram_denominated_keys(self):
        # Printed as "912.75 mg"; the schema wants grams.
        assert self.panel.per_100g["sat_fat"] == 0.9

    def test_kilojoule_energy_row_is_ignored(self):
        # The table prints Energy twice: 614.18 kJ then 147.89 kcal.
        assert self.panel.per_100g["cal"] == 147.9

    def test_implausible_source_value_is_dropped(self):
        # anuvaad duplicated its PUFA number into cholesterol: 4651 mg/100 g, where
        # egg yolk is ~1085. Reading a source faithfully is not forwarding its
        # arithmetic errors.
        assert self.panel.per_100g["chol"] is None
        assert any("implausible" in n for n in self.panel.notes)

    def test_stays_tier_d_on_an_unverified_domain(self):
        from app.domain.models import Tier

        assert self.panel.tier is Tier.D
