"""Adapter contract.

An adapter is a PURE function over (html, markdown, url). No network, no clock, no
I/O — which is what makes every one of them unit-testable against a saved fixture
page, and why adding a site is one file plus one fixture.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.models import Panel


@runtime_checkable
class Adapter(Protocol):
    domains: tuple[str, ...]
    source_type: str

    def can_parse(self, html: str, url: str) -> bool: ...

    def parse(self, html: str, markdown: str, url: str) -> Panel | None: ...


# Label text on Indian nutrition panels -> our schema key. Longest match wins, so
# "saturated fat" must be tested before "fat".
# Real pages abbreviate. FatSecret prints "Cals / Prot / Carbs"; nothing matched
# a map that only knew "calories" and "protein".
LABEL_MAP: tuple[tuple[str, str], ...] = (
    ("energy", "cal"), ("calories", "cal"), ("kcal", "cal"), ("cals", "cal"),
    ("saturated fat", "sat_fat"), ("saturates", "sat_fat"),
    ("trans fat", "trans_fat"),
    ("cholesterol", "chol"),
    ("dietary fibre", "fib"), ("dietary fiber", "fib"), ("fibre", "fib"), ("fiber", "fib"),
    ("total sugar", "sugar"), ("sugars", "sugar"), ("sugar", "sugar"),
    ("protein", "prot"), ("prot", "prot"),
    ("total carbohydrate", "carb"), ("carbohydrate", "carb"), ("carbs", "carb"),
    ("carbohydrates", "carb"),
    ("total fat", "fat"), ("fat", "fat"),
    ("sodium", "na"), ("salt", "na"),
    ("calcium", "ca"), ("iron", "fe"), ("magnesium", "mg"), ("phosphorus", "p"),
    ("potassium", "k"), ("zinc", "zn"), ("copper", "cu"), ("manganese", "mn"),
    ("selenium", "se"), ("folate", "folate"), ("folic acid", "folate"),
    ("omega 3", "omg_3"), ("omega-3", "omg_3"),
    ("vitamin a", "vit_a"), ("thiamine", "vit_b1"), ("vitamin b1", "vit_b1"),
    ("riboflavin", "vit_b2"), ("vitamin b2", "vit_b2"),
    ("niacin", "vit_b3"), ("vitamin b3", "vit_b3"),
    ("vitamin b6", "vit_b6"), ("vitamin b12", "vit_b12"),
    ("vitamin c", "vit_c"), ("ascorbic acid", "vit_c"),
    ("vitamin d", "vit_d"), ("vitamin e", "vit_e"), ("vitamin k", "vit_k"),
)


def map_label(label: str) -> str | None:
    low = label.strip().lower()
    for needle, key in sorted(LABEL_MAP, key=lambda kv: -len(kv[0])):
        if needle in low:
            return key
    return None
