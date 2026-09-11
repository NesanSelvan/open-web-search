"""Honesty rules. Each test corresponds to a rule that exists because breaking it
silently corrupted the corpus before."""

from app.domain.models import PER100_KEYS, Basis, Tier
from app.extract.panel import (
    build_panel,
    is_combo,
    normalise,
    parse_number,
    should_drop_label,
)


class TestParseNumber:
    def test_absent_is_none_not_zero(self):
        # The whole point: a missing nutrient must never become a claim of zero.
        assert parse_number("") is None
        assert parse_number(None) is None
        assert parse_number("not available") is None

    def test_printed_zero_is_zero(self):
        assert parse_number("0.0 g") == 0.0

    def test_comma_decimal(self):
        assert parse_number("3,5 g") == 3.5

    def test_pulls_first_number_from_cell(self):
        assert parse_number("12.4 g (24% RDA)") == 12.4


class TestDropLabels:
    def test_drops_added_sugar(self):
        # Added sugar is a SUBSET of total sugar; adding it double-counts.
        assert should_drop_label("Added Sugars")

    def test_drops_fields_with_no_schema_home(self):
        for label in ("MUFA", "Polyunsaturated Fat", "Polyols", "Net Carbs"):
            assert should_drop_label(label), label

    def test_keeps_total_sugar(self):
        assert not should_drop_label("Total Sugar")


class TestCombo:
    def test_variety_pack_is_combo(self):
        assert is_combo("Assorted Nuts Variety Pack")
        assert is_combo("Protein Bar Combo")
        assert is_combo("Chips & Dip Gift Box")

    def test_same_item_multipack_is_not_combo(self):
        # A "pack of 6" of ONE bar shares the single's per-100g — honest to keep.
        assert not is_combo("Yogabar Protein Bar Pack of 6")
        assert not is_combo("Amul Butter 100g x 4")

    def test_plain_product_is_not_combo(self):
        assert not is_combo("Amul Masti Dahi 400g")


class TestNormalise:
    def test_per_100g_is_verbatim(self):
        values = {"cal": 120.0, "prot": 24.0}
        out, basis, notes = normalise(values, Basis.PER_100G, None)
        assert out == values
        assert basis is Basis.PER_100G
        assert notes == []

    def test_per_serving_scales_by_serving_weight(self):
        out, basis, notes = normalise({"cal": 60.0}, Basis.PER_SERVING, 50.0)
        assert out["cal"] == 120.0
        assert basis is Basis.PER_100G
        assert "per-serving" in notes[0]

    def test_per_serving_without_weight_yields_nothing(self):
        # Guessing the serving weight is exactly how a panel gets silently wrong.
        out, _, notes = normalise({"cal": 60.0}, Basis.PER_SERVING, None)
        assert out["cal"] is None
        assert "cannot normalise" in notes[0]

    def test_per_100ml_is_noted_not_converted(self):
        out, basis, notes = normalise({"cal": 60.0}, Basis.PER_100ML, None)
        assert out["cal"] == 60.0
        assert basis is Basis.PER_100ML
        assert "water-density" in notes[0]

    def test_per_pack_is_refused(self):
        out, _, notes = normalise({"cal": 600.0}, Basis.PER_PACK, None)
        assert all(v is None for v in out.values())
        assert "no honest single-item" in notes[0]


class TestBuildPanel:
    def _panel(self, **kw):
        defaults = dict(
            product="Amul Masti Dahi",
            values={"cal": 60.0, "prot": 3.1},
            basis=Basis.PER_100G,
            serving_g=None,
            url="https://www.zeptonow.com/pn/x",
            domain="zeptonow.com",
            source_type="zepto",
            tier=Tier.C,
            extractor="adapter:zeptonow.com",
        )
        return build_panel(**{**defaults, **kw})

    def test_unlisted_nutrients_are_none(self):
        panel = self._panel()
        assert panel.per_100g["cal"] == 60.0
        assert panel.per_100g["vit_b12"] is None
        assert set(panel.per_100g) == set(PER100_KEYS)

    def test_values_rounded_to_one_decimal(self):
        panel = self._panel(values={"cal": 60.449, "prot": 3.16})
        assert panel.per_100g["cal"] == 60.4
        assert panel.per_100g["prot"] == 3.2

    def test_confidence_rises_with_completeness(self):
        thin = self._panel(values={"cal": 60.0})
        full = self._panel(values={k: 1.0 for k in PER100_KEYS})
        assert full.confidence > thin.confidence

    def test_serialises_all_31_keys_in_order(self):
        out = self._panel().to_dict()
        assert list(out["per_100g"].keys()) == list(PER100_KEYS)


class TestViability:
    """A panel with one lone number is page furniture, not a reading.

    The live run returned `cal: 728` for BOTH "Amul Masti Dahi" and "Milky Mist
    Skyr" — two different foods cannot share a calorie figure, and the page had no
    nutrition table at all. The tell was that nothing came with it.
    """

    def test_lone_calorie_value_is_not_viable(self):
        from app.extract.panel import is_viable
        assert not is_viable({"cal": 728.0})

    def test_calories_with_a_macro_is_viable(self):
        from app.extract.panel import is_viable
        assert is_viable({"cal": 101.0, "prot": 11.0})

    def test_macros_without_energy_is_not_viable(self):
        from app.extract.panel import is_viable
        assert not is_viable({"prot": 11.0, "carb": 9.5})

    def test_energy_plus_only_a_micro_is_not_viable(self):
        from app.extract.panel import is_viable
        assert not is_viable({"cal": 101.0, "ca": 400.0})

    def test_adapter_returns_none_for_thin_panel(self):
        from app.extract.adapters import NutrabayAdapter
        html = "<html><head><meta property='og:title' content='X'></head><body>" \
               "<h2>Nutritional Information</h2><table><tr><th>N</th><th>Per 100 g</th></tr>" \
               "<tr><td>Energy</td><td>728 kcal</td></tr></table></body></html>"
        assert NutrabayAdapter().parse(html, "", "https://nutrabay.com/product/x") is None
