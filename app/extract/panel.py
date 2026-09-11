"""Panel construction and the honesty rules, enforced in code.

These are not conventions. Every one of them exists because the current pipeline
was burnt by its absence:
  * absent nutrient must be None, never 0 (a 0 silently becomes a real claim);
  * per_basis is read from the page, never assumed;
  * combos/variety packs have no honest single per-100g -> rejected, not averaged;
  * never borrow a sibling SKU's values.
"""

from __future__ import annotations

import re

from app.domain.models import PER100_KEYS, Basis, Panel, Tier

_NUM_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)")

# "Added Sugar" is dropped entirely: it is a subset of total sugar and adding it to
# `sugar` double-counts. Everything below has no schema field and is dropped too.
DROP_LABELS = (
    "added sugar", "added sugars",
    # "Energy from Fat - 28 kcal" (BigBasket) contains BOTH "energy" and "fat".
    # Longest-match maps it to energy and overwrites the real 62 kcal with 28.
    "energy from fat", "calories from fat",
    "mufa", "pufa", "monounsaturated", "polyunsaturated",
    # "Poly Unsaturated Fatty Acids" (spaced) slipped past the joined spellings —
    # and worse, "unSATURATED FATty" CONTAINS "saturated fat", so it was being read
    # as saturated fat: 4942 g per 100 g. Matching "unsaturated" catches mono and
    # poly in every spelling while leaving plain "Saturated Fatty Acids" alone.
    "unsaturated",
    # Composition tables often list per-100g AND per-serving rows in ONE table.
    # Without this the serving rows overwrite the per-100g ones and every value
    # silently shifts by the serving ratio.
    "per serving", "per serve",
    "polyol", "polyols", "net carb", "net carbs",
)

_COMBO_RE = re.compile(
    r"\b(combo|assorted|variety|sampler|all[- ]in[- ]one|gift\s*(pack|box)|"
    r"\w+\s*\+\s*\w+|\w+\s+&\s+\w+\s+pack)\b",
    re.I,
)
_MULTIPACK_RE = re.compile(r"\b(pack\s*of\s*\d+|\d+\s*[x×]\s*\d+\s*(g|ml)|combo\s*of\s*\d+)\b", re.I)


def parse_number(text: str | float | int | None) -> float | None:
    """Pull the first number out of a label cell. No number -> None, never 0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _NUM_RE.search(str(text).replace(",", "."))
    return float(match.group(1)) if match else None


# Keys the schema denominates in GRAMS. Composition tables print several of these
# in mg ("Saturated Fatty Acids | 912.75 | mg"); storing that number as grams claims
# 912 g of fat in 100 g of food.
GRAM_KEYS = frozenset({"prot", "carb", "fat", "sat_fat", "trans_fat", "fib", "sugar"})

_UNIT_TO_G = {"g": 1.0, "gm": 1.0, "mg": 1e-3, "mcg": 1e-6, "ug": 1e-6, "µg": 1e-6}


def to_grams(key: str, value: float, unit: str | None) -> float:
    """Normalise a gram-denominated nutrient printed in mg/µg. Others pass through."""
    if key not in GRAM_KEYS or not unit:
        return value
    factor = _UNIT_TO_G.get(unit.strip().lower())
    return value * factor if factor else value


# Nothing edible carries more than 100 g of a single macro per 100 g, and no food
# exceeds ~900 kcal/100 g (pure fat is 884). A value past these is not a reading.
_MAX_PER_100G = 100.0
_MAX_CAL_PER_100G = 900.0


# A few mg-denominated nutrients have unambiguous physical ceilings. Cholesterol
# earns one from evidence: anuvaad.org.in duplicated its PUFA figure into the
# cholesterol row and printed 4651 mg/100 g, where the richest real food (egg yolk)
# is ~1085. Reading a source faithfully does not mean forwarding its arithmetic
# errors into a nutrition app.
_MAX_MG_PER_100G = {"chol": 3000.0, "na": 40000.0}


def implausible(key: str, value: float) -> bool:
    if key == "cal":
        return value > _MAX_CAL_PER_100G
    if key in GRAM_KEYS:
        return value > _MAX_PER_100G
    ceiling = _MAX_MG_PER_100G.get(key)
    return ceiling is not None and value > ceiling


def empty_per100() -> dict[str, float | None]:
    return {k: None for k in PER100_KEYS}


def is_combo(product_name: str) -> bool:
    """Combo / assorted / variety packs carry a pack-total or mixed panel.

    A same-item multipack ("pack of 6") is NOT a combo — it shares the single's
    per-100g — so it is excluded here and deduped to the single SKU downstream.
    """
    if _MULTIPACK_RE.search(product_name) and not _COMBO_RE.search(product_name):
        return False
    return bool(_COMBO_RE.search(product_name))


def should_drop_label(label: str) -> bool:
    low = label.strip().lower()
    return any(bad in low for bad in DROP_LABELS)


def normalise(
    values: dict[str, float | None],
    basis: Basis,
    serving_g: float | None,
) -> tuple[dict[str, float | None], Basis, list[str]]:
    """Bring a panel onto per-100g, honestly.

    per-100g  -> verbatim, no arithmetic.
    per-100ml -> treated as per-100g (water-density approximation), and we say so.
    per-serve -> value * 100 / serving_g, and only if serving_g is actually known.
    per-pack  -> refused; the caller turns this into a rejection.
    """
    notes: list[str] = []

    if basis is Basis.PER_100G:
        return values, basis, notes

    if basis is Basis.PER_100ML:
        notes.append("per-100ml treated as per-100g (water-density approximation)")
        return values, basis, notes

    if basis is Basis.PER_SERVING:
        if not serving_g or serving_g <= 0:
            notes.append("per-serving panel with no serving weight — cannot normalise")
            return empty_per100(), basis, notes
        factor = 100.0 / serving_g
        scaled = {k: (v * factor if v is not None else None) for k, v in values.items()}
        notes.append(f"normalised from per-serving ({serving_g:g} g)")
        return scaled, Basis.PER_100G, notes

    notes.append("per-pack basis — no honest single-item per-100g")
    return empty_per100(), basis, notes


def round_panel(values: dict[str, float | None]) -> dict[str, float | None]:
    return {k: (round(v, 1) if v is not None else None) for k, v in values.items()}


# A panel carrying one lone number is not a reading — it is a number that happened
# to sit next to a word we recognised. The live run produced exactly that: `cal:
# 728` for BOTH "Amul Masti Dahi" and "Milky Mist Skyr", on a page that contained
# neither the value nor a nutrition table. Two different foods cannot share a
# calorie figure; the giveaway was that nothing else came with it.
MIN_VIABLE_MACROS = 2


def is_viable(values: dict[str, float | None]) -> bool:
    """Reject a panel too thin to be a real reading.

    Requires energy plus at least one macro. A nutrition panel that prints calories
    always prints protein/carbs/fat beside them, so energy alone means we scraped
    page furniture, not a label.
    """
    present = {k for k, v in values.items() if v is not None}
    if "cal" not in present:
        return False
    macros = present & {"prot", "carb", "fat"}
    return len(present) >= MIN_VIABLE_MACROS and bool(macros)


def build_panel(
    *,
    product: str,
    values: dict[str, float | None],
    basis: Basis,
    serving_g: float | None,
    url: str,
    domain: str,
    source_type: str,
    tier: Tier,
    extractor: str,
    brand: str | None = None,
    fetched_at: float = 0.0,
    notes: list[str] | None = None,
) -> Panel:
    merged = empty_per100()
    for key, val in values.items():
        if key in merged and val is not None:
            merged[key] = float(val)

    normalised, final_basis, norm_notes = normalise(merged, basis, serving_g)

    # Final guard. A physically impossible number is a parsing failure wearing a
    # reading's clothes — drop it and say so rather than store it.
    dropped: list[str] = []
    for key, val in list(normalised.items()):
        if val is not None and implausible(key, val):
            normalised[key] = None
            dropped.append(f"{key}={val:g}")
    if dropped:
        norm_notes.append("dropped implausible value(s): " + ", ".join(dropped))

    filled = sum(1 for v in normalised.values() if v is not None)

    return Panel(
        product=product.strip(),
        brand=brand,
        per_100g=round_panel(normalised),
        serving_g=serving_g,
        per_basis=final_basis,
        source_url=url,
        source_domain=domain,
        source_type=source_type,
        tier=tier,
        # Confidence tracks how much of the schema the page actually printed.
        # A 4-macro page is honest but thin; it should not outrank a full panel.
        confidence=round(min(0.95, 0.35 + 0.6 * (filled / len(PER100_KEYS))), 3),
        fetched_at=fetched_at,
        extractor=extractor,
        notes=(notes or []) + norm_notes,
    )
