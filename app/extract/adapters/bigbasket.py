"""BigBasket — retailer-transcribed pack label.

Written against a captured page. The panel is not a table and not structured data:
it is one run of prose with the basis at the front and `label - value` pairs after,
all concatenated:

    Amount per 100 g)Energy - 62 kcalEnergy from Fat - 28 kcalTotal Fat - 3.1 g
    Saturated fat - 1.9 gCholesterol - 8 mgTotal Carbohydrate - 4.4 g
    Added Sugar - 0 gProtein - 4.1 gCalcium - 183 mg...

Two traps live in that string, and both would silently corrupt a row:

  * "Energy from Fat - 28 kcal" contains BOTH "energy" and "fat". A longest-match
    label map maps it to energy and overwrites the real 62 kcal with 28. It is
    dropped explicitly.
  * "Added Sugar" is a subset of total sugar; keeping it double-counts.

This is a transcription of a printed pack label, so it earns tier c — better than a
crowd aggregate, below reading the label image itself.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from app.domain.models import Basis, Panel, Tier
from app.extract.adapters.base import map_label
from app.extract.panel import build_panel, is_combo, is_viable, parse_number, should_drop_label
from app.scrape.policy import registrable_domain

# "Amount per 100 g)" / "Amount Per 100 ml"
_BASIS_RE = re.compile(r"amount\s+per\s+([\d.]+)\s*(g|ml|gm)\b", re.I)

# `Total Fat - 3.1 g` — the label runs up to " - ", the value follows.
#
# The trailing guard is `(?![a-z])`, NOT `\b`. Values run straight into the next
# label with no separator ("62 kcalEnergy from Fat"), so there is no word boundary
# after the unit and `\b` matches nothing at all. Rejecting only a following
# lowercase letter keeps "kcalEnergy" while still refusing the "g" inside "grams".
#
# Unit order matters too: the bare "g" comes last so it cannot swallow the "g" of
# "mg" or "mcg" first.
_PAIR_RE = re.compile(
    r"([A-Z][A-Za-z.\s]{1,28}?)\s*-\s*([\d.,]+)\s*(kcal|kj|mcg|mg|µg|ml|g)(?![a-z])"
)

_PANEL_RE = re.compile(r"amount\s+per\s+[\d.]+\s*(?:g|ml|gm)\b.{0,2000}", re.I | re.S)


class BigBasketAdapter:
    domains = ("bigbasket.com",)
    source_type = "retailer_label_bigbasket"
    tier = Tier.C

    def can_parse(self, html: str, url: str) -> bool:
        return bool(_BASIS_RE.search(html))

    def parse(self, html: str, markdown: str, url: str) -> Panel | None:
        tree = HTMLParser(html)

        heading = tree.css_first("h1")
        product = heading.text(strip=True) if heading else None
        if not product or is_combo(product):
            return None

        block = _PANEL_RE.search(html)
        if not block:
            return None
        blob = re.sub(r"<[^>]+>", " ", block.group(0))

        basis = Basis.PER_100G
        basis_match = _BASIS_RE.search(blob)
        notes: list[str] = []
        if basis_match:
            amount, unit = float(basis_match.group(1)), basis_match.group(2).lower()
            if unit == "ml":
                basis = Basis.PER_100ML if amount == 100 else Basis.PER_SERVING
            elif amount != 100:
                basis = Basis.PER_SERVING
        else:
            notes.append("basis not printed — assumed per-100g")

        serving_g = None if basis in (Basis.PER_100G, Basis.PER_100ML) else (
            float(basis_match.group(1)) if basis_match else None
        )

        values: dict[str, float | None] = {}
        for label, number, unit in _PAIR_RE.findall(blob):
            label = label.strip()
            if should_drop_label(label):
                continue
            key = map_label(label)
            if not key:
                continue
            # Energy is printed in kcal AND kJ; kcal is what the schema wants.
            if key == "cal" and unit.lower() == "kj":
                continue
            num = parse_number(number)
            if num is not None:
                values.setdefault(key, num)

        if not values or not is_viable(values):
            return None

        return build_panel(
            product=product,
            values=values,
            basis=basis,
            serving_g=serving_g,
            url=url,
            domain=registrable_domain(url),
            source_type=self.source_type,
            tier=self.tier,
            extractor="adapter:bigbasket",
            notes=notes,
        )
