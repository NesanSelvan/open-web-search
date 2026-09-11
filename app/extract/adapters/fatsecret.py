"""FatSecret India — the domain that shows up in almost every Indian food search.

Written against a captured page, not assumed markup. The panel is NOT a table: it
lives in `div.factPanel`, and each label sits in its own node immediately before its
value, so the rendered text reads `Cals211Fat16.66gCarbs8.83gProt7.68g` when you
join it naively. Reading it needs a separator, and the labels are abbreviations
(`Cals`, `Prot`, `Carbs`) that a "protein"/"carbohydrate" label map never matches.

The basis is stated in prose — "There are 211 calories in 100 grams of X" — so we
read it from there rather than assuming per-100g, and the serving table underneath
gives the gram weight when the page is per-serving.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from app.domain.models import Basis, Panel, Tier
from app.extract.adapters.base import map_label
from app.extract.panel import build_panel, is_combo, is_viable, parse_number, should_drop_label
from app.scrape.policy import registrable_domain

# "There are 211 calories in 100 grams of Paneer Butter Masala."
# "There are 423 calories in 1 serving (200 g) of ..."
_BASIS_RE = re.compile(
    r"there\s+are\s+[\d.,]+\s+calories\s+in\s+([\d.]+)\s*(grams?|g|ml|millilitres?)\b", re.I
)
_SERVING_RE = re.compile(r"([\d.]+)\s*g\s*\)", re.I)


class FatSecretAdapter:
    domains = ("fatsecret.co.in", "fatsecret.com")
    source_type = "fatsecret"
    # Crowd/API aggregate. Honest, but any printed pack label outranks it.
    tier = Tier.D

    def can_parse(self, html: str, url: str) -> bool:
        return "factPanel" in html

    def parse(self, html: str, markdown: str, url: str) -> Panel | None:
        tree = HTMLParser(html)

        heading = tree.css_first("h1")
        product = heading.text(strip=True) if heading else None
        if not product:
            return None
        # FatSecret's h1 sometimes carries a breadcrumb prefix.
        product = product.split("Food database and calorie counter")[-1].strip()
        if not product or is_combo(product):
            return None

        panel = tree.css_first("div.factPanel")
        if panel is None:
            return None

        # A separator is essential: without it label and value fuse into "Cals211".
        parts = [p.strip() for p in panel.text(separator="|", strip=True).split("|") if p.strip()]

        values: dict[str, float | None] = {}
        for i in range(len(parts) - 1):
            label = parts[i]
            if should_drop_label(label):
                continue
            key = map_label(label)
            if not key:
                continue
            num = parse_number(parts[i + 1])
            if num is not None:
                values.setdefault(key, num)

        if not values or not is_viable(values):
            return None

        # Basis, read off the page's own sentence rather than assumed.
        blob = panel.text(separator=" ", strip=True)
        basis = Basis.PER_100G
        serving_g: float | None = None
        notes: list[str] = []

        match = _BASIS_RE.search(blob)
        if match:
            amount, unit = float(match.group(1)), match.group(2).lower()
            if unit.startswith("ml") or unit.startswith("milli"):
                basis = Basis.PER_100ML if amount == 100 else Basis.PER_SERVING
                serving_g = None if amount == 100 else amount
            elif amount == 100:
                basis = Basis.PER_100G
            else:
                basis = Basis.PER_SERVING
                serving_g = amount
        else:
            serving_match = _SERVING_RE.search(blob)
            if serving_match:
                basis = Basis.PER_SERVING
                serving_g = float(serving_match.group(1))
            else:
                notes.append("basis not printed — assumed per-100g")

        return build_panel(
            product=product,
            values=values,
            basis=basis,
            serving_g=serving_g,
            url=url,
            domain=registrable_domain(url),
            source_type=self.source_type,
            tier=self.tier,
            extractor="adapter:fatsecret",
            notes=notes,
        )
