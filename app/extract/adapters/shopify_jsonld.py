"""JSON-LD / __NEXT_DATA__ adapter — covers every Shopify brand store at once.

Brand DTC sites almost always emit schema.org `NutritionInformation`, which is
structured, printed by the site itself, and needs no guessing. This one adapter
therefore covers the whole Shopify long tail plus custom Next.js stores, without a
file per brand.

Note the units: schema.org gives "12 g" / "250 calories" as strings, and the basis
lives in `servingSize` — so a per-serving panel here is normalised through the same
serving_g path as everything else, never assumed to be per-100g.
"""

from __future__ import annotations

import json
import re

from selectolax.parser import HTMLParser

from app.domain.models import Basis, Panel, Tier
from app.extract.panel import build_panel, is_combo, is_viable, parse_number
from app.scrape.policy import TRUST_TIERS, registrable_domain

# "140 grams", "60 g", "250 ml" all appear in the wild.
_SERVING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(grams?|g|millilitres?|ml)\b", re.I)
_PER100_RE = re.compile(r"100\s*(grams?|g|millilitres?|ml)\b", re.I)

# schema.org NutritionInformation property -> our schema key.
_JSONLD_MAP = {
    "calories": "cal",
    "proteinContent": "prot",
    "carbohydrateContent": "carb",
    "fatContent": "fat",
    "saturatedFatContent": "sat_fat",
    "transFatContent": "trans_fat",
    "cholesterolContent": "chol",
    "fiberContent": "fib",
    "sugarContent": "sugar",
    "sodiumContent": "na",
}


def _iter_jsonld(tree: HTMLParser):
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))


class ShopifyJsonLdAdapter:
    """Reads schema.org NutritionInformation from any site that publishes it.

    Tier tracks PROVENANCE, not how convenient the data was to read. A brand
    publishing this on its own store is transcribing its own pack label; a calorie
    aggregator publishing identical markup is not, and must not outrank a
    retailer's transcribed label just because its JSON is tidier.

    Which domains count as which is a deployment's judgement, so it comes from
    `trust:` in domains.yaml — not a list baked into the engine. An unlisted domain
    is `unknown`, which means tier d.
    """

    domains = ()          # domain-agnostic: the structured-data fallback
    source_type = "jsonld"
    tier = Tier.D         # default; the real tier comes from domain trust

    def can_parse(self, html: str, url: str) -> bool:
        return "NutritionInformation" in html or "nutrition" in html.lower()

    def __init__(self, policies=None):
        # Optional so the adapter stays a pure function in tests.
        self._policies = policies

    def parse(self, html: str, markdown: str, url: str) -> Panel | None:
        tree = HTMLParser(html)

        nutrition: dict | None = None
        product_name: str | None = None
        brand: str | None = None

        for node in _iter_jsonld(tree):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "NutritionInformation" in types:
                nutrition = node
            if "Product" in types:
                product_name = product_name or node.get("name")
                raw_brand = node.get("brand")
                if isinstance(raw_brand, dict):
                    brand = brand or raw_brand.get("name")
                elif isinstance(raw_brand, str):
                    brand = brand or raw_brand
                nested = node.get("nutrition")
                if isinstance(nested, dict):
                    nutrition = nutrition or nested

        if not nutrition:
            return None

        if not product_name:
            og = tree.css_first('meta[property="og:title"]')
            product_name = og.attributes.get("content") if og else None
        # Many sites emit a BARE NutritionInformation with no Product wrapper and no
        # og:title (eatthismuch does exactly this). Its own `name`, then the h1.
        if not product_name:
            product_name = nutrition.get("name")
        if not product_name:
            h1 = tree.css_first("h1")
            product_name = h1.text(strip=True) if h1 else None
        if not product_name:
            return None
        if is_combo(product_name):
            return None

        values: dict[str, float | None] = {}
        for prop, key in _JSONLD_MAP.items():
            num = parse_number(nutrition.get(prop))
            if num is not None:
                values[key] = num
        if not values or not is_viable(values):
            return None

        serving_raw = str(nutrition.get("servingSize") or "")
        if _PER100_RE.search(serving_raw):
            basis, serving_g = Basis.PER_100G, None
        else:
            match = _SERVING_RE.search(serving_raw)
            serving_g = float(match.group(1)) if match else None
            basis = Basis.PER_SERVING if serving_g else Basis.PER_100G

        domain = registrable_domain(url)
        trust = "unknown"
        if self._policies is not None:
            trust = self._policies.for_url(url).trust

        tier = Tier(TRUST_TIERS.get(trust, "d"))
        notes = [] if serving_raw else ["servingSize absent — assumed per-100g"]
        if trust == "unknown":
            notes.append(f"structured data from {domain}, provenance unverified")

        return build_panel(
            product=product_name,
            values=values,
            basis=basis,
            serving_g=serving_g,
            url=url,
            domain=domain,
            source_type=f"jsonld_{trust}",
            tier=tier,
            extractor="adapter:jsonld",
            brand=brand,
            notes=notes,
        )
